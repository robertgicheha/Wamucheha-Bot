"""
Tests for the Stocks & ETFs research module. No network: both feeds are stubbed.

Properties worth protecting:
  * NSE parsing and the screener agree with Sokowatch-Web
  * no invented data: no key / dead feed means "unavailable", never sample prices
  * the NSE request quota is not burned (cache, shut market, backoff)
  * the outlook refuses short histories and only counts outcomes already known
  * every route needs the dashboard key
"""
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("DASHBOARD_SECRET_KEY", "test-secret-123")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from stock_analysis import analytics, fundamentals, history, metrics, nse, screener, screens, service, us  # noqa: E402

RAW = {"ticker": "SCOM", "name": "Safaricom Ltd Ord 0.05", "volume": "4,843,210", "price": "36.35",
       "change": "0.41", "sector": "Telecommunication"}


def _at(text):
    return datetime.fromisoformat(text + "+03:00")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    nse._cache.clear()
    us._cache.clear()
    us._failed.clear()
    us._live.clear()
    us._intraday.clear()
    fundamentals._cache.clear()
    fundamentals._extra_cache.clear()
    monkeypatch.setattr(fundamentals, "_loaded", False)
    monkeypatch.setattr(fundamentals, "_fetch_info", lambda s: {"marketCap": 5e10, "totalRevenue": 100.0,
                                                               "fullTimeEmployees": 4, "recommendationKey": "buy"})
    monkeypatch.setattr(fundamentals, "NSE_CSV", tmp_path / "nse_fundamentals.csv")
    monkeypatch.setattr(nse, "_backoff_until", 0.0)
    monkeypatch.setattr(nse, "_last_error", "")


def test_parsing_matches_sokowatch():
    s = nse.transform(RAW)
    assert (s["name"], s["price"], s["volume"], s["change_pct"]) == ("Safaricom Ltd", 36.35, 4843210, 0.41)
    assert s["change"] == pytest.approx(0.15)            # bare value is a percentage
    assert nse.parse_change("+0.30 (+1.74%)") == (0.30, 1.74)
    assert nse.parse_change("n/a", 10) == (0.0, 0.0)
    assert nse.transform({**RAW, "sector": nse.ETF_SECTOR})["kind"] == "etf"


def test_screener_matches_sokowatch_scoring():
    # balanced: 20 base + Telecom 0.2/0.25*40=32 + liquidity .8950*20=17.9 + momentum .082*10=0.82
    r = screener.score_stock(nse.transform(RAW), "balanced")
    assert (r["match_score"], r["verdict"]) == (71, "BUY")
    assert r["stop_loss"] == pytest.approx(31.99, abs=0.01)


def test_no_key_means_unavailable_not_sample_prices():
    q = nse.get_quotes()
    assert q["source"] == "unavailable" and q["stocks"] == [] and "RAPIDAPI_KEY" in q["note"]


