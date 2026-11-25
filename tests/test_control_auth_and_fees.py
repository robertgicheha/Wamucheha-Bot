"""
Executor accounting invariants.

These guard a failure that produced a bot that looked healthy and misreported
its costs. It is not reachable from a normal end-to-end run, so it is tested at
the boundary: the executor signature that the fee model depends on.
(Control is via the dashboard now; its auth is covered in test_admin_console.py.)
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


class ExecutorFeeAccountingTest(unittest.TestCase):
    """Every venue that fills a trade must report gross P&L, entry and exit fees.

    Only ExecutionManager (ccxt) was wired to FeeModel. MT5
    omitted the arguments, so the notifier's defaults filled them with 0.0 and
    every report claimed those venues cost nothing, and held_seconds was always
    zero because opened_at was never passed.
    """

    EXECUTORS = {
        "core/mt5_executor.py": "MT5Executor",
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

        wanted = {"MT5Executor", "ExecutionManager"}
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
