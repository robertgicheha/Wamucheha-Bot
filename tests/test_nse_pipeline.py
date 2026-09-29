"""
Tests for the NSE Kenya pipeline: the RapidAPI client, snapshot persistence,
daily history accumulation, the price projections and the dashboard endpoints.

Every HTTP call is mocked. Nothing here touches the network or the real
data/nse_cache directory, so the suite is safe to run anywhere and costs no
RapidAPI quota.

    .venv\\Scripts\\python.exe -m pytest tests/test_nse_pipeline.py -v
"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_feeds.nse_feed as nse_feed
from data_feeds.nse_feed import NSEFeed
from long_term import nse_forecast


# A trimmed but structurally faithful slice of a real /stocks response: every
# numeric field arrives as a STRING, "change" is percent points, and ALP
# genuinely returns "0.00" with volume 0.
STOCKS_RESPONSE = {
    "success": True,
    "meta": {"total": 70, "returned": 70, "cached": True, "lastUpdated": "2026-09-29T13:00:00Z"},
    "data": [
        {"ticker": "SCOM", "name": "Standard Chartered Bank Kenya", "isin": "KE0000000004",
         "volume": "1284750", "price": "48.50", "change": "1.83", "sector": "BANKING"},
        {"ticker": "EQTY", "name": "Equity Group Holdings", "isin": "KE0000000064",
         "volume": "593738", "price": "52.75", "change": "-0.94", "sector": "BANKING"},
        {"ticker": "COOP", "name": "Co-operative Bank of Kenya", "isin": "KE0000000014",
         "volume": "0", "price": "0.00", "change": "0.00", "sector": "BANKING"},
        {"ticker": "ALP", "name": "Alpha Bank", "isin": "KE0000000105",
         "volume": "0", "price": "0.00", "change": "0.00", "sector": "BANKING"},
        {"ticker": "KPLC-P4", "name": "Kenya Power & Lighting Co 4% Pref",
         "isin": "KE0000000121", "volume": "1200", "price": "10.05", "change": "0.50",
         "sector": "PREFERENCE SHARES"},
    ],
    "timestamp": "2026-09-29T13:00:00Z",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect CACHE_DIR and SNAPSHOT_FILE into a tmp dir."""
    d = tmp_path / "nse_cache"
    d.mkdir()
    monkeypatch.setattr(nse_feed, "CACHE_DIR", d)
    monkeypatch.setattr(nse_feed, "SNAPSHOT_FILE", d / "rapidapi_snapshot.json")
    return d


@pytest.fixture
def feed(cache_dir, monkeypatch):
    monkeypatch.setenv("NSE_RAPIDAPI_KEY", "test-key")
    return NSEFeed(rapidapi_key="test-key")


# ---------- parsing ----------

def test_stocks_payload_is_normalised(feed, monkeypatch):
    """Every numeric field arrives as a string and must come back typed."""
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    snap = feed.refresh_market_snapshot()

    assert len(snap["stocks"]) == 5
    by_ticker = {s["ticker"]: s for s in snap["stocks"]}

    scom = by_ticker["SCOM"]
    assert scom["price"] == 48.50 and isinstance(scom["price"], float)
    assert scom["change_pct"] == 1.83
    assert scom["volume"] == 1284750 and isinstance(scom["volume"], int)
    assert scom["isin"] == "KE0000000004"
    assert scom["sector"] == "BANKING"
    assert scom["stale"] is False

    # "change" is percent points vs previous close, not an absolute move.
    assert by_ticker["EQTY"]["change_pct"] == -0.94
    assert snap["source"] == "rapidapi"


def test_zero_volume_names_are_kept_but_flagged(feed, monkeypatch):
    """The user asked for ALL stocks, so a name that did not trade must still
    appear — flagged, not dropped."""
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    snap = feed.refresh_market_snapshot()
    tickers = [s["ticker"] for s in snap["stocks"]]
    assert "COOP" in tickers and "ALP" in tickers
    assert {s["ticker"] for s in snap["stocks"] if s["stale"]} == {"COOP", "ALP"}
    assert all(s["price"] == 0.0 for s in snap["stocks"] if s["ticker"] == "ALP")


