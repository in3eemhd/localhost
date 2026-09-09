#!/usr/bin/env python3
"""Fetch Saudi market data (indices + large-cap stocks) into a single JSON file.

Usage:
    python scripts/fetch_market.py --out data/market.json

Sources, in fallback order:
  1. Yahoo Finance chart API  (query1 then query2)      -> indices + stocks
     + Yahoo quote endpoint (best effort, needs a crumb) -> marketCap only
  2. Saudi Exchange (saudiexchange.sa) HTML/JSON         -> TASI / MT30 / NomuC
  3. Stooq CSV (optional last resort)                    -> TASI only

Nothing is ever fabricated: a value that could not be fetched is null and the
failure is appended to the "errors" list.  Exit code 0 if at least one index or
stock succeeded (and the file is written), 1 otherwise (the file is left as-is
so a previous good snapshot is never clobbered by an empty one).

Designed to run in GitHub Actions; only dependency is `requests`.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional

import requests

log = logging.getLogger("fetch_market")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

RIYADH_TZ = timezone(timedelta(hours=3), name="Asia/Riyadh")  # KSA has no DST
TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.8,en;q=0.7",
}

YAHOO_CHART_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"

SAUDIEXCHANGE_INDICES_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/"
    "main-market-watch/indices-performance?locale=ar"
)
SAUDIEXCHANGE_MARKET_WATCH_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/"
    "main-market-watch/?locale=ar"
)
# Candidate JSON endpoints (the portal exposes a few unstable ajax URLs; every
# one is optional and any failure is just logged).
SAUDIEXCHANGE_JSON_CANDIDATES = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/"
    "main-market-watch/indices-performance/!ut/p/z1/?locale=ar&format=json",
)

STOOQ_CANDIDATES = (
    "https://stooq.com/q/d/l/?s=^tasi&i=d",
    "https://stooq.com/q/d/l/?s=tasi.sa&i=d",
)

INDEX_DEFS = {
    "TASI": {
        "name_ar": "المؤشر العام تاسي",
        "yahoo": "^TASI.SR",
        "keywords": ("تاسي", "tasi", "المؤشر العام", "all share"),
    },
    "MT30": {
        "name_ar": "مؤشر إم تي 30",
        "yahoo": None,  # not reliably on Yahoo
        "keywords": ("mt30", "إم تي 30", "ام تي 30", "mt 30"),
    },
    "NomuC": {
        "name_ar": "مؤشر نمو الموازية",
        "yahoo": None,
        "keywords": ("nomu", "نمو"),
    },
}

DEFAULT_CONSTITUENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "constituents.json")

# Indirections so tests can monkeypatch without touching the network/clock.
SESSION: requests.Session = requests.Session()
sleep: Callable[[float], None] = time.sleep
now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731


class FetchError(Exception):
    """Raised when a source could not be fetched or parsed."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fmt_riyadh(dt: datetime) -> str:
    return dt.astimezone(RIYADH_TZ).strftime("%Y-%m-%d %H:%M:%S")


