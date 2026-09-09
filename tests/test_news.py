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
    assert it["market"] == "macro"
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
    assert [(r["code"], r["sector"]) for r in rows] == [("2222", "energy"), ("1120", "banks")]
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