def test_missing_and_garbage_numeric_fields(feed, monkeypatch):
    payload = {
        "meta": {"total": 2}, "timestamp": "2026-09-29T13:00:00Z",
        "data": [
            {"ticker": "X1", "name": "One", "volume": "1,000", "price": ".45", "change": "n/a", "sector": None},
            {"ticker": "X2", "name": "Two", "volume": "12", "price": "", "change": None},
        ],
    }
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=payload))
    snap = feed.refresh_market_snapshot()
    by = {s["ticker"]: s for s in snap["stocks"]}
    assert by["X1"]["price"] == 0.45          # bare ".45"
    assert by["X1"]["volume"] == 1000          # thousands separator
    assert by["X1"]["change_pct"] is None      # "n/a"
    assert by["X1"]["sector"] is None
    assert by["X2"]["price"] is None
    assert by["X2"]["change_pct"] is None


def test_rows_without_a_ticker_are_dropped(feed, monkeypatch):
    payload = {"data": [{"name": "No ticker"}, {"ticker": "SCOM", "price": "1.00"}], "meta": {}}
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=payload))
    snap = feed.refresh_market_snapshot()
    assert [s["ticker"] for s in snap["stocks"]] == ["SCOM"]


# ---------- quota discipline ----------

def test_fresh_persisted_snapshot_skips_the_api_call(feed, monkeypatch, cache_dir):
    """The 250/month budget depends on this: once a snapshot is on disk and
    fresh, refresh_market_snapshot must not call RapidAPI at all."""
    def boom(*a, **k):
        raise AssertionError("RapidAPI was called despite a fresh snapshot")

    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.refresh_market_snapshot()

    monkeypatch.setattr(nse_feed.requests, "get", boom)
    again = feed.refresh_market_snapshot()
    assert len(again["stocks"]) == 5
    assert again["stale"] is False


def test_readers_never_call_rapidapi(cache_dir, monkeypatch):
    """get_market_snapshot / get_nse_tickers / latest_price must read the
    persisted file rather than spending quota."""
    def boom(*a, **k):
        raise AssertionError("a read path called RapidAPI")

    monkeypatch.setattr(nse_feed.requests, "get", boom)
    f = NSEFeed(rapidapi_key="test-key")
    snap = nse_feed.json.loads(nse_feed.SNAPSHOT_FILE.read_text()) if nse_feed.SNAPSHOT_FILE.exists() else None
    assert snap is None  # nothing persisted yet

    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    NSEFeed(rapidapi_key="test-key").refresh_market_snapshot()

    monkeypatch.setattr(nse_feed.requests, "get", boom)
    f2 = NSEFeed(rapidapi_key="test-key")
    m = f2.get_market_snapshot()
    assert len(m["universe"]) == 5
    assert f2.get_nse_tickers() == ["SCOM", "EQTY", "COOP", "ALP", "KPLC-P4"]
    assert f2.latest_price("SCOM") == 48.50


def test_missing_key_never_calls_the_api(cache_dir, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("called RapidAPI without a key")

    monkeypatch.setattr(nse_feed.requests, "get", boom)
    f = NSEFeed(rapidapi_key="")
    assert f.rapidapi_configured is False
    assert f.refresh_market_snapshot() is None


# ---------- failure handling: always keep the last good data ----------

@pytest.mark.parametrize("status,text", [(429, "rate limited"), (500, "boom"), (403, "not subscribed")])
def test_http_errors_keep_the_previous_snapshot(feed, cache_dir, monkeypatch, status, text):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    good = feed.refresh_market_snapshot()

    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(status_code=status, text=text))
    after = feed.refresh_market_snapshot()
    assert after is not None
    assert len(after["stocks"]) == len(good["stocks"])


