#!/usr/bin/env python3
"""Enrich raw news items with market/signal/tickers/impact for the dashboard.

Usage:
    python scripts/analyze_news.py --in data/news_raw.json --out data/news.json \
        [--market data/market.json] [--rules scripts/impact_rules.json] \
        [--constituents scripts/constituents.json] [--no-llm] [--batch 10]

Modes
  * LLM mode  - when ANTHROPIC_API_KEY is set (and --no-llm is absent): items are
                sent in batches to the Claude Messages API (plain `requests`,
                no SDK) asking for strict JSON; every returned item is validated
                and any failure falls back to rules mode for that batch/item.
  * Rules mode - deterministic keyword rules from scripts/impact_rules.json.

Output schema (data/news.json):
{
  "generated_at_utc", "analysis_mode": "llm"|"rules"|"llm+rules",
  "sectors": {key: arabic_label},
  "items": [ raw item fields + {
      "id", "market": "sa"|"macro"|"us"|"other", "signal": "pos"|"neg"|"mix",
      "tickers": [{"code","name_ar"}], "summary_ar", "lang": "ar"|"en",
      "beneficiary": {"name","why"}, "hurt": {"name","why"},
      "impact": {<14 sector keys>: int -3..3, "why": str}, "cf": 1..3,
      "rules": [rule ids], "analysis": "llm"|"rules", "relevance": 0..1
  }],   # ordered: market=="sa" first (newest first), then macro, us, other; non-Saudi capped at 25, total 60
  "dropped": int, "stats": {input, kept, dropped_irrelevant, dropped_cap, by_market, llm_items, fetch_dropped}
}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

log = logging.getLogger("analyze_news")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # relevance gate + time correction live in fetch_news.py (same directory)
    from fetch_news import RELEVANCE_MIN, adjust_future_time, relevance_score
except Exception:  # pragma: no cover
    RELEVANCE_MIN = 0.35
    relevance_score = None  # type: ignore
    adjust_future_time = None  # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "impact_rules.json")
DEFAULT_CONSTITUENTS = os.path.join(HERE, "constituents.json")

SECTOR_KEYS = ["energy", "banks", "petrochem", "insurance", "transport", "realestate",
               "cement", "tech", "smallcaps", "telecom", "health", "retail", "food", "utilities"]
SECTOR_LABELS_AR = {
    "energy": "الطاقة", "banks": "البنوك", "petrochem": "البتروكيماويات", "insurance": "التأمين",
    "transport": "النقل", "realestate": "العقار", "cement": "الأسمنت", "tech": "التقنية",
    "smallcaps": "الشركات الصغيرة", "telecom": "الاتصالات", "health": "الرعاية الصحية",
    "retail": "التجزئة", "food": "الأغذية", "utilities": "المرافق",
}
MARKETS = ("sa", "macro", "us", "other")
SIGNALS = ("pos", "neg", "mix")

# Used only when scripts/constituents.json is missing at runtime.
BUILTIN_CONSTITUENTS: list[dict[str, Any]] = [
    {"code": "2222", "name_ar": "أرامكو السعودية", "name_en": "Saudi Aramco", "sector": "energy", "aliases": ["أرامكو", "aramco"]},
    {"code": "1120", "name_ar": "مصرف الراجحي", "name_en": "Al Rajhi Bank", "sector": "banks", "aliases": ["الراجحي", "al rajhi", "alrajhi"]},
    {"code": "1180", "name_ar": "الأهلي السعودي", "name_en": "Saudi National Bank", "sector": "banks", "aliases": ["البنك الأهلي", "الأهلي", "snb", "saudi national bank"]},
    {"code": "1010", "name_ar": "بنك الرياض", "name_en": "Riyad Bank", "sector": "banks", "aliases": ["riyad bank"]},
    {"code": "1150", "name_ar": "الإنماء", "name_en": "Alinma Bank", "sector": "banks", "aliases": ["مصرف الإنماء", "alinma"]},
    {"code": "1060", "name_ar": "الأول", "name_en": "Saudi Awwal Bank", "sector": "banks", "aliases": ["البنك الأول", "sab", "saudi awwal"]},
    {"code": "1050", "name_ar": "البنك السعودي الفرنسي", "name_en": "Banque Saudi Fransi", "sector": "banks", "aliases": ["الفرنسي", "saudi fransi", "bsf"]},
    {"code": "1140", "name_ar": "البلاد", "name_en": "Bank AlBilad", "sector": "banks", "aliases": ["بنك البلاد", "albilad"]},
    {"code": "2010", "name_ar": "سابك", "name_en": "SABIC", "sector": "petrochem", "aliases": ["sabic"]},
    {"code": "2020", "name_ar": "سابك للمغذيات الزراعية", "name_en": "SABIC Agri-Nutrients", "sector": "petrochem", "aliases": ["sabic agri"]},
    {"code": "2290", "name_ar": "ينساب", "name_en": "Yansab", "sector": "petrochem", "aliases": ["yansab"]},
    {"code": "2350", "name_ar": "كيان السعودية", "name_en": "Saudi Kayan", "sector": "petrochem", "aliases": ["كيان", "kayan"]},
    {"code": "2310", "name_ar": "سبكيم", "name_en": "Sipchem", "sector": "petrochem", "aliases": ["sipchem"]},
    {"code": "2380", "name_ar": "بترو رابغ", "name_en": "Petro Rabigh", "sector": "petrochem", "aliases": ["petro rabigh", "رابغ"]},
    {"code": "1211", "name_ar": "معادن", "name_en": "Ma'aden", "sector": "petrochem", "aliases": ["maaden", "ma'aden"]},
    {"code": "7010", "name_ar": "إس تي سي", "name_en": "stc", "sector": "telecom", "aliases": ["الاتصالات السعودية", "stc group", "stc"]},
    {"code": "7020", "name_ar": "موبايلي", "name_en": "Mobily", "sector": "telecom", "aliases": ["mobily", "اتحاد اتصالات"]},
    {"code": "7030", "name_ar": "زين السعودية", "name_en": "Zain KSA", "sector": "telecom", "aliases": ["زين", "zain"]},
    {"code": "7200", "name_ar": "سلوشنز", "name_en": "solutions by stc", "sector": "tech", "aliases": ["solutions by stc", "حلول إس تي سي"]},
    {"code": "7203", "name_ar": "علم", "name_en": "Elm", "sector": "tech", "aliases": ["شركة علم", "elm"]},
    {"code": "8010", "name_ar": "التعاونية", "name_en": "Tawuniya", "sector": "insurance", "aliases": ["tawuniya"]},
    {"code": "8210", "name_ar": "بوبا العربية", "name_en": "Bupa Arabia", "sector": "insurance", "aliases": ["بوبا", "bupa"]},
    {"code": "4030", "name_ar": "البحري", "name_en": "Bahri", "sector": "transport", "aliases": ["bahri"]},
    {"code": "4260", "name_ar": "بدجت السعودية", "name_en": "Budget Saudi", "sector": "transport", "aliases": ["بدجت", "budget saudi"]},
    {"code": "4261", "name_ar": "ذيب", "name_en": "Theeb Rent a Car", "sector": "transport", "aliases": ["theeb"]},
    {"code": "4263", "name_ar": "سال", "name_en": "SAL Logistics", "sector": "transport", "aliases": ["سال للخدمات اللوجستية", "sal logistics"]},
    {"code": "4300", "name_ar": "دار الأركان", "name_en": "Dar Al Arkan", "sector": "realestate", "aliases": ["dar al arkan"]},
    {"code": "4250", "name_ar": "جبل عمر", "name_en": "Jabal Omar", "sector": "realestate", "aliases": ["jabal omar"]},
    {"code": "4321", "name_ar": "المركز الكندي", "name_en": "Cenomi Centers", "sector": "realestate", "aliases": ["cenomi centers", "سينومي سنترز"]},
    {"code": "4324", "name_ar": "بنان", "name_en": "Banan", "sector": "realestate", "aliases": ["بنان العقارية", "banan"]},
    {"code": "3020", "name_ar": "أسمنت اليمامة", "name_en": "Yamama Cement", "sector": "cement", "aliases": ["yamama cement"]},
    {"code": "3030", "name_ar": "أسمنت السعودية", "name_en": "Saudi Cement", "sector": "cement", "aliases": ["saudi cement"]},
    {"code": "3060", "name_ar": "أسمنت ينبع", "name_en": "Yanbu Cement", "sector": "cement", "aliases": ["yanbu cement"]},
    {"code": "2090", "name_ar": "الجبس", "name_en": "National Gypsum", "sector": "cement", "aliases": ["الجبس الوطني", "national gypsum"]},
    {"code": "3004", "name_ar": "أسمنت الشمالية", "name_en": "Northern Region Cement", "sector": "cement", "aliases": ["northern cement", "الشمالية للأسمنت"]},
    {"code": "3080", "name_ar": "أسمنت الشرقية", "name_en": "Eastern Province Cement", "sector": "cement", "aliases": ["eastern cement"]},
    {"code": "3050", "name_ar": "أسمنت الجنوبية", "name_en": "Southern Province Cement", "sector": "cement", "aliases": ["southern cement"]},
    {"code": "3010", "name_ar": "أسمنت العربية", "name_en": "Arabian Cement", "sector": "cement", "aliases": ["arabian cement"]},
    {"code": "3040", "name_ar": "أسمنت القصيم", "name_en": "Qassim Cement", "sector": "cement", "aliases": ["qassim cement"]},
    {"code": "4001", "name_ar": "أسواق عبدالله العثيم", "name_en": "Abdullah Al Othaim Markets", "sector": "retail", "aliases": ["العثيم", "al othaim", "othaim"]},
    {"code": "2223", "name_ar": "لوبريف", "name_en": "Luberef", "sector": "energy", "aliases": ["luberef"]},
    {"code": "2030", "name_ar": "المصافي", "name_en": "Saudi Arabia Refineries", "sector": "energy", "aliases": ["sarco", "المصافي العربية"]},
    {"code": "4142", "name_ar": "الرياض للكابلات", "name_en": "Riyadh Cables", "sector": "smallcaps", "aliases": ["riyadh cables"]},
    {"code": "1182", "name_ar": "أملاك", "name_en": "Amlak International", "sector": "banks", "aliases": ["amlak"]},
    {"code": "4292", "name_ar": "أرامكو لزيوت الأساس", "name_en": "Aramco Base Oil", "sector": "energy", "aliases": []},
    {"code": "4700", "name_ar": "الرياض ريت", "name_en": "Riyad REIT", "sector": "realestate", "aliases": ["riyad reit"]},
    {"code": "4071", "name_ar": "العربية للتعهدات", "name_en": "Arabian Contracting Services", "sector": "smallcaps", "aliases": ["العربية للإعلانات", "arabian contracting"]},
    {"code": "2140", "name_ar": "أيان", "name_en": "Ayyan Investment", "sector": "smallcaps", "aliases": ["ayyan"]},
    {"code": "9408", "name_ar": "القصيبي للخدمات", "name_en": "Gosaibi Services", "sector": "smallcaps", "aliases": ["gosaibi"]},
    {"code": "4013", "name_ar": "سليمان الحبيب", "name_en": "Dr. Sulaiman Al Habib", "sector": "health", "aliases": ["الحبيب الطبية", "al habib"]},
    {"code": "4002", "name_ar": "المواساة", "name_en": "Mouwasat", "sector": "health", "aliases": ["mouwasat"]},
    {"code": "4004", "name_ar": "دلة الصحية", "name_en": "Dallah Health", "sector": "health", "aliases": ["دلة", "dallah"]},
    {"code": "4190", "name_ar": "جرير", "name_en": "Jarir", "sector": "retail", "aliases": ["jarir"]},
    {"code": "4240", "name_ar": "الحكير", "name_en": "Cenomi Retail", "sector": "retail", "aliases": ["فواز الحكير", "cenomi retail"]},
    {"code": "4164", "name_ar": "النهدي", "name_en": "Nahdi Medical", "sector": "retail", "aliases": ["nahdi"]},
    {"code": "2280", "name_ar": "المراعي", "name_en": "Almarai", "sector": "food", "aliases": ["almarai"]},
    {"code": "6010", "name_ar": "نادك", "name_en": "NADEC", "sector": "food", "aliases": ["nadec"]},
    {"code": "6001", "name_ar": "حلواني", "name_en": "Halwani Bros", "sector": "food", "aliases": ["halwani"]},
    {"code": "2050", "name_ar": "صافولا", "name_en": "Savola", "sector": "food", "aliases": ["savola"]},
    {"code": "5110", "name_ar": "كهرباء السعودية", "name_en": "Saudi Electricity", "sector": "utilities", "aliases": ["الكهرباء", "saudi electricity", "sec"]},
    {"code": "2082", "name_ar": "أكوا باور", "name_en": "ACWA Power", "sector": "utilities", "aliases": ["acwa power", "acwa"]},
    {"code": "2081", "name_ar": "الخريف لتقنية المياه", "name_en": "Alkhorayef Water", "sector": "utilities", "aliases": ["الخريف", "alkhorayef"]},
    {"code": "4031", "name_ar": "الخدمات الأرضية", "name_en": "Saudi Ground Services", "sector": "transport", "aliases": ["saudi ground services"]},
    {"code": "4200", "name_ar": "الدريس", "name_en": "Aldrees", "sector": "retail", "aliases": ["aldrees"]},
    {"code": "1303", "name_ar": "صناعات كهربائية", "name_en": "Electrical Industries", "sector": "smallcaps", "aliases": ["الصناعات الكهربائية"]},
]

# constituents.json sector names (Arabic or English, from another agent) -> our keys
SECTOR_NAME_MAP = [
    ("طاقة", "energy"), ("energy", "energy"), ("بنوك", "banks"), ("bank", "banks"),
    ("الخدمات المالية", "banks"), ("financial", "banks"), ("تمويل", "banks"),
    ("أسمنت", "cement"), ("اسمنت", "cement"), ("cement", "cement"),
    ("بتروكيم", "petrochem"), ("مواد أساسية", "petrochem"), ("مواد اساسية", "petrochem"), ("أساسية", "petrochem"), ("basic material", "petrochem"),
    ("material", "petrochem"), ("petrochem", "petrochem"), ("chemical", "petrochem"),
    ("تأمين", "insurance"), ("تامين", "insurance"), ("insurance", "insurance"),
    ("نقل", "transport"), ("transport", "transport"), ("logistic", "transport"),
    ("عقار", "realestate"), ("real estate", "realestate"), ("reit", "realestate"), ("ريت", "realestate"),
    ("تقنية", "tech"), ("برمجيات", "tech"), ("tech", "tech"), ("software", "tech"),
    ("اتصالات", "telecom"), ("telecom", "telecom"), ("media", "telecom"), ("إعلام", "telecom"),
    ("صحية", "health"), ("صحة", "health"), ("health", "health"), ("pharma", "health"), ("أدوية", "health"),
    ("تجزئة", "retail"), ("retail", "retail"), ("consumer", "retail"), ("استهلاكية", "retail"),
    ("أغذية", "food"), ("اغذية", "food"), ("غذاء", "food"), ("food", "food"), ("agri", "food"), ("زراع", "food"),
    ("مرافق", "utilities"), ("utilit", "utilities"), ("كهرباء", "utilities"), ("مياه", "utilities"),
    ("نمو", "smallcaps"), ("nomu", "smallcaps"), ("parallel", "smallcaps"),
]

# ---------------------------------------------------------------------------
# Text normalization (shared by rules + ticker matching)
# ---------------------------------------------------------------------------
# Arabic harakat / tanween / superscript alef / Quranic marks / tatweel (explicit escapes:
# a literal range would silently swallow the whole Arabic block).
_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _DIACRITICS.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", t).strip()


def has_arabic(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text or ""))


def detect_lang(text: str) -> str:
    ar = len(re.findall(r"[؀-ۿ]", text or ""))
    en = len(re.findall(r"[A-Za-z]", text or ""))
    return "ar" if ar >= en and ar > 0 else "en"


def item_id(item: dict[str, Any]) -> str:
    key = (item.get("link") or "") + "|" + norm(item.get("title", ""))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Loading rules & constituents
# ---------------------------------------------------------------------------
def load_rules(path: str = DEFAULT_RULES) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        rules = json.load(fh)
    sectors = rules.get("sectors") or {}
    for k in SECTOR_KEYS:
        sectors.setdefault(k, SECTOR_LABELS_AR[k])
    rules["sectors"] = sectors
    # Pre-normalize keywords once (deterministic order preserved).
    for r in rules.get("rules", []):
        r["_kw"] = [norm(k) for k in r.get("keywords", []) if k]
        r["impact"] = {k: int(v) for k, v in (r.get("impact") or {}).items() if k in SECTOR_KEYS}
    rules["_market_kw"] = {m: [norm(k) for k in kws] for m, kws in (rules.get("market_keywords") or {}).items()
                           if isinstance(kws, list)}
    amb: dict[str, dict[str, list[str]]] = {}
    for k, ctx in (rules.get("ambiguous_context") or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(ctx, list):
            ctx = {"any": ctx}
        if isinstance(ctx, dict):
            amb[norm(k)] = {"any": [norm(c) for c in ctx.get("any", [])],
                            "none": [norm(c) for c in ctx.get("none", [])]}
    rules["_ambiguous"] = amb
    return rules


def keyword_ok(kw_norm: str, text_norm: str, ambiguous: dict[str, dict[str, list[str]]]) -> bool:
    """A keyword hit counts unless it contains an ambiguous word whose required
    context ("any") is absent or whose excluding context ("none") is present."""
    for term, ctx in ambiguous.items():
        if re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", kw_norm):
            if ctx.get("any") and not any(c in text_norm for c in ctx["any"]):
                return False
            if any(c in text_norm for c in ctx.get("none", [])):
                return False
    return True


COMPANY_CONTEXT = [norm(c) for c in ("سهم", "أسهم", "شركة", "الشركة", "تداول", "تاسي", "stock", "shares", "share",
                                     "company", "tadawul", "tasi", "listed", "المدرجة", "أرباح", "profit", "earnings")]
# Aliases that are common words / other entities (football clubs, cities...): need company context or the code.
AMBIGUOUS_ALIASES = {norm(a) for a in ("كيان", "kayan", "علم", "elm", "الأول", "البلاد", "الأهلي", "زين", "zain",
                                        "سال", "ذيب", "دلة", "الجبس", "بنان", "الخريف", "المواساة", "ناس",
                                        "الكهرباء", "الفرنسي", "الإنماء", "جرير")}


def map_sector(name: str | None) -> str | None:
    if not name:
        return None
    n = norm(str(name))
    if n in SECTOR_KEYS:
        return n
    for needle, key in SECTOR_NAME_MAP:
        if norm(needle) in n:
            return key
    return None


def _normalize_constituent(raw: dict[str, Any], code_hint: str | None = None) -> dict[str, Any] | None:
    code = str(raw.get("code") or raw.get("symbol") or raw.get("ticker") or raw.get("tasi_code") or code_hint or "").strip()
    code = re.sub(r"\.(SE|SR)$", "", code, flags=re.I)
    if not re.fullmatch(r"\d{4}", code):
        return None
    name_ar = raw.get("name_ar") or raw.get("nameAr") or raw.get("short_name_ar") or raw.get("ar") or ""
    name_en = raw.get("name_en") or raw.get("nameEn") or raw.get("short_name_en") or raw.get("en") or raw.get("name") or ""
    if not name_ar and has_arabic(str(raw.get("name") or "")):
        name_ar = raw.get("name")
    aliases = list(raw.get("aliases") or [])
    for k in ("short_name_ar", "short_name_en", "short_ar", "short_en", "full_name_ar", "full_name_en"):
        if raw.get(k):
            aliases.append(str(raw[k]))
    sector = map_sector(raw.get("sector") or raw.get("sector_ar") or raw.get("sector_en") or raw.get("industry"))
    if raw.get("market") and "nomu" in str(raw["market"]).lower():
        sector = sector or "smallcaps"
    return {"code": code, "name_ar": str(name_ar or name_en or code), "name_en": str(name_en or ""),
            "sector": sector, "aliases": [str(a) for a in aliases if a]}


def load_constituents(path: str | None = DEFAULT_CONSTITUENTS) -> list[dict[str, Any]]:
    data = None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read constituents %s: %s", path, exc)
    if data is None:
        log.info("constituents.json not found; using built-in list (%d companies)", len(BUILTIN_CONSTITUENTS))
        return [dict(c) for c in BUILTIN_CONSTITUENTS]

    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for key in ("constituents", "companies", "items", "data", "tickers"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if isinstance(data, dict):  # {code: {...}} or {code: "name"}
        for k, v in data.items():
            if isinstance(v, dict):
                c = _normalize_constituent(v, code_hint=k)
            else:
                c = _normalize_constituent({"name": str(v)}, code_hint=k)
            if c:
                rows.append(c)
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, dict):
                c = _normalize_constituent(v)
                if c:
                    rows.append(c)
    if not rows:
        log.warning("constituents.json had no usable rows; using built-in list")
        return [dict(c) for c in BUILTIN_CONSTITUENTS]
    rows = merge_builtin(rows)
    log.info("loaded %d constituents from %s (+ built-in aliases/rows)", len(rows), path)
    return rows


def merge_builtin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Supplement file rows with the built-in list: aliases (short names such as
    'الراجحي' for 'مصرف الراجحي') and sector fall-backs are added to matching
    codes, and built-in companies missing from the file are appended. Names from
    the file are never overridden."""
    by_code = {r["code"]: r for r in rows}
    for b in BUILTIN_CONSTITUENTS:
        r = by_code.get(b["code"])
        if r is None:
            rows.append(dict(b))
            by_code[b["code"]] = rows[-1]
            continue
        extra = [b["name_ar"], b["name_en"]] + list(b.get("aliases") or [])
        have = {norm(a) for a in [r.get("name_ar", ""), r.get("name_en", "")] + list(r.get("aliases") or [])}
        r["aliases"] = list(r.get("aliases") or []) + [a for a in extra if a and norm(a) not in have]
        if not r.get("sector"):
            r["sector"] = b.get("sector")
    return rows