def test_one_request_is_reused_then_failure_serves_stale_and_backs_off(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    calls = []

    def ok(key):
        calls.append(1)
        return {"rows": [RAW], "fetched_at": time.time(), "updated": "now"}
    monkeypatch.setattr(nse, "_fetch_upstream", ok)
    assert nse.get_quotes()["source"] == "live"
    nse._cache.clear()                                    # as after a restart: the disk copy is used
    assert nse.get_quotes()["source"] == "live" and len(calls) == 1

    def boom(key):
        calls.append(1)
        raise RuntimeError("NSE API error: HTTP 429")
    monkeypatch.setattr(nse, "_fetch_upstream", boom)
    monkeypatch.setattr(nse, "needs_refresh", lambda *a: True)
    first, second = nse.get_quotes(), nse.get_quotes()
    assert first["source"] == second["source"] == "stale" and "429" in second["note"]
    assert len(calls) == 2


def test_data_too_old_is_dropped(monkeypatch):
    nse._cache.update({"rows": [RAW], "fetched_at": time.time() - nse.STALE_MAX_SECONDS - 1, "updated": "old"})
    assert nse.get_quotes()["source"] == "unavailable"


def test_shut_market_is_not_refetched_once_closes_are_held():
    saturday = _at("2026-11-28T12:00:00").timestamp()
    assert not nse.needs_refresh(_at("2026-11-27T16:00:00").timestamp(), saturday, 3600)
    assert nse.needs_refresh(_at("2026-11-27T11:00:00").timestamp(), saturday, 3600)   # mid-session copy
    assert nse.needs_refresh(_at("2026-11-27T10:00:00").timestamp(), _at("2026-11-27T12:00:00").timestamp(), 3600)


def test_snapshot_dates_skip_weekends_and_pre_open():
    assert nse.snapshot_date(_at("2026-11-27T16:00:00")) == "2026-11-27"
    assert nse.snapshot_date(_at("2026-11-27T08:00:00")) is None
    assert nse.snapshot_date(_at("2026-11-28T12:00:00")) is None


def test_history_keeps_last_price_of_day_and_csv_import_is_all_or_nothing(tmp_path):
    stock = nse.transform(RAW)
    history.record("2026-11-26", [stock])
    history.record("2026-11-26", [{**stock, "price": 37.0}])
    assert history.load_all()["SCOM"]["close"] == [37.0]
    bad = tmp_path / "bad.csv"
    bad.write_text("date,symbol,close\n2026-11-24,SCOM,35\n2026-11-25,SCOM,oops\n")
    with pytest.raises(ValueError, match="line 3"):
        history.import_csv(str(bad))
    assert history.load_all()["SCOM"]["close"] == [37.0]


def test_short_history_gives_no_indicators_or_outlook():
    assert analytics.analyze([10.0] * 59) == {"bars": 59, "enough_history": False}
    assert analytics.analyze([10.0 + i for i in range(100)])["outlook"] is None


def test_outlook_on_a_steady_riser_shows_no_edge_over_its_own_average():
    o = analytics.outlook([100 * 1.001 ** i for i in range(600)])
    assert o["rose_pct"] == o["all_days_rose_pct"] == 100.0 and o["edge_pct"] == 0.0
    assert o["matches"] == 600 - 199 - analytics.HORIZON  # nothing without a known outcome is counted
    row = {"symbol": "X", "analysis": {"outlook": o}}
    assert analytics.picks([row])["likely_to_rise"] == []


def test_best_buy_excludes_overbought_and_volatile():
    def row(symbol, **kw):
        return {"symbol": symbol, "analysis": {"buy_score": 80, "volatility_pct": 20, "rsi14": 55, **kw}}
    got = analytics.picks([row("GOOD"), row("HOT", rsi14=75), row("WILD", volatility_pct=60)])
    assert got["best_buy"] == ["GOOD"]


def test_dividend_yield_and_growth():
    series = {"dates": ["2022-01-03", "2026-11-25"], "close": [50.0, 100.0],
              "dividends": [("2023-06-01", 1.0), ("2025-06-01", 1.21), ("2026-06-01", 2.0)]}
    d = us.dividend_summary(series, today=date(2026, 11, 27))
    assert (d["yield_pct"], d["payments_last_year"], d["annual_growth_pct"]) == (2.0, 1, 10.0)
    assert us.dividend_summary({**series, "dividends": []}) is None


def test_nse_dividend_only_from_a_valid_user_entry():
    assert service.nse_dividend({"dividend_per_share": 2}, 40)["yield_pct"] == 5.0
    for bad in (None, {}, {"dividend_per_share": "x"}, {"dividend_per_share": 0}):
        assert service.nse_dividend(bad, 40) is None


def _fake_download(symbols, period=""):
    days = [(date.today() - timedelta(days=399 - i)).isoformat() for i in range(400)]
    closes = [100 + i * 0.1 for i in range(400)]
    return {s: {"dates": days, "open": closes, "high": closes, "low": closes, "close": closes, "adj": closes, "volume": [1000.0] * 400, "dividends": []}
            for s in symbols if s != "NOPE"}


def test_api_needs_key_and_serves_both_markets(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard import admin
    from dashboard.app import app
    monkeypatch.setattr(us, "_download", _fake_download)
    admin._fail_log.clear()
    client = TestClient(app)
    assert client.get("/api/admin/stocks/market?market=us").status_code == 401
    assert client.get("/api/admin/stocks/detail?market=us&symbol=AAPL").status_code == 401
    admin._fail_log.clear()
    h = {"X-Dashboard-Key": os.environ["DASHBOARD_SECRET_KEY"]}

    view = client.get("/api/admin/stocks/market?market=us", headers=h).json()
    assert view["meta"]["source"] == "live" and {r["kind"] for r in view["rows"]} == {"stock", "etf"}
    assert {s["key"] for s in view["screens"]} >= {"all", "large_cap", "ath", "high_52w", "oversold"}
    got = client.get("/api/admin/stocks/detail?market=us&symbol=orcl", headers=h).json()
    assert got["row"]["symbol"] == "ORCL" and len(got["chart"]) == 400 and got["ohlc"]
    assert got["technicals"]["summary"]["label"] and got["seasonals"]
    monkeypatch.setattr(us, "_fetch_intraday", lambda s, d: [{"time": 4102444800, "value": 1.0, "volume": 2.0}])
    day = client.get("/api/admin/stocks/intraday?market=us&symbol=ORCL&range=1d", headers=h).json()
    assert day["points"] and day["previous_close"] == pytest.approx(139.9)
    assert client.get("/api/admin/stocks/intraday?market=us&symbol=ORCL&range=9y", headers=h).status_code == 400
    for path in ("intraday", "financials", "news"):
        admin._fail_log.clear()
        assert client.get(f"/api/admin/stocks/{path}?market=us&symbol=ORCL").status_code == 401
    admin._fail_log.clear()

    empty = client.get("/api/admin/stocks/market?market=nse", headers=h).json()
    assert empty["meta"]["source"] == "unavailable" and empty["rows"] == []
    assert client.get("/api/admin/stocks/market?market=mars", headers=h).status_code == 400
    assert client.get("/api/admin/stocks/detail?market=us&symbol=NOPE", headers=h).status_code == 404
    assert client.get("/api/admin/stocks/detail?market=us&symbol=a%20b", headers=h).status_code == 400


def test_admin_page_loads_the_tab_and_assets_expose_nothing_else():
    from fastapi.testclient import TestClient
    from dashboard.app import app
    html = (service.ROOT / "dashboard" / "templates" / "admin.html").read_text(encoding="utf-8")
    assert '["stocks","Stocks & ETFs"]' in html and 'id="t-stocks"' in html and "/stocks-assets/stocks.js" in html
    client = TestClient(app)
    assert client.get("/stocks-assets/stocks.js").status_code == 200
    assert client.get("/stocks-assets/stocks.css").status_code == 200
    logo = client.get("/stocks-assets/logos/SCOM")
    assert logo.status_code == 200 and "default-src 'none'" in logo.headers["content-security-policy"]
    for bad in ("/stocks-assets/..%2Fadmin.py", "/stocks-assets/admin.py", "/stocks-assets/logos/..%2F..%2Fapp",
                "/stocks-assets/logos/NOPE"):
        assert client.get(bad).status_code == 404, bad


def test_logo_files_are_really_images():
    """One file copied from Sokowatch-Web turned out to be a saved web page full of scripts."""
    for path in (service.ROOT / "dashboard" / "stocks_ui" / "logos").iterdir():
        body = path.read_bytes()
        if path.suffix == ".svg":
            assert body.lstrip().startswith((b"<svg", b"<?xml")) and b"<script" not in body.lower(), path.name
        else:
            assert body.startswith((b"\x89PNG", b"\xff\xd8\xff")), path.name


def _daily(n, start=100.0, step=1.0):
    days = [(date(2026, 11, 27) - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    return days, [start + i * step for i in range(n)]


def test_performance_is_blank_when_history_does_not_reach_back():
    got = metrics.performance(*_daily(40))
    assert got["1m"] == pytest.approx((139 / 109 - 1) * 100, abs=0.01)
    assert got["all"] == 39.0 and got["1y"] is None and got["ytd"] is None
    assert metrics.row_metrics(None, 10, 5)["high_52w"] is None


def test_technical_rating_and_seasonals():
    got = metrics.technicals([100 * 1.002 ** i for i in range(300)])
    assert got["ma_rating"]["label"] == "Strong buy" and got["ma_rating"]["sell"] == 0
    assert metrics.technicals([5.0, 6.0])["summary"]["label"] is None
    table = metrics.seasonals(["2026-01-30", "2026-02-27", "2026-04-30"], [100.0, 110.0, 120.0])
    assert table == [{"year": 2026, "months": [None, 10.0] + [None] * 10}]     # a gap is not a month's move


def _row(symbol, price=10.0, change=1.0, **fund):
    return {"symbol": symbol, "price": price, "change_pct": change, "volume": 100, "fund": fund, "dividend": None,
            "metrics": metrics.row_metrics(None, price, 100)}


def test_filters_leave_out_rows_without_the_figure():
    rows = [_row("BIG", market_cap=5e11, all_time_high=10.1), _row("TINY", price=2.0, change=-3.0, market_cap=1e8),
            _row("BLANK")]
    got = {s["key"]: s["symbols"] for s in screens.run(rows, "nse")}
    assert got["all"][0] == "BIG" and len(got["all"]) == 3
    assert got["large_cap"] == ["BIG"] and got["small_cap"] == ["TINY"] and got["penny"] == ["TINY"]
    assert got["biggest_losers"] == ["TINY"] and got["ath"] == ["BIG"]
    assert got["high_beta"] == got["oversold"] == got["high_52w"] == []
    assert len(got) == 25


def test_nse_csv_figures_are_used_and_bad_cells_ignored():
    assert fundamentals.nse_table() == {}
    fundamentals.NSE_CSV.write_text(
        "symbol,eps,shares_outstanding,employees,revenue,div_yield_pct,beta,perf_1y_pct,analyst_rating\n"
        'SCOM,2,1000,10,"5,000",5%,oops,12.5,Buy\n', encoding="utf-8")
    figures = fundamentals.nse_table()["SCOM"]
    assert figures["revenue"] == 5000 and figures["revenue_per_employee"] == 500 and "beta" not in figures
    row = {**nse.transform(RAW), "metrics": metrics.row_metrics(None, 36.35, 1), "dividend": None}
    service.nse_overlay(row, figures)
    assert row["fund"]["market_cap"] == pytest.approx(36350)
    assert row["fund"]["pe"] == pytest.approx(18.18, abs=0.01)
    assert row["metrics"]["perf"]["1y"] == 12.5 and row["dividend"]["yield_pct"] == 5


def test_nse_intraday_keeps_only_recent_fetches():
    stock = nse.transform(RAW)
    history.record_tick(1_000_000, [stock])
    history.record_tick(1_000_000 + 20 * 86400, [stock])     # the old point is pruned
    assert [row[0] for row in history.ticks("SCOM")] == [1_000_000 + 20 * 86400]


def test_live_bars_extend_history_without_duplicating_days():
    old = _fake_download(["X"])["X"]
    new = {**{k: v[-1:] for k, v in old.items() if k != "dividends"}, "dividends": [], "close": [999.0]}
    merged = us.merge_bars(old, new)
    assert len(merged["dates"]) == len(old["dates"]) and merged["close"][-1] == 999.0
    assert old["close"][-1] != 999.0


def test_rss_keeps_safe_links_and_refuses_entity_declarations():
    feed = ("<rss><channel><title>T</title><item><title>A</title><link>https://example.com/a</link></item>"
            "<item><title>B</title><link>javascript:alert(1)</link></item></channel></rss>")
    assert [n["title"] for n in fundamentals.parse_rss(feed, "x")] == ["A"]
    assert fundamentals.parse_rss('<!DOCTYPE r [<!ENTITY a "b">]>' + feed, "x") == []
    assert fundamentals.parse_rss("<broken", "x") == []