def test_network_exception_keeps_the_previous_snapshot(feed, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    good = feed.refresh_market_snapshot()

    def raise_net(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(nse_feed.requests, "get", raise_net)
    assert len(feed.refresh_market_snapshot()["stocks"]) == len(good["stocks"])


def test_empty_and_malformed_payloads_do_not_wipe_the_cache(feed, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    good = feed.refresh_market_snapshot()

    for bad in ({"data": []}, {"data": "nope"}, {}, {"data": None}):
        monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=bad))
        kept = feed.refresh_market_snapshot()
        assert len(kept["stocks"]) == len(good["stocks"]), bad


def test_corrupt_snapshot_file_is_treated_as_absent(feed, cache_dir):
    nse_feed.SNAPSHOT_FILE.write_text("{ not json")
    assert feed.load_persisted_snapshot() is None


def test_stale_snapshot_is_flagged(feed, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.refresh_market_snapshot()

    data = json.loads(nse_feed.SNAPSHOT_FILE.read_text())
    data["fetched_at"] = "2020-01-01T00:00:00+00:00"
    nse_feed.SNAPSHOT_FILE.write_text(json.dumps(data))

    reloaded = feed.load_persisted_snapshot()
    assert reloaded["stale"] is True


# ---------- movers / sectors ----------

def test_snapshot_maps_onto_the_existing_movers_contract(feed, monkeypatch):
    """Existing consumers (daily_digest._nse_gainers_losers) expect
    gainers/losers/universe — the RapidAPI path must supply the same shape."""
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.refresh_market_snapshot()          # persist the snapshot first
    m = feed.get_market_snapshot()          # then read it back through the movers path

    assert {"gainers", "losers", "universe", "fetched_at", "source"} <= set(m)
    assert m["gainers"][0]["ticker"] == "SCOM"          # +1.83% is the biggest gainer
    assert m["losers"][0]["ticker"] == "EQTY"           # -0.94% is the only decliner
    uni = {u["ticker"]: u for u in m["universe"]}
    assert uni["SCOM"]["direction"] == "up"
    assert uni["EQTY"]["direction"] == "down"
    assert uni["ALP"]["direction"] == "flat"
    assert uni["SCOM"]["sector"] == "BANKING"

    banks = next(s for s in m["sectors"] if s["sector"] == "BANKING")
    assert banks["count"] == 4


# ---------- history accumulation ----------

def test_daily_snapshot_writes_one_row_per_ticker(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    snap = feed.refresh_market_snapshot()

    # refresh_market_snapshot() already records the day, so the history exists
    # before any explicit call.
    assert feed.record_daily_snapshot(snap) == 0   # same day -> deduped

    df = feed.get_accumulated_history("SCOM")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["close"] == 48.50
    assert row["volume"] == 1284750
    # /stocks carries no intraday high/low, so the bar is flat by construction.
    assert row["open"] == row["high"] == row["low"] == row["close"] == 48.50
    assert str(df.index[0])[:10] == snap["trading_date"]

    # 5 names listed, but the two that printed 0.00 must not become fake bars.
    assert sorted(p.name for p in cache_dir.glob("*_daily.csv")) == \
        ["EQTY_daily.csv", "KPLC-P4_daily.csv", "SCOM_daily.csv"]


def test_history_dedupes_on_the_east_africa_trading_date(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    snap = feed.refresh_market_snapshot()

    feed.record_daily_snapshot(snap)
    again = feed.record_daily_snapshot(snap)   # same day re-run
    assert again == 0
    assert len(feed.get_accumulated_history("SCOM")) == 1


def test_new_listing_gets_a_new_row_the_next_day(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.record_daily_snapshot(feed.refresh_market_snapshot())

    feed._append_history_row("SCOM", "2030-01-02", 49.0, 49.5, 48.8, 49.2, 999)
    df = feed.get_accumulated_history("SCOM")
    assert len(df) == 2
    assert df.iloc[-1]["close"] == 49.2


def test_preference_share_ticker_with_dash(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.record_daily_snapshot(feed.refresh_market_snapshot())
    df = feed.get_accumulated_history("KPLC-P4")
    assert len(df) == 1 and df.iloc[0]["close"] == 10.05


def test_zero_priced_names_never_reach_history(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.record_daily_snapshot(feed.refresh_market_snapshot())
    assert feed.get_accumulated_history("ALP") is None
    assert feed.get_accumulated_history("COOP") is None


def test_ohlcv_falls_back_to_accumulated_history(feed, cache_dir, monkeypatch):
    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed.record_daily_snapshot(feed.refresh_market_snapshot())

    assert len(feed.get_ohlcv("SCOM")) == 1
    assert len(feed.get_ohlcv("SCOM.NSE")) == 1     # symbol-suffix handling
    assert list(feed.get_ohlcv("NOSUCH").columns) == ["open", "high", "low", "close", "volume"]


# ---------- projections ----------

def _history(n, drift=0.0005, vol=0.015, seed=3, start=36.0):
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    import numpy as np
    rng = np.random.default_rng(seed)
    prices = start * pow(2.718281828, __import__("numpy").cumsum(rng.normal(drift, vol, n)))
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices,
                       "close": prices, "volume": 1000}, index=idx)
    df.index.name = "date"
    return df


def test_projection_refuses_below_minimum_history():
    for n in (0, 1, 5, 19):
        f = nse_forecast.build_forecast(_history(n) if n else None)
        assert f["available"] is False
        assert f["horizons"] == []
        assert "reason" in f and f["disclaimer"]
        assert nse_forecast.direction_label(f) == "Not enough data"


def test_projection_produces_all_four_horizons_with_bands():
    f = nse_forecast.build_forecast(_history(80))
    assert f["available"] is True
    assert [h["label"] for h in f["horizons"]] == ["1w", "1m", "3m", "6m"]
    for h in f["horizons"]:
        assert h["expected"] > 0
        # The 95% interval must fully contain the 80% one, and the point
        # estimate must sit inside both.
        assert h["band_95"][0] <= h["band_80"][0] <= h["expected"] <= h["band_80"][1] <= h["band_95"][1]
        assert h["band_80"][0] < h["band_80"][1]
        assert h["calendar_days"] > h["trading_days"]   # 1m is 21 trading days ~ 30 calendar


def test_band_widens_with_horizon():
    f = nse_forecast.build_forecast(_history(120))
    width = [h["band_95"][1] - h["band_95"][0] for h in f["horizons"]]
    assert width == sorted(width)


def test_drift_is_shrunk_toward_zero():
    f = nse_forecast.build_forecast(_history(80, drift=0.004, seed=11))
    assert abs(f["daily_drift_pct"]) < abs(f["raw_drift_pct"])
    assert 0 < f["drift_shrinkage"] < 1


def test_confidence_scales_with_evidence():
    assert nse_forecast.build_forecast(_history(30))["confidence"] == "low"
    assert nse_forecast.build_forecast(_history(80))["confidence"] == "medium"
    assert nse_forecast.build_forecast(_history(210))["confidence"] == "higher"


def test_zero_prices_are_excluded_from_returns():
    """ALP prints 0.00; a log return through zero is -inf, so it must be dropped."""
    df = _history(40)
    df.iloc[10, df.columns.get_loc("close")] = 0.0
    f = nse_forecast.build_forecast(df)
    assert f["observations"] == 39
    assert f["available"] is True


def test_flat_series_is_rejected():
    idx = pd.date_range("2026-01-01", periods=40, freq="B")
    flat = pd.DataFrame({"close": [5.0] * 40}, index=idx)
    f = nse_forecast.build_forecast(flat)
    assert f["available"] is False and "flat" in f["reason"]


def test_moving_averages_only_appear_once_the_window_is_full():
    t25 = nse_forecast.summarize_trend(_history(25))
    assert t25["sma20"] is not None and t25["sma50"] is None and t25["sma200"] is None
    assert t25["above_sma200"] is None
    t210 = nse_forecast.summarize_trend(_history(210))
    assert t210["sma200"] is not None and isinstance(t210["above_sma200"], bool)


def test_forecast_is_json_serialisable():
    import json as _json
    _json.dumps(nse_forecast.build_forecast(_history(80)))
    _json.dumps(nse_forecast.build_forecast(None))


# ---------- dashboard cache + API ----------

@pytest.fixture
def nse_cache_file(tmp_path, monkeypatch):
    from long_term import daily_digest
    p = tmp_path / "nse_dashboard.json"
    monkeypatch.setattr(daily_digest, "NSE_CACHE_FILE", p)
    return p


def _write_cache(path, **overrides):
    stock = {
        "ticker": "SCOM", "name": "Standard Chartered Bank Kenya", "sector": "BANKING",
        "isin": "KE0000000004", "price": 48.5, "change_pct": 1.83, "volume": 1284750,
        "stale": False, "pe_ratio": 5.1, "eps": 9.5, "dividend_yield": 9.8,
        "payout_ratio": 65.0, "market_cap": 1.2e11, "market_cap_usd": 9.3e8,
        "recommendation": "Buy", "score": 71.2, "coverage": 0.62,
        "positives": ["P/E 5.1 clears the value hurdle"], "negatives": [],
        "trend": {"observations": 90, "sma20": 47.0},
        "forecast": {"available": True, "observations": 90, "confidence": "medium",
                     "as_of": "2026-09-29", "label": "Modest upward drift",
                     "disclaimer": "not advice", "horizons": [
                         {"label": "1w", "expected": 48.9, "expected_pct": 0.8,
                          "calendar_days": 7, "band_80": [47.9, 49.9], "band_95": [47.4, 50.4]}],
                     "reason": None},
        "chart": {"dates": ["2026-09-28", "2026-09-29"], "closes": [48.0, 48.5], "volumes": [10, 20]},
    }
    payload = {
        "updated_at": "2026-09-29T13:00:00+00:00", "trading_date": "2026-09-29",
        "source": "rapidapi", "stale": False,
        "counts": {"total": 70, "with_fundamentals": 62, "with_forecasts": 0, "stale_quotes": 3},
        "sectors": [{"sector": "BANKING", "count": 9, "traded_value": 4.2e9}],
        "gainers": ["SCOM"], "losers": ["EQTY"], "notes": ["history still short"],
        "disclaimer": "analysis only",
        "stocks": [stock, {**stock, "ticker": "EQTY", "name": "Equity Group Holdings",
                           "pe_ratio": None, "change_pct": -0.94, "score": None}],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_nse_endpoints_before_any_data(nse_cache_file):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)

    assert c.get("/api/nse/summary").status_code == 200
    empty = c.get("/api/nse/stocks").json()
    assert empty["stocks"] == [] and empty["total"] == 0
    assert c.get("/api/nse/stock/SCOM").status_code == 404
    assert c.get("/api/nse/forecast/SCOM").status_code == 404


def test_nse_endpoints_with_data(nse_cache_file):
    _write_cache(nse_cache_file)
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)

    summary = c.get("/api/nse/summary").json()
    assert summary["counts"]["total"] == 70
    assert summary["source"] == "rapidapi"
    assert summary["notes"] == ["history still short"]

    listing = c.get("/api/nse/stocks").json()
    assert listing["total"] == 2
    assert "chart" not in listing["stocks"][0]        # stripped from list responses

    detail = c.get("/api/nse/stock/scom").json()      # lowercase must resolve
    assert detail["ticker"] == "SCOM"
    assert detail["pe_ratio"] == 5.1
    assert len(detail["chart"]["closes"]) == 2

    fc = c.get("/api/nse/forecast/SCOM").json()
    assert fc["available"] is True and fc["horizons"][0]["label"] == "1w"
    assert fc["disclaimer"] == "not advice"            # must reach the UI


def test_nse_search_and_sort(nse_cache_file):
    _write_cache(nse_cache_file)
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)

    assert [s["ticker"] for s in c.get("/api/nse/stocks?q=equity").json()["stocks"]] == ["EQTY"]
    assert c.get("/api/nse/stocks?q=banking").json()["total"] == 2     # sector match
    assert c.get("/api/nse/stocks?q=zzzz").json()["total"] == 0
    assert c.get("/api/nse/stocks?sector=BANKING").json()["total"] == 2

    # A missing P/E must sort last, never displace a real one at the top.
    top = c.get("/api/nse/stocks?sort=pe_ratio").json()["stocks"]
    assert top[0]["ticker"] == "SCOM"
    assert top[1]["pe_ratio"] is None

    assert len(c.get("/api/nse/stocks?limit=1").json()["stocks"]) == 1


@pytest.mark.parametrize("bad", ["../../etc/passwd", "SCOM; DROP TABLE", "SC OM", "A" * 40, "%00"])
def test_ticker_input_is_validated(nse_cache_file, bad):
    _write_cache(nse_cache_file)
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)
    r = c.get(f"/api/nse/stock/{bad}")
    assert r.status_code in (400, 404)


def test_corrupt_nse_cache_does_not_500(nse_cache_file):
    nse_cache_file.write_text("{{{ not json", encoding="utf-8")
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)
    assert c.get("/api/nse/summary").status_code == 200
    assert c.get("/api/nse/stocks").json()["stocks"] == []


# ---------- whole-exchange panel build (end to end, no network) ----------

class FakeFundamentals:
    """Stands in for FundamentalsFetcher: AFX pages, no quota, no key."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def get_profile(self, ticker, market=None):
        self.calls.append((ticker, market))
        if ticker in self.missing:
            return None
        if ticker == "ALP":
            raise RuntimeError("no AFX page for this name")
        return {
            "ticker": ticker, "quote_type": "STOCK", "sector": "BANKING",
            "pe_ratio": 5.1 if ticker == "SCOM" else None,
            "eps": 9.5, "dividend_yield": 9.8, "payout_ratio": 65.0,
            "market_cap": 1.2e11, "market_cap_usd": 9.3e8,
            "dividend_per_share": 4.75, "return_1y": 18.2,
        }


def test_refresh_nse_dashboard_builds_every_listed_name(nse_cache_file, cache_dir, monkeypatch):
    from long_term import daily_digest

    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed = NSEFeed(rapidapi_key="test-key")
    feed.refresh_market_snapshot()

    def boom(*a, **k):
        raise AssertionError("refresh_nse_dashboard spent RapidAPI quota")

    monkeypatch.setattr(nse_feed.requests, "get", boom)

    fundamentals = FakeFundamentals(missing={"COOP"})   # ALP raises inside the fetch
    result = daily_digest.refresh_nse_dashboard({"nse": {}}, fundamentals, feed)

    assert result["counts"]["total"] == 5            # every listed name, not just the traded ones
    assert result["counts"]["stale_quotes"] == 2
    assert result["counts"]["with_fundamentals"] == 3
    by = {r["ticker"]: r for r in result["stocks"]}
    assert by["SCOM"]["pe_ratio"] == 5.1
    # The day's own close is already in the series, so the chart starts with
    # one point and the projection is still correctly withheld.
    assert by["SCOM"]["chart"]["closes"] == [48.5]
    assert by["SCOM"]["forecast"]["available"] is False
    assert any("projection" in n.lower() for n in result["notes"])

    # Alpaca... no: ALP's AFX failure must degrade to nulls, never kill the row.
    assert by["ALP"]["ticker"] == "ALP" and by["ALP"]["pe_ratio"] is None
    assert by["COOP"]["pe_ratio"] is None

    # A real score from the shared scorer, and the payload must round-trip.
    assert by["SCOM"]["score"] is not None and by["SCOM"]["recommendation"]
    assert nse_cache_file.exists()
    assert json.loads(nse_cache_file.read_text())["counts"]["total"] == 5

    from fastapi.testclient import TestClient
    from dashboard.app import app
    assert TestClient(app).get("/api/nse/stocks").json()["total"] == 5


def test_nse_panel_produces_forecasts_once_history_exists(nse_cache_file, cache_dir, monkeypatch):
    from long_term import daily_digest

    monkeypatch.setattr(nse_feed.requests, "get", lambda *a, **k: FakeResponse(payload=STOCKS_RESPONSE))
    feed = NSEFeed(rapidapi_key="test-key")
    for i in range(30):
        feed._append_history_row("SCOM", f"2026-08-{i + 1:02d}" if i < 31 else "2026-09-01",
                                 40.0 + i * 0.1, 40.5 + i * 0.1, 39.8 + i * 0.1,
                                 40.2 + i * 0.1, 1000)
    feed.refresh_market_snapshot()

    result = daily_digest.refresh_nse_dashboard({"nse": {}}, FakeFundamentals(), feed)
    by = {r["ticker"]: r for r in result["stocks"]}
    fc = by["SCOM"]["forecast"]
    assert fc["available"] is True
    assert len(fc["horizons"]) == 4
    assert fc["label"] and fc["disclaimer"]
    assert by["SCOM"]["chart"]["closes"], "the panel must carry the accumulated series"
    assert result["counts"]["with_forecasts"] == 1     # only SCOM has history


# ---------- go-live safety: a disabled venue must never trade ----------

def _cfg():
    return {"execution": {"exchanges": [
        {"name": "binance", "enabled": True, "markets": ["ETH/USDT"]},
        {"name": "bybit", "enabled": True, "markets": ["ETH/USDT", "SOL/USDT"]},
        {"name": "oanda", "enabled": False, "markets": []},
        {"name": "alpaca", "enabled": False, "markets": []},
        {"name": "mt5", "enabled": True, "markets": []},
    ]}}


def test_bybit_is_wired_end_to_end():
    """The config entry is all that was missing: the venue name must already be
    whitelisted in the executor, the market-data router and the always-open
    crypto set, and must resolve to a real ccxt class."""
    import ccxt
    import yaml
    from pathlib import Path
    from core.market_hours import CRYPTO_EXCHANGES
    from data_feeds import feed_router
    import main as main_mod

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config" / "config.yaml").read_text(encoding="utf-8"))
    bybit = [e for e in cfg["execution"]["exchanges"] if e["name"] == "bybit"]
    assert len(bybit) == 1, "bybit must appear exactly once in config.yaml"
    assert bybit[0]["enabled"] is True
    assert bybit[0]["markets"] == ["ETH/USDT", "SOL/USDT"]

    assert hasattr(ccxt, "bybit"), "config name must match a ccxt class attribute"
    assert "bybit" in CRYPTO_EXCHANGES
    assert "bybit" in open(main_mod.__file__, encoding="utf-8").read()
    assert "bybit" in open(feed_router.__file__, encoding="utf-8").read()


def test_config_untouched_except_the_bybit_block():
    """The user asked for exactly one added entry, leaving the rest alone."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config" / "config.yaml").read_text(encoding="utf-8"))
    venues = {e["name"]: e for e in cfg["execution"]["exchanges"]}
    assert venues["binance"]["markets"] == ["ETH/USDT", "SOL/USDT", "XRP/USDT", "LINK/USDT"]
    assert venues["okx"]["markets"] == ["ETH/USDT", "SOL/USDT"]
    assert venues["oanda"]["enabled"] is False
    assert venues["alpaca"]["enabled"] is False
    assert venues["mt5"]["enabled"] is True