# ---------------------------------------------------------------------------
# Ticker matching
# ---------------------------------------------------------------------------
_GENERIC_ALIASES = {"sec", "sab", "stc", "sal", "cma", "pif"}  # only match uppercase / with code


def build_matcher(constituents: list[dict[str, Any]]):
    by_code = {c["code"]: c for c in constituents}
    patterns: list[tuple[re.Pattern, dict[str, Any]]] = []
    for c in constituents:
        names = [c.get("name_ar"), c.get("name_en")] + list(c.get("aliases") or [])
        for n in names:
            n = (n or "").strip()
            if len(n) < 3:
                continue
            key = norm(n)
            if key in _GENERIC_ALIASES or len(key) < 3:
                continue
            # Arabic definite article: allow "ال" + "و/ب/ل" prefixes before the name.
            pat = r"(?<![\w])(?:و|ب|ل|ال|وال|بال)?" + re.escape(key) + r"(?![\w])"
            ambiguous = bool(c.get("ambiguous")) or key in AMBIGUOUS_ALIASES or re.sub(r"^ال", "", key) in AMBIGUOUS_ALIASES
            patterns.append((re.compile(pat), c, ambiguous))

    def match(text: str) -> list[dict[str, Any]]:
        t = norm(text)
        has_ctx = any(re.search(r"(?<![\w])(?:و|ب|ل|ال|وال|بال)?" + re.escape(c) + r"(?![\w])", t) for c in COMPANY_CONTEXT)
        found: dict[str, dict[str, Any]] = {}
        # explicit codes: (2222), 2222.SE, "2222:" or bare code not looking like a year
        for m in re.finditer(r"(?<![\d.])(\d{4})(?:\.s[er])?(?![\d])", t):
            code = m.group(1)
            if code not in by_code:
                continue
            start, end = m.start(), m.end()
            explicit = (start > 0 and t[start - 1] in "(#:") or (end < len(t) and t[end] in ")") or m.group(0) != code
            if explicit or not (1990 <= int(code) <= 2100):
                found[code] = by_code[code]
        for pat, c, ambiguous in patterns:
            if c["code"] in found:
                continue
            if ambiguous and not (has_ctx or c["code"] in t):
                continue
            if pat.search(t):
                found[c["code"]] = c
        return [{"code": c["code"], "name_ar": c["name_ar"], "sector": c.get("sector")}
                for c in sorted(found.values(), key=lambda x: x["code"])]

    return match


