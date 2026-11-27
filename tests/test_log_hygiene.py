"""
Log hygiene. Properties worth protecting:
  * a Telegram token or Discord webhook never reaches a log or the console's log viewer
  * an unreachable sentiment feed is tried once per cache window, not on every loop
  * a failing periodic check in the engine does not re-run on every loop
"""
import ast
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from core import market_intelligence as mi  # noqa: E402
from core.log_redact import RedactingFilter, redact  # noqa: E402

FAKE = "123456789:" + "A" * 35          # shaped like a token, not a real one
LEAK = f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{FAKE}/sendMessage"


def test_tokens_and_webhooks_are_removed_but_the_error_stays_readable():
    cleaned = redact(LEAK)
    assert FAKE not in cleaned and "/bot<redacted>/sendMessage" in cleaned and "Max retries" in cleaned
    hook = redact("POST https://discord.com/api/webhooks/42/abcDEF_secret-part failed")
    assert "secret-part" not in hook and "webhooks/42/<redacted>" in hook
    assert redact("MT5 BTCUSD: lot 0.01") == "MT5 BTCUSD: lot 0.01"


def test_filter_cleans_secrets_passed_as_arguments():
    stream = io.StringIO()
    log = logging.getLogger("redaction-test")
    handler = logging.StreamHandler(stream)
    log.addHandler(handler)
    log.addFilter(RedactingFilter())
    try:
        log.error("telegram failed: %s", RuntimeError(LEAK))
    finally:
        log.removeHandler(handler)
    assert FAKE not in stream.getvalue() and "<redacted>" in stream.getvalue()


def test_notifier_logger_and_error_file_are_protected(tmp_path, monkeypatch):
    import alerts.notifier
    from core import structured_logger
    assert any(isinstance(f, RedactingFilter) for f in alerts.notifier.logger.filters)
    monkeypatch.setattr(structured_logger, "ERROR_LOG", tmp_path / "errors.log")
    structured_logger._append_error_line(LEAK)
    assert FAKE not in (tmp_path / "errors.log").read_text(encoding="utf-8")


def test_log_viewer_masks_tokens_already_on_disk(tmp_path):
    from dashboard import admin
    (tmp_path / "old.log").write_text(LEAK + "\n", encoding="utf-8")
    assert FAKE not in admin._tail(tmp_path / "old.log", 5)[0]


class _Reply:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"value": "30", "value_classification": "Fear", "timestamp": "1"}]}


def test_fear_greed_outage_is_tried_once_per_window_and_uses_the_last_reading(monkeypatch):
    intel = mi.MarketIntelligence({"intelligence": {}})
    clock, calls = [1000.0], []
    monkeypatch.setattr(mi.time, "time", lambda: clock[0])
    monkeypatch.setattr(mi.requests, "get", lambda *a, **k: _Reply())
    assert intel.get_fear_greed()["value"] == 30

    def down(*a, **k):
        calls.append(1)
        raise RuntimeError("Read timed out")
    monkeypatch.setattr(mi.requests, "get", down)
    clock[0] += 301
    assert intel.get_fear_greed()["value"] == intel.get_fear_greed()["value"] == 30 and len(calls) == 1
    clock[0] += 25 * 3600                     # a reading over a day old is not reused
    assert intel.get_fear_greed() is None and intel.get_fear_greed() is None and len(calls) == 2


@pytest.mark.parametrize("name", ["_last_arb_check", "_last_sh_check", "_last_dca_check"])
def test_periodic_check_is_timed_from_the_attempt(name):
    """main.py cannot be imported (it would start trading), so check where the timer is set."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    sets = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == name for t in n.targets)]
    inside_try = {id(x) for t in ast.walk(tree) if isinstance(t, ast.Try) for stmt in t.body for x in ast.walk(stmt)
                  if isinstance(x, ast.Assign) and x in sets}
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(s in n.body for s in sets)]
    assert len(sets) == 1 and len(blocks) == 1, "the timer is set once, directly in the due-check block"
    body = blocks[0].body
    assert body.index(sets[0]) < next(i for i, s in enumerate(body) if isinstance(s, ast.Try))
    assert inside_try <= {id(sets[0])}
