"""Offline tests for scripts/fetch_market.py (no network: SESSION is faked)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import fetch_market as fm  # noqa: E402

# 2025-06-02 (Monday) 12:00 UTC = 15:00 Riyadh, market open 10:00-15:00 Riyadh.
NOW = datetime(2025, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
SESSION_START = int(datetime(2025, 6, 2, 7, 0, tzinfo=timezone.utc).timestamp())   # 10:00 Riyadh
SESSION_END = int(datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc).timestamp())    # 15:00 Riyadh
MARKET_TIME = int(datetime(2025, 6, 2, 11, 58, 40, tzinfo=timezone.utc).timestamp())
DAY = 86400


def yahoo_fixture(symbol="^TASI.SR", price=11500.25, closes=(11300.0, 11350.5, 11420.0, 11400.0, 11500.25),
                  with_previous_close=False, trading_period=True, volume=123456789):
    n = len(closes)
    ts = [SESSION_START - (n - 1 - i) * DAY for i in range(n)]
    meta = {
        "currency": "SAR", "symbol": symbol, "exchangeName": "SAU",
        "regularMarketTime": MARKET_TIME, "regularMarketPrice": price,
        "chartPreviousClose": 11250.0, "regularMarketVolume": volume,
    }
    if with_previous_close:
        meta["previousClose"] = 11111.0
    if trading_period:
        meta["currentTradingPeriod"] = {"regular": {"start": SESSION_START, "end": SESSION_END, "gmtoffset": 10800}}
    return {"chart": {"result": [{
        "meta": meta, "timestamp": ts,
        "indicators": {"quote": [{"close": list(closes), "volume": [1000 * (i + 1) for i in range(n)]}]},
    }], "error": None}}


# US session on 2025-06-02: 09:30-16:00 New York (EDT, UTC-4) = 13:30-20:00 UTC.
US_SESSION_START = int(datetime(2025, 6, 2, 13, 30, tzinfo=timezone.utc).timestamp())
US_SESSION_END = int(datetime(2025, 6, 2, 20, 0, tzinfo=timezone.utc).timestamp())


def us_fixture(symbol="AAPL", price=200.0, closes=(195.0, 196.0, 197.0, 198.0, 200.0), market_time=MARKET_TIME,
               trading_period=True, currency="USD"):
    fx = yahoo_fixture(symbol, price=price, closes=closes, trading_period=False)
    meta = fx["chart"]["result"][0]["meta"]
    meta.update(currency=currency, exchangeName="NMS", regularMarketTime=market_time, chartPreviousClose=190.0)
    fx["chart"]["result"][0]["timestamp"] = [US_SESSION_START - (len(closes) - 1 - i) * DAY for i in range(len(closes))]
    if trading_period:
        meta["currentTradingPeriod"] = {"regular": {"start": US_SESSION_START, "end": US_SESSION_END, "gmtoffset": -14400}}
    return fx


def quote_obj(symbol, price, prev, mcap, market_time=MARKET_TIME, state="REGULAR", volume=5000, currency="USD"):
    return {"symbol": symbol, "regularMarketPrice": price, "regularMarketPreviousClose": prev, "marketCap": mcap,
            "regularMarketTime": market_time, "marketState": state, "regularMarketVolume": volume, "currency": currency}


SAUDIEXCHANGE_HTML = """
<html><body><div class="indices">
<table class="table">
 <thead><tr><th>المؤشر</th><th>القيمة</th><th>التغير</th><th>التغير %</th></tr></thead>
 <tbody>
  <tr><td><a href="#">المؤشر العام تاسي (TASI)</a></td><td>11,432.17</td><td>-58.44</td><td>-0.51 %</td></tr>
  <tr><td>مؤشر MT30</td><td>1,480.22</td><td>+3.10</td><td>0.21%</td></tr>
  <tr><td>مؤشر نمو الموازية (NomuC)</td><td>26,801.90</td><td>(120.30)</td><td>(0.45%)</td></tr>
 </tbody>