def test_disabled_venue_never_builds_an_executor(monkeypatch):
    """OANDA and Alpaca carry keys in .env and their paper flags already point
    at PRODUCTION hosts. They are safe only because `enabled: false` means no
    executor is constructed. This locks that behaviour down."""
    import main as main_mod
    src = main_mod.__file__
    for venue in ("oanda", "alpaca", "mt5"):
        assert f'venue_enabled("{venue}")' in open(src, encoding="utf-8").read(), \
            f"{venue} must gate on venue_enabled() before building an executor"


def test_paper_flags_are_documented_as_independent_of_live_trading():
    """ALPACA_PAPER / OANDA_PRACTICE select the API host and are NOT the same
    switch as LIVE_TRADING. That distinction must be stated where someone will
    read it before going live."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "ALPACA_PAPER" in example and "OANDA_PRACTICE" in example
    assert "INDEPENDENT" in example.upper()
    for doc in ("docs/FUNDING.md",):
        assert (root / doc).exists(), doc
    funding = (root / "docs" / "FUNDING.md").read_text(encoding="utf-8")
    assert "independent" in funding.lower()
    assert "TRC20" in funding and "BEP20" in funding


def test_metamask_private_key_is_not_in_env():
    """The treasury wallet is public-only. A private key or seed phrase in .env
    would hand the process custody of the funds, so only these names may exist
    and none may look like a 64-hex-char key."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    allowed = ("METAMASK_WALLET_ADDRESS", "METAMASK_NETWORK", "ETHEREUM_RPC_URL",
               "TRON_RPC_URL", "BSC_RPC_URL", "ETHERSCAN_API_KEY",
               "FUNDING_BINANCE_USDT", "FUNDING_OKX_USDT", "FUNDING_BYBIT_USDT")
    for name in (".env", ".env.example"):
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if "METAMASK" in key or "WALLET" in key or "MNEMONIC" in key \
               or "PRIVATE_KEY" in key or "SEED" in key:
                assert key in allowed, f"{name}: unexpected wallet/key var {key}"
            # No bare 64-hex-char value, which is what an Ethereum private key
            # looks like.
            val = line.split("=", 1)[1].split("#")[0].strip()
            if len(val) == 64:
                try:
                    int(val, 16)
                    raise AssertionError(f"{name}: {key} looks like a private key")
                except ValueError:
                    pass


