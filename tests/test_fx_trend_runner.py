import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from core.fx_trend_broker import Position, Quote
from core.fx_trend_runner import FxTrendRunner, RunnerConfig

NOW = datetime(2026, 3, 4, tzinfo=timezone.utc)


def bars(n=300, drift=0.0015, seed=1, start=1.1, end="2026-03-03"):
    rng = np.random.default_rng(seed)
    c = start * np.cumprod(1 + drift + rng.normal(0, 0.003, n))
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.001,
                         "low": np.minimum(o, c) * 0.999, "close": c}, index=idx)


class FakeBroker:
    def __init__(self, data, equity=10_000.0, spread=0.0001, lots=0.1):
        self.data, self._equity, self.spread, self._lots = data, equity, spread, lots
        self.pos, self.opened, self.closed, self.modified = [], [], [], []

    def connect(self): return True
    def is_demo(self): return True
    def equity(self): return self._equity
    def daily_bars(self, pair, n): return self.data.get(pair, pd.DataFrame())

    def account_summary(self):
        return {"equity": self._equity, "balance": self._equity, "currency": "USD"}

    def closed_trades(self, since):
        return list(getattr(self, "history", []))

    def latest_bar_date(self, pair):
        df = self.data.get(pair)
        return None if df is None or df.empty else df.index[-1].strftime("%Y-%m-%d")
    def positions(self): return list(self.pos)

    def quote(self, pair):
        px = float(self.data[pair]["close"].iloc[-1])
        return Quote(px, px + self.spread, 0.0001)

    def lots_for_risk(self, pair, side, risk_amount, entry, stop):
        return self._lots

    def open(self, pair, side, lots, sl):
        self.opened.append((pair, side, lots, sl))
        self.pos.append(Position(len(self.opened), pair, side, lots, 1.0, sl))
        return len(self.opened)

    def close(self, pos):
        self.closed.append(pos.pair)
        self.pos = [p for p in self.pos if p.ticket != pos.ticket]
        return True

    def modify_sl(self, pos, sl):
        self.modified.append((pos.pair, sl))
        return True


def runner(tmp_path, broker, **kw):
    cfg = RunnerConfig(pairs=list(broker.data), dry_run=kw.pop("dry_run", False), **kw)
    return FxTrendRunner(broker, cfg, base_dir=tmp_path)


def test_dry_run_sends_nothing(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    r = runner(tmp_path, b, dry_run=True).run_once(NOW)
    assert ("open", "EURUSD") in r["actions"] and not b.opened


def test_opens_with_stop_and_is_idempotent(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    rn = runner(tmp_path, b)
    rn.run_once(NOW)
    assert len(b.opened) == 1
    pair, side, lots, sl = b.opened[0]
    assert side == 1 and sl < float(b.data["EURUSD"]["close"].iloc[-1])   # long, stop below
    assert rn.run_once(NOW)["skipped"] == "already_processed"
    assert len(b.opened) == 1                                             # no double trade


def test_closes_when_signal_flips(tmp_path):
    b = FakeBroker({"EURUSD": bars()})                                    # uptrend -> long signal
    b.pos.append(Position(99, "EURUSD", -1, 0.1, 1.2, 1.5))               # we hold a short
    runner(tmp_path, b).run_once(NOW)
    assert "EURUSD" in b.closed
    assert b.opened and b.opened[0][1] == 1                               # reverses to long


def test_kill_switch_flattens_and_blocks_entries(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    b.pos.append(Position(99, "EURUSD", 1, 0.1, 1.2, 1.1))
    (tmp_path / "data").mkdir()
    (tmp_path / "data/fx_trend.KILL").write_text("x")
    r = runner(tmp_path, b).run_once(NOW)
    assert r["skipped"] == "kill_switch" and b.closed == ["EURUSD"] and not b.opened


def test_daily_loss_halt_blocks_new_entries(tmp_path):
    b = FakeBroker({"EURUSD": bars()}, equity=10_000)
    rn = runner(tmp_path, b)
    rn.run_once(NOW)                       # records prev_equity=10k, opens
    b.opened.clear(); b.pos.clear()
    b._equity = 9_700                      # -3% > 2% halt
    b.data["EURUSD"] = bars(n=301, end="2026-03-04")   # a new closed bar
    r = rn.run_once(datetime(2026, 3, 5, tzinfo=timezone.utc))
    assert not b.opened and ("open", "EURUSD") not in r["actions"]


def test_currency_cap_and_spread_guard(tmp_path):
    data = {p: bars(seed=i) for i, p in enumerate(["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"], 1)}
    b = FakeBroker(data)
    runner(tmp_path, b, max_net_per_currency=2).run_once(NOW)
    assert len(b.opened) == 2                                             # 3rd would be 3 net short USD
    b2 = FakeBroker({"EURUSD": bars()}, spread=0.0010)                    # 10 pips
    runner(tmp_path / "b2", b2, max_spread_pips=3).run_once(NOW)
    assert not b2.opened


def test_zero_lots_skips_trade(tmp_path):
    b = FakeBroker({"EURUSD": bars()}, lots=0.0)
    runner(tmp_path, b).run_once(NOW)
    assert not b.opened


def test_trailing_stop_only_tightens(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    b.pos.append(Position(5, "EURUSD", 1, 0.1, 1.0, 0.5))                 # very loose stop
    runner(tmp_path, b).run_once(NOW)
    assert b.modified and b.modified[0][1] > 0.5


def test_journal_records_cycle_signals_events(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    rn = runner(tmp_path, b)
    rn.run_once(NOW)
    j = rn.journal
    assert len(j.equity_series()) == 1
    sig = j.latest_signals()
    assert sig and sig[0]["pair"] == "EURUSD" and sig[0]["side"] == 1
    assert 0.0 <= sig[0]["p_flip_5d"] <= sig[0]["p_flip_20d"] <= 1.0
    assert any(e["kind"] == "open" for e in j.events())


def test_snapshot_and_trade_sync(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    b.pos.append(Position(7, "EURUSD", 1, 0.1, 1.0, 0.9, profit=12.5))
    b.history = [{"ticket": 1, "pair": "EURUSD", "side": 1, "volume": 0.1, "open_time": "2026-01-01T00:00:00",
                  "close_time": "2026-01-09T00:00:00", "entry": 1.1, "exit": 1.12, "profit": 20.0,
                  "commission": 0.0, "swap": -1.0}]
    rn = runner(tmp_path, b)
    snap = rn.tick(NOW)
    s = json.loads((tmp_path / "data/fx_trend_snapshot.json").read_text())
    assert s["floating_pnl"] == 12.5 and s["positions"][0]["pair"] == "EURUSD"
    assert rn.journal.trades()[0]["profit"] == 20.0


def test_idle_poll_does_not_download_history(tmp_path):
    b = FakeBroker({"EURUSD": bars()})
    calls = []
    orig = b.daily_bars
    b.daily_bars = lambda pair, n: (calls.append(pair), orig(pair, n))[1]
    rn = runner(tmp_path, b)
    rn.run_once(NOW)
    n_after_first = len(calls)
    assert rn.run_once(NOW)["skipped"] == "already_processed"
    assert len(calls) == n_after_first      # second poll used only the 1-bar probe