# ---------------------------------------------------------------------------
# Rules-mode analysis
# ---------------------------------------------------------------------------
def classify_market(text_norm: str, item: dict[str, Any], rules: dict[str, Any],
                    has_ticker: bool = False) -> str:
    """sa requires an explicit Saudi signal in the text (sa keyword or a listed
    company); a Saudi *source* is not enough. Then us -> macro -> other."""
    mk = rules.get("_market_kw", {})
    padded = f" {text_norm} "

    amb = rules.get("_ambiguous", {})

    def hit(cls: str) -> bool:
        return any(k in padded and keyword_ok(k, padded, amb) for k in mk.get(cls, []))

    if has_ticker or hit("sa"):
        return "sa"
    if hit("us"):
        return "us"
    if hit("macro"):
        return "macro"
    return "other"


def empty_impact() -> dict[str, Any]:
    d: dict[str, Any] = {k: 0 for k in SECTOR_KEYS}
    d["why"] = ""
    return d


def analyze_rules(item: dict[str, Any], rules: dict[str, Any], matcher) -> dict[str, Any]:
    text = f"{item.get('title', '')} . {item.get('summary', '')}"
    tn = norm(text)
    padded = f" {tn} "
    amb = rules.get("_ambiguous", {})
    matched = [r for r in rules.get("rules", [])
               if any(k in padded and keyword_ok(k, padded, amb) for k in r["_kw"])]
    tickers_full = matcher(text)
    tickers = [{"code": t["code"], "name_ar": t["name_ar"]} for t in tickers_full]

    impact = empty_impact()
    score = 0
    for r in matched:
        for k, v in r["impact"].items():
            impact[k] += v
        score += {"pos": 1, "neg": -1}.get(r.get("signal"), 0) * int(r.get("cf", 1))
    signal = "pos" if score > 0 else "neg" if score < 0 else "mix"

    # Company-specific news (earnings, dividends, contracts...) nudges the company's sector.
    if tickers_full and signal in ("pos", "neg"):
        for t in tickers_full:
            if t.get("sector") in SECTOR_KEYS:
                impact[t["sector"]] += 1 if signal == "pos" else -1
    for k in SECTOR_KEYS:
        impact[k] = max(-3, min(3, int(impact[k])))

    whys: list[str] = []
    for r in matched:
        w = r.get("why")
        if w and w not in whys:
            whys.append(w)
    impact["why"] = " · ".join(whys[:3]) if whys else "لا توجد قاعدة مطابقة؛ الأثر القطاعي محايد"

    labels = rules["sectors"]
    beneficiary = hurt = None
    for r in matched:
        if beneficiary is None and r.get("beneficiary"):
            beneficiary = dict(r["beneficiary"])
        if hurt is None and r.get("hurt"):
            hurt = dict(r["hurt"])
    if beneficiary is None:
        if tickers and signal == "pos":
            beneficiary = {"name": f"{tickers[0]['name_ar']} ({tickers[0]['code']})", "why": whys[0] if whys else "خبر إيجابي يخص الشركة"}
        else:
            top = max(SECTOR_KEYS, key=lambda k: (impact[k], -SECTOR_KEYS.index(k)))
            beneficiary = ({"name": labels[top], "why": "أعلى أثر إيجابي بحسب القواعد"} if impact[top] > 0
                           else {"name": "—", "why": "لا يوجد مستفيد واضح"})
    if hurt is None:
        if tickers and signal == "neg":
            hurt = {"name": f"{tickers[0]['name_ar']} ({tickers[0]['code']})", "why": whys[0] if whys else "خبر سلبي يخص الشركة"}
        else:
            low = min(SECTOR_KEYS, key=lambda k: (impact[k], SECTOR_KEYS.index(k)))
            hurt = ({"name": labels[low], "why": "أكبر أثر سلبي بحسب القواعد"} if impact[low] < 0
                    else {"name": "—", "why": "لا يوجد متضرر واضح"})

    cf = max([int(r.get("cf", 1)) for r in matched], default=1)
    if tickers and matched:
        cf += 1
    cf = max(1, min(3, cf))

    lang = detect_lang(f"{item.get('title', '')} {item.get('summary', '')}")
    # Rules mode never machine-translates: Arabic sources keep their Arabic
    # summary (title if the feed had none); English items keep the English text
    # unchanged and are flagged lang="en" for the UI.
    summary_src = (item.get("summary") or "").strip() or (item.get("title") or "")
    return {
        "market": classify_market(tn, item, rules, has_ticker=bool(tickers)),
        "signal": signal,
        "tickers": tickers,
        "summary_ar": summary_src,
        "lang": lang,
        "beneficiary": beneficiary,
        "hurt": hurt,
        "impact": impact,
        "cf": cf,
        "rules": [r.get("id", "?") for r in matched],
        "analysis": "rules",
    }


