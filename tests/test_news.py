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
    assert "<" not in first["source_excerpt"] and "alert" not in first["source_excerpt"]
    assert "&amp;" not in first["source_excerpt"] and "&" in first["source_excerpt"]
    assert "summary" not in first  # raw feed text is only ever `source_excerpt`
    assert first["date_raw"] == "Wed, 09 Sep 2026 10:15:42 +0300"
    assert entries[2]["date_raw"] == "2026-09-09"  # dc:date fallback


def test_parse_atom():
    with open(os.path.join(FIXTURES, "atom_sample.xml"), "rb") as fh:
        entries = fn.parse_feed(fh.read(), "Arab News")
    assert len(entries) == 4
    assert entries[0]["link"] == "https://www.arabnews.com/node/3000001/business"
    assert entries[0]["source_excerpt"] == "Brent crude climbed above $100 a barrel on Wednesday. Bahri shares fell."
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
    required = {"title", "link", "source", "published_utc", "published_riyadh", "precision", "source_excerpt",
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
    return an.analyze(raw, rules, consts, market=None, api_key=None, now=NOW)


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
        assert isinstance(it["brief_ar"], str) and an.has_arabic(it["brief_ar"]) and it["brief_origin"] == "template"
        assert it["analysis_note_ar"] == an.ANALYSIS_NOTE_AR
        assert not set(it) & set(an.VIEW_DROP_FIELDS)   # raw feed text never reaches the view
        assert set(it["beneficiary"]) == {"name", "why"} and set(it["hurt"]) == {"name", "why"}
        assert set(it["impact"]) == set(an.SECTOR_KEYS) | {"why"}
        assert all(isinstance(it["impact"][k], int) and -3 <= it["impact"][k] <= 3 for k in an.SECTOR_KEYS)
        assert isinstance(it["impact"]["why"], str)
        for t in it["tickers"]:
            assert set(t) == {"code", "name_ar", "market"} and t["market"] in ("sa", "us")
            assert len(t["code"]) == 4 if t["market"] == "sa" else t["code"].isupper()
        assert it["analysis"] == "rules" and "id" in it
        assert it["impact_market"] in an.IMPACT_MARKETS
        assert len(it["day_local"]) == 10 and isinstance(it["is_today"], bool)
        assert it["age_hours"] is None or it["age_hours"] >= 0
        assert it["first_seen_utc"] == "2026-09-09T12:00:00Z"
    assert analyzed["today_riyadh"] == "2026-09-09"
    assert [d["day_local"] for d in analyzed["days"]] == sorted(analyzed["stats"]["by_day"], reverse=True)
    assert analyzed["days"][0]["label_ar"] == "الأربعاء 9 سبتمبر" and analyzed["days"][0]["is_today"]
    assert "_archive" in analyzed and len(analyzed["_archive"]) == analyzed["stats"]["archive_total"]


def test_rules_aramco_earnings(analyzed):
    it = next(i for i in analyzed["items"] if i["title"].startswith("أرامكو السعودية"))
    assert it["market"] == "sa" and it["signal"] == "pos" and it["lang"] == "ar"
    assert {"code": "2222", "name_ar": "أرامكو السعودية", "market": "sa"} in it["tickers"]
    assert it["impact_market"] == "sa"
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
    assert it["lang"] == "en" and an.has_arabic(it["brief_ar"]) and "source_excerpt" not in it  # own Arabic brief, no source text
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
    a = an.analyze(raw, rules, consts, now=NOW)
    b = an.analyze(raw, rules, consts, now=NOW)
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
            "brief_ar": "موجز عربي أصلي بكلمات النموذج عن نتائج الشركة.", "beneficiary": {"name": "أرامكو", "why": "سبب"}, "hurt": {"name": "—", "why": ""},
            "impact": {"energy": 5, "banks": -7, "why": "لأن"}, "cf": 9}
    out = an.validate_llm_item(good, known)
    assert out["tickers"] == [{"code": "2222", "name_ar": "أرامكو", "market": "sa"}]
    assert out["impact"]["energy"] == 3 and out["impact"]["banks"] == -3 and out["impact"]["retail"] == 0
    assert out["cf"] == 3 and out["analysis"] == "llm" and "lang" not in out   # lang stays that of the source
    assert out["brief_ar"].startswith("موجز") and out["brief_origin"] == "llm"
    assert out["impact_market"] == "sa"  # derived when the model omits it
    # US tickers: symbol validated against known_us (case-folded), unknown symbols dropped
    us = an.validate_llm_item(dict(good, market="us", impact_market="both",
                                   tickers=[{"code": "nvda", "name_ar": ""}, {"code": "ZZZZ", "name_ar": "x"}, {"code": "2222", "name_ar": ""}]),
                              known, {"NVDA": "إنفيديا"})
    assert us["tickers"] == [{"code": "2222", "name_ar": "أرامكو", "market": "sa"}, {"code": "NVDA", "name_ar": "إنفيديا", "market": "us"}]
    assert us["impact_market"] == "both"
    assert an.validate_llm_item({"market": "moon", "signal": "pos"}, known) is None
    weak = an.validate_llm_item({"market": "sa", "signal": "pos", "brief_ar": "", "impact": {}}, known)
    assert weak is not None and "brief_ar" not in weak and "brief_origin" not in weak   # analysis kept, brief -> template
    assert an.validate_llm_item("nope", known) is None