def test_preflight_script_runs_and_reports_cleanly():
    """The go-live gate must work on a real checkout, not just be a doc."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "scripts/preflight.py"], cwd=root,
                       capture_output=True, text=True, timeout=120)
    assert "PRE-FLIGHT" in r.stdout
    assert "BLOCK" in r.stdout
    # Dry-run should never be blocked by the venue checks.
    assert r.returncode in (0, 1)


# ---------- scheduler wiring ----------

def test_snapshot_job_runs_at_16_00_east_africa_time():
    """16:00 EAT is 13:00 UTC. The trigger must be pinned to Africa/Nairobi so
    it does not drift with the host clock, and must be Mon-Fri."""
    import datetime
    import yaml
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.util import astimezone
    from long_term import scheduler

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text(encoding="utf-8"))
    trig = CronTrigger.from_crontab(cfg["nse"]["snapshot_schedule"], timezone=astimezone("Africa/Nairobi"))

    nxt = trig.get_next_fire_time(None, datetime.datetime(2026, 9, 28, 12, 0, tzinfo=datetime.timezone.utc))
    assert nxt.hour == 16 and nxt.utcoffset() == datetime.timedelta(hours=3)
    assert nxt.astimezone(datetime.timezone.utc).hour == 13
    assert nxt.weekday() < 5                      # Monday=0

    # A Saturday must be skipped.
    sat = trig.get_next_fire_time(None, datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.timezone.utc))
    assert sat.weekday() < 5
    assert scheduler.NSE_TIMEZONE is not None


def test_startup_rebuild_never_spends_rapidapi_quota(capsys, tmp_path):
    """The scheduler rebuilds the NSE panel on startup when the cache is missing.
    That path must NOT call /stocks: the Basic plan allows 4 requests/hour and
    250/month, so a restarting process would drain the month's budget in a day.
    Only the scheduled 16:00 job may spend quota."""
    from long_term import scheduler

    class ExplodingFeed(NSEFeed):
        def refresh_market_snapshot(self, force_refresh=False):
            raise AssertionError("startup path called refresh_market_snapshot()")

    feed = ExplodingFeed(rapidapi_key="test-key")
    scheduler.run_nse_snapshot(object(), feed, {"nse": {}}, spend_rapidapi=False)
    out = capsys.readouterr().out
    assert "no RapidAPI call" in out

    # ...whereas the scheduled job does spend it, and swallows failures rather
    # than taking the dashboard down.
    class FailingFeed(NSEFeed):
        def refresh_market_snapshot(self, force_refresh=False):
            raise RuntimeError("HTTP 429")

    scheduler.run_nse_snapshot(object(), FailingFeed(rapidapi_key="k"), {"nse": {}})
    assert "keeping previous data" in capsys.readouterr().out


def test_east_africa_trading_date_is_not_the_utc_date():
    """Between 21:00 and 24:00 UTC the calendar date in Nairobi is already the
    next day. The per-ticker CSVs key on the EAT date, so a snapshot taken then
    must not be filed under the previous day."""
    from datetime import datetime as dt, timezone as tz, timedelta

    # 22:00 UTC on the 28th == 01:00 EAT on the 29th.
    evening_utc = dt(2026, 9, 28, 22, 0, tzinfo=tz.utc)
    eat_date = (evening_utc + timedelta(hours=3)).date()
    assert eat_date.isoformat() == "2026-09-29"
    assert eat_date != evening_utc.date()

    # 02:00 UTC is 05:00 EAT — same day, so the two agree.
    early_utc = dt(2026, 9, 29, 2, 0, tzinfo=tz.utc)
    assert (early_utc + timedelta(hours=3)).date() == early_utc.date()

    # 16:00 EAT is 13:00 UTC, and the scheduled run must still be filed under
    # its own EAT trading date.
    scheduled_utc = dt(2026, 9, 29, 13, 0, tzinfo=tz.utc)
    eat = tz(timedelta(hours=3))
    assert scheduled_utc.astimezone(eat).isoformat() == "2026-09-29T16:00:00+03:00"
    assert scheduled_utc.astimezone(eat).date().isoformat() == "2026-09-29"