# ---------------------------------------------------------------------------
# LLM mode (Claude Messages API via plain requests)
# ---------------------------------------------------------------------------
API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """أنت محلل أسواق مالية متخصص في السوق السعودية (تداول/تاسي). ستتلقى قائمة أخبار (عنوان + ملخص + مصدر) ولقطة من بيانات السوق إن وُجدت.
لكل خبر أعد كائن JSON بالحقول التالية بدقة:
- id: كما ورد.
- market: "sa" (خبر سعودي/خليجي مباشر)، "us" (الأسواق الأمريكية/الفيدرالي)، "macro" (نفط، سلع، جيوسياسة، اقتصاد عالمي)، "other".
- signal: "pos" | "neg" | "mix" من منظور مستثمر في الأسهم السعودية.
- tickers: قائمة الشركات السعودية المدرجة المذكورة أو المتأثرة مباشرة [{"code":"2222","name_ar":"أرامكو"}] — استخدم فقط رموز من قائمة الشركات المرفقة، وقائمة فارغة إن لم يوجد.
- summary_ar: ملخص عربي من جملتين إلى ثلاث، واقعي بلا مبالغة، يذكر الأرقام إن وُجدت.
- beneficiary: {"name": شركة أو قطاع سعودي مستفيد, "why": سبب في جملة}.
- hurt: {"name": شركة أو قطاع سعودي متضرر, "why": سبب في جملة}. استخدم "—" إن لم يوجد.
- impact: أعداد صحيحة من -3 إلى +3 لكل قطاع من: energy, banks, petrochem, insurance, transport, realestate, cement, tech, smallcaps, telecom, health, retail, food, utilities، مع حقل "why" يشرح المنطق بالعربية في جملة أو جملتين. 0 يعني لا أثر.
- cf: ثقة 1..3 (3 = أثر مباشر وواضح على السوق السعودية).
أعد JSON فقط وفق المخطط المطلوب."""