def test_llm_failure_falls_back_to_rules(raw, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(an, "_post_messages", boom)
    res = an.analyze(raw, an.load_rules(), an.load_constituents(path=None), api_key="sk-test", now=NOW)
    assert res["analysis_mode"] == "rules"
    assert all(i["analysis"] == "rules" for i in res["items"])


def test_llm_success_merges(raw, monkeypatch):
    rules = an.load_rules()
    consts = an.load_constituents(path=None)
    base = an.analyze(raw, rules, consts, now=NOW)
    target = base["items"][0]["id"]
    captured = {}

    def fake_post(payload, api_key, use_fallbacks, timeout=300):
        captured["payload"] = payload
        captured["fallbacks"] = use_fallbacks
        body = {"results": [{"id": target, "market": "macro", "signal": "neg", "tickers": [],
                             "brief_ar": "موجز عربي من النموذج بكلماته الخاصة.", "beneficiary": {"name": "أ", "why": "ب"},
                             "hurt": {"name": "ج", "why": "د"}, "impact": {k: 0 for k in an.SECTOR_KEYS} | {"why": "شرح"},
                             "cf": 2}]}
        return {"stop_reason": "end_turn", "model": payload["model"], "usage": {},
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}

    monkeypatch.setattr(an, "_post_messages", fake_post)
    res = an.analyze(raw, rules, consts, api_key="sk-test", batch_size=10, now=NOW,
                     constituents_us=an.load_constituents_us())
    assert captured["payload"]["model"] == an.DEFAULT_MODEL
    user_msg = captured["payload"]["messages"][0]["content"]
    assert "NVDA=" in user_msg and "الشركات الأمريكية" in user_msg          # US list handed to the model
    assert "brief_ar" in captured["payload"]["system"] and "impact_market" in captured["payload"]["system"]
    assert "summary_ar" not in captured["payload"]["system"] and "ننصح" in captured["payload"]["system"]
    assert "source_excerpt" in user_msg and "brief_ar" in json.dumps(captured["payload"]["output_config"]["format"]["schema"])
    assert captured["payload"]["output_config"]["format"]["schema"]["properties"]["results"]["items"]["required"].count("impact_market") == 1
    assert captured["payload"]["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in captured["payload"]
    assert res["analysis_mode"] == "llm+rules"
    it = next(i for i in res["items"] if i["id"] == target)
    assert it["analysis"] == "llm" and it["brief_ar"] == "موجز عربي من النموذج بكلماته الخاصة." and it["market"] == "macro"
    assert it["brief_origin"] == "llm" and it["analysis_note_ar"] == an.ANALYSIS_NOTE_AR and not set(it) & set(an.VIEW_DROP_FIELDS)
    assert it["rules"]  # rule ids retained for transparency
    others = [i for i in res["items"] if i["id"] != target]
    assert all(i["analysis"] == "rules" for i in others)


def test_analyze_cli(raw, tmp_path):
    inp = tmp_path / "raw.json"
    inp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "news.json"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    archive = tmp_path / "news_archive.json"
    cmd = [sys.executable, os.path.join(SCRIPTS, "analyze_news.py"), "--in", str(inp), "--out", str(out),
           "--market", str(tmp_path / "missing_market.json"), "--constituents", str(tmp_path / "none.json"),
           "--archive", str(archive), "--now", "2026-09-09T12:00:00Z"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["analysis_mode"] == "rules" and len(data["items"]) == len(raw["items"])
    assert data["sectors"]["banks"] == "البنوك" and "_archive" not in data
    assert not any(set(i) & set(an.VIEW_DROP_FIELDS) for i in data["items"])         # policy: no source text in news.json
    assert all(i["brief_ar"] and i["brief_origin"] == "template" and i["analysis_note_ar"] for i in data["items"])
    arc = json.loads(archive.read_text(encoding="utf-8"))
    assert arc["count"] == len(raw["items"]) == len(arc["items"]) and arc["days"] == 7
    assert all("source_excerpt" in i and "summary" not in i for i in arc["items"])   # analysis input kept in the archive only
    # second run: nothing new to analyze, archive stable, view identical
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    data2 = json.loads(out.read_text(encoding="utf-8"))
    assert data2["stats"]["new_items"] == 0 and data2["stats"]["already_known"] == len(raw["items"])
    assert [i["id"] for i in data2["items"]] == [i["id"] for i in data["items"]]


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
    for site in ("argaam.com", "arabnews.com", "alarabiya.net", "mubasher.info",
                 "aleqt.com", "maaal.com", "cnbcarabia.com"):
        assert any(f"site%3A{site}" in u for u in gn), site
    # owner's request: never query or link Saudi Exchange
    assert not any("saudiexchange" in u for u in urls)


def test_blocked_domain_items_are_dropped():
    assert fn.is_blocked_domain("https://www.saudiexchange.sa/wps/portal/x")
    assert fn.is_blocked_domain("https://saudiexchange.sa/")
    assert not fn.is_blocked_domain("https://www.argaam.com/ar/article/1")
    assert not fn.is_blocked_domain("")
    us = [f for f in fn.DEFAULT_FEEDS if f.get("market_hint") == "us"]
    assert len(us) >= 20 and all(f["priority"] == 3 for f in us)
    us_urls = " ".join(f["url"] for f in us)
    for host in ("cnbc.com", "dowjones.io", "finance.yahoo.com", "seekingalpha.com", "investing.com",
                 "federalreserve.gov", "sec.gov"):
        assert host in us_urls, host
    for site in ("reuters.com", "bloomberg.com", "cnbc.com", "marketwatch.com"):
        assert f"site%3A{site}" in us_urls, site
    for q in ("Wall+Street", "S%26P+500", "Fed+rates", "earnings+report"):
        assert q in us_urls, q
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
    """Rules-mode analysis at NOW. Test items often share one link; ids are
    link-based, so duplicate links get a distinct fragment to stay separate."""
    seen, fixed = set(), []
    for i, it in enumerate(items):
        it = dict(it)
        if it.get("link") in seen:
            it["link"] = f"{it['link']}?n={i}"
        seen.add(it.get("link"))
        fixed.append(it)
    kw.setdefault("now", NOW)
    kw.setdefault("constituents_us", US_CONSTS)
    return an.analyze({"items": fixed}, an.load_rules(), an.load_constituents(path=None), **kw)


US_CONSTS = an.load_constituents_us()


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


def _mk(i, market, day, hour=8):
    return {"id": f"{market}{day}{i}", "market": market, "published_utc": f"2026-09-{day:02d}T{hour:02d}:{i % 60:02d}:{i // 60:02d}Z"}


def test_rank_items_today_first_then_market_order():
    # today (Riyadh 2026-09-09 at NOW): sa newest-first, then us, macro, other; yesterday after, even if newer-looking
    items = [_mk(i, "macro", 9, 11) for i in range(5)] + [_mk(i, "sa", 9, 8) for i in range(6)] \
        + [_mk(i, "us", 9, 10) for i in range(4)] + [_mk(0, "other", 9, 9)] \
        + [_mk(i, "us", 8, 12) for i in range(3)] + [_mk(i, "sa", 8, 6) for i in range(2)]
    ranked, dropped = an.rank_items(items, now=NOW)
    assert dropped == 0 and len(ranked) == len(items)
    days = [i["day_local"] for i in ranked]
    assert days == sorted(days, reverse=True) and days[:16] == ["2026-09-09"] * 16
    assert [i["market"] for i in ranked[:16]] == ["sa"] * 6 + ["us"] * 4 + ["macro"] * 5 + ["other"]
    assert [i["market"] for i in ranked[16:]] == ["sa"] * 2 + ["us"] * 3
    sa_stamps = [i["published_utc"] for i in ranked[:6]]
    assert sa_stamps == sorted(sa_stamps, reverse=True)
    assert all(i["is_today"] for i in ranked[:16]) and not any(i["is_today"] for i in ranked[16:])
    assert ranked[0]["age_hours"] == pytest.approx(4.0 - 5 / 60, abs=0.1)   # 08:05Z vs NOW 12:00Z
    # a Riyadh date boundary: 22:30Z on the 8th is the 9th in Riyadh
    late = {"id": "x", "market": "sa", "published_utc": "2026-09-08T22:30:00Z"}
    r2, _ = an.rank_items([late], now=NOW)
    assert r2[0]["day_local"] == "2026-09-09" and r2[0]["is_today"]


def test_rank_items_caps_today_quota_and_per_day():
    today = [_mk(i, "sa", 9) for i in range(50)] + [_mk(i, "us", 9) for i in range(50)] \
        + [_mk(i, "macro", 9) for i in range(30)] + [_mk(i, "other", 9) for i in range(5)]
    yesterday = [_mk(i, "sa", 8) for i in range(70)] + [_mk(i, "us", 8) for i in range(30)]
    older = [_mk(i, m, d) for d in (7, 6, 5, 4, 3) for m in ("sa", "us") for i in range(40)]
    ranked, dropped = an.rank_items(today + yesterday + older, now=NOW)
    by_day = {}
    for it in ranked:
        by_day.setdefault(it["day_local"], []).append(it["market"])
    t = by_day["2026-09-09"]
    # reservations 40/40/20 honoured, then the 5 leftover slots (110 - 105) go to sa in market order
    assert t.count("sa") == 45 and t.count("us") == 40 and t.count("macro") == 20 and t.count("other") == 5
    assert t == ["sa"] * 45 + ["us"] * 40 + ["macro"] * 20 + ["other"] * 5 and len(t) == sum(an.TODAY_QUOTA.values())
    y = by_day["2026-09-08"]
    assert len(y) == 60 and y.count("us") == 20 and y.count("sa") == 40   # quota pass then fill in market order
    assert len(ranked) == 300 == an.MAX_TOTAL and dropped == len(today + yesterday + older) - 300
    assert all(len(v) <= 60 for d, v in by_day.items() if d != "2026-09-09")
    assert list(by_day) == sorted(by_day, reverse=True)
    # Saudi-only day still fills to the day cap through the second pass
    r2, _ = an.rank_items([_mk(i, "sa", 8) for i in range(80)], now=NOW)
    assert len(r2) == 60


def test_analyze_stats_relevance_and_summary_policy(raw):
    res = _analyze_items(raw["items"] + [SPORTS])
    assert res["dropped"] == 1 and res["stats"]["dropped_irrelevant"] == 1
    assert res["stats"]["kept"] == len(res["items"]) and set(res["stats"]["by_market"]) == set(an.MARKETS)
    assert all("relevance" in i and 0 <= i["relevance"] <= 1 for i in res["items"])
    for day in {i["day_local"] for i in res["items"]}:
        markets = [i["market"] for i in res["items"] if i["day_local"] == day]
        assert markets == sorted(markets, key=lambda m: an.MARKET_ORDER[m])
    excerpts = {an.item_id(i): an._excerpt(i) for i in raw["items"]}
    for it in res["items"]:
        assert it["lang"] in ("ar", "en") and an.has_arabic(it["brief_ar"]) and it["brief_origin"] == "template"
        assert not set(it) & set(an.VIEW_DROP_FIELDS)
        # original wording: no 8-word run shared with the feed text, and the source name kept for attribution
        assert an.shared_ngram(it["brief_ar"], excerpts.get(it["id"], "")) is None
        assert it["source"] in it["brief_ar"] and it["link"] == it["link"]
    no_excerpt = dict(SPORTS, title="أرباح شركة سعودية ترتفع", source_excerpt="", lang="ar", relevance=1.0)
    it = _analyze_items([no_excerpt])["items"][0]
    assert it["title"] == "أرباح شركة سعودية ترتفع" and it["lang"] == "ar" and it["brief_ar"].startswith("خبر من")


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


# ---------------------------------------------------------------- US market: feeds, tickers, classification
US_FEED = [{"name": "CNBC – Markets", "url": os.path.join(FIXTURES, "us_rss_sample.xml"), "priority": 3, "market_hint": "us"}]


@pytest.fixture(scope="module")
def us_raw():
    return fn.run(US_FEED, now=NOW)  # default window = 168h


def test_us_rss_fetch_window_and_hint(us_raw):
    assert us_raw["window_hours"] == 168 and us_raw["feeds_ok"] == 1
    titles = [i["title"] for i in us_raw["items"]]
    assert "Old US market story that must expire from the archive" not in titles   # 20 Aug < NOW - 7d
    assert "Visa applications surge for Hajj season, travel agents say" not in titles  # no finance term
    assert titles[0].startswith("Nvidia shares jump") and len(titles) == 5
    assert all(i["market_hint"] == "us" for i in us_raw["items"])
    nv = us_raw["items"][0]
    assert nv["link"] == "https://www.cnbc.com/2026/09/09/nvidia-earnings-q2.html?utm_source=rss"
    assert "<p>" not in nv["source_excerpt"] and nv["lang"] == "en"


def test_fetch_cap_reserves_us_slots():
    sa = [{"priority": 1, "published_utc": f"2026-09-09T08:{i:02d}:00Z", "t": f"sa{i}"} for i in range(50)]
    us = [{"priority": 3, "published_utc": f"2026-09-09T11:{i:02d}:00Z", "market_hint": "us", "t": f"us{i}"} for i in range(50)]
    kept = fn.apply_cap(sa + us, 60, us_reserve=20)
    assert len(kept) == 60 and sum(1 for i in kept if i.get("market_hint") == "us") == 20
    assert [i["t"] for i in kept][:40] == [f"sa{i}" for i in range(40)]         # input order kept
    assert fn.apply_cap(sa + us, 200, us_reserve=20) == sa + us                 # cap not binding
    few_sa = fn.apply_cap(sa[:5] + us, 40, us_reserve=20)
    assert len(few_sa) == 40 and sum(1 for i in few_sa if i.get("market_hint")) == 35    # unused Saudi slots go to US
    few_us = fn.apply_cap(sa + us[:3], 40, us_reserve=20)
    assert len(few_us) == 40 and sum(1 for i in few_us if not i.get("market_hint")) == 37  # unused US slots go back


def test_us_ticker_matching():
    m = an.build_us_matcher(US_CONSTS)
    codes = lambda t: [x["code"] for x in m(t)]  # noqa: E731
    assert codes("AAPL rises 2%") == ["AAPL"] and codes("aapl rises") == []            # symbols: uppercase only
    assert codes("BRK-B hits a record") == ["BRK-B"] and codes("$TSLA and NASDAQ:MSFT") == ["MSFT", "TSLA"]
    assert codes("META") == ["META"] and codes("Meta analysis of trials") == []          # META only uppercase
    assert codes("Alphabet's earnings beat") == ["GOOGL"] and codes("Google unveils Gemini") == ["GOOGL"]
    assert codes("$V gains") == ["V"] and codes("Caterpillar (CAT) results") == ["CAT"] and codes("NYSE: GE debuts") == ["GE"]
    assert codes("3 PM update from MS Office") == [] and codes("the CAT is here") == []   # short/word-like symbols need markers
    assert codes("Visa applications surge for Hajj") == [] and codes("Visa shares rise after earnings") == ["V"]
    assert codes("apple pie recipe") == [] and codes("Apple stock climbs") == ["AAPL"]   # ordinary-word names need context
    assert codes("Amazon rainforest fires") == [] and codes("Amazon shares fall") == ["AMZN"]
    assert codes("Microsoft and Nvidia lead the rally") == ["MSFT", "NVDA"]
    assert codes("إنفيديا تعلن نتائجها") == ["NVDA"] and codes("أرباح مايكروسوفت وأبل") == ["AAPL", "MSFT"]   # Arabic names
    assert codes("جوجل تطلق نموذجاً جديداً") == ["GOOGL"] and codes("Morgan Stanley upgrades Tesla") == ["MS", "TSLA"]
    assert m("Nvidia")[0] == {"code": "NVDA", "name_ar": "إنفيديا", "sector": "tech", "market": "us"}
    assert an.build_us_matcher([])("NVDA") == []


def test_us_constituents_loader():
    rows = an.load_constituents_us()
    by = {r["code"]: r for r in rows}
    assert len(rows) >= 50 and by["NVDA"]["sector"] == "tech" and by["JPM"]["sector"] == "banks"
    assert by["XOM"]["sector"] == "energy" and by["LLY"]["sector"] == "health" and by["WMT"]["sector"] == "retail"
    assert by["GOOGL"]["sector"] == "telecom" and "جوجل" in by["GOOGL"]["aliases"]
    assert an.load_constituents_us("/nonexistent.json") == []


def test_us_classification_and_impact_market(us_raw):
    res = an.analyze(us_raw, an.load_rules(), an.load_constituents(path=None), constituents_us=US_CONSTS, now=NOW)
    by = {i["title"][:20]: i for i in res["items"]}
    nv = by["Nvidia shares jump 6"]
    assert nv["market"] == "us" and nv["impact_market"] == "us" and nv["signal"] == "pos"
    assert [t["code"] for t in nv["tickers"]] == ["AMD", "NVDA"] and all(t["market"] == "us" for t in nv["tickers"])
    assert {"us_tech_ai", "us_semis", "earnings_up", "guidance_raise"} <= set(nv["rules"]) and nv["impact"]["tech"] >= 2
    assert nv["beneficiary"]["name"].startswith("إنفيديا") and "NVDA" in nv["beneficiary"]["name"]
    dow = by["Dow falls 400 points"]
    assert dow["market"] == "us" and dow["signal"] == "neg" and "us_market_down" in dow["rules"]
    assert "treasury_yields" in dow["rules"] and dow["impact_market"] == "both"   # yields move Saudi rates too
    assert dow["impact"]["tech"] < 0 and dow["impact"]["smallcaps"] < 0
    fed = by["Fed holds rates stea"]
    assert fed["market"] == "us" and fed["impact_market"] == "both" and "fed_generic" in fed["rules"]
    assert "tech_sector" not in fed["rules"]   # " ai " keyword must not match inside "said"/"remains"
    ap = by["Apple (AAPL) unveils"]
    assert ap["market"] == "us" and [t["code"] for t in ap["tickers"]] == ["AAPL", "GOOGL", "MSFT"]
    wk = by["Wall Street week ahe"]
    assert wk["market"] == "us" and [t["code"] for t in wk["tickers"]] == ["GS", "JPM"]
    assert "us_bank_earnings" in wk["rules"] and wk["impact"]["banks"] >= 1 and "transport_sector" not in wk["rules"]
    assert res["stats"]["by_market"]["us"] == 5 and res["stats"]["by_market"]["sa"] == 0
    assert [i["day_local"] for i in res["items"]] == ["2026-09-09", "2026-09-09", "2026-09-08", "2026-09-08", "2026-09-06"]


def test_us_needs_explicit_signal_and_feed_hint():
    base = dict(SPORTS, relevance=1.0, source="CNBC", link="https://www.cnbc.com/x")
    generic_us_feed = dict(base, title="Stocks rally as earnings season kicks off", summary="", market_hint="us")
    generic_no_hint = dict(base, title="Stocks rally as the earnings season begins", summary="", link="https://www.cnbc.com/y")
    visa = dict(base, title="Visa applications surge for Hajj season", summary="Travel demand is up.", market_hint="us", link="https://www.cnbc.com/v", relevance=0.6)
    mixed = dict(base, title="Aramco and Exxon sign LNG supply deal", summary="Saudi Aramco shares rose.", market_hint="us", link="https://www.cnbc.com/m")
    ar_us = dict(base, title="الأسهم الأمريكية تتراجع مع صعود عوائد الخزانة", summary="", lang="ar", link="https://www.cnbc.com/a")
    res = _analyze_items([generic_us_feed, generic_no_hint, visa, mixed, ar_us])
    by = {i["id"]: i for i in res["items"]}
    assert by[an.item_id(generic_us_feed)]["market"] == "us"        # US feed + stock-market words
    assert by[an.item_id(generic_no_hint)]["market"] == "other"     # same text, no explicit US signal
    assert an.item_id(visa) not in by                               # market "other", no ticker -> relevance penalised below the gate
    mx = by[an.item_id(mixed)]
    assert mx["market"] == "sa" and mx["impact_market"] == "both"   # Saudi first; US ticker widens the impact
    assert [(t["code"], t["market"]) for t in mx["tickers"]] == [("2222", "sa"), ("XOM", "us")]
    a = by[an.item_id(ar_us)]
    assert a["market"] == "us" and a["signal"] == "neg" and "us_market_down" in a["rules"] and a["impact_market"] == "both"


# ---------------------------------------------------------------- archive: merge / expire / ids
def test_item_id_stable_on_link_not_title():
    a = {"title": "Aramco Q2 profit", "link": "https://argaam.com/a/1?utm_source=rss"}
    b = {"title": "Aramco Q2 profit (updated)", "link": "https://argaam.com/a/1/"}
    assert an.item_id(a) == an.item_id(b) and len(an.item_id(a)) == 12
    assert an.item_id({"title": "أرامكو: أرباح!", "link": ""}) == an.item_id({"title": "أرامكو أرباح"})
    assert an.item_id(a) != an.item_id({"title": "Aramco Q2 profit", "link": "https://argaam.com/a/2"})


def test_archive_merge_keeps_prior_analysis_and_expires(raw, monkeypatch):
    rules, consts = an.load_rules(), an.load_constituents(path=None)
    first = an.analyze(raw, rules, consts, now=NOW)
    archive = first["_archive"]
    assert len(archive) == len(raw["items"]) and all("impact_market" in i for i in archive)
    # pretend an LLM analysed one archived item earlier; add an expired one and one from 6 days ago
    aramco = next(i for i in archive if i["title"].startswith("أرامكو"))
    aramco.update(brief_ar="موجز من النموذج", brief_origin="llm", analysis="llm")
    expired = dict(aramco, id="old1", title="خبر قديم جداً عن سوق الأسهم", link="https://x.sa/old",
                   published_utc="2026-08-30T10:00:00Z")
    six_days = dict(aramco, id="six1", title="خبر عمره ستة أيام عن سوق الأسهم", link="https://x.sa/six",
                    published_utc="2026-09-03T10:00:00Z", analysis="rules")
    archive = archive + [expired, six_days]
    calls = []
    monkeypatch.setattr(an, "analyze_rules", lambda it, r, m, um=None: (calls.append(it["title"]), an.empty_impact()) and {
        "market": "sa", "signal": "mix", "tickers": [], "lang": "ar",
        "beneficiary": {"name": "—", "why": ""}, "hurt": {"name": "—", "why": ""}, "impact": an.empty_impact(),
        "impact_market": "sa", "cf": 1, "rules": [], "analysis": "rules"})
    new_item = dict(SPORTS, title="سابك توقع اتفاقية جديدة", link="https://x.sa/new", relevance=1.0,
                    published_utc="2026-09-09T11:30:00Z")
    same_title_new_link = dict(new_item, title=aramco["title"], link="https://other.sa/copy")
    second = an.analyze({"items": raw["items"] + [new_item, same_title_new_link]}, rules, consts, archive=archive, now=NOW)
    assert calls == ["سابك توقع اتفاقية جديدة"]                       # only the truly new item was analysed
    st = second["stats"]
    assert st["new_items"] == 1 and st["already_known"] == len(raw["items"]) + 1 and st["expired"] == 1
    assert st["archive_total"] == len(raw["items"]) + 2
    ids = {i["id"] for i in second["_archive"]}
    assert "six1" in ids and "old1" not in ids
    kept = next(i for i in second["_archive"] if i["id"] == aramco["id"])
    assert kept["brief_ar"] == "موجز من النموذج" and kept["brief_origin"] == "llm" and kept["analysis"] == "llm"   # prior analysis untouched
    stamps = [i["published_utc"] for i in second["_archive"]]
    assert stamps == sorted(stamps, reverse=True)                                    # archive: full, newest first
    view_ids = [i["id"] for i in second["items"]]
    assert view_ids[0] == an.item_id(new_item) and "six1" in view_ids               # today first, 6-day-old still in view
    assert second["days"][-1]["day_local"] == "2026-09-03" and second["days"][-1]["label_ar"] == "الخميس 3 سبتمبر"
    assert second["stats"]["by_day"]["2026-09-03"] == 1
    # LLM cost: only new items reach the model
    sent = []
    monkeypatch.setattr(an, "analyze_llm_batch", lambda items, *a, **k: (sent.extend(i["title"] for i in items), {})[1])
    an.analyze({"items": raw["items"] + [new_item]}, rules, consts, archive=second["_archive"], now=NOW, api_key="sk")
    assert sent == []
    an.analyze({"items": [dict(new_item, title="خبر آخر جديد", link="https://x.sa/n2")]}, rules, consts,
               archive=second["_archive"], now=NOW, api_key="sk")
    assert sent == ["خبر آخر جديد"]


def test_load_archive_shapes(tmp_path):
    p = tmp_path / "a.json"
    good = {"id": "a1", "title": "t", "market": "sa", "published_utc": "2026-09-09T00:00:00Z", "link": "https://x/1"}
    p.write_text(json.dumps({"items": [good, {"title": "no market"}, "junk"]}), encoding="utf-8")
    assert [i["id"] for i in an.load_archive(str(p))] == ["a1"]
    p.write_text(json.dumps([dict(good, id=None)]), encoding="utf-8")
    assert an.load_archive(str(p))[0]["id"] == an.item_id(good)
    p.write_text("{not json", encoding="utf-8")
    assert an.load_archive(str(p)) == [] and an.load_archive(str(tmp_path / "missing.json")) == [] and an.load_archive("") == []
    doc = an.archive_document({"generated_at_utc": "x", "days_window": 7, "_archive": [good]})
    assert doc == {"generated_at_utc": "x", "days": 7, "count": 1, "items": [good]}


def test_day_label_and_expiry_helpers():
    assert an.day_label_ar("2026-09-09") == "الأربعاء 9 سبتمبر"
    assert an.day_label_ar("2026-01-01") == "الخميس 1 يناير" and an.day_label_ar("bad") == "bad"
    items = [{"published_utc": "2026-09-02T12:00:00Z"}, {"published_utc": "2026-09-02T11:59:59Z"}, {"published_utc": "?"}]
    kept, n = an.expire_items(items, NOW, 7)
    assert kept == items[:1] and n == 2


# ---------------------------------------------------------------- text policy: own wording, no advice
@pytest.mark.parametrize("src,expected", [
    ("المستفيد الأكبر هو أرامكو", "قد يستفيد أرامكو"),
    ("المتضرر: البنوك", "قد يتأثر سلبًا: البنوك"),
    ("المستفيد الرئيسي قطاع الطاقة والمتضرر الأكبر شركات النقل", "قد يستفيد قطاع الطاقة وقد يتأثر سلبًا شركات النقل"),
    ("سيستفيد القطاع وسترتفع الأسهم بالتأكيد", "قد يستفيد القطاع وقد ترتفع الأسهم على الأرجح"),
    ("ستتضرر البنوك حتماً", "قد تتأثر سلبًا البنوك على الأرجح"),
    ("قد يستفيد قطاع الطاقة", "قد يستفيد قطاع الطاقة"),             # already possibility phrasing: untouched
    ("نتائج إيجابية تدعم السهم والقطاع", "نتائج إيجابية تدعم السهم والقطاع"),
    ("", ""),
])
def test_sanitize_ar(src, expected):
    assert an.sanitize_ar(src) == expected
    assert not an.has_advice_language(an.sanitize_ar(src))


def test_sanitize_ar_removes_advice_words():
    s = "ننصح بشراء السهم، فرصة يجب اقتناصها، توصية: اشترِ الآن ثم بِع"
    assert an.has_advice_language(s)
    out = an.sanitize_ar(s)
    assert not an.has_advice_language(out)
    for w in ("ننصح", "فرصة", "يجب", "توصية", "اشترِ", "بِع"):
        assert w not in out
    assert an.sanitize_ar(None) == "" and an.sanitize_ar("قد قد يستفيد") == "قد يستفيد"


def test_has_advice_language_is_word_bounded_and_diacritics_insensitive():
    assert an.has_advice_language("اشْتَرِ السهم") and an.has_advice_language("هذه فُرْصَة") and an.has_advice_language("بِع الآن")
    assert an.has_advice_language("ويجب الحذر") and an.has_advice_language("التوصية: احتفظ")
    assert not an.has_advice_language("بعد الإعلان ارتفع السهم")          # "بعد" is not "بع"
    assert not an.has_advice_language("أرباح الشركة ترتفع 12% مع توزيعات")
    assert not an.has_advice_language("")


def test_brief_rejection_reason():
    src = "أعلنت أرامكو السعودية عن ارتفاع صافي أرباحها بنسبة 12% في الربع الثاني مع توزيعات نقدية قدرها 20 مليار ريال"
    assert an.brief_rejection_reason("سجّلت الشركة نموًا في الربحية خلال الربع الثاني وأقرّت توزيعات نقدية. قد يستفيد قطاع الطاقة.", src) is None
    assert "copies" in an.brief_rejection_reason("وفق الخبر، أعلنت أرامكو السعودية عن ارتفاع صافي أرباحها بنسبة 12% في الربع الثاني.", src)
    assert "copies" in an.brief_rejection_reason("أَعْلَنَتْ أَرَامْكُو السُّعُودِيَّة عَنْ ارتفاع صافي أرباحها بنسبة 12% في الربع الثاني", src)  # diacritics ignored
    assert an.brief_rejection_reason("The brief is in English only", src) == "empty or not Arabic"
    assert an.brief_rejection_reason("", src) == "empty or not Arabic"
    assert an.brief_rejection_reason("نمو الأرباح فرصة للمستثمرين.", src) == "advice language"
    assert "quote" in an.brief_rejection_reason("قال المصدر «الأرباح ارتفعت بشكل كبير خلال الربع الثاني الحالي بفضل الأسعار».", src)
    assert an.brief_rejection_reason("قال المصدر «الأرباح ارتفعت بقوة».", src) is None    # short quote (<= 6 words) is fine
    assert an.brief_rejection_reason("أعتقد أن الأرباح سترتفع.", src) == "first person"
    assert an.brief_rejection_reason("موجز " * 400, src) == "too long"
    # English sources: case-insensitive
    en = "Brent crude climbed above $100 a barrel on Wednesday after a tanker attack. Bahri shares fell."
    assert an.shared_ngram("brent CRUDE climbed above $100 a barrel on wednesday", en) is not None
    assert an.shared_ngram("خام برنت فوق 100 دولار", en) is None


def test_template_brief_is_original_and_structured_only(raw, us_raw):
    long_excerpt = dict(SPORTS, title="سابك تعلن نتائجها المالية للربع الثاني", relevance=1.0, link="https://x.sa/sabic",
                        source="أرقام", lang="ar",
                        source_excerpt="أعلنت الشركة السعودية للصناعات الأساسية سابك اليوم عن نتائجها المالية للربع الثاني "
                                       "حيث ارتفع صافي الربح بنسبة 30% مقارنة بالفترة نفسها من العام الماضي مدعوماً بتحسن الأسعار")
    items = raw["items"] + us_raw["items"] + [long_excerpt]
    res = _analyze_items(items)
    src_by_id = {an.item_id(i): an._excerpt(i) for i in items}
    assert len(res["items"]) >= len(items) - 1
    for it in res["items"]:
        assert it["brief_origin"] == "template" and it["analysis_note_ar"] == an.ANALYSIS_NOTE_AR
        assert an.shared_ngram(it["brief_ar"], src_by_id[it["id"]]) is None
        assert not an.has_advice_language(it["brief_ar"])
        assert not an.has_advice_language(it["beneficiary"]["why"]) and not an.has_advice_language(it["hurt"]["why"])
        assert not set(it) & set(an.VIEW_DROP_FIELDS) and it["source"] and it["link"] and it["title"]
        # a template never reads the feed text: even 3-word runs of the excerpt (beyond words in the title) do not appear
        excerpt_only = an.word_ngrams(src_by_id[it["id"]], 3) - an.word_ngrams(it["title"], 3)
        assert not (an.word_ngrams(it["brief_ar"], 3) & excerpt_only)
    by = {i["title"]: i for i in res["items"]}
    sabic = by[long_excerpt["title"]]
    assert "سابك (2010)" in sabic["brief_ar"] and "أرقام" in sabic["brief_ar"] and "الأربعاء 9 سبتمبر" in sabic["brief_ar"]
    assert "قد يستفيد" in sabic["brief_ar"]
    # nothing known -> explicit "no parties" sentence, still grammatical
    bare = an.template_brief_ar({"source": "مصدر", "published_riyadh": "2026-09-09 10:00:00", "market": "macro",
                                 "signal": "mix", "tickers": [], "impact": an.empty_impact(),
                                 "beneficiary": {"name": "—", "why": ""}, "hurt": {"name": "—", "why": ""}})
    assert bare == ("خبر من مصدر بتاريخ الأربعاء 9 سبتمبر يخص الاقتصاد الكلي والسلع. "
                    "التصنيف الآلي للإشارة: مختلط أو محايد. " + an.NO_PARTIES_AR + ".")
    # names coming from the LLM/rules are sanitized inside the template too
    t = an.template_brief_ar({"source": "س", "published_riyadh": "2026-09-09", "market": "sa", "signal": "neg", "cf": 2,
                              "tickers": [{"code": "4030", "name_ar": "البحري"}], "beneficiary": {"name": "المستفيد الأكبر أرامكو"},
                              "hurt": {"name": "المتضرر البحري"}})
    assert "قد يستفيد: قد يستفيد أرامكو" not in t and "أرامكو" in t and "البحري (4030)" in t and "المتضرر" not in t


def test_llm_copied_or_advice_brief_falls_back_to_template(raw, monkeypatch):
    rules, consts = an.load_rules(), an.load_constituents(path=None)
    base = an.analyze(raw, rules, consts, now=NOW)
    oil = next(i for i in base["items"] if i["title"].startswith("Oil prices rise"))
    aramco = next(i for i in base["items"] if i["title"].startswith("أرامكو"))
    rajhi = next(i for i in base["items"] if i["title"].startswith("الراجحي"))
    excerpt = {an.item_id(i): an._excerpt(i) for i in raw["items"]}
    assert len(excerpt[oil["id"]].split()) >= 8
    ok = {k: 0 for k in an.SECTOR_KEYS} | {"why": "شرح"}
    body = {"results": [
        # verbatim copy of the English source excerpt inside an Arabic brief -> rejected
        {"id": oil["id"], "market": "macro", "signal": "neg", "tickers": [], "impact": ok, "cf": 2,
         "brief_ar": "بحسب المصدر: " + excerpt[oil["id"]] + " وهذا موجز.",
         "beneficiary": {"name": "أرامكو (2222)", "why": "سيستفيد من ارتفاع البرميل"},
         "hurt": {"name": "البحري (4030)", "why": "ننصح ببيع السهم"}},
        # advice vocabulary -> rejected
        {"id": aramco["id"], "market": "sa", "signal": "pos", "tickers": [], "impact": ok, "cf": 3,
         "brief_ar": "نمو الأرباح فرصة للمستثمرين ويجب اقتناصها.", "beneficiary": {"name": "أ", "why": "ب"}, "hurt": {"name": "—", "why": ""}},
        # original wording -> accepted
        {"id": rajhi["id"], "market": "sa", "signal": "pos", "tickers": [], "impact": ok, "cf": 3,
         "brief_ar": "وقّع المصرف اتفاقًا تمويليًا كبيرًا في قطاع الإسكان. قد ينعكس ذلك على محفظته التمويلية تدريجيًا.",
         "beneficiary": {"name": "الراجحي (1120)", "why": "قد يستفيد من نمو المحفظة"}, "hurt": {"name": "—", "why": ""}},
    ]}
    calls = []

    def fake_post(payload, api_key, use_fallbacks, timeout=300):
        calls.append(payload)
        return {"stop_reason": "end_turn", "model": payload["model"], "usage": {},
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}

    monkeypatch.setattr(an, "_post_messages", fake_post)
    res = an.analyze(raw, rules, consts, api_key="sk-test", batch_size=50, now=NOW)
    assert len(calls) == 1
    by = {i["id"]: i for i in res["items"]}
    o, a, r = by[oil["id"]], by[aramco["id"]], by[rajhi["id"]]
    assert o["analysis"] == "llm" and o["market"] == "macro"                       # the rest of the analysis is kept
    assert o["brief_origin"] == "template" and an.shared_ngram(o["brief_ar"], excerpt[oil["id"]]) is None
    assert o["beneficiary"]["why"] == "قد يستفيد من ارتفاع البرميل"                # certainty -> possibility
    assert o["hurt"]["why"] == ""                                                  # advice language blanked
    assert a["brief_origin"] == "template" and not an.has_advice_language(a["brief_ar"])
    assert r["brief_origin"] == "llm" and r["brief_ar"].startswith("وقّع المصرف")
    assert all(i["analysis_note_ar"] == an.ANALYSIS_NOTE_AR and not set(i) & set(an.VIEW_DROP_FIELDS) for i in res["items"])
    assert all("source_excerpt" in i for i in res["_archive"] if i["id"] in (o["id"], a["id"], r["id"]))


def test_legacy_summary_key_is_migrated_to_source_excerpt():
    old = dict(SPORTS, title="سابك تعلن نتائجها المالية", relevance=1.0, link="https://x.sa/legacy",
               summary="نص من المصدر بصيغة الحقل القديم summary")
    res = _analyze_items([old])
    arc = res["_archive"][0]
    assert arc["source_excerpt"] == old["summary"] and "summary" not in arc
    assert "summary" not in res["items"][0] and "source_excerpt" not in res["items"][0]


def test_legacy_archive_fields_are_migrated(raw):
    rules, consts = an.load_rules(), an.load_constituents(path=None)
    first = an.analyze(raw, rules, consts, now=NOW)
    a, b, c = [dict(i) for i in first["_archive"][:3]]
    for x in (a, b, c):
        x["summary"] = x.pop("source_excerpt")                      # old key
    a.update(summary_ar=a["summary"], analysis="rules")             # rules mode used to copy the feed text
    b.update(summary_ar="موجز قديم كتبه النموذج بكلماته عن الخبر.", analysis="llm")
    c.update(summary_ar="وفق المصدر: " + c["summary"], analysis="llm")   # old LLM summary that quotes the feed
    for x in (a, b, c):
        x.pop("brief_ar", None); x.pop("brief_origin", None)
    c_excerpt = c["summary"]                                        # archive dicts are migrated in place
    res = an.analyze({"items": []}, rules, consts, archive=[a, b, c], now=NOW)
    arc = {i["id"]: i for i in res["_archive"]}
    assert all("summary" not in i and "summary_ar" not in i and "source_excerpt" in i for i in arc.values())
    assert arc[a["id"]]["brief_origin"] == "template"
    assert arc[b["id"]]["brief_origin"] == "llm" and arc[b["id"]]["brief_ar"] == "موجز قديم كتبه النموذج بكلماته عن الخبر."
    assert arc[c["id"]]["brief_origin"] == "template" and an.shared_ngram(arc[c["id"]]["brief_ar"], c_excerpt) is None
    assert all(not set(i) & set(an.VIEW_DROP_FIELDS) and i["analysis_note_ar"] for i in res["items"])
