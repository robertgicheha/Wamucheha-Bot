"""
Control-channel authorization and executor accounting invariants.

Both test groups here guard a failure that produced a bot that looked healthy
and behaved unsafely. Neither is reachable from a normal end-to-end run, so
they are tested at the boundary instead: the source of the auth rule, and the
executor signature that the fee model depends on.
"""
import ast
import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_import(rel_path: str, env: dict):
    """Import a module fresh, under a controlled environment.

    Both control bots read their allowlist at import time into a module global,
    so the deny-by-default path cannot be tested by patching after the fact: the
    global is already set. Loading a second, independent copy under a different
    module name avoids mutating sys.modules for everyone else, and avoids the
    problem that importlib.reload() would re-run against whatever state the
    previous test left behind.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_isolated_{Path(rel_path).stem}_{abs(hash(tuple(sorted(env.items()))))}",
        ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        for key in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_ROLES"):
            os.environ.pop(key, None)
        os.environ.update(env)
        spec.loader.exec_module(module)
    return module


class ControlAuthFailClosedTest(unittest.TestCase):
    """An empty allowlist must deny everyone, not allow everyone.

    The original code did the opposite: `if not ALLOWED_USERS: return True`.
    That made an unconfigured bot fully controllable by anyone who could DM it,
    which included /resume (clears a risk-manager circuit breaker) and /deposit
    (forges the cash-flow ledger behind every ROI figure).
    """

    def test_telegram_empty_allowlist_denies_everyone(self):
        tg = _fresh_import("control/telegram_bot.py", {"TELEGRAM_ALLOWED_USERS": ""})
        for user_id in (1, 42, 123456789, 999999999):
            self.assertFalse(
                tg._is_authorized(user_id),
                f"an empty TELEGRAM_ALLOWED_USERS must deny user {user_id}")

    def test_discord_empty_allowlist_denies_everyone(self):
        dc = _fresh_import("control/discord_bot.py", {"DISCORD_ALLOWED_ROLES": ""})

        class FakeRole:
            def __init__(self, name):
                self.name = name

        class FakeCtx:
            def __init__(self, roles):
                self.author = type("A", (), {"roles": roles})()

        ctx = FakeCtx([FakeRole("admin"), FakeRole("everyone")])
        self.assertFalse(
            dc._is_authorized(ctx),
            "an empty DISCORD_ALLOWED_ROLES must deny even a user who is admin")

    def test_telegram_allows_only_listed_ids(self):
        tg = _fresh_import("control/telegram_bot.py",
                           {"TELEGRAM_ALLOWED_USERS": "111, 222 ,333"})
        self.assertTrue(tg._is_authorized(111))
        self.assertTrue(tg._is_authorized(222))
        self.assertTrue(tg._is_authorized(333))
        self.assertFalse(tg._is_authorized(444))
        self.assertFalse(tg._is_authorized(0))

    def test_discord_allows_only_listed_roles_case_insensitively(self):
        dc = _fresh_import("control/discord_bot.py",
                           {"DISCORD_ALLOWED_ROLES": "Trader, admin"})

        class FakeRole:
            def __init__(self, name):
                self.name = name

        class FakeCtx:
            def __init__(self, roles):
                self.author = type("A", (), {"roles": roles})()

        self.assertTrue(dc._is_authorized(FakeCtx([FakeRole("trader")])))
        self.assertTrue(dc._is_authorized(FakeCtx([FakeRole("ADMIN")])))
        self.assertFalse(dc._is_authorized(FakeCtx([FakeRole("member")])))
        self.assertFalse(dc._is_authorized(FakeCtx([])))

    def test_malformed_id_is_dropped_not_fatal_and_never_widens_access(self):
        """A typo in the list must not stop the bot booting, and must not
        accidentally authorize the wrong person."""
        tg = _fresh_import("control/telegram_bot.py",
                           {"TELEGRAM_ALLOWED_USERS": "111, notanumber, 222"})
        self.assertTrue(tg._is_authorized(111))
        self.assertTrue(tg._is_authorized(222))
        self.assertFalse(tg._is_authorized(0))
        self.assertFalse(tg._is_authorized(-111))

    def test_allow_all_pattern_is_absent_from_both_control_bots(self):
        """Belt and braces: if someone reintroduces `if not ALLOWED: return True`,
        the behavioural tests above would catch it, but this pins the literal so
        the intent is greppable in review."""
        for rel in ("control/telegram_bot.py", "control/discord_bot.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotRegex(
                src, r"if not ALLOWED_(USERS|ROLES)\s*:\s*\n\s*return True",
                f"{rel} allows every caller when the allowlist is empty")
            self.assertNotIn(
                "empty = anyone can control", src,
                f"{rel} documents the allow-all behaviour in .env.example text")

    def test_main_refuses_to_start_a_control_bot_with_no_allowlist(self):
        """A token plus an empty allowlist used to print 'control bot started'.
        That is worse than skipping: every command then fails at the auth check
        and it looks like the bot is broken rather than unconfigured."""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        starts = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "start" and isinstance(node.func.value, ast.Name):
                starts[node.func.value.id] = node.lineno
        self.assertIn("tg_bot", starts)
        self.assertIn("dc_bot", starts)

        # The guard must exist and must appear BEFORE the start() call, so an
        # empty allowlist can never reach a running bot.
        for var, env_key, start_var in (("tg_allowed", "TELEGRAM_ALLOWED_USERS", "tg_bot"),
                                        ("dc_allowed", "DISCORD_ALLOWED_ROLES", "dc_bot")):
            self.assertIn(f"{var} = os.environ.get(\"{env_key}\"", src,
                          f"main.py must read {env_key} before starting the bot")
            guard_line = next(
                ln for ln, line in enumerate(src.split("\n"), 1)
                if f"{var} = os.environ.get" in line)
            self.assertLess(
                guard_line, starts[start_var],
                f"{env_key} is read at line {guard_line} but "
                f"{start_var}.start() is at line {starts[start_var]}; the check "
                f"must come first")


class ResumeGoesThroughRiskManagerTest(unittest.TestCase):
    """/resume must clear the halt via RiskManager.resume_trading.

    Writing `trading_halted=0` directly to the state store has the same effect
    on trading but skips the circuit_breaker_reset notification, so the only
    record of who re-armed a halted bot is a state row changing.
    """

    def _cmd_body(self, rel: str, name: str) -> str:
        """Body of a single `def <name>` using the AST, so a decorator or a
        same-named string elsewhere in the file cannot throw the boundaries
        off."""
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return ast.get_source_segment(
                    (ROOT / rel).read_text(encoding="utf-8"), node) or ""
        self.fail(f"{rel} has no function named {name}")

    def test_telegram_resume_calls_resume_trading(self):
        body = self._cmd_body("control/telegram_bot.py", "_cmd_resume")
        self.assertIn("resume_trading", body)
        # The bare update_risk_state is a deliberate fallback for the case where
        # no risk manager was injected, so it may appear — but only guarded, never
        # as the primary path.
        self.assertIn("if risk is None", body)
        self.assertLess(body.index("if risk is None"),
                        body.index("resume_trading"))

    def test_discord_resume_calls_resume_trading(self):
        body = self._cmd_body("control/discord_bot.py", "cmd_resume")
        self.assertIn("resume_trading", body)
        self.assertIn("if risk is None", body)


class ExecutorFeeAccountingTest(unittest.TestCase):
    """Every venue that fills a trade must report gross P&L, entry and exit fees.

    Only ExecutionManager (ccxt) was wired to FeeModel. MT5, OANDA and Alpaca
    omitted the arguments, so the notifier's defaults filled them with 0.0 and
    every report claimed those venues cost nothing, and held_seconds was always
    zero because opened_at was never passed.
    """

    EXECUTORS = {
        "core/mt5_executor.py": "MT5Executor",
        "core/oanda_executor.py": "OandaExecutor",
        "core/alpaca_executor.py": "AlpacaExecutor",
        "core/execution_manager.py": "ExecutionManager",
    }

    def test_every_executor_records_gross_pnl_and_both_fees(self):
        for rel, cls in self.EXECUTORS.items():
            src = (ROOT / rel).read_text(encoding="utf-8")
            for field in ("gross_pnl", "entry_fee", "exit_fee"):
                self.assertIn(
                    field, src,
                    f"{rel}:{cls} never passes {field}; reports for this venue "
                    f"will show $0.00 fees and gross == net")

    def test_every_executor_passes_opened_at_so_hold_time_is_reported(self):
        for rel, cls in self.EXECUTORS.items():
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("opened_at", src,
                          f"{rel}:{cls} does not pass opened_at, so held_seconds "
                          f"is always 0.0 in trade notifications")

    def test_mt5_executor_receives_a_fee_model(self):
        """MT5's cost is in the spread, not a commission line, so its fee rate
        is 0 — but it must still go through FeeModel so the zero is a
        deliberate, consistent statement rather than an absent argument."""
        src = (ROOT / "core/mt5_executor.py").read_text(encoding="utf-8")
        self.assertIn("fee_model", src,
                      "core/mt5_executor.py does not accept a fee_model, so its "
                      "fees are never computed at all")
        self.assertIn("FeeModel", src)

    def test_main_passes_fee_model_to_every_executor(self):
        """Checked on the parsed call, not on the text near it.

        A character window around `MT5Executor(` is not a reliable place to
        look for a keyword argument: a three-line comment explaining the bridge
        guards is enough to push `fee_model=` past the end of the window, and a
        test that then fails reads as "the wiring is missing" when it is really
        the test that is wrong. The keyword list of the call itself has no such
        ambiguity.
        """
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        wanted = {"MT5Executor", "OandaExecutor", "AlpacaExecutor", "ExecutionManager"}
        seen = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in wanted:
                seen[node.func.id] = {kw.arg for kw in node.keywords if kw.arg}

        for cls in sorted(wanted):
            self.assertIn(cls, seen, f"{cls} is never constructed in main.py")
            self.assertIn(
                "fee_model", seen[cls],
                f"{cls} is constructed without fee_model=, so it cannot report fees")


if __name__ == "__main__":
    unittest.main()