def epoch_to_dt(epoch: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def to_float(value: Any) -> Optional[float]:
    """Parse '12,345.67', '(1.2)', '1.2%', Arabic digits ... -> float or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    # Arabic-Indic digits -> ASCII
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))
    s = s.replace("‏", "").replace("‎", "").replace("%", "").replace("٪", "")
    s = s.replace(",", "").replace(" ", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace("−", "-").replace("–", "-")
    if s.endswith("-"):  # "1.23-" style
        neg, s = True, s[:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def pct_change(price: Optional[float], prev: Optional[float]) -> Optional[float]:
    if price is None or prev in (None, 0):
        return None
    return round((price - prev) / prev * 100.0, 4)


def round_or_none(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

def http_get(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = TIMEOUT,
    retries: int = 3,
    backoff: float = 2.0,
) -> requests.Response:
    """GET with a browser UA, retrying on 429 / 5xx / connection errors."""
    hdrs = dict(BASE_HEADERS)
    if headers:
        hdrs.update(headers)
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, params=params, headers=hdrs, timeout=timeout)
        except requests.RequestException as exc:  # includes timeouts
            last_err = exc
            log.warning("GET %s attempt %d/%d failed: %s", url, attempt, retries, exc)
        else:
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = to_float(resp.headers.get("Retry-After")) if resp.headers else None
                wait = retry_after if retry_after else backoff * attempt
                last_err = FetchError(f"HTTP {resp.status_code}")
                log.warning("GET %s -> %s (attempt %d/%d), sleeping %.1fs",
                            url, resp.status_code, attempt, retries, wait)
                if attempt < retries:
                    sleep(wait)
                continue
            if resp.status_code != 200:
                raise FetchError(f"HTTP {resp.status_code} for {url}")
            return resp
        if attempt < retries:
            sleep(backoff * attempt)
    raise FetchError(str(last_err))


# --------------------------------------------------------------------------- #
# Source 1: Yahoo Finance
# --------------------------------------------------------------------------- #

def yahoo_chart_urls(symbol: str) -> list[str]:
    return [f"https://{h}/v8/finance/chart/{requests.utils.quote(symbol, safe='.')}" for h in YAHOO_CHART_HOSTS]


def fetch_yahoo_chart(symbol: str) -> tuple[dict, str]:
    """Return (raw_json, url) for the first Yahoo host that answers."""
    params = {"range": "5d", "interval": "1d", "includePrePost": "false"}
    errors = []
    for url in yahoo_chart_urls(symbol):
        try:
            resp = http_get(url, params=params, headers={"Accept": "application/json"})
            data = resp.json()
        except (FetchError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        err = (data.get("chart") or {}).get("error")
        if err:
            errors.append(f"{url}: {err.get('code')}: {err.get('description')}")
            continue
        return data, url
    raise FetchError("; ".join(errors) or "no Yahoo host answered")


def parse_yahoo_chart(data: dict, now: Optional[datetime] = None) -> dict:
    """Normalise a Yahoo v8 chart response.  Pure function; raises FetchError."""
    now = now or now_utc()
    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
    except (KeyError, IndexError, TypeError) as exc:
        raise FetchError(f"unexpected Yahoo chart shape: {exc!r}")

    price = to_float(meta.get("regularMarketPrice"))
    if price is None:
        raise FetchError("Yahoo chart has no regularMarketPrice")

    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    series = [(ts, c, v) for ts, c, v in zip(timestamps, closes, volumes + [None] * (len(closes) - len(volumes)))
              if c is not None]

    market_time = epoch_to_dt(meta.get("regularMarketTime"))
    if market_time is None and series:
        market_time = epoch_to_dt(series[-1][0])
    if market_time is None:
        market_time = now

    # Previous close: meta.previousClose (newer responses) > second-to-last daily
    # close when the last bar is today's > chartPreviousClose (close before the
    # 5d window; only right when the window has a single bar).
    prev = to_float(meta.get("previousClose"))
    if prev is None and len(series) >= 2:
        last_bar_day = epoch_to_dt(series[-1][0])
        if last_bar_day and last_bar_day.astimezone(RIYADH_TZ).date() == market_time.astimezone(RIYADH_TZ).date():
            prev = to_float(series[-2][1])
        else:
            prev = to_float(series[-1][1])
    if prev is None:
        prev = to_float(meta.get("chartPreviousClose"))

    volume = to_float(meta.get("regularMarketVolume"))
    if volume is None and series and series[-1][2] is not None:
        volume = to_float(series[-1][2])

    is_closed = True
    regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    start, end = epoch_to_dt(regular.get("start")), epoch_to_dt(regular.get("end"))
    if start and end:
        is_closed = not (start <= now <= end) or market_time < start

    return {
        "price": price,
        "prev_close": prev,
        "change_pts": round_or_none(price - prev if prev is not None else None),
        "change_pct": pct_change(price, prev),
        "volume": int(volume) if volume is not None else None,
        "as_of": market_time,
        "session_date": market_time.astimezone(RIYADH_TZ).strftime("%Y-%m-%d"),
        "is_closed": is_closed,
        "currency": meta.get("currency"),
        "symbol": meta.get("symbol"),
    }


def yahoo_get_crumb() -> Optional[str]:
    """Yahoo's quote endpoint needs a cookie + crumb.  Best effort only."""
    try:
        http_get(YAHOO_COOKIE_URL, retries=1)  # sets the A1/A3 cookie (may 404; fine)
    except FetchError as exc:
        log.info("Yahoo cookie bootstrap: %s (continuing)", exc)
    try:
        resp = http_get(YAHOO_CRUMB_URL, retries=1, headers={"Accept": "text/plain"})
        crumb = resp.text.strip()
        if crumb and "<" not in crumb and len(crumb) < 64:
            return crumb
        log.info("Yahoo crumb response did not look like a crumb: %r", crumb[:40])
    except FetchError as exc:
        log.info("Yahoo crumb fetch failed: %s", exc)
    return None


def fetch_yahoo_quotes(symbols: Iterable[str]) -> dict[str, dict]:
    """Return {symbol: quote-dict} from the v7 quote endpoint.  Never raises."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    crumb = yahoo_get_crumb()
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        params = {"symbols": ",".join(batch), "fields": "regularMarketPrice,marketCap,regularMarketVolume"}
        if crumb:
            params["crumb"] = crumb
        try:
            data = http_get(YAHOO_QUOTE_URL, params=params, retries=2,
                            headers={"Accept": "application/json"}).json()
            for q in ((data.get("quoteResponse") or {}).get("result") or []):
                if q.get("symbol"):
                    out[q["symbol"]] = q
        except (FetchError, ValueError) as exc:
            log.warning("Yahoo quote endpoint failed for batch %d: %s", i // 50, exc)
    log.info("Yahoo quote endpoint returned %d/%d symbols", len(out), len(symbols))
    return out


# --------------------------------------------------------------------------- #
# Source 2: Saudi Exchange
# --------------------------------------------------------------------------- #

class _TableParser(HTMLParser):
    """Collect every <table> as a list of rows, each a list of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            self.tables.append([])
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.tables:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _match_index_code(text: str) -> Optional[str]:
    t = text.lower()
    # Order matters: "نمو" must not steal TASI rows and MT30 must beat "tasi".
    for code in ("MT30", "NomuC", "TASI"):
        if any(k in t for k in INDEX_DEFS[code]["keywords"]):
            return code
    return None


def _numbers_from_cells(cells: list[str]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Heuristically pick (value, change_pts, change_pct) from a table row."""
    value = change = pct = None
    for c in cells:
        f = to_float(c)
        if f is None:
            continue
        if ("%" in c or "٪" in c) and pct is None:
            pct = f
        elif value is None and abs(f) >= 100:
            value = f
        elif change is None and (abs(f) < 100 or value is not None):
            change = f
    if value is not None and change is not None and pct is None:
        pct = pct_change(value, value - change)
    return value, change, pct


def parse_saudiexchange_indices_html(html: str) -> dict[str, dict]:
    """Extract TASI/MT30/NomuC rows from an indices table.  Returns {code: {...}}."""
    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception as exc:  # html.parser is lenient, but never let it kill us
        raise FetchError(f"html parse error: {exc}")
    found: dict[str, dict] = {}
    for table in parser.tables:
        for row in table:
            if not row:
                continue
            code = _match_index_code(" ".join(row[:2]))
            if code is None or code in found:
                continue
            value, change, pct = _numbers_from_cells(row)
            if value is None:
                continue
            found[code] = {"value": value, "change_pts": change, "change_pct": pct}
    if not found:
        raise FetchError("no index rows found in Saudi Exchange HTML")
    return found


def _walk_json(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v)


def parse_saudiexchange_indices_json(data: Any) -> dict[str, dict]:
    """Very tolerant: any dict with an index-ish name and a numeric value/last."""
    found: dict[str, dict] = {}
    name_keys = ("indexName", "name", "indexNameAr", "indexNameEn", "index")
    value_keys = ("lastValue", "last", "value", "indexValue", "previousClose")
    change_keys = ("change", "changeValue", "netChange")
    pct_keys = ("changePercent", "percentChange", "changePct", "percent")
    for d in _walk_json(data):
        name = next((str(d[k]) for k in name_keys if isinstance(d.get(k), str)), None)
        if not name:
            continue
        code = _match_index_code(name)
        if code is None or code in found:
            continue
        value = next((to_float(d[k]) for k in value_keys if d.get(k) is not None), None)
        if value is None:
            continue
        found[code] = {
            "value": value,
            "change_pts": next((to_float(d[k]) for k in change_keys if d.get(k) is not None), None),
            "change_pct": next((to_float(d[k]) for k in pct_keys if d.get(k) is not None), None),
        }
    if not found:
        raise FetchError("no index objects found in Saudi Exchange JSON")
    return found


def fetch_saudiexchange_indices() -> tuple[dict[str, dict], str]:
    """Try JSON candidates then the HTML indices page.  Returns ({code: {...}}, url)."""
    errors = []
    for url in SAUDIEXCHANGE_JSON_CANDIDATES:
        try:
            resp = http_get(url, retries=1, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
            return parse_saudiexchange_indices_json(resp.json()), url
        except (FetchError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    for url in (SAUDIEXCHANGE_INDICES_URL, SAUDIEXCHANGE_MARKET_WATCH_URL):
        try:
            resp = http_get(url, retries=2, headers={"Accept": "text/html"})
            return parse_saudiexchange_indices_html(resp.text), url
        except FetchError as exc:
            errors.append(f"{url}: {exc}")
    raise FetchError("; ".join(errors))


# --------------------------------------------------------------------------- #
# Source 3: Stooq (optional)
# --------------------------------------------------------------------------- #

def parse_stooq_csv(text: str) -> dict:
    """Stooq daily CSV: Date,Open,High,Low,Close,Volume.  Uses last two rows."""
    rows = [r for r in csv.DictReader(io.StringIO(text.strip())) if r.get("Close")]
    if len(rows) < 1 or "Date" not in rows[-1]:
        raise FetchError("stooq CSV empty or unexpected")
    last = rows[-1]
    value = to_float(last["Close"])
    prev = to_float(rows[-2]["Close"]) if len(rows) >= 2 else None
    if value is None:
        raise FetchError("stooq CSV has no close")
    try:
        as_of = datetime.strptime(last["Date"], "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    except ValueError:
        raise FetchError(f"stooq bad date {last['Date']!r}")
    return {
        "value": value,
        "change_pts": round_or_none(value - prev if prev is not None else None),
        "change_pct": pct_change(value, prev),
        "as_of": as_of,
        "session_date": last["Date"],
    }


def fetch_stooq_tasi() -> tuple[dict, str]:
    errors = []
    for url in STOOQ_CANDIDATES:
        try:
            resp = http_get(url, retries=1, headers={"Accept": "text/csv,text/plain"})
            if "No data" in resp.text[:200] or "<html" in resp.text[:200].lower():
                raise FetchError("no data")
            return parse_stooq_csv(resp.text), url
        except FetchError as exc:
            errors.append(f"{url}: {exc}")
    raise FetchError("; ".join(errors))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def load_constituents(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data["constituents"] if isinstance(data, dict) else data
    out = []
    for c in items:
        if not c.get("code"):
            continue
        c = dict(c)
        c.setdefault("yahoo", f"{c['code']}.SR")
        out.append(c)
    return out


def _index_record(code: str, **kw) -> dict:
    rec = {
        "code": code,
        "name_ar": INDEX_DEFS[code]["name_ar"],
        "value": None, "change_pts": None, "change_pct": None,
        "as_of_utc": None, "as_of_riyadh": None, "session_date": None,
        "is_closed": None, "source": None, "source_url": None,
    }
    rec.update(kw)
    return rec


def fetch_indices(errors: list[dict], now: datetime, use_stooq: bool = True) -> list[dict]:
    indices: dict[str, dict] = {}

    # 1. Yahoo
    for code, spec in INDEX_DEFS.items():
        if not spec["yahoo"]:
            continue
        try:
            raw, url = fetch_yahoo_chart(spec["yahoo"])
            p = parse_yahoo_chart(raw, now=now)
            indices[code] = _index_record(
                code, value=p["price"], change_pts=p["change_pts"], change_pct=p["change_pct"],
                as_of_utc=iso_utc(p["as_of"]), as_of_riyadh=fmt_riyadh(p["as_of"]),
                session_date=p["session_date"], is_closed=p["is_closed"],
                source="yahoo", source_url=url,
            )
            log.info("index %s via yahoo: %s (%s%%)", code, p["price"], p["change_pct"])
        except FetchError as exc:
            errors.append({"what": f"index:{code}:yahoo", "detail": str(exc)})
            log.warning("index %s via yahoo failed: %s", code, exc)

    # 2. Saudi Exchange (fills whatever Yahoo did not)
    missing = [c for c in INDEX_DEFS if c not in indices]
    if missing:
        try:
            found, url = fetch_saudiexchange_indices()
            for code in missing:
                if code in found:
                    f = found[code]
                    indices[code] = _index_record(
                        code, value=f["value"], change_pts=round_or_none(f.get("change_pts")),
                        change_pct=round_or_none(f.get("change_pct")),
                        as_of_utc=iso_utc(now), as_of_riyadh=fmt_riyadh(now),
                        session_date=now.astimezone(RIYADH_TZ).strftime("%Y-%m-%d"),
                        is_closed=None, source="saudiexchange", source_url=url,
                    )
                    log.info("index %s via saudiexchange: %s", code, f["value"])
            still = [c for c in missing if c not in indices]
            if still:
                errors.append({"what": "index:saudiexchange",
                               "detail": f"page parsed but rows not found for: {', '.join(still)}"})
        except FetchError as exc:
            errors.append({"what": "index:saudiexchange", "detail": str(exc)})
            log.warning("saudiexchange indices failed: %s", exc)

    # 3. Stooq for TASI only
    if use_stooq and "TASI" not in indices:
        try:
            p, url = fetch_stooq_tasi()
            indices["TASI"] = _index_record(
                "TASI", value=p["value"], change_pts=p["change_pts"], change_pct=p["change_pct"],
                as_of_utc=iso_utc(p["as_of"]), as_of_riyadh=fmt_riyadh(p["as_of"]),
                session_date=p["session_date"], is_closed=True, source="stooq", source_url=url,
            )
            log.info("index TASI via stooq: %s", p["value"])
        except FetchError as exc:
            errors.append({"what": "index:TASI:stooq", "detail": str(exc)})
            log.warning("stooq TASI failed: %s", exc)

    # Keep the canonical order; MT30/NomuC only appear when found ("if available").
    return [indices[c] for c in INDEX_DEFS if c in indices]


def fetch_stocks(constituents: list[dict], errors: list[dict], now: datetime,
                 pause: float = 0.4, use_quote: bool = True) -> list[dict]:
    stocks: list[dict] = []
    for i, c in enumerate(constituents):
        rec = {
            "code": str(c["code"]), "yahoo": c["yahoo"],
            "name_ar": c.get("name_ar"), "name_en": c.get("name_en"), "sector_ar": c.get("sector_ar"),
            "price": None, "prev_close": None, "change_pct": None, "volume": None,
            "market_cap_sar": None, "as_of_utc": None, "source": None, "source_url": None,
        }
        try:
            raw, url = fetch_yahoo_chart(c["yahoo"])
            p = parse_yahoo_chart(raw, now=now)
            rec.update(price=p["price"], prev_close=p["prev_close"], change_pct=p["change_pct"],
                       volume=p["volume"], as_of_utc=iso_utc(p["as_of"]), source="yahoo", source_url=url)
        except FetchError as exc:
            errors.append({"what": f"stock:{c['code']}:yahoo", "detail": str(exc)})
            log.warning("stock %s failed: %s", c["code"], exc)
        stocks.append(rec)
        if pause and i < len(constituents) - 1:
            sleep(pause)

    if use_quote and stocks:
        quotes = fetch_yahoo_quotes(s["yahoo"] for s in stocks)
        for s in stocks:
            q = quotes.get(s["yahoo"])
            if q and q.get("marketCap") is not None:
                s["market_cap_sar"] = to_float(q["marketCap"])
        if not quotes:
            errors.append({"what": "stocks:yahoo_quote", "detail": "quote endpoint unavailable; market_cap_sar left null"})
    ok = sum(1 for s in stocks if s["price"] is not None)
    log.info("stocks: %d/%d fetched", ok, len(stocks))
    return stocks


def build_payload(indices: list[dict], stocks: list[dict], errors: list[dict], now: datetime) -> dict:
    return {
        "generated_at_utc": iso_utc(now),
        "generated_at_riyadh": fmt_riyadh(now),
        "indices": indices,
        "stocks": stocks,
        "errors": errors,
    }


def write_json(path: str, payload: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output JSON path, e.g. data/market.json")
    ap.add_argument("--constituents", default=DEFAULT_CONSTITUENTS, help="constituents.json path")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between Yahoo symbols (rate-limit courtesy)")
    ap.add_argument("--no-quote", action="store_true", help="skip Yahoo quote endpoint (market cap)")
    ap.add_argument("--no-stooq", action="store_true", help="skip the Stooq fallback")
    ap.add_argument("--indices-only", action="store_true", help="do not fetch stocks")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    now = now_utc()
    errors: list[dict] = []

    try:
        constituents = [] if args.indices_only else load_constituents(args.constituents)
    except (OSError, ValueError, KeyError) as exc:
        errors.append({"what": "constituents", "detail": f"{args.constituents}: {exc}"})
        log.error("could not load constituents: %s", exc)
        constituents = []

    indices = fetch_indices(errors, now, use_stooq=not args.no_stooq)
    stocks = fetch_stocks(constituents, errors, now, pause=args.sleep, use_quote=not args.no_quote) if constituents else []

    n_idx = len(indices)
    n_stk = sum(1 for s in stocks if s["price"] is not None)
    payload = build_payload(indices, stocks, errors, now)

    if n_idx == 0 and n_stk == 0:
        log.error("nothing fetched (%d errors); NOT overwriting %s", len(errors), args.out)
        for e in errors:
            log.error("  %s: %s", e["what"], e["detail"])
        return 1

    write_json(args.out, payload)
    log.info("wrote %s: %d indices, %d/%d stocks, %d errors", args.out, n_idx, n_stk, len(stocks), len(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