</table></div></body></html>
"""

STOOQ_CSV = "Date,Open,High,Low,Close,Volume\n2025-05-29,11400,11450,11380,11420,1\n2025-06-01,11420,11500,11390,11480.5,2\n"


class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body
        self.text = text if body is None else json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Routes GETs by substring of the URL; a handler may be a response, an
    exception, or a list consumed one call at a time."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        assert timeout == fm.TIMEOUT, "every request must use the 15s timeout"
        assert "Mozilla" in headers["User-Agent"], "browser-like UA required"
        for key, handler in self.routes.items():
            if key in url:
                if isinstance(handler, list):
                    handler = handler.pop(0)
                if isinstance(handler, Exception):
                    raise handler
                return handler
        return FakeResponse(404, text="not found")


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(fm, "now_utc", lambda: NOW)
    monkeypatch.setattr(fm, "sleep", lambda s: None)
    monkeypatch.setattr(fm, "SESSION", FakeSession({}))  # default: everything 404s


def use_session(monkeypatch, routes):
    s = FakeSession(routes)
    monkeypatch.setattr(fm, "SESSION", s)
    return s


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_yahoo_chart_basic():
    p = fm.parse_yahoo_chart(yahoo_fixture(), now=NOW)
    assert p["price"] == 11500.25
    # last bar is today's, so prev close = second-to-last daily close
    assert p["prev_close"] == 11400.0
    assert p["change_pts"] == pytest.approx(100.25)
    assert p["change_pct"] == pytest.approx(100.25 / 11400 * 100, abs=1e-4)
    assert p["volume"] == 123456789
    assert p["as_of"] == datetime(2025, 6, 2, 11, 58, 40, tzinfo=timezone.utc)
    assert p["session_date"] == "2025-06-02"
    assert p["is_closed"] is False  # NOW == session end, still inside the window


def test_parse_yahoo_chart_prefers_meta_previous_close():
    p = fm.parse_yahoo_chart(yahoo_fixture(with_previous_close=True), now=NOW)
    assert p["prev_close"] == 11111.0


def test_parse_yahoo_chart_closed_after_session():
    later = datetime(2025, 6, 2, 13, 0, tzinfo=timezone.utc)
    assert fm.parse_yahoo_chart(yahoo_fixture(), now=later)["is_closed"] is True
    # no trading-period info -> assume closed rather than guess
    assert fm.parse_yahoo_chart(yahoo_fixture(trading_period=False), now=NOW)["is_closed"] is True


def test_parse_yahoo_chart_falls_back_to_chart_previous_close():
    fx = yahoo_fixture(closes=(11500.25,), volume=None)
    p = fm.parse_yahoo_chart(fx, now=NOW)
    assert p["prev_close"] == 11250.0
    assert p["volume"] == 1000  # from the last bar when meta has no volume


def test_parse_yahoo_chart_rejects_missing_price():
    fx = yahoo_fixture()
    del fx["chart"]["result"][0]["meta"]["regularMarketPrice"]
    with pytest.raises(fm.FetchError):
        fm.parse_yahoo_chart(fx, now=NOW)
    with pytest.raises(fm.FetchError):
        fm.parse_yahoo_chart({"chart": {"result": None}}, now=NOW)


def test_parse_yahoo_chart_us_uses_new_york_calendar_and_session():
    ny = fm.MARKETS["us"]["tz"]
    # 11:58 UTC is before the 13:30 UTC open -> closed (pre-market); last bar is "today" in NY
    p = fm.parse_yahoo_chart(us_fixture(), now=NOW, tz=ny)
    assert p["price"] == 200.0 and p["prev_close"] == 198.0 and p["currency"] == "USD"
    assert p["session_date"] == "2025-06-02" and p["is_closed"] is True
    # inside the regular session -> open
    during = datetime(2025, 6, 2, 15, 0, tzinfo=timezone.utc)
    p = fm.parse_yahoo_chart(us_fixture(market_time=int(during.timestamp()) - 60), now=during, tz=ny)
    assert p["is_closed"] is False
    # a bar stamped 23:30 UTC belongs to the same NY day (19:30 EDT), but the next Riyadh day
    late = int(datetime(2025, 6, 2, 23, 30, tzinfo=timezone.utc).timestamp())
    fx = us_fixture(market_time=late)
    assert fm.parse_yahoo_chart(fx, now=NOW, tz=ny)["session_date"] == "2025-06-02"
    assert fm.parse_yahoo_chart(fx, now=NOW)["session_date"] == "2025-06-03"


def test_parse_yahoo_quote():
    p = fm.parse_yahoo_quote(quote_obj("MSFT", 460.25, 455.0, 3.4e12), now=NOW, tz=fm.MARKETS["us"]["tz"])
    assert p["price"] == 460.25 and p["prev_close"] == 455.0 and p["market_cap"] == 3.4e12
    assert p["change_pts"] == pytest.approx(5.25) and p["change_pct"] == pytest.approx(5.25 / 455 * 100, abs=1e-4)
    assert p["volume"] == 5000 and p["is_closed"] is False and p["session_date"] == "2025-06-02"
    assert p["as_of"] == datetime(2025, 6, 2, 11, 58, 40, tzinfo=timezone.utc)
    assert fm.parse_yahoo_quote(quote_obj("MSFT", 1, 1, 1, state="CLOSED"), now=NOW)["is_closed"] is True
    assert fm.parse_yahoo_quote({"symbol": "MSFT", "regularMarketPrice": 1}, now=NOW)["is_closed"] is None
    with pytest.raises(fm.FetchError):
        fm.parse_yahoo_quote({"symbol": "MSFT", "marketCap": 1}, now=NOW)


def test_local_time_conversion_handles_us_dst():
    ny = fm.MARKETS["us"]["tz"]
    summer = datetime(2025, 7, 1, 20, 0, tzinfo=timezone.utc)   # EDT, UTC-4
    winter = datetime(2025, 12, 1, 21, 0, tzinfo=timezone.utc)  # EST, UTC-5
    assert fm.fmt_local(summer, ny) == "2025-07-01 16:00:00"
    assert fm.fmt_local(winter, ny) == "2025-12-01 16:00:00"
    assert fm.fmt_riyadh(summer) == "2025-07-01 23:00:00" and fm.fmt_riyadh(winter) == "2025-12-02 00:00:00"
    us = fm.as_of_fields(summer, "us")
    assert us == {"as_of_utc": "2025-07-01T20:00:00Z", "as_of_local": "2025-07-01 16:00:00", "tz": "America/New_York"}
    sa = fm.as_of_fields(summer, "sa")
    assert sa == {"as_of_utc": "2025-07-01T20:00:00Z", "as_of_local": "2025-07-01 23:00:00", "tz": "Asia/Riyadh",
                  "as_of_riyadh": "2025-07-01 23:00:00"}
    assert fm.as_of_fields(None, "us") == {"as_of_utc": None, "as_of_local": None, "tz": "America/New_York"}


def test_us_eastern_fallback_matches_zoneinfo():
    """The built-in rule (used only when tzdata is missing) must agree with the real zone."""
    zoneinfo = pytest.importorskip("zoneinfo")
    real, fb = zoneinfo.ZoneInfo("America/New_York"), fm._USEastern()
    probes = [datetime(2025, 3, 9, 6, 59, tzinfo=timezone.utc), datetime(2025, 3, 9, 7, 0, tzinfo=timezone.utc),
              datetime(2025, 11, 2, 5, 59, tzinfo=timezone.utc), datetime(2025, 11, 2, 6, 0, tzinfo=timezone.utc),
              datetime(2026, 1, 15, 12, tzinfo=timezone.utc), datetime(2026, 8, 15, 12, tzinfo=timezone.utc)]
    for dt in probes:
        assert fm.fmt_local(dt, fb) == fm.fmt_local(dt, real), dt


def test_parse_saudiexchange_html():
    found = fm.parse_saudiexchange_indices_html(SAUDIEXCHANGE_HTML)
    assert set(found) == {"TASI", "MT30", "NomuC"}
    assert found["TASI"] == {"value": 11432.17, "change_pts": -58.44, "change_pct": -0.51}
    assert found["MT30"] == {"value": 1480.22, "change_pts": 3.10, "change_pct": 0.21}
    assert found["NomuC"] == {"value": 26801.90, "change_pts": -120.30, "change_pct": -0.45}


def test_parse_saudiexchange_html_no_rows():
    with pytest.raises(fm.FetchError):
        fm.parse_saudiexchange_indices_html("<html><table><tr><td>nothing</td></tr></table></html>")


def test_parse_saudiexchange_json():
    data = {"data": [{"indexName": "TASI", "lastValue": "11,432.17", "change": "-58.44", "changePercent": "-0.51"},
                     {"indexName": "Something else", "lastValue": "1"}]}
    found = fm.parse_saudiexchange_indices_json(data)
    assert found == {"TASI": {"value": 11432.17, "change_pts": -58.44, "change_pct": -0.51}}


def test_parse_stooq_csv():
    p = fm.parse_stooq_csv(STOOQ_CSV)
    assert p["value"] == 11480.5 and p["session_date"] == "2025-06-01"
    assert p["change_pts"] == pytest.approx(60.5)


def test_to_float_variants():
    assert fm.to_float("11,432.17") == 11432.17
    assert fm.to_float("(1.5)") == -1.5
    assert fm.to_float("-0.51 %") == -0.51
    assert fm.to_float("١٢٫٥") == 12.5
    assert fm.to_float("n/a") is None and fm.to_float("") is None and fm.to_float(None) is None


# --------------------------------------------------------------------------- #
# HTTP layer: retries / fallback hosts
# --------------------------------------------------------------------------- #

def test_http_get_retries_on_429(monkeypatch):
    s = use_session(monkeypatch, {"example": [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, text="ok")]})
    assert fm.http_get("https://example.test/x").text == "ok"
    assert len(s.calls) == 2


def test_http_get_gives_up(monkeypatch):
    use_session(monkeypatch, {"example": [FakeResponse(503), FakeResponse(503), FakeResponse(503)]})
    with pytest.raises(fm.FetchError):
        fm.http_get("https://example.test/x", retries=3)


def test_yahoo_chart_falls_back_to_query2(monkeypatch):
    import requests
    s = use_session(monkeypatch, {
        "query1": requests.ConnectionError("boom"),
        "query2": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5)),
    })
    data, url = fm.fetch_yahoo_chart("2222.SR")
    assert "query2" in url and data["chart"]["result"][0]["meta"]["symbol"] == "2222.SR"
    assert any("query1" in c[0] for c in s.calls)


def test_yahoo_chart_error_payload(monkeypatch):
    use_session(monkeypatch, {"query": FakeResponse(body={"chart": {"result": None, "error": {"code": "Not Found", "description": "No data"}}})})
    with pytest.raises(fm.FetchError, match="Not Found"):
        fm.fetch_yahoo_chart("XXXX.SR")


# --------------------------------------------------------------------------- #
# Orchestration: fallback order + error recording
# --------------------------------------------------------------------------- #

def test_indices_yahoo_then_saudiexchange_for_the_rest(monkeypatch):
    use_session(monkeypatch, {
        "chart/%5ETASI.SR": FakeResponse(body=yahoo_fixture()),
        "indices-performance": FakeResponse(text=SAUDIEXCHANGE_HTML),
    })
    errors = []
    idx = fm.fetch_indices(errors, NOW)
    by = {i["code"]: i for i in idx}
    assert [i["code"] for i in idx] == ["TASI", "MT30", "NomuC"]
    assert by["TASI"]["source"] == "yahoo" and by["TASI"]["value"] == 11500.25
    assert by["TASI"]["as_of_utc"] == "2025-06-02T11:58:40Z" and by["TASI"]["as_of_riyadh"] == "2025-06-02 14:58:40"
    assert by["MT30"]["source"] == "saudiexchange" and by["MT30"]["value"] == 1480.22
    assert by["NomuC"]["change_pct"] == -0.45 and by["NomuC"]["is_closed"] is None
    assert errors == []


def test_indices_saudiexchange_when_yahoo_down(monkeypatch):
    use_session(monkeypatch, {
        "yahoo.com": FakeResponse(500),
        "indices-performance": FakeResponse(text=SAUDIEXCHANGE_HTML),
    })
    errors = []
    idx = fm.fetch_indices(errors, NOW)
    assert {i["code"]: i["source"] for i in idx} == {"TASI": "saudiexchange", "MT30": "saudiexchange", "NomuC": "saudiexchange"}
    assert [e["what"] for e in errors] == ["index:TASI:yahoo"]


def test_indices_stooq_last_resort(monkeypatch):
    use_session(monkeypatch, {"stooq.com/q/d/l/?s=^tasi": FakeResponse(text=STOOQ_CSV)})
    errors = []
    idx = fm.fetch_indices(errors, NOW)
    assert len(idx) == 1 and idx[0]["code"] == "TASI" and idx[0]["source"] == "stooq"
    assert idx[0]["value"] == 11480.5 and idx[0]["session_date"] == "2025-06-01"
    assert {e["what"] for e in errors} == {"index:TASI:yahoo", "index:saudiexchange"}


def test_indices_all_sources_fail(monkeypatch):
    errors = []
    assert fm.fetch_indices(errors, NOW) == []
    assert {e["what"] for e in errors} == {"index:TASI:yahoo", "index:saudiexchange", "index:TASI:stooq"}
    assert all(e["detail"] for e in errors)


def test_stocks_partial_failure_and_market_cap(monkeypatch):
    use_session(monkeypatch, {
        "chart/2222.SR": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5, closes=(27.0, 27.2, 27.1, 27.0, 27.5))),
        "chart/1120.SR": FakeResponse(429),
        "getcrumb": FakeResponse(text="abc123"),
        "v7/finance/quote": FakeResponse(body={"quoteResponse": {"result": [
            {"symbol": "2222.SR", "marketCap": 6.6e12}], "error": None}}),
    })
    cons = [
        {"code": "2222", "yahoo": "2222.SR", "name_ar": "أرامكو السعودية", "name_en": "Saudi Aramco", "sector_ar": "الطاقة"},
        {"code": "1120", "yahoo": "1120.SR", "name_ar": "مصرف الراجحي", "name_en": "Al Rajhi Bank", "sector_ar": "البنوك"},
    ]
    errors = []
    stocks = fm.fetch_stocks(cons, errors, NOW, pause=0.1)
    assert len(stocks) == 2
    a, r = stocks
    assert a["price"] == 27.5 and a["prev_close"] == 27.0 and a["change_pct"] == pytest.approx(1.8519, abs=1e-3)
    assert a["market_cap_sar"] == 6.6e12 and a["source"] == "yahoo" and a["as_of_utc"].endswith("Z")
    # failed stock keeps its identity but every market field is null, never fabricated
    assert r["name_ar"] == "مصرف الراجحي"
    assert all(r[k] is None for k in ("price", "prev_close", "change_pct", "volume", "market_cap_sar", "as_of_utc", "source", "source_url"))
    assert [e["what"] for e in errors] == ["stock:1120:yahoo"] and "429" in errors[0]["detail"]


def test_stocks_prefer_batched_quote_then_chart_fallback_mixed_markets(monkeypatch):
    s = use_session(monkeypatch, {
        "getcrumb": FakeResponse(text="abc123"),
        "v7/finance/quote": FakeResponse(body={"quoteResponse": {"result": [
            quote_obj("AAPL", 201.5, 200.0, 3.0e12),
            quote_obj("2222.SR", 27.5, 27.0, 6.6e12, currency="SAR", state="CLOSED"),
            {"symbol": "MSFT", "marketCap": 3.4e12},            # stub: no price -> chart, keep mcap
        ], "error": None}}),
        "chart/MSFT": FakeResponse(body=us_fixture("MSFT", price=460.0, closes=(450.0, 452.0, 455.0, 458.0, 460.0))),
        "chart/BRK-B": FakeResponse(429),
    })
    cons = fm.load_constituents(fm.DEFAULT_CONSTITUENTS, "sa")[:1] + [
        c for c in fm.load_constituents(fm.DEFAULT_CONSTITUENTS_US, "us") if c["code"] in ("AAPL", "MSFT", "BRK-B")]
    assert sorted(c["code"] for c in cons) == ["2222", "AAPL", "BRK-B", "MSFT"]
    errors = []
    stocks = fm.fetch_stocks(cons, errors, NOW, pause=0.25)
    by = {st["code"]: st for st in stocks}

    aramco = by["2222"]
    assert aramco["market"] == "sa" and aramco["currency"] == "SAR" and aramco["source"] == "yahoo_quote"
    assert aramco["price"] == 27.5 and aramco["prev_close"] == 27.0 and aramco["is_closed"] is True
    assert aramco["market_cap"] == aramco["market_cap_sar"] == 6.6e12 and "market_cap_usd" not in aramco
    assert aramco["as_of_riyadh"] == "2025-06-02 14:58:40" and aramco["as_of_local"] == aramco["as_of_riyadh"]

    aapl = by["AAPL"]
    assert aapl["market"] == "us" and aapl["currency"] == "USD" and aapl["source"] == "yahoo_quote"
    assert aapl["price"] == 201.5 and aapl["change_pct"] == pytest.approx(0.75) and aapl["is_closed"] is False
    assert aapl["market_cap"] == aapl["market_cap_usd"] == 3.0e12 and "market_cap_sar" not in aapl
    assert aapl["as_of_utc"] == "2025-06-02T11:58:40Z" and aapl["as_of_local"] == "2025-06-02 07:58:40"
    assert aapl["tz"] == "America/New_York" and "as_of_riyadh" not in aapl

    msft = by["MSFT"]  # priced via chart, market cap from the stub quote
    assert msft["source"] == "yahoo" and "chart/MSFT" in msft["source_url"]
    assert msft["price"] == 460.0 and msft["prev_close"] == 458.0 and msft["market_cap_usd"] == 3.4e12

    brk = by["BRK-B"]
    assert brk["yahoo"] == "BRK-B" and brk["name_ar"] == "بيركشاير هاثاواي"
    assert all(brk[k] is None for k in ("price", "prev_close", "change_pct", "volume", "market_cap", "market_cap_usd",
                                        "as_of_utc", "as_of_local", "is_closed", "source", "source_url"))
    assert [e["what"] for e in errors] == ["stock:BRK-B:yahoo"]

    # only the two unpriced symbols hit the chart endpoint; one batched quote call for all four
    chart_calls = [c for c in s.calls if "/v8/finance/chart/" in c[0]]
    assert {c[0].rsplit("/", 1)[1] for c in chart_calls} == {"MSFT", "BRK-B"}
    quote_calls = [c for c in s.calls if "v7/finance/quote" in c[0]]
    assert len(quote_calls) == 1 and set(quote_calls[0][1]["symbols"].split(",")) == {"2222.SR", "AAPL", "MSFT", "BRK-B"}


def test_stocks_quote_endpoint_failure_is_soft(monkeypatch):
    use_session(monkeypatch, {"chart/2222.SR": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5))})
    errors = []
    stocks = fm.fetch_stocks([{"code": "2222", "yahoo": "2222.SR"}], errors, NOW, pause=0)
    assert stocks[0]["price"] == 27.5 and stocks[0]["market_cap_sar"] is None
    assert [e["what"] for e in errors] == ["stocks:yahoo_quote"]


# --------------------------------------------------------------------------- #
# End to end: CLI, schema, exit codes
# --------------------------------------------------------------------------- #

COMMON_INDEX_KEYS = {"code", "market", "name_ar", "name_en", "yahoo", "currency", "value", "change_pts", "change_pct",
                     "as_of_utc", "as_of_local", "tz", "session_date", "is_closed", "source", "source_url"}
COMMON_STOCK_KEYS = {"code", "yahoo", "market", "name_ar", "name_en", "sector_ar", "currency", "price", "prev_close",
                     "change_pct", "volume", "market_cap", "as_of_utc", "as_of_local", "tz", "is_closed",
                     "source", "source_url"}
SA_INDEX_KEYS = COMMON_INDEX_KEYS | {"as_of_riyadh"}
US_INDEX_KEYS = COMMON_INDEX_KEYS
SA_STOCK_KEYS = COMMON_STOCK_KEYS | {"as_of_riyadh", "market_cap_sar"}
US_STOCK_KEYS = COMMON_STOCK_KEYS | {"market_cap_usd"}
PAYLOAD_KEYS = {"generated_at_utc", "generated_at_riyadh", "generated_at_new_york", "markets", "indices", "stocks", "errors"}


def test_main_writes_schema_and_exits_0(monkeypatch, tmp_path):
    use_session(monkeypatch, {
        "chart/%5ETASI.SR": FakeResponse(body=yahoo_fixture()),
        "chart/2222.SR": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5)),
        "indices-performance": FakeResponse(text=SAUDIEXCHANGE_HTML),
    })
    out = tmp_path / "data" / "market.json"
    rc = fm.main(["--out", str(out), "--constituents", fm.DEFAULT_CONSTITUENTS, "--markets", "sa",
                  "--no-quote", "--sleep", "0"])
    assert rc == 0 and out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == PAYLOAD_KEYS
    assert data["generated_at_utc"] == "2025-06-02T12:00:00Z"
    assert data["generated_at_riyadh"] == "2025-06-02 15:00:00"
    assert data["generated_at_new_york"] == "2025-06-02 08:00:00"  # EDT
    assert data["markets"] == ["sa"]
    assert [i["code"] for i in data["indices"]] == ["TASI", "MT30", "NomuC"]
    for i in data["indices"]:
        assert set(i) == SA_INDEX_KEYS and i["market"] == "sa" and i["currency"] == "SAR" and i["tz"] == "Asia/Riyadh"
        assert i["as_of_riyadh"] == i["as_of_local"]
    assert len(data["stocks"]) >= 40
    for s in data["stocks"]:
        assert set(s) == SA_STOCK_KEYS and s["yahoo"] == f"{s['code']}.SR"
        assert s["market"] == "sa" and s["currency"] == "SAR" and s["tz"] == "Asia/Riyadh"
    aramco = next(s for s in data["stocks"] if s["code"] == "2222")
    assert aramco["price"] == 27.5 and aramco["name_ar"] == "أرامكو السعودية" and aramco["sector_ar"] == "الطاقة"
    assert aramco["as_of_utc"] == "2025-06-02T11:58:40Z" and aramco["as_of_riyadh"] == "2025-06-02 14:58:40"
    assert aramco["is_closed"] is False
    # every other stock 404'd in the fake -> null fields + one error each
    assert sum(1 for s in data["stocks"] if s["price"] is None) == len(data["stocks"]) - 1
    assert all({"what", "detail"} == set(e) for e in data["errors"])
    assert len(data["errors"]) == len(data["stocks"]) - 1


def test_main_both_markets_default(monkeypatch, tmp_path):
    use_session(monkeypatch, {
        "chart/%5ETASI.SR": FakeResponse(body=yahoo_fixture()),
        "chart/%5EGSPC": FakeResponse(body=us_fixture("^GSPC", price=6000.5)),
        "chart/%5EDJI": FakeResponse(body=us_fixture("^DJI", price=42000.0)),
        "chart/2222.SR": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5)),
        "chart/AAPL": FakeResponse(body=us_fixture("AAPL", price=201.5, closes=(198.0, 199.0, 200.0, 200.0, 201.5))),
        "indices-performance": FakeResponse(text=SAUDIEXCHANGE_HTML),
    })
    out = tmp_path / "market.json"
    rc = fm.main(["--out", str(out), "--no-quote", "--sleep", "0"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["markets"] == ["sa", "us"]
    # SA indices first, then US ones; ^IXIC 404'd so it is absent (never fabricated)
    assert [i["code"] for i in data["indices"]] == ["TASI", "MT30", "NomuC", "^GSPC", "^DJI"]
    assert {"index:^IXIC:yahoo"} <= {e["what"] for e in data["errors"]}
    spx = next(i for i in data["indices"] if i["code"] == "^GSPC")
    assert set(spx) == US_INDEX_KEYS and spx["market"] == "us" and spx["currency"] == "USD"
    assert spx["name_ar"] == "ستاندرد آند بورز 500" and spx["value"] == 6000.5
    assert spx["tz"] == "America/New_York" and spx["as_of_local"] == "2025-06-02 07:58:40"
    assert "as_of_riyadh" not in spx and "market_cap_sar" not in spx
    stocks_by_mkt = {m: [s for s in data["stocks"] if s["market"] == m] for m in ("sa", "us")}
    assert len(stocks_by_mkt["sa"]) >= 40 and len(stocks_by_mkt["us"]) >= 40
    for s in stocks_by_mkt["us"]:
        assert set(s) == US_STOCK_KEYS and s["currency"] == "USD" and s["yahoo"] == s["code"] and s["tz"] == "America/New_York"
    aapl = next(s for s in stocks_by_mkt["us"] if s["code"] == "AAPL")
    assert aapl["price"] == 201.5 and aapl["prev_close"] == 200.0 and aapl["name_ar"] == "أبل" and aapl["sector_ar"] == "التقنية"
    assert aapl["as_of_utc"] == "2025-06-02T11:58:40Z" and aapl["as_of_local"] == "2025-06-02 07:58:40"
    assert aapl["market_cap_usd"] is None and aapl["market_cap"] is None  # --no-quote


def test_main_markets_us_only(monkeypatch, tmp_path):
    s = use_session(monkeypatch, {"chart/%5EGSPC": FakeResponse(body=us_fixture("^GSPC", price=6000.5))})
    out = tmp_path / "market.json"
    rc = fm.main(["--out", str(out), "--markets", "us", "--no-quote", "--sleep", "0",
                  "--constituents-us", fm.DEFAULT_CONSTITUENTS_US])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["markets"] == ["us"]
    assert [i["code"] for i in data["indices"]] == ["^GSPC"]
    assert all(st["market"] == "us" for st in data["stocks"]) and len(data["stocks"]) >= 40
    # no Saudi source was touched at all
    assert not any("TASI" in c[0] or "saudiexchange" in c[0] or "stooq" in c[0] for c in s.calls)


def test_markets_flag_rejects_unknown():
    with pytest.raises(SystemExit):
        fm.parse_args(["--out", "x.json", "--markets", "sa,jp"])
    assert fm.parse_markets("us, sa,us") == ["us", "sa"]


def test_main_us_constituents_missing_is_soft(monkeypatch, tmp_path):
    use_session(monkeypatch, {
        "chart/%5ETASI.SR": FakeResponse(body=yahoo_fixture()),
        "chart/2222.SR": FakeResponse(body=yahoo_fixture("2222.SR", price=27.5)),
    })
    out = tmp_path / "market.json"
    rc = fm.main(["--out", str(out), "--constituents-us", str(tmp_path / "nope.json"), "--no-quote", "--no-stooq", "--sleep", "0"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "constituents:us" in {e["what"] for e in data["errors"]}
    assert all(st["market"] == "sa" for st in data["stocks"]) and len(data["stocks"]) >= 40


def test_main_exits_1_and_keeps_previous_file_when_nothing_fetched(tmp_path):
    out = tmp_path / "market.json"
    out.write_text('{"previous": true}', encoding="utf-8")
    rc = fm.main(["--out", str(out), "--constituents", fm.DEFAULT_CONSTITUENTS, "--sleep", "0"])
    assert rc == 1
    assert json.loads(out.read_text()) == {"previous": True}


def test_main_missing_constituents_still_writes_indices(monkeypatch, tmp_path):
    use_session(monkeypatch, {"chart/%5ETASI.SR": FakeResponse(body=yahoo_fixture())})
    out = tmp_path / "market.json"
    rc = fm.main(["--out", str(out), "--constituents", str(tmp_path / "nope.json"), "--no-stooq", "--markets", "sa"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["stocks"] == [] and data["indices"][0]["code"] == "TASI"
    assert {e["what"] for e in data["errors"]} == {"constituents:sa", "index:saudiexchange"}


def test_constituents_file_is_sane():
    cons = fm.load_constituents(fm.DEFAULT_CONSTITUENTS)
    codes = [c["code"] for c in cons]
    assert len(cons) >= 40 and len(set(codes)) == len(codes)
    for must in ("2222", "1120", "1180", "7010", "2010", "2082", "1211", "4013", "7203"):
        assert must in codes
    for c in cons:
        assert c["code"].isdigit() and c["yahoo"] == f"{c['code']}.SR"
        assert c["name_ar"] and c["name_en"] and c["sector_ar"]
        assert isinstance(c["approx_mcap_bn_sar"], (int, float)) and c["approx_mcap_bn_sar"] > 0
        assert c["market"] == "sa"


US_SECTORS = {"التقنية", "الرعاية الصحية", "المالية", "الطاقة", "السلع الاستهلاكية", "السلع الأساسية", "الصناعة",
              "الاتصالات", "المواد", "المرافق", "العقار"}


def test_us_constituents_file_is_sane():
    cons = fm.load_constituents(fm.DEFAULT_CONSTITUENTS_US, "us")
    codes = [c["code"] for c in cons]
    assert len(cons) >= 45 and len(set(codes)) == len(codes)
    for must in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "BRK-B", "TSLA", "AVGO", "LLY", "JPM", "XOM"):
        assert must in codes
    for c in cons:
        assert c["market"] == "us" and c["yahoo"] == c["code"] and c["code"] == c["code"].upper()
        assert c["name_ar"] and c["name_en"] and c["sector_ar"] in US_SECTORS
        assert isinstance(c["approx_mcap_bn_usd"], (int, float)) and c["approx_mcap_bn_usd"] > 0
    assert next(c for c in cons if c["code"] == "AAPL")["name_ar"] == "أبل"
    assert next(c for c in cons if c["code"] == "NVDA")["name_ar"] == "إنفيديا"