IMPACT_SCHEMA = {
    "type": "object",
    "properties": {**{k: {"type": "integer"} for k in SECTOR_KEYS}, "why": {"type": "string"}},
    "required": SECTOR_KEYS + ["why"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "market": {"type": "string", "enum": list(MARKETS)},
                    "signal": {"type": "string", "enum": list(SIGNALS)},
                    "tickers": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}, "name_ar": {"type": "string"}},
                        "required": ["code", "name_ar"], "additionalProperties": False}},
                    "summary_ar": {"type": "string"},
                    "beneficiary": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
                                    "required": ["name", "why"], "additionalProperties": False},
                    "hurt": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
                             "required": ["name", "why"], "additionalProperties": False},
                    "impact": IMPACT_SCHEMA,
                    "cf": {"type": "integer"},
                },
                "required": ["id", "market", "signal", "tickers", "summary_ar", "beneficiary", "hurt", "impact", "cf"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _market_context(market: dict[str, Any] | None, limit: int = 3000) -> str:
    if not market:
        return ""
    try:
        s = json.dumps(market, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return ""
    return s if len(s) <= limit else s[:limit] + "…"


def _post_messages(payload: dict[str, Any], api_key: str, use_fallbacks: bool, timeout: float = 300) -> dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests not installed")
    headers = {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    body = dict(payload)
    if use_fallbacks:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        body["fallbacks"] = "default"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=timeout)
            if r.status_code in (408, 409, 429, 500, 502, 503, 504, 529):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code == 400 and use_fallbacks and "fallback" in r.text.lower():
                log.warning("API rejected fallbacks parameter; retrying without it")
                return _post_messages(payload, api_key, use_fallbacks=False, timeout=timeout)
            if r.status_code >= 400:
                raise ValueError(f"HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - network / retryable
            last_err = exc
            wait = 2 ** attempt * 3
            log.warning("API attempt %d failed (%s); retrying in %ss", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"API failed after retries: {last_err}")


def _extract_text(resp: dict[str, Any]) -> str:
    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise RuntimeError(f"model refused: {resp.get('stop_details')}")
    if stop == "max_tokens":
        raise RuntimeError("response truncated (max_tokens)")
    for block in resp.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    raise RuntimeError("no text block in response")


def _parse_json_loose(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


def validate_llm_item(obj: Any, known_codes: dict[str, str]) -> dict[str, Any] | None:
    """Return a cleaned analysis dict or None if the object is unusable."""
    if not isinstance(obj, dict):
        return None
    try:
        market = obj["market"] if obj["market"] in MARKETS else None
        signal = obj["signal"] if obj["signal"] in SIGNALS else None
        if market is None or signal is None:
            return None
        imp_raw = obj.get("impact") or {}
        impact = empty_impact()
        for k in SECTOR_KEYS:
            v = imp_raw.get(k, 0)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            impact[k] = max(-3, min(3, int(round(v))))
        impact["why"] = str(imp_raw.get("why") or "").strip()
        tickers = []
        seen = set()
        for t in obj.get("tickers") or []:
            if not isinstance(t, dict):
                continue
            code = re.sub(r"\.(SE|SR)$", "", str(t.get("code", "")).strip(), flags=re.I)
            if not re.fullmatch(r"\d{4}", code) or code in seen:
                continue
            if known_codes and code not in known_codes:
                continue
            seen.add(code)
            tickers.append({"code": code, "name_ar": str(t.get("name_ar") or known_codes.get(code) or code)})
        tickers.sort(key=lambda x: x["code"])

        def pair(v: Any) -> dict[str, str]:
            if not isinstance(v, dict):
                return {"name": "—", "why": ""}
            return {"name": str(v.get("name") or "—"), "why": str(v.get("why") or "")}

        summary_ar = str(obj.get("summary_ar") or "").strip()
        if not summary_ar:
            return None
        cf = obj.get("cf", 1)
        cf = max(1, min(3, int(cf))) if isinstance(cf, (int, float)) and not isinstance(cf, bool) else 1
        return {"market": market, "signal": signal, "tickers": tickers, "summary_ar": summary_ar,
                "lang": "ar" if has_arabic(summary_ar) else "en",
                "beneficiary": pair(obj.get("beneficiary")), "hurt": pair(obj.get("hurt")),
                "impact": impact, "cf": cf, "analysis": "llm"}
    except (KeyError, TypeError, ValueError):
        return None


def analyze_llm_batch(items: list[dict[str, Any]], constituents: list[dict[str, Any]],
                      market: dict[str, Any] | None, api_key: str, model: str,
                      use_fallbacks: bool = True) -> dict[str, dict[str, Any]]:
    """Returns {item_id: analysis} for validated items only. Raises on API failure."""
    known = {c["code"]: c["name_ar"] for c in constituents}
    comp_list = ", ".join(f"{c['code']}={c['name_ar']}" for c in constituents[:400])
    payload_items = [{"id": it["id"], "title": it.get("title", ""), "summary": (it.get("summary") or "")[:500],
                      "source": it.get("source", ""), "published_riyadh": it.get("published_riyadh", "")} for it in items]
    user = ("الشركات المدرجة (رمز=اسم): " + comp_list + "\n\n"
            + ("لقطة السوق الحالية (JSON): " + _market_context(market) + "\n\n" if market else "")
            + "الأخبار (JSON):\n" + json.dumps(payload_items, ensure_ascii=False)
            + "\n\nأعد {\"results\": [...]} بعنصر واحد لكل id.")
    payload = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    }
    resp = _post_messages(payload, api_key, use_fallbacks=use_fallbacks)
    usage = resp.get("usage", {})
    log.info("LLM batch of %d: model=%s in=%s out=%s stop=%s", len(items), resp.get("model", model),
             usage.get("input_tokens"), usage.get("output_tokens"), resp.get("stop_reason"))
    data = _parse_json_loose(_extract_text(resp))
    results = data.get("results") if isinstance(data, dict) else data
    out: dict[str, dict[str, Any]] = {}
    for obj in results or []:
        if not isinstance(obj, dict):
            continue
        cleaned = validate_llm_item(obj, known)
        if cleaned is None:
            log.warning("LLM item %s failed validation; will use rules", obj.get("id") if isinstance(obj, dict) else "?")
            continue
        out[str(obj.get("id"))] = cleaned
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
MARKET_ORDER = {"sa": 0, "macro": 1, "us": 2, "other": 3}
RIYADH_TZ = timezone(timedelta(hours=3))


def _fix_future_time(it: dict[str, Any]) -> None:
    """Safety net for raw files produced before fetch_news learned to correct
    mislabeled (Riyadh-local) or future publish times."""
    if adjust_future_time is None or it.get("time_adjusted"):
        return
    try:
        pub = datetime.strptime(it["published_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.strptime(it["fetched_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return
    dt, precision, note = adjust_future_time(pub, now, it.get("precision", "second"))
    if note:
        it["published_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        it["published_riyadh"] = dt.astimezone(RIYADH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        it["precision"] = precision
        it["time_adjusted"] = note


def rank_items(items: list[dict[str, Any]], max_items: int = 60, max_non_sa: int = 25) -> tuple[list[dict[str, Any]], int]:
    """Saudi-first ordering: market=="sa" (newest first), then macro, us, other
    (each newest first). Non-Saudi items are capped at `max_non_sa`, the whole
    list at `max_items`. Returns (ranked, dropped_by_cap)."""
    ordered = sorted(items, key=lambda i: i.get("published_utc") or "", reverse=True)
    ordered.sort(key=lambda i: MARKET_ORDER.get(i.get("market"), 3))
    out: list[dict[str, Any]] = []
    non_sa = 0
    for it in ordered:
        if len(out) >= max_items:
            break
        if it.get("market") != "sa":
            if non_sa >= max_non_sa:
                continue
            non_sa += 1
        out.append(it)
    return out, len(items) - len(out)


def analyze(raw: dict[str, Any], rules: dict[str, Any], constituents: list[dict[str, Any]],
            market: dict[str, Any] | None = None, api_key: str | None = None,
            model: str = DEFAULT_MODEL, batch_size: int = 10,
            use_fallbacks: bool = True, max_items: int = 60, max_non_sa: int = 25,
            min_relevance: float = RELEVANCE_MIN) -> dict[str, Any]:
    items = raw.get("items") if isinstance(raw, dict) else raw
    items = list(items or [])
    matcher = build_matcher(constituents)

    enriched: list[dict[str, Any]] = []
    dropped_relevance = 0
    for it in items:
        it = dict(it)
        it["id"] = it.get("id") or item_id(it)
        _fix_future_time(it)
        if "relevance" not in it:
            it["relevance"] = (relevance_score(it.get("title", ""), it.get("summary", ""), it.get("source", ""),
                                               it.get("link", "")) if relevance_score else 1.0)
        if it["relevance"] < min_relevance:
            dropped_relevance += 1
            log.debug("dropped irrelevant (%.2f): %s", it["relevance"], it.get("title", "")[:80])
            continue
        it.update(analyze_rules(it, rules, matcher))
        # Neither Saudi nor macro/US finance (generic tech/opinion pieces from
        # Saudi outlets): penalise relevance so weak items fall below the gate.
        if it["market"] == "other" and not it["tickers"]:
            strong = any(int(r.get("cf", 1)) >= 2 for r in rules.get("rules", []) if r.get("id") in it["rules"])
            if not strong:
                it["relevance"] = round(it["relevance"] * 0.4, 2)
                it["relevance_penalized"] = True
                if it["relevance"] < min_relevance:
                    dropped_relevance += 1
                    log.debug("dropped off-topic 'other' (%.2f): %s", it["relevance"], it.get("title", "")[:80])
                    continue
        enriched.append(it)

    llm_used = 0
    if api_key:
        for i in range(0, len(enriched), max(1, batch_size)):
            batch = enriched[i:i + batch_size]
            try:
                res = analyze_llm_batch(batch, constituents, market, api_key, model, use_fallbacks)
            except Exception as exc:  # noqa: BLE001
                log.error("LLM batch %d-%d failed, keeping rules result: %s", i, i + len(batch), exc)
                continue
            for it in batch:
                a = res.get(it["id"])
                if a:
                    # keep rule-matched tickers the model missed; rules ids kept for transparency
                    codes = {t["code"] for t in a["tickers"]}
                    a["tickers"] = sorted(a["tickers"] + [t for t in it["tickers"] if t["code"] not in codes],
                                          key=lambda x: x["code"])
                    it.update(a)
                    llm_used += 1
    mode = "rules" if not api_key else ("llm" if llm_used == len(enriched) and enriched else
                                        "llm+rules" if llm_used else "rules")
    ranked, dropped_cap = rank_items(enriched, max_items=max_items, max_non_sa=max_non_sa)
    by_market = {m: sum(1 for i in ranked if i["market"] == m) for m in MARKETS}
    log.info("analysis mode=%s in=%d kept=%d llm=%d dropped: irrelevant=%d cap=%d  by_market=%s",
             mode, len(items), len(ranked), llm_used, dropped_relevance, dropped_cap, by_market)
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_generated_at_utc": raw.get("generated_at_utc") if isinstance(raw, dict) else None,
        "analysis_mode": mode,
        "model": model if api_key else None,
        "dropped": dropped_relevance + dropped_cap,
        "stats": {"input": len(items), "kept": len(ranked), "dropped_irrelevant": dropped_relevance,
                  "dropped_cap": dropped_cap, "by_market": by_market, "llm_items": llm_used,
                  "fetch_dropped": raw.get("dropped") if isinstance(raw, dict) else None},
        "sectors": {k: rules["sectors"][k] for k in SECTOR_KEYS},
        "items": ranked,
    }


def _load_json(path: str | None, required: bool = False) -> Any:
    if not path:
        return None
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(path)
        log.warning("optional file missing: %s", path)
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--market", help="data/market.json (optional context for the LLM)")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--constituents", default=DEFAULT_CONSTITUENTS)
    ap.add_argument("--no-llm", action="store_true", help="force rules mode even if ANTHROPIC_API_KEY is set")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--max", type=int, default=60, help="total item cap (60)")
    ap.add_argument("--max-non-sa", type=int, default=25, help="cap on non-Saudi (macro/us/other) items (25)")
    ap.add_argument("--min-relevance", type=float, default=RELEVANCE_MIN, help="drop items below this relevance (0.35)")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    raw = _load_json(args.inp, required=True)
    rules = load_rules(args.rules)
    constituents = load_constituents(args.constituents)
    market = _load_json(args.market)
    api_key = None if args.no_llm else (os.environ.get("ANTHROPIC_API_KEY") or None)
    if api_key:
        log.info("LLM mode enabled (model=%s)", args.model)
    else:
        log.info("rules mode (no ANTHROPIC_API_KEY or --no-llm)")
    use_fallbacks = os.environ.get("NEWS_LLM_FALLBACKS", "1") not in ("0", "false", "no")

    result = analyze(raw, rules, constituents, market=market, api_key=api_key, model=args.model,
                     batch_size=args.batch, use_fallbacks=use_fallbacks, max_items=args.max,
                     max_non_sa=args.max_non_sa, min_relevance=args.min_relevance)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    log.info("wrote %s (%d items, mode=%s)", args.out, len(result["items"]), result["analysis_mode"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
