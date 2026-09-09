"""Offline tests for scripts/fetch_news.py and scripts/analyze_news.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, SCRIPTS)

import analyze_news as an  # noqa: E402
import fetch_news as fn  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
FEEDS = [
    {"name": "أرقام", "url": os.path.join(FIXTURES, "argaam_sample.xml"), "priority": 1},
    {"name": "Arab News", "url": os.path.join(FIXTURES, "atom_sample.xml"), "priority": 2},
]


@pytest.fixture(scope="module")
def raw():
    return fn.run(FEEDS, window_hours=48, max_items=60, now=NOW)


# ---------------------------------------------------------------- fetch: dates
@pytest.mark.parametrize("value,expected_utc,precision", [
    ("Wed, 09 Sep 2026 10:15:42 +0300", "2026-09-09T07:15:42Z", "second"),
    ("Wed, 09 Sep 2026 09:30 +0300", "2026-09-09T06:30:00Z", "minute"),
    ("2026-09-09T07:05:09Z", "2026-09-09T07:05:09Z", "second"),
    ("2026-09-08T22:30:00+00:00", "2026-09-08T22:30:00Z", "second"),
    ("2026-09-09T07:05:09.123456789Z", "2026-09-09T07:05:09Z", "second"),
    ("2026-09-09", "2026-09-09T00:00:00Z", "day"),
    ("09 Sep 2026 10:00 AST", "2026-09-09T07:00:00Z", "minute"),
    ("Wed, 09 Sep 2026 10:15:42 GMT", "2026-09-09T10:15:42Z", "second"),
])
def test_parse_date(value, expected_utc, precision):
    dt, prec = fn.parse_date(value)
    assert dt is not None
    assert fn.fmt_utc(dt) == expected_utc
    assert prec == precision


def test_parse_date_garbage():
    assert fn.parse_date("not a date") == (None, "day")
    assert fn.parse_date(None) == (None, "day")


def test_riyadh_conversion():
    dt, _ = fn.parse_date("2026-09-09T07:05:09Z")
    assert fn.fmt_riyadh(dt) == "2026-09-09 10:05:09"


# ---------------------------------------------------------------- fetch: parse
def test_parse_rss_with_cdata_and_html():
    with open(os.path.join(FIXTURES, "argaam_sample.xml"), "rb") as fh:
        entries = fn.parse_feed(fh.read(), "أرقام")
    assert len(entries) == 6
    first = entries[0]
    assert first["title"].startswith("أرامكو السعودية تعلن")
    assert "<" not in first["summary"] and "alert" not in first["summary"]
    assert "&amp;" not in first["summary"] and "&" in first["summary"]
    assert first["date_raw"] == "Wed, 09 Sep 2026 10:15:42 +0300"
    assert entries[2]["date_raw"] == "2026-09-09"  # dc:date fallback


def test_parse_atom():
    with open(os.path.join(FIXTURES, "atom_sample.xml"), "rb") as fh:
        entries = fn.parse_feed(fh.read(), "Arab News")
    assert len(entries) == 4
    assert entries[0]["link"] == "https://www.arabnews.com/node/3000001/business"
    assert entries[0]["summary"] == "Brent crude climbed above $100 a barrel on Wednesday. Bahri shares fell."
    assert entries[1]["date_raw"] == "2026-09-08T22:30:00+00:00"


def test_google_news_link_decoded():
    url = ("https://news.google.com/rss/articles/CBMiRWh0dHBzOi8vd3d3LnJldXRlcnMuY29tL21hcmtldHMv"
           "c2F1ZGktc3RvY2tzLWNsb3NlLWhpZ2hlci0yMDI2LTA5LTA5L9IBAA?oc=5")
    assert fn.resolve_google_news_link(url) == "https://www.reuters.com/markets/saudi-stocks-close-higher-2026-09-09/"
    # undecodable (new-style) ids are kept as-is
    keep = "https://news.google.com/rss/articles/AU_yqLPabcdef?oc=5"
    assert fn.resolve_google_news_link(keep) == keep


# ---------------------------------------------------------------- fetch: run
def test_run_window_dedupe_sort_and_schema(raw):
    assert raw["feeds_ok"] == 2
    items = raw["items"]
    titles = [i["title"] for i in items]
    # 48h window & missing-date drop
    assert "خبر قديم يجب استبعاده" not in titles
    assert "خبر بلا تاريخ" not in titles
    # dedupe: same title (utm-stripped link) and same canonical link across feeds
    assert titles.count("أرامكو السعودية تعلن ارتفاع أرباحها 12% في الربع الثاني وتوزيعات نقدية") == 1
    assert "Saudi Aramco reports 12% jump in Q2 profit, announces dividend" not in titles
    assert len(items) == 6
    # newest first
    stamps = [i["published_utc"] for i in items]
    assert stamps == sorted(stamps, reverse=True)
    # Saudi-priority copy survives dedupe
    aramco = next(i for i in items if i["title"].startswith("أرامكو السعودية"))
    assert aramco["source"] == "أرقام" and aramco["precision"] == "second"
    assert aramco["published_riyadh"] == "2026-09-09 10:15:42"
    assert aramco["link"].endswith("id/1000001?utm_source=rss")  # link kept verbatim, dedupe uses normalized form
    rajhi = next(i for i in items if i["title"].startswith("الراجحي"))
    assert rajhi["precision"] == "minute"
    houthi = next(i for i in items if i["title"].startswith("هجوم"))
    assert houthi["precision"] == "day" and houthi["published_utc"] == "2026-09-09T00:00:00Z"
    gn = next(i for i in items if i["title"] == "Google News redirect item")
    assert gn["link"].startswith("https://www.reuters.com/")
    required = {"title", "link", "source", "published_utc", "published_riyadh", "precision", "summary",
                "fetched_at_utc", "lang", "priority"}
    for it in items:
        assert required <= set(it)
        assert it["precision"] in ("second", "minute", "day")
        assert len(it["published_utc"]) == 20 and it["published_utc"].endswith("Z")
        assert len(it["published_riyadh"]) == 19
        assert it["fetched_at_utc"] == "2026-09-09T12:00:00Z"


def test_run_cap(raw):
    capped = fn.run(FEEDS, window_hours=48, max_items=2, now=NOW)
    assert len(capped["items"]) == 2


def test_run_all_feeds_broken(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<html>not a feed", encoding="utf-8")
    res = fn.run([{"name": "x", "url": str(bad), "priority": 1}], now=NOW)
    assert res["feeds_ok"] == 0 and res["items"] == []
    assert res["feeds"][0]["error"]


def test_fetch_cli_exit_codes(tmp_path):
    feeds = tmp_path / "feeds.json"
    feeds.write_text(json.dumps(FEEDS, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "raw.json"
    # window huge so the fixed fixture dates are inside it regardless of wall clock
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "fetch_news.py"), "--out", str(out),
                        "--feeds", str(feeds), "--hours", "1000000"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["feeds_ok"] == 2 and data["items"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "x", "url": str(tmp_path / "missing.xml")}]), encoding="utf-8")
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "fetch_news.py"), "--out", str(out),
                        "--feeds", str(bad)], capture_output=True, text=True)
    assert r.returncode == 1


# ---------------------------------------------------------------- analyze: rules
@pytest.fixture(scope="module")
def analyzed(raw):
    rules = an.load_rules()
    consts = an.load_constituents(path=None)  # force built-in list
    return an.analyze(raw, rules, consts, market=None, api_key=None)


def test_rules_file_sectors():
    rules = an.load_rules()
    assert set(rules["sectors"]) == set(an.SECTOR_KEYS)
    for r in rules["rules"]:
        assert r["signal"] in an.SIGNALS
        assert set(r["impact"]) <= set(an.SECTOR_KEYS)
        assert all(-3 <= v <= 3 for v in r["impact"].values())


def test_analyze_schema(analyzed):
    assert analyzed["analysis_mode"] == "rules"
    assert set(analyzed["sectors"]) == set(an.SECTOR_KEYS)
    assert all(an.has_arabic(v) for v in analyzed["sectors"].values())
    for it in analyzed["items"]:
        assert it["market"] in an.MARKETS
        assert it["signal"] in an.SIGNALS
        assert 1 <= it["cf"] <= 3
        assert it["lang"] in ("ar", "en")
        assert isinstance(it["summary_ar"], str) and it["summary_ar"]
        assert set(it["beneficiary"]) == {"name", "why"} and set(it["hurt"]) == {"name", "why"}
        assert set(it["impact"]) == set(an.SECTOR_KEYS) | {"why"}
        assert all(isinstance(it["impact"][k], int) and -3 <= it["impact"][k] <= 3 for k in an.SECTOR_KEYS)
        assert isinstance(it["impact"]["why"], str)
        for t in it["tickers"]:
            assert set(t) == {"code", "name_ar"} and len(t["code"]) == 4
        assert it["analysis"] == "rules" and "id" in it


def test_rules_aramco_earnings(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("أرامكو السعودية"))
    assert it["market"] == "sa" and it["signal"] == "pos" and it["lang"] == "ar"
    assert {"code": "2222", "name_ar": "أرامكو السعودية"} in it["tickers"]
    assert "earnings_up" in it["rules"] and "dividend" in it["rules"]
    assert it["impact"]["energy"] >= 1
    assert it["cf"] == 3
    assert it["beneficiary"]["name"].startswith("أرامكو")


def test_rules_attack(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("هجوم"))
    assert it["signal"] == "neg" and "attack" in it["rules"]
    assert it["impact"]["insurance"] <= -2 and it["impact"]["transport"] < 0
    assert it["hurt"]["name"].startswith("التأمين")
    assert it["market"] == "sa"


def test_rules_oil_english(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("Oil prices rise"))
    assert it["lang"] == "en" and it["summary_ar"] == it["summary"]  # English kept when no LLM
    assert it["market"] == "sa"  # summary names Bahri (4030): a constituent mention is an explicit Saudi signal
    assert "oil_up" in it["rules"] and "attack" in it["rules"]
    assert it["impact"]["energy"] >= 2 and it["impact"]["transport"] <= -2
    assert any(t["code"] == "4030" for t in it["tickers"])  # Bahri


def test_rules_fed_cut(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("Fed signals"))
    assert it["market"] == "us" and it["signal"] == "pos"
    assert "fed_cut" in it["rules"]
    assert it["impact"]["realestate"] > 0


def test_rules_contract_rajhi(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("الراجحي"))
    assert it["signal"] == "pos" and "contract_award" in it["rules"]
    assert any(t["code"] == "1120" for t in it["tickers"])
    assert it["impact"]["banks"] >= 1


def test_rules_deterministic(raw):
    rules = an.load_rules()
    consts = an.load_constituents(path=None)
    a = an.analyze(raw, rules, consts)
    b = an.analyze(raw, rules, consts)
    strip = lambda d: {k: v for k, v in d.items() if k != "generated_at_utc"}  # noqa: E731
    assert json.dumps(strip(a), sort_keys=True, ensure_ascii=False) == json.dumps(strip(b), sort_keys=True, ensure_ascii=False)


def test_ticker_matcher_avoids_years():
    m = an.build_matcher(an.BUILTIN_CONSTITUENTS)
    assert m("رؤية 2030 تدعم الاقتصاد") == []  # 2030 is a real code but reads as a year
    assert [t["code"] for t in m("Saudi Arabia Refineries (2030) shares")] == ["2030"] or m("x") == []
    assert [t["code"] for t in m("سهم البحري (4030) يتراجع وبوبا العربية ترتفع")] == ["4030", "8210"]
    assert m("الناس يتحدثون") == []  # 'ناس' alias must not match inside a longer word


def test_constituents_loader_shapes(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"symbol": "2222.SE", "name": "أرامكو", "sector": "الطاقة"},
                             {"code": "1120", "name_en": "Al Rajhi", "sector": "Banks"},
                             {"code": "bad"}], ensure_ascii=False), encoding="utf-8")
    rows = an.load_constituents(str(p))
    assert [(r["code"], r["sector"]) for r in rows[:2]] == [("2222", "energy"), ("1120", "banks")]  # file rows first, "bad" skipped
    assert rows[0]["name_ar"] == "أرامكو" and len(rows) > 2                                          # built-ins appended after
    p.write_text(json.dumps({"2010": {"name_ar": "سابك", "sector": "المواد الأساسية"}}, ensure_ascii=False), encoding="utf-8")
    rows = an.load_constituents(str(p))
    assert rows[0]["code"] == "2010" and rows[0]["sector"] == "petrochem"
    assert an.load_constituents(str(tmp_path / "missing.json"))[0]["code"] == "2222"


# ---------------------------------------------------------------- analyze: llm validation (offline)
def test_validate_llm_item():
    known = {"2222": "أرامكو"}
    good = {"id": "x", "market": "sa", "signal": "pos", "tickers": [{"code": "2222.SE", "name_ar": ""}, {"code": "9999", "name_ar": "?"}],
            "summary_ar": "ملخص", "beneficiary": {"name": "أرامكو", "why": "سبب"}, "hurt": {"name": "—", "why": ""},
            "impact": {"energy": 5, "banks": -7, "why": "لأن"}, "cf": 9}
    out = an.validate_llm_item(good, known)
    assert out["tickers"] == [{"code": "2222", "name_ar": "أرامكو"}]
    assert out["impact"]["energy"] == 3 and out["impact"]["banks"] == -3 and out["impact"]["retail"] == 0
    assert out["cf"] == 3 and out["lang"] == "ar" and out["analysis"] == "llm"
    assert an.validate_llm_item({"market": "moon", "signal": "pos"}, known) is None
    assert an.validate_llm_item({"market": "sa", "signal": "pos", "summary_ar": "", "impact": {}}, known) is None
    assert an.validate_llm_item("nope", known) is None


def test_llm_failure_falls_back_to_rules(raw, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(an, "_post_messages", boom)
    res = an.analyze(raw, an.load_rules(), an.load_constituents(path=None), api_key="sk-test")
    assert res["analysis_mode"] == "rules"
    assert all(i["analysis"] == "rules" for i in res["items"])


def test_llm_success_merges(raw, monkeypatch):
    rules = an.load_rules()
    consts = an.load_constituents(path=None)
    base = an.analyze(raw, rules, consts)
    target = base["items"][0]["id"]
    captured = {}

    def fake_post(payload, api_key, use_fallbacks, timeout=300):
        captured["payload"] = payload
        captured["fallbacks"] = use_fallbacks
        body = {"results": [{"id": target, "market": "macro", "signal": "neg", "tickers": [],
                             "summary_ar": "ملخص عربي من النموذج", "beneficiary": {"name": "أ", "why": "ب"},
                             "hurt": {"name": "ج", "why": "د"}, "impact": {k: 0 for k in an.SECTOR_KEYS} | {"why": "شرح"},
                             "cf": 2}]}
        return {"stop_reason": "end_turn", "model": payload["model"], "usage": {},
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}

    monkeypatch.setattr(an, "_post_messages", fake_post)
    res = an.analyze(raw, rules, consts, api_key="sk-test", batch_size=10)
    assert captured["payload"]["model"] == an.DEFAULT_MODEL
    assert captured["payload"]["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in captured["payload"]
    assert res["analysis_mode"] == "llm+rules"
    it = next(i for i in res["items"] if i["id"] == target)
    assert it["analysis"] == "llm" and it["summary_ar"] == "ملخص عربي من النموذج" and it["market"] == "macro"
    assert it["rules"]  # rule ids retained for transparency
    others = [i for i in res["items"] if i["id"] != target]
    assert all(i["analysis"] == "rules" for i in others)


def test_analyze_cli(raw, tmp_path):
    inp = tmp_path / "raw.json"
    inp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "news.json"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "analyze_news.py"), "--in", str(inp), "--out", str(out),
                        "--market", str(tmp_path / "missing_market.json"), "--constituents", str(tmp_path / "none.json")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["analysis_mode"] == "rules" and len(data["items"]) == len(raw["items"])
    assert data["sectors"]["banks"] == "البنوك"


# ---------------------------------------------------------------- fetch: http robustness (mocked)
class _Resp:
    def __init__(self, status, body=b"", ctype="application/rss+xml", url=None):
        self.status_code, self.content, self.url = status, body, url
        self.headers = {"Content-Type": ctype}


FEED_XML = open(os.path.join(FIXTURES, "atom_sample.xml"), "rb").read()


def test_looks_like_feed():
    assert fn.looks_like_feed(FEED_XML)
    assert fn.looks_like_feed(b'<?xml version="1.0"?>\n<rss version="2.0"><channel/></rss>')
    assert not fn.looks_like_feed(b"<html><body>no feed</body></html>")
    assert not fn.looks_like_feed(b"")


def test_403_retries_with_alternate_profile(monkeypatch):
    calls = []

    def fake_get(url, timeout, profile):
        calls.append(profile["User-Agent"])
        return _Resp(403, b"blocked", "text/html") if len(calls) == 1 else _Resp(200, FEED_XML, url=url)

    monkeypatch.setattr(fn, "_http_get", fake_get)
    notes = []
    body, eff = fn.fetch_feed("https://example.sa/rss", timeout=1, retries=0, notes=notes)
    assert body == FEED_XML and eff == "https://example.sa/rss"
    assert len(calls) == 2 and calls[0] != calls[1]
    assert calls[1] == fn.UA_PROFILES[1]["User-Agent"]
    assert any("403" in n for n in notes)


def test_403_twice_is_permanent_no_retry_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(fn, "_http_get", lambda url, timeout, profile: (calls.append(1), _Resp(403, b"x", "text/html"))[1])
    monkeypatch.setattr(fn.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        fn.fetch_feed("https://example.sa/rss", timeout=1, retries=3)
    assert len(calls) == 2  # 4xx is permanent: no backoff retries


def test_autodiscovery_one_hop(monkeypatch):
    html = (b"<html><head><title>x</title>"
            b'<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
            b'<link rel="alternate" type="application/rss+xml" title="RSS" href="/ar/rss/latest.xml">'
            b"</head><body></body></html>")
    seen = []

    def fake_get(url, timeout, profile):
        seen.append(url)
        if url.endswith("/ar/rss"):
            return _Resp(200, html, "text/html; charset=utf-8", url=url)
        if url.endswith("/ar/rss/latest.xml"):
            return _Resp(200, FEED_XML, url=url)
        raise AssertionError(url)

    monkeypatch.setattr(fn, "_http_get", fake_get)
    notes = []
    body, eff = fn.fetch_feed("https://www.argaam.com/ar/rss", timeout=1, retries=0, notes=notes)
    assert body == FEED_XML
    assert eff == "https://www.argaam.com/ar/rss/latest.xml"  # rss preferred over atom, relative href resolved
    assert seen == ["https://www.argaam.com/ar/rss", "https://www.argaam.com/ar/rss/latest.xml"]
    assert any("autodiscovered" in n for n in notes)


def test_autodiscovery_is_capped_to_one_hop(monkeypatch):
    html = b'<html><head><link rel="alternate" type="application/rss+xml" href="https://x.sa/other"></head></html>'
    seen = []

    def fake_get(url, timeout, profile):
        seen.append(url)
        return _Resp(200, html, "text/html", url=url)  # every page is HTML pointing at another page

    monkeypatch.setattr(fn, "_http_get", fake_get)
    with pytest.raises(RuntimeError, match="not a feed"):
        fn.fetch_feed("https://x.sa/rss", timeout=1, retries=0)
    assert seen == ["https://x.sa/rss", "https://x.sa/other"]


def test_discover_feed_url_anchor_fallback():
    html = b'<html><body><a href="//www.spa.gov.sa/rss?lang=ar">RSS</a><a href="/about">about</a></body></html>'
    assert fn.discover_feed_url(html, "https://www.spa.gov.sa/") == "https://www.spa.gov.sa/rss?lang=ar"
    assert fn.discover_feed_url(b"<html><body>nothing</body></html>", "https://x/") is None


def test_run_reports_effective_url_and_notes(monkeypatch):
    html = b'<html><head><link rel="alternate" type="application/rss+xml" href="https://site.sa/feed.xml"></head></html>'
    monkeypatch.setattr(fn, "_http_get", lambda url, timeout, profile:
                        _Resp(200, html, "text/html", url=url) if url == "https://site.sa/" else _Resp(200, FEED_XML, url=url))
    res = fn.run([{"name": "s", "url": "https://site.sa/", "priority": 1}], window_hours=1e6, now=NOW)
    rep = res["feeds"][0]
    assert rep["ok"] and rep["effective_url"] == "https://site.sa/feed.xml" and rep["notes"]


def test_default_feed_list_shape():
    urls = [f["url"] for f in fn.DEFAULT_FEEDS]
    assert len(urls) == len(set(urls))
    assert all(u.startswith("https://") for u in urls)
    gn = [u for u in urls if "news.google.com/rss/search" in u]
    assert len(gn) >= 10
    for site in ("argaam.com", "arabnews.com", "alarabiya.net", "saudiexchange.sa", "mubasher.info",
                 "aleqt.com", "maaal.com", "cnbcarabia.com"):
        assert any(f"site%3A{site}" in u for u in gn), site
    # Saudi-first: every priority-1 feed comes before any priority-2/3 feed once sorted (stable)
    pr = [f["priority"] for f in sorted(fn.DEFAULT_FEEDS, key=lambda f: f["priority"])]
    assert pr == sorted(pr) and pr[0] == 1


# ---------------------------------------------------------------- relevance gate / ambiguity / ranking
SPORTS = {"title": "Brent Venables hopes elite nonconference games won't go away - Yahoo Sports",
          "summary": "", "source": "Yahoo Sports (عبر Google News)", "link": "https://sports.yahoo.com/x",
          "published_utc": "2026-09-09T09:00:00Z", "published_riyadh": "2026-09-09 12:00:00", "precision": "second",
          "fetched_at_utc": "2026-09-09T12:00:00Z", "lang": "en", "priority": 3}


def test_relevance_scores():
    assert fn.relevance_score(SPORTS["title"], "", SPORTS["source"], SPORTS["link"]) == 0.0
    assert fn.relevance_score("Brent tops $100 a barrel", "") >= 0.4          # brent + context counts
    assert fn.relevance_score("Fed up with traffic in Riyadh", "") == 0.0      # fed without rate context
    assert fn.relevance_score("Fed holds rates steady", "") >= 0.4
    assert fn.relevance_score("أرامكو تعلن أرباحها", "") >= 0.4
    ar_sport = fn.relevance_score("الهلال يفوز بالدوري", "أسهم اللاعبين ترتفع في سوق الانتقالات", "رياضة")
    assert ar_sport < fn.RELEVANCE_MIN
    # off-topic URL section penalises even with a finance word
    assert fn.relevance_score("Best stock of the season", "", "", "https://x.com/sports/football/1") < fn.RELEVANCE_MIN
    assert fn.relevance_score("Best stock of the season", "", "", "https://x.com/business/1") >= fn.RELEVANCE_MIN
    assert 0.0 <= fn.relevance_score("Saudi stocks, banks and oil: TASI index rises 1%", "earnings, dividend") <= 1.0


def test_fetch_gate_drops_offtopic_and_reports(raw, tmp_path):
    xml = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
           b"<item><title>Brent Venables hopes elite nonconference games won't go away - Yahoo Sports</title>"
           b"<link>https://sports.yahoo.com/a</link><pubDate>Wed, 09 Sep 2026 10:00:00 GMT</pubDate></item>"
           b"<item><title>Brent crude tops $100 a barrel</title><link>https://r.com/b</link>"
           b"<pubDate>Wed, 09 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>")
    f = tmp_path / "f.xml"
    f.write_bytes(xml)
    res = fn.run([{"name": "gn", "url": str(f), "priority": 3}], now=NOW)
    assert [i["title"] for i in res["items"]] == ["Brent crude tops $100 a barrel"]
    assert res["feeds"][0]["dropped_irrelevant"] == 1
    assert res["dropped"] == 1 and res["stats"]["dropped_irrelevant"] == 1
    assert all(0 <= i["relevance"] <= 1 for i in raw["items"]) and "stats" in raw and "dropped" in raw


def test_fetch_cap_reserves_saudi_first(tmp_path):
    def rss(n, host, hour):
        items = "".join(f"<item><title>{host} market news {i}</title><link>https://{host}/{i}</link>"
                        f"<pubDate>Wed, 09 Sep 2026 {hour:02d}:{i:02d}:00 GMT</pubDate></item>" for i in range(n))
        return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'.encode()
    sa, gl = tmp_path / "sa.xml", tmp_path / "gl.xml"
    sa.write_bytes(rss(5, "argaam.com", 8))   # older but Saudi
    gl.write_bytes(rss(5, "cnbc.com", 11))    # newer but global
    res = fn.run([{"name": "sa", "url": str(sa), "priority": 1}, {"name": "gl", "url": str(gl), "priority": 3}],
                 max_items=6, now=NOW)
    srcs = [i["source"] for i in res["items"]]
    assert srcs.count("sa") == 5 and srcs.count("gl") == 1 and res["stats"]["dropped_cap"] == 4
    stamps = [i["published_utc"] for i in res["items"]]
    assert stamps == sorted(stamps, reverse=True)  # still newest-first in the output


def _analyze_items(items, **kw):
    return an.analyze({"items": items}, an.load_rules(), an.load_constituents(path=None), **kw)


def test_ambiguous_brent_and_fed_keywords():
    sports = dict(SPORTS, relevance=1.0)  # bypass the gate to test the rules themselves
    fed = dict(SPORTS, title="Fed up with Riyadh traffic, commuters demand a metro market", relevance=1.0)
    res = _analyze_items([sports, fed])
    s, f = sorted(res["items"], key=lambda i: i["title"])
    assert s["title"].startswith("Brent") and s["impact"]["energy"] == 0
    assert not any(r.startswith("oil") for r in s["rules"]) and s["market"] != "macro"
    assert f["title"].startswith("Fed") and f["market"] != "us" and not any(r.startswith("fed") for r in f["rules"])
    # with context the same words count
    real = dict(SPORTS, title="Brent tops $100 a barrel; Fed seen holding rates", relevance=1.0)
    r = _analyze_items([real])["items"][0]
    assert "oil_up" in r["rules"] and "fed_generic" in r["rules"] and r["impact"]["energy"] >= 2


def test_ambiguous_ticker_aliases_need_company_context():
    m = an.build_matcher(an.BUILTIN_CONSTITUENTS)
    assert m("كيان سياسي جديد في المنطقة") == []
    assert [t["code"] for t in m("سهم كيان السعودية يرتفع 3%")] == ["2350"]
    assert [t["code"] for t in m("كيان (2350) تعلن نتائجها")] == ["2350"]
    assert m("علم النفس وأثره على الطلاب") == []
    assert [t["code"] for t in m("شركة علم توقع عقداً")] == ["7203"]
    assert m("الأهلي يفوز على الهلال") == []
    assert [t["code"] for t in m("أرباح الأهلي ترتفع")] == ["1180"]


def test_rank_items_saudi_first_and_non_sa_cap():
    def mk(i, market, hour):
        return {"id": f"{market}{i}", "market": market, "published_utc": f"2026-09-09T{hour:02d}:{i:02d}:00Z"}
    items = [mk(i, "macro", 11) for i in range(30)] + [mk(i, "sa", 8) for i in range(10)] \
        + [mk(i, "us", 10) for i in range(5)] + [mk(0, "other", 9)]
    ranked, dropped = an.rank_items(items, max_items=60, max_non_sa=25)
    markets = [i["market"] for i in ranked]
    assert markets[:10] == ["sa"] * 10 and markets.count("sa") == 10
    assert len(ranked) == 35 and dropped == 11 and markets[10:] == ["macro"] * 25
    sa_stamps = [i["published_utc"] for i in ranked[:10]]
    assert sa_stamps == sorted(sa_stamps, reverse=True)
    ranked2, _ = an.rank_items([mk(i, "sa", 8) for i in range(70)], max_items=60)
    assert len(ranked2) == 60


def test_analyze_stats_relevance_and_summary_policy(raw):
    res = _analyze_items(raw["items"] + [SPORTS])
    assert res["dropped"] == 1 and res["stats"]["dropped_irrelevant"] == 1
    assert res["stats"]["kept"] == len(res["items"]) and set(res["stats"]["by_market"]) == set(an.MARKETS)
    assert all("relevance" in i and 0 <= i["relevance"] <= 1 for i in res["items"])
    markets = [i["market"] for i in res["items"]]
    assert markets == sorted(markets, key=lambda m: an.MARKET_ORDER[m])
    for it in res["items"]:
        if it["lang"] == "ar":
            assert an.has_arabic(it["summary_ar"])
        else:
            assert it["summary_ar"] == (it["summary"] or it["title"])
    no_summary = dict(SPORTS, title="أرباح شركة سعودية ترتفع", summary="", lang="ar", relevance=1.0)
    it = _analyze_items([no_summary])["items"][0]
    assert it["summary_ar"] == "أرباح شركة سعودية ترتفع" and it["lang"] == "ar"


# ---------------------------------------------------------------- future timestamps / explicit-Saudi market
def _rss(items):
    body = "".join(f"<item><title>{t}</title><link>https://x.sa/{i}</link><pubDate>{d}</pubDate></item>"
                   for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


def test_future_times_riyadh_shift_then_clamp(tmp_path):
    f = tmp_path / "future.xml"
    f.write_bytes(_rss([
        ("سوق الأسهم: خبر بتوقيت الرياض موسوم كـ UTC", "Wed, 09 Sep 2026 14:00:00 +0000"),   # now+2h -> -3h = 11:00Z
        ("Stock market item far in the future", "Wed, 09 Sep 2026 20:30:00 GMT"),           # now+8.5h -> clamp
        ("Stock market item slightly ahead", "Wed, 09 Sep 2026 12:03:00 GMT"),              # within 5 min tolerance
        ("Stock market item dated tomorrow", "2026-09-10"),                                  # day precision, clamp
    ]))
    res = fn.run([{"name": "f", "url": str(f), "priority": 1}], now=NOW)
    by = {i["title"]: i for i in res["items"]}
    a = by["سوق الأسهم: خبر بتوقيت الرياض موسوم كـ UTC"]
    assert a["published_utc"] == "2026-09-09T11:00:00Z" and a["published_riyadh"] == "2026-09-09 14:00:00"
    assert a["time_adjusted"] == "riyadh_local" and a["precision"] == "second"
    assert a["raw_published"] == "Wed, 09 Sep 2026 14:00:00 +0000"
    b = by["Stock market item far in the future"]
    assert b["published_utc"] == "2026-09-09T12:00:00Z" and b["time_adjusted"] == "clamped" and b["precision"] == "minute"
    c = by["Stock market item slightly ahead"]
    assert c["published_utc"] == "2026-09-09T12:03:00Z" and "time_adjusted" not in c
    d = by["Stock market item dated tomorrow"]
    assert d["published_utc"] == "2026-09-09T12:00:00Z" and d["time_adjusted"] == "clamped" and d["precision"] == "day"
    assert all(i["published_utc"] <= "2026-09-09T12:05:00Z" for i in res["items"])
    assert all("raw_published" in i for i in res["items"])


def test_market_requires_explicit_saudi_signal():
    base = dict(SPORTS, priority=1, source="أرقام", lang="ar")
    china = dict(base, title="انتعاش نمو الصادرات الصينية مع اقتراب الفائض التجاري من 806 مليارات دولار", summary="")
    ai = dict(base, title="ماذا لو فازت الصين بسباق الذكاء الاصطناعي؟",
              summary="تخيّل أن دولة تنفق مئات المليارات من الدولارات لتطوير التكنولوجيا الأذكى في العالم")
    ai_warn = dict(base, title="تحذيرات من خروج تكنولوجيا الذكاء الاصطناعي عن السيطرة", summary="قال باحث في مجال السلامة")
    nomu = dict(base, title="إدراج شركة جديدة في سوق نمو الموازية", summary="")
    generic = dict(base, title="ارتفاع أسعار الذهب عالمياً مع تراجع الدولار", summary="")
    ticker = dict(base, title="سابك تعلن نتائجها المالية", summary="")
    us = dict(base, title="Wall Street closes higher as Nasdaq rallies", summary="", lang="en")
    res = _analyze_items([china, ai, ai_warn, nomu, generic, ticker, us])
    by = {i["title"]: i for i in res["items"]}
    assert by[china["title"]]["market"] == "macro"            # source is Saudi, text is not
    assert by[nomu["title"]]["market"] == "sa"                 # نمو only as a market, with context
    assert by[generic["title"]]["market"] == "macro"
    assert by[ticker["title"]]["market"] == "sa"               # constituent name is an explicit signal
    assert by[us["title"]]["market"] == "us"
    # generic AI opinion pieces: market 'other' -> relevance penalised -> dropped
    assert ai["title"] not in by and ai_warn["title"] not in by
    assert res["stats"]["dropped_irrelevant"] == 2
    assert "sa" not in [i["market"] for i in res["items"] if i["title"] in (china["title"], generic["title"])]


def test_pure_oil_headline_is_macro_not_sa():
    it = _analyze_items([dict(SPORTS, title="Oil prices rise as Brent tops $100 a barrel", summary="", relevance=1.0)])["items"][0]
    assert it["market"] == "macro" and "oil_up" in it["rules"]


def test_analyzer_fixes_future_times_in_old_raw_files():
    old = dict(SPORTS, title="Saudi stocks: TASI closes higher", summary="", source="Argaam", link="https://argaam.com/1",
               published_utc="2026-09-09T17:16:00Z",
               published_riyadh="2026-09-09 20:16:00", fetched_at_utc="2026-09-09T14:24:55Z", precision="second")
    it = _analyze_items([old])["items"][0]
    assert it["published_utc"] == "2026-09-09T14:16:00Z" and it["published_riyadh"] == "2026-09-09 17:16:00"
    assert it["time_adjusted"] == "riyadh_local" and it["precision"] == "second"


def test_negative_context_and_gulf_macro():
    base = dict(SPORTS, priority=1, source="الاقتصادية", link="https://aleqt.com/1", lang="ar")
    ksa = dict(base, title="تكوين رأس المال الثابت في المملكة يقفز 5.2% خلال النصف الأول", summary="")
    uk = dict(base, title="التضخم في المملكة المتحدة يتراجع إلى 3%", summary="")
    gulf = dict(base, title="«جيفريز» تطلق مؤشر «GCC 30» لاقتناص فرص النمو في أسواق الخليج", summary="")
    cement = dict(base, title="أسمنت الشمالية: تحليل متوسط سعر بيع الطن والهوامش والحصة السوقية بالربع الثاني", summary="")
    by = {i["title"]: i for i in _analyze_items([ksa, uk, gulf, cement])["items"]}
    assert by[ksa["title"]]["market"] == "sa"
    assert by[uk["title"]]["market"] == "macro"        # "المملكة المتحدة" is excluded from the Saudi signal
    assert by[gulf["title"]]["market"] == "macro"      # Gulf-region finance is macro, not dropped
    assert by[cement["title"]]["market"] == "sa" and by[cement["title"]]["tickers"][0]["code"] == "3004"


def test_constituents_file_merged_with_builtin_aliases(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"constituents": [
        {"code": "1120", "name_ar": "مصرف الراجحي", "name_en": "Al Rajhi Bank", "sector_ar": "البنوك"},
        {"code": "9999", "name_ar": "شركة وهمية", "name_en": "Fake Co", "sector_ar": "الطاقة"}]}, ensure_ascii=False), encoding="utf-8")
    rows = an.load_constituents(str(p))
    by = {r["code"]: r for r in rows}
    assert by["1120"]["name_ar"] == "مصرف الراجحي" and "الراجحي" in by["1120"]["aliases"]   # file name kept, alias added
    assert "2222" in by and "9999" in by                                                       # built-in rows appended
    m = an.build_matcher(rows)
    assert [t["code"] for t in m("الراجحي يوقع اتفاقية")] == ["1120"]
    assert [t["code"] for t in m("أسمنت الشمالية تعلن نتائجها")] == ["3004"]
