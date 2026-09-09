#!/usr/bin/env python3
"""Enrich raw news items with market/signal/tickers/impact for the dashboard,
maintain a 7-day rolling archive and write the ranked view.

Usage:
    python scripts/analyze_news.py --in data/news_raw.json --out data/news.json \
        [--market data/market.json] [--archive data/news_archive.json] [--days 7] \
        [--rules scripts/impact_rules.json] [--constituents scripts/constituents.json] \
        [--constituents-us scripts/constituents_us.json] [--no-llm] [--batch 10]

Modes
  * LLM mode  - when ANTHROPIC_API_KEY is set (and --no-llm is absent): items are
                sent in batches to the Claude Messages API (plain `requests`,
                no SDK) asking for strict JSON; every returned item is validated
                and any failure falls back to rules mode for that batch/item.
  * Rules mode - deterministic keyword rules from scripts/impact_rules.json.

Archive (data/news_archive.json)
  {"generated_at_utc", "days": 7, "count", "items": [analyzed items, newest first]}
  Items already in the archive (same id, or same normalized title) are NOT
  re-analyzed (their prior analysis is kept, so LLM cost only covers new items);
  items older than --days (by published_utc relative to now) are dropped.

Output schema (data/news.json) - the ranked VIEW of the archive:
{
  "generated_at_utc", "today_riyadh": "YYYY-MM-DD", "analysis_mode": "llm"|"rules"|"llm+rules",
  "sectors": {key: arabic_label},
  "days": [{"day_local", "count", "label_ar", "is_today"}],   # ordered newest day first
  "items": [ raw item fields + {
      "id", "market": "sa"|"us"|"macro"|"other", "signal": "pos"|"neg"|"mix",
      "tickers": [{"code","name_ar","market": "sa"|"us"}], "summary_ar", "lang": "ar"|"en",
      "beneficiary": {"name","why"}, "hurt": {"name","why"},
      "impact": {<14 sector keys>: int -3..3, "why": str}, "impact_market": "sa"|"us"|"both",
      "cf": 1..3, "rules": [rule ids], "analysis": "llm"|"rules", "relevance": 0..1,
      "day_local": "YYYY-MM-DD" (Riyadh), "is_today": bool, "age_hours": float, "first_seen_utc"
  }],   # ordered: day_local desc (today first); within a day sa, us, macro, other; each newest first.
        # Caps: 300 total, 60 per day (today up to 40 sa + 40 us + 20 macro + 10 other).
  "dropped": int, "stats": {input, new_items, archive_total, expired, kept, dropped_irrelevant, dropped_cap,
                            by_market, by_day, llm_items, fetch_dropped}
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
    from fetch_news import RELEVANCE_MIN, adjust_future_time, normalize_link, relevance_score
except Exception:  # pragma: no cover
    RELEVANCE_MIN = 0.35
    relevance_score = None  # type: ignore
    adjust_future_time = None  # type: ignore

    def normalize_link(link: str) -> str:  # type: ignore
        return (link or "").strip().lower()

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "impact_rules.json")
DEFAULT_CONSTITUENTS = os.path.join(HERE, "constituents.json")
DEFAULT_CONSTITUENTS_US = os.path.join(HERE, "constituents_us.json")
DEFAULT_ARCHIVE = "data/news_archive.json"
DEFAULT_DAYS = 7

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
IMPACT_MARKETS = ("sa", "us", "both")

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
    ("الخدمات المالية", "banks"), ("financial", "banks"), ("تمويل", "banks"), ("المالية", "banks"),
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
    # US GICS labels (constituents_us.json): communication services -> telecom,
    # materials -> petrochem, industrials -> transport (aerospace/machinery/logistics)
    ("communication", "telecom"), ("المواد", "petrochem"), ("الصناعة", "transport"), ("industrial", "transport"),
    ("staples", "food"), ("discretionary", "retail"),
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


def norm_keyword(kw: str) -> str:
    """norm() for rule keywords, keeping a deliberate leading/trailing space
    (" ai ", "fed ", "sar ") as a word boundary; the matched text is padded."""
    return (" " if kw[:1] == " " else "") + norm(kw) + (" " if kw[-1:] == " " else "")


def has_arabic(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text or ""))


def detect_lang(text: str) -> str:
    ar = len(re.findall(r"[؀-ۿ]", text or ""))
    en = len(re.findall(r"[A-Za-z]", text or ""))
    return "ar" if ar >= en and ar > 0 else "en"


def title_key(title: str) -> str:
    """Normalized title used for cross-run duplicate detection (punctuation-free)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", norm(title))).strip()


def item_id(item: dict[str, Any]) -> str:
    """Stable id: sha1 of the normalized link (tracking params stripped), or of
    the normalized title when the item has no link."""
    link = normalize_link(item.get("link") or "")
    key = ("link:" + link) if link else ("title:" + title_key(item.get("title", "")))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


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
        r["_kw"] = [norm_keyword(k) for k in r.get("keywords", []) if k and k.strip()]
        r["impact"] = {k: int(v) for k, v in (r.get("impact") or {}).items() if k in SECTOR_KEYS}
    rules["_market_kw"] = {m: [norm_keyword(k) for k in kws if k and k.strip()]
                           for m, kws in (rules.get("market_keywords") or {}).items() if isinstance(kws, list)}
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
# US constituents + ticker matching
# ---------------------------------------------------------------------------
# Extra English/Arabic aliases for US names whose feed spelling differs from
# constituents_us.json (Alphabet/Google -> GOOGL, Facebook -> META, ...).
US_ALIASES: dict[str, list[str]] = {
    "GOOGL": ["Google", "Alphabet", "جوجل", "غوغل", "ألفابت", "الفابت"],
    "META": ["Meta Platforms", "Facebook", "فيسبوك", "ميتا بلاتفورمز"],
    "NVDA": ["Nvidia", "إنفيديا", "نفيديا", "انفيديا"],
    "BRK-B": ["Berkshire Hathaway", "Berkshire", "بيركشاير"],
    "JPM": ["JPMorgan", "JP Morgan", "جي بي مورغان", "جي بي مورجان", "جيه بي مورغان"],
    "AMZN": ["Amazon", "أمازون", "امازون"],
    "TSLA": ["Tesla", "تسلا", "تيسلا"],
    "MSFT": ["Microsoft", "مايكروسوفت", "ميكروسوفت"],
    "AAPL": ["Apple", "أبل", "ابل", "آبل"],
    "XOM": ["Exxon", "ExxonMobil", "Exxon Mobil", "إكسون موبيل", "إكسون"],
    "GS": ["Goldman Sachs", "Goldman", "غولدمان ساكس", "جولدمان ساكس"],
    "MS": ["Morgan Stanley", "مورغان ستانلي", "مورجان ستانلي"],
    "BAC": ["Bank of America", "BofA", "بنك أوف أمريكا"],
    "WFC": ["Wells Fargo", "ويلز فارغو"],
    "GE": ["GE Aerospace", "General Electric", "جنرال إلكتريك"],
    "AMD": ["Advanced Micro Devices", "إيه إم دي"],
    "TXN": ["Texas Instruments", "تكساس إنسترومنتس"],
    "UNH": ["UnitedHealth", "United Health", "يونايتد هيلث"],
    "LLY": ["Eli Lilly", "Lilly", "إيلاي ليلي", "ليلي"],
    "V": ["Visa Inc", "فيزا"],
    "MA": ["Mastercard", "ماستركارد"],
    "PM": ["Philip Morris", "فيليب موريس"],
    "KO": ["Coca-Cola", "Coca Cola", "كوكا كولا"],
    "MCD": ["McDonald's", "McDonalds", "ماكدونالدز"],
    "HD": ["Home Depot", "هوم ديبوت"],
    "DIS": ["Walt Disney", "Disney", "ديزني"],
    "PG": ["Procter & Gamble", "Procter and Gamble", "بروكتر آند غامبل"],
    "JNJ": ["Johnson & Johnson", "Johnson and Johnson", "J&J", "جونسون آند جونسون"],
    "CAT": ["Caterpillar", "كاتربيلر"],
    "RTX": ["Raytheon", "RTX Corp", "رايثيون"],
    "IBM": ["IBM", "آي بي إم"],
    "CRM": ["Salesforce", "سيلزفورس"],
    "NFLX": ["Netflix", "نتفليكس"],
    "ORCL": ["Oracle", "أوراكل"],
    "AVGO": ["Broadcom", "برودكوم"],
    "QCOM": ["Qualcomm", "كوالكوم"],
    "CSCO": ["Cisco", "سيسكو"],
    "INTC": ["Intel", "إنتل", "انتل"],
    "PLTR": ["Palantir", "بالانتير"],
    "WMT": ["Walmart", "وول مارت", "وولمارت"],
    "COST": ["Costco", "كوستكو"],
    "CVX": ["Chevron", "شيفرون"],
    "PEP": ["PepsiCo", "Pepsi", "بيبسيكو", "بيبسي"],
    "MRK": ["Merck", "ميرك"],
    "AXP": ["American Express", "AmEx", "أمريكان إكسبريس"],
    "BA": ["Boeing", "بوينغ", "بوينج"],
    "UBER": ["Uber", "أوبر"],
}
# English names that are ordinary words: need company context (shares/stock/earnings...).
US_AMBIGUOUS_NAMES = {norm(a) for a in ("visa", "apple", "ابل", "oracle", "alphabet", "meta", "lilly", "target",
                                         "disney", "pepsi", "amazon", "linde", "abbott", "intuit", "uber",
                                         "ليلي", "ابل", "امازون", "فيزا")}
US_ALIAS_STOP = {norm(a) for a in ("meta", "rtx", "ge", "ibm", "j&j")}  # never as a case-insensitive word
# Sector overrides where the GICS label maps poorly onto our 14 keys.
US_SECTOR_OVERRIDE = {"WMT": "retail", "COST": "retail", "HD": "retail", "MCD": "retail", "TGT": "retail",
                      "AMZN": "retail", "V": "banks", "MA": "banks", "AXP": "banks", "GE": "transport",
                      "BA": "transport", "UBER": "transport", "UPS": "transport", "LIN": "petrochem"}
# Symbols that are also English words in ALL-CAPS headlines: need "$X", "(X)" or "NYSE: X".
US_EXPLICIT_ONLY = {"CAT", "LIN", "PEP", "NOW", "ALL", "LOW", "ARE", "FOR", "BIG", "ONE", "GAP", "SEE",
                    "COST", "WELL", "REAL", "PLAY", "LOVE", "GOOD", "FAST", "DAY", "KEY", "RUN", "TAP"}
_US_SYMBOL = re.compile(r"^[A-Z]{1,5}(?:[-.][A-Z])?$")


def _normalize_us(raw: dict[str, Any]) -> dict[str, Any] | None:
    code = str(raw.get("code") or raw.get("symbol") or raw.get("ticker") or raw.get("yahoo") or "").strip().upper()
    code = code.replace(".", "-") if re.fullmatch(r"[A-Z]+\.[A-Z]", code) else code
    if not _US_SYMBOL.match(code):
        return None
    name_en = str(raw.get("name_en") or raw.get("name") or "").strip()
    name_ar = str(raw.get("name_ar") or "").strip()
    sector = US_SECTOR_OVERRIDE.get(code) or map_sector(raw.get("sector") or raw.get("sector_ar") or raw.get("sector_en"))
    aliases: list[str] = [name_en] + list(raw.get("aliases") or []) + US_ALIASES.get(code, [])
    if name_ar:
        aliases.append(name_ar)
        # "ألفابت (جوجل)" -> both halves; "آر تي إكس (رايثيون)" likewise
        for part in re.split(r"[()]", name_ar):
            part = part.strip()
            if part and part != name_ar:
                aliases.append(part)
    seen: set[str] = set()
    uniq = []
    for a in aliases:
        k = norm(a)
        if k and k not in seen:
            seen.add(k)
            uniq.append(a)
    return {"code": code, "name_ar": name_ar or name_en or code, "name_en": name_en, "sector": sector,
            "aliases": uniq, "market": "us"}


def load_constituents_us(path: str | None = DEFAULT_CONSTITUENTS_US) -> list[dict[str, Any]]:
    """US constituents (symbol, name_en, name_ar, sector). Missing file -> []."""
    if not path or not os.path.exists(path):
        if path:
            log.info("US constituents file missing (%s); US ticker matching disabled", path)
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read US constituents %s: %s", path, exc)
        return []
    rows = data.get("constituents") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            c = _normalize_us(r)
            if c:
                out.append(c)
    log.info("loaded %d US constituents from %s", len(out), path)
    return out


US_COMPANY_CONTEXT = [norm(c) for c in ("shares", "share", "stock", "stocks", "earnings", "revenue", "profit",
                                        "quarter", "ceo", "investors", "market cap", "nasdaq", "nyse", "wall street",
                                        "inc", "corp", "shareholders", "guidance", "results", "سهم", "أسهم", "شركة",
                                        "أرباح", "إيرادات", "المستثمرين", "ناسداك", "وول ستريت")]


def build_us_matcher(constituents: list[dict[str, Any]]):
    """US ticker matcher.

    * symbols: case-sensitive, word-bounded, uppercase only (AAPL, NVDA, BRK-B,
      $TSLA, NASDAQ:MSFT). Symbols of 1-2 letters (V, MA, GE, MS, PM, KO, HD...)
      need an explicit marker: "$V", "(MA)", "NYSE: GE".
    * English names: case-insensitive whole words; ordinary-word names (Apple,
      Visa, Alphabet, Oracle...) require company context (shares/earnings/...).
    * Arabic names: whole words, definite-article prefixes allowed.
    """
    patterns: list[tuple[re.Pattern, dict[str, Any], bool]] = []
    symbols: list[tuple[re.Pattern, re.Pattern, dict[str, Any]]] = []
    for c in constituents:
        sym = re.escape(c["code"])
        bare = re.compile(r"(?<![A-Za-z0-9$\-])" + sym + r"(?![A-Za-z0-9\-])")
        explicit = re.compile(r"(?:\$|\(|(?:NYSE|NASDAQ|Nasdaq)\s*:\s*)" + sym + r"(?![A-Za-z0-9\-])|(?<![A-Za-z0-9])" + sym + r"\)")
        symbols.append((bare, explicit, c))
        for a in c.get("aliases") or []:
            key = norm(a)
            if len(key) < 3 or key in US_ALIAS_STOP:
                continue
            if re.search(r"[؀-ۿ]", key):
                pat = r"(?<![\w])(?:و|ب|ل|ال|وال|بال|لل)?" + re.escape(key) + r"(?![\w])"
            else:
                pat = r"(?<![\w])" + re.escape(key) + r"(?:'s)?(?![\w])"
            patterns.append((re.compile(pat), c, key in US_AMBIGUOUS_NAMES))

    def match(text: str) -> list[dict[str, Any]]:
        raw = text or ""
        t = norm(raw)
        found: dict[str, dict[str, Any]] = {}
        for bare, explicit, c in symbols:
            code = c["code"]
            bare_ok = len(code.replace("-", "")) >= 3 and code not in US_EXPLICIT_ONLY
            if explicit.search(raw) or (bare_ok and bare.search(raw)):
                found[code] = c
        has_ctx = None
        for pat, c, ambiguous in patterns:
            if c["code"] in found or not pat.search(t):
                continue
            if ambiguous:
                if has_ctx is None:
                    has_ctx = any(re.search(r"(?<![\w])(?:و|ب|ل|ال|وال|بال)?" + re.escape(x) + r"(?![\w])", t)
                                  for x in US_COMPANY_CONTEXT)
                if not has_ctx:
                    continue
            found[c["code"]] = c
        return [{"code": c["code"], "name_ar": c["name_ar"], "sector": c.get("sector"), "market": "us"}
                for c in sorted(found.values(), key=lambda x: x["code"])]

    return match


# ---------------------------------------------------------------------------
# Rules-mode analysis
# ---------------------------------------------------------------------------
def classify_market(text_norm: str, item: dict[str, Any], rules: dict[str, Any],
                    has_ticker: bool = False, has_us_ticker: bool = False) -> str:
    """sa requires an explicit Saudi signal in the text (sa keyword or a listed
    company); a Saudi *source* is not enough. us requires an explicit US signal:
    a us keyword (Wall Street, S&P, Nasdaq, Dow, NYSE, Fed/FOMC...), a US
    constituent, or - for items from a US-market feed (market_hint=="us") - a
    generic stock-market word (us_weak keywords). Then macro -> other."""
    mk = rules.get("_market_kw", {})
    padded = f" {text_norm} "

    amb = rules.get("_ambiguous", {})

    def hit(cls: str) -> bool:
        return any(k in padded and keyword_ok(k, padded, amb) for k in mk.get(cls, []))

    if has_ticker or hit("sa"):
        return "sa"
    if has_us_ticker or hit("us"):
        return "us"
    if item.get("market_hint") == "us" and hit("us_weak"):
        return "us"
    if hit("macro"):
        return "macro"
    return "other"


def derive_impact_market(market: str, matched: list[dict[str, Any]], sa_tickers: bool, us_tickers: bool) -> str:
    """Which market the sector impact scores refer to: "sa", "us" or "both"."""
    if sa_tickers and us_tickers:
        return "both"
    declared = {str(r.get("impact_market")) for r in matched if r.get("impact_market") in IMPACT_MARKETS}
    if market == "sa":
        return "both" if us_tickers or "both" in declared else "sa"
    if market == "us":
        return "both" if sa_tickers or "both" in declared else "us"
    if market == "macro":
        return "both" if not declared or "both" in declared or len(declared) > 1 else declared.pop()
    return "sa" if sa_tickers or not us_tickers else "us"


def empty_impact() -> dict[str, Any]:
    d: dict[str, Any] = {k: 0 for k in SECTOR_KEYS}
    d["why"] = ""
    return d


def analyze_rules(item: dict[str, Any], rules: dict[str, Any], matcher, us_matcher=None) -> dict[str, Any]:
    text = f"{item.get('title', '')} . {item.get('summary', '')}"
    tn = norm(text)
    padded = f" {tn} "
    amb = rules.get("_ambiguous", {})
    matched = [r for r in rules.get("rules", [])
               if any(k in padded and keyword_ok(k, padded, amb) for k in r["_kw"])]
    sa_full = matcher(text)
    us_full = us_matcher(text) if us_matcher else []
    tickers_full = sa_full + us_full
    tickers = [{"code": t["code"], "name_ar": t["name_ar"], "market": t.get("market") or "sa"} for t in tickers_full]

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
    market = classify_market(tn, item, rules, has_ticker=bool(sa_full), has_us_ticker=bool(us_full))
    return {
        "market": market,
        "signal": signal,
        "tickers": tickers,
        "summary_ar": summary_src,
        "lang": lang,
        "beneficiary": beneficiary,
        "hurt": hurt,
        "impact": impact,
        "impact_market": derive_impact_market(market, matched, bool(sa_full), bool(us_full)),
        "cf": cf,
        "rules": [r.get("id", "?") for r in matched],
        "analysis": "rules",
    }


# ---------------------------------------------------------------------------
# LLM mode (Claude Messages API via plain requests)
# ---------------------------------------------------------------------------
API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """أنت محلل أسواق مالية متخصص في السوق السعودية (تداول/تاسي) والسوق الأمريكية (وول ستريت). ستتلقى قائمة أخبار (عنوان + ملخص + مصدر) ولقطة من بيانات السوق إن وُجدت.
لكل خبر أعد كائن JSON بالحقول التالية بدقة:
- id: كما ورد.
- market: "sa" (خبر سعودي/خليجي مباشر)، "us" (الأسهم الأمريكية، وول ستريت، الفيدرالي، شركات أمريكية مدرجة)، "macro" (نفط، سلع، جيوسياسة، اقتصاد عالمي)، "other".
- signal: "pos" | "neg" | "mix" من منظور مستثمر في السوق المعنية (السعودية لخبر sa، الأمريكية لخبر us).
- tickers: الشركات المدرجة المذكورة أو المتأثرة مباشرة، سعودية أو أمريكية: [{"code":"2222","name_ar":"أرامكو","market":"sa"}, {"code":"NVDA","name_ar":"إنفيديا","market":"us"}] — استخدم فقط رموزاً من القائمتين المرفقتين (رمز رقمي من 4 خانات للسعودية، رمز حروف كبيرة للأمريكية)، وقائمة فارغة إن لم يوجد.
- summary_ar: ملخص عربي دائماً (ترجم الأخبار الإنجليزية) من جملتين إلى ثلاث، واقعي بلا مبالغة، يذكر الأرقام إن وُجدت.
- beneficiary: {"name": الشركة أو القطاع المستفيد مع الرمز إن أمكن مثل "أرامكو (2222)" أو "إنفيديا (NVDA)", "why": سبب في جملة}.
- hurt: {"name": الشركة أو القطاع المتضرر مع الرمز إن أمكن, "why": سبب في جملة}. استخدم "—" إن لم يوجد.
- impact: أعداد صحيحة من -3 إلى +3 لكل قطاع من: energy, banks, petrochem, insurance, transport, realestate, cement, tech, smallcaps, telecom, health, retail, food, utilities، مع حقل "why" يشرح المنطق بالعربية في جملة أو جملتين. 0 يعني لا أثر. لخبر أمريكي تشير القطاعات إلى نظيراتها الأمريكية (tech = التقنية والذكاء الاصطناعي وأشباه الموصلات، banks = البنوك والمالية، retail = الاستهلاك، transport = الصناعة والنقل...).
- impact_market: "sa" إذا كانت درجات الأثر تخص السوق السعودية، "us" إذا كانت تخص السوق الأمريكية، "both" إذا كان الخبر يؤثر على السوقين (الفيدرالي، النفط، الرسوم الجمركية...).
- cf: ثقة 1..3 (3 = أثر مباشر وواضح على السوق المعنية).
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
                        "properties": {"code": {"type": "string"}, "name_ar": {"type": "string"},
                                       "market": {"type": "string", "enum": ["sa", "us"]}},
                        "required": ["code", "name_ar", "market"], "additionalProperties": False}},
                    "summary_ar": {"type": "string"},
                    "beneficiary": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
                                    "required": ["name", "why"], "additionalProperties": False},
                    "hurt": {"type": "object", "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
                             "required": ["name", "why"], "additionalProperties": False},
                    "impact": IMPACT_SCHEMA,
                    "impact_market": {"type": "string", "enum": list(IMPACT_MARKETS)},
                    "cf": {"type": "integer"},
                },
                "required": ["id", "market", "signal", "tickers", "summary_ar", "beneficiary", "hurt", "impact",
                             "impact_market", "cf"],
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


def validate_llm_item(obj: Any, known_codes: dict[str, str],
                      known_us: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Return a cleaned analysis dict or None if the object is unusable.

    `known_codes` = Saudi {code: name_ar}; `known_us` = US {SYMBOL: name_ar}.
    Saudi tickers must be 4 digits, US tickers an uppercase symbol from known_us."""
    if not isinstance(obj, dict):
        return None
    known_us = known_us or {}
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
            if code in seen:
                continue
            if re.fullmatch(r"\d{4}", code):
                if known_codes and code not in known_codes:
                    continue
                seen.add(code)
                tickers.append({"code": code, "name_ar": str(t.get("name_ar") or known_codes.get(code) or code),
                                "market": "sa"})
                continue
            sym = code.upper().replace(".", "-")
            if sym in known_us:
                seen.add(code)
                tickers.append({"code": sym, "name_ar": str(t.get("name_ar") or known_us.get(sym) or sym),
                                "market": "us"})
        tickers.sort(key=lambda x: (x["market"], x["code"]))
        sa_t = any(t["market"] == "sa" for t in tickers)
        us_t = any(t["market"] == "us" for t in tickers)
        impact_market = obj.get("impact_market")
        if impact_market not in IMPACT_MARKETS:
            impact_market = derive_impact_market(market, [], sa_t, us_t)

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
                "impact": impact, "impact_market": impact_market, "cf": cf, "analysis": "llm"}
    except (KeyError, TypeError, ValueError):
        return None


def analyze_llm_batch(items: list[dict[str, Any]], constituents: list[dict[str, Any]],
                      market: dict[str, Any] | None, api_key: str, model: str,
                      use_fallbacks: bool = True,
                      constituents_us: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Returns {item_id: analysis} for validated items only. Raises on API failure."""
    constituents_us = constituents_us or []
    known = {c["code"]: c["name_ar"] for c in constituents}
    known_us = {c["code"]: c["name_ar"] for c in constituents_us}
    comp_list = ", ".join(f"{c['code']}={c['name_ar']}" for c in constituents[:400])
    us_list = ", ".join(f"{c['code']}={c['name_ar']}" + (f" ({c['name_en']})" if c.get("name_en") else "")
                        for c in constituents_us[:200])
    payload_items = [{"id": it["id"], "title": it.get("title", ""), "summary": (it.get("summary") or "")[:500],
                      "source": it.get("source", ""), "published_riyadh": it.get("published_riyadh", ""),
                      "lang": it.get("lang", ""), "market_hint": it.get("market_hint") or ""} for it in items]
    user = ("الشركات السعودية المدرجة (رمز=اسم): " + comp_list + "\n\n"
            + ("الشركات الأمريكية المدرجة (رمز=اسم): " + us_list + "\n\n" if us_list else "")
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
        cleaned = validate_llm_item(obj, known, known_us)
        if cleaned is None:
            log.warning("LLM item %s failed validation; will use rules", obj.get("id") if isinstance(obj, dict) else "?")
            continue
        out[str(obj.get("id"))] = cleaned
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
MARKET_ORDER = {"sa": 0, "us": 1, "macro": 2, "other": 3}
RIYADH_TZ = timezone(timedelta(hours=3))
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Ranking caps for the news.json view.
MAX_TOTAL = 300
MAX_PER_DAY = 60
TODAY_QUOTA = {"sa": 40, "us": 40, "macro": 20, "other": 10}   # today may hold up to the sum (110)
DAY_QUOTA = {"sa": 30, "us": 20, "macro": 8, "other": 2}       # older days: quota pass, then fill to MAX_PER_DAY

AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]  # Monday = 0
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


def parse_utc(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", UTC_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def day_label_ar(day_local: str) -> str:
    """'2026-09-09' -> 'الأربعاء 9 سبتمبر'."""
    try:
        d = datetime.strptime(day_local, "%Y-%m-%d")
    except ValueError:
        return day_local
    return f"{AR_WEEKDAYS[d.weekday()]} {d.day} {AR_MONTHS[d.month - 1]}"


def add_day_fields(items: list[dict[str, Any]], now: datetime) -> None:
    """Set day_local (Riyadh date), is_today, age_hours on every item (in place)."""
    today = now.astimezone(RIYADH_TZ).strftime("%Y-%m-%d")
    for it in items:
        pub = parse_utc(it.get("published_utc"))
        if pub is None:
            it["day_local"] = (it.get("published_riyadh") or "")[:10] or today
            it["age_hours"] = None
        else:
            it["day_local"] = pub.astimezone(RIYADH_TZ).strftime("%Y-%m-%d")
            it["age_hours"] = round(max(0.0, (now - pub).total_seconds() / 3600), 1)
        it["is_today"] = it["day_local"] == today


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


def _sort_key(it: dict[str, Any]) -> tuple:
    return (it.get("day_local") or "", -MARKET_ORDER.get(it.get("market"), 3), it.get("published_utc") or "")


def rank_items(items: list[dict[str, Any]], now: datetime | None = None, max_items: int = MAX_TOTAL,
               per_day: int = MAX_PER_DAY, today_quota: dict[str, int] | None = None,
               day_quota: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Rank for the news.json view.

    Primary key: day_local (Riyadh date) descending, so today's items always
    come first. Within a day: sa, then us, then macro, then other, each newest
    first. Caps: `max_items` overall; today may hold up to the sum of
    today_quota (40 sa + 40 us + 20 macro + 10 other = 110), older days up to
    `per_day`. Every day is filled in two passes: per-market reservations
    first (so Saudi volume cannot crowd out US items), then any remaining day
    slots in market order. Returns (ranked, dropped_by_cap).
    """
    now = now or datetime.now(timezone.utc)
    today_quota = today_quota or TODAY_QUOTA
    day_quota = day_quota or DAY_QUOTA
    add_day_fields(items, now)
    today = now.astimezone(RIYADH_TZ).strftime("%Y-%m-%d")

    by_day: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_day.setdefault(it["day_local"], []).append(it)

    selected: list[dict[str, Any]] = []
    for day in sorted(by_day, reverse=True):
        if len(selected) >= max_items:
            break
        is_today = day == today
        quota = today_quota if is_today else day_quota
        day_cap = max(per_day, sum(quota.values())) if is_today else per_day
        day_cap = min(day_cap, max_items - len(selected))
        day_items = sorted(by_day[day], key=lambda i: i.get("published_utc") or "", reverse=True)
        day_items.sort(key=lambda i: MARKET_ORDER.get(i.get("market"), 3))  # stable: newest first per market
        taken: list[dict[str, Any]] = []
        used: dict[str, int] = {}
        for it in day_items:  # pass 1: per-market quota
            m = it.get("market") or "other"
            if len(taken) >= day_cap:
                break
            if used.get(m, 0) < quota.get(m, 0):
                used[m] = used.get(m, 0) + 1
                taken.append(it)
        if len(taken) < day_cap:  # pass 2: fill remaining slots in market order
            chosen = {id(i) for i in taken}
            for it in day_items:
                if len(taken) >= day_cap:
                    break
                if id(it) not in chosen:
                    taken.append(it)
        selected.extend(taken)

    selected.sort(key=_sort_key, reverse=True)
    return selected, len(items) - len(selected)


def days_summary(items: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("day_local") or ""] = counts.get(it.get("day_local") or "", 0) + 1
    return [{"day_local": d, "count": counts[d], "label_ar": day_label_ar(d), "is_today": d == today}
            for d in sorted(counts, reverse=True)]


# ---------------------------------------------------------------------------
# Archive (7-day rolling)
# ---------------------------------------------------------------------------
def load_archive(path: str | None) -> list[dict[str, Any]]:
    """Existing analyzed items ({"items": [...]} or a bare list). Missing/corrupt -> []."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("archive %s unreadable (%s); starting fresh", path, exc)
        return []
    items = data.get("items") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get("title") and it.get("market") in MARKETS:
            it = dict(it)
            it["id"] = it.get("id") or item_id(it)
            out.append(it)
    return out


def expire_items(items: list[dict[str, Any]], now: datetime, days: int) -> tuple[list[dict[str, Any]], int]:
    """Drop items whose published_utc is older than `days` (or unparsable)."""
    cutoff = now - timedelta(days=days)
    kept = []
    for it in items:
        pub = parse_utc(it.get("published_utc"))
        if pub is not None and pub >= cutoff:
            kept.append(it)
    return kept, len(items) - len(kept)


def split_new_items(raw_items: list[dict[str, Any]], archive: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Return (raw items not yet in the archive, number already known). An item
    is known when its id or normalized title matches an archived item."""
    known_ids = {a["id"] for a in archive}
    known_titles = {title_key(a.get("title", "")) for a in archive}
    new, dup = [], 0
    for it in raw_items:
        it = dict(it)
        it["id"] = it.get("id") or item_id(it)
        tk = title_key(it.get("title", ""))
        if it["id"] in known_ids or (tk and tk in known_titles):
            dup += 1
            continue
        known_ids.add(it["id"])
        if tk:
            known_titles.add(tk)
        new.append(it)
    return new, dup


def analyze(raw: dict[str, Any], rules: dict[str, Any], constituents: list[dict[str, Any]],
            market: dict[str, Any] | None = None, api_key: str | None = None,
            model: str = DEFAULT_MODEL, batch_size: int = 10,
            use_fallbacks: bool = True, max_items: int = MAX_TOTAL, per_day: int = MAX_PER_DAY,
            min_relevance: float = RELEVANCE_MIN, constituents_us: list[dict[str, Any]] | None = None,
            archive: list[dict[str, Any]] | None = None, days: int = DEFAULT_DAYS,
            now: datetime | None = None, max_non_sa: int | None = None) -> dict[str, Any]:
    """Analyze new raw items, merge them into the rolling archive and build the
    ranked view. The merged archive (full, unranked, newest first) is returned
    under the "_archive" key; main() writes it to --archive and drops the key
    before writing news.json. `max_non_sa` is accepted for backwards
    compatibility and ignored (per-day/per-market quotas replaced it)."""
    now = now or datetime.now(timezone.utc)
    items = raw.get("items") if isinstance(raw, dict) else raw
    items = list(items or [])
    archive = list(archive or [])
    matcher = build_matcher(constituents)
    us_matcher = build_us_matcher(constituents_us or [])

    new_items, already_known = split_new_items(items, archive)
    enriched: list[dict[str, Any]] = []
    dropped_relevance = 0
    first_seen = now.strftime(UTC_FMT)
    for it in new_items:
        _fix_future_time(it)
        if "relevance" not in it:
            it["relevance"] = (relevance_score(it.get("title", ""), it.get("summary", ""), it.get("source", ""),
                                               it.get("link", "")) if relevance_score else 1.0)
        if it["relevance"] < min_relevance:
            dropped_relevance += 1
            log.debug("dropped irrelevant (%.2f): %s", it["relevance"], it.get("title", "")[:80])
            continue
        it.update(analyze_rules(it, rules, matcher, us_matcher))
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
        it.setdefault("first_seen_utc", first_seen)
        enriched.append(it)

    llm_used = 0
    if api_key:
        for i in range(0, len(enriched), max(1, batch_size)):
            batch = enriched[i:i + batch_size]
            try:
                res = analyze_llm_batch(batch, constituents, market, api_key, model, use_fallbacks,
                                        constituents_us=constituents_us)
            except Exception as exc:  # noqa: BLE001
                log.error("LLM batch %d-%d failed, keeping rules result: %s", i, i + len(batch), exc)
                continue
            for it in batch:
                a = res.get(it["id"])
                if a:
                    # keep rule-matched tickers the model missed; rules ids kept for transparency
                    codes = {t["code"] for t in a["tickers"]}
                    a["tickers"] = sorted(a["tickers"] + [t for t in it["tickers"] if t["code"] not in codes],
                                          key=lambda x: (x.get("market", "sa"), x["code"]))
                    it.update(a)
                    llm_used += 1
    # Merge into the rolling archive: prior analysis kept, expired items dropped.
    merged, expired = expire_items(archive + enriched, now, days)
    n_llm = sum(1 for i in merged if i.get("analysis") == "llm")
    mode = "llm" if merged and n_llm == len(merged) else "llm+rules" if n_llm else "rules"
    for it in merged:  # older archives may predate these fields
        it.setdefault("impact_market", derive_impact_market(
            it.get("market", "other"), [], any(t.get("market", "sa") == "sa" for t in it.get("tickers") or []),
            any(t.get("market") == "us" for t in it.get("tickers") or [])))
        for t in it.get("tickers") or []:
            t.setdefault("market", "sa")
    add_day_fields(merged, now)
    merged.sort(key=lambda i: i.get("published_utc") or "", reverse=True)

    ranked, dropped_cap = rank_items([dict(i) for i in merged], now=now, max_items=max_items, per_day=per_day)
    today = now.astimezone(RIYADH_TZ).strftime("%Y-%m-%d")
    by_market = {m: sum(1 for i in ranked if i["market"] == m) for m in MARKETS}
    days_list = days_summary(ranked, today)
    log.info("analysis mode=%s in=%d new=%d known=%d archive=%d expired=%d view=%d llm=%d dropped: irrelevant=%d cap=%d  by_market=%s",
             mode, len(items), len(enriched), already_known, len(merged), expired, len(ranked), llm_used,
             dropped_relevance, dropped_cap, by_market)
    return {
        "generated_at_utc": now.strftime(UTC_FMT),
        "today_riyadh": today,
        "source_generated_at_utc": raw.get("generated_at_utc") if isinstance(raw, dict) else None,
        "analysis_mode": mode,
        "model": model if api_key else None,
        "days_window": days,
        "dropped": dropped_relevance + dropped_cap,
        "stats": {"input": len(items), "new_items": len(enriched), "already_known": already_known,
                  "archive_total": len(merged), "expired": expired, "kept": len(ranked),
                  "dropped_irrelevant": dropped_relevance, "dropped_cap": dropped_cap, "by_market": by_market,
                  "by_day": {d["day_local"]: d["count"] for d in days_list}, "llm_items": llm_used,
                  "fetch_dropped": raw.get("dropped") if isinstance(raw, dict) else None},
        "sectors": {k: rules["sectors"][k] for k in SECTOR_KEYS},
        "days": days_list,
        "items": ranked,
        "_archive": merged,
    }


def archive_document(result: dict[str, Any]) -> dict[str, Any]:
    """The data/news_archive.json document for an analyze() result."""
    items = result.get("_archive") or []
    return {"generated_at_utc": result.get("generated_at_utc"), "days": result.get("days_window", DEFAULT_DAYS),
            "count": len(items), "items": items}


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
    ap.add_argument("--out", required=True, help="ranked view (data/news.json)")
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE,
                    help=f"rolling archive read+written each run ({DEFAULT_ARCHIVE}); '' disables")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"archive window in days ({DEFAULT_DAYS})")
    ap.add_argument("--market", help="data/market.json (optional context for the LLM)")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--constituents", default=DEFAULT_CONSTITUENTS)
    ap.add_argument("--constituents-us", default=DEFAULT_CONSTITUENTS_US, help="US symbols/names (constituents_us.json)")
    ap.add_argument("--no-llm", action="store_true", help="force rules mode even if ANTHROPIC_API_KEY is set")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--max", type=int, default=MAX_TOTAL, help=f"total item cap of the view ({MAX_TOTAL})")
    ap.add_argument("--per-day", type=int, default=MAX_PER_DAY, help=f"per-day cap for older days ({MAX_PER_DAY})")
    ap.add_argument("--max-non-sa", type=int, default=None, help=argparse.SUPPRESS)  # deprecated no-op
    ap.add_argument("--min-relevance", type=float, default=RELEVANCE_MIN, help="drop items below this relevance (0.35)")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    ap.add_argument("--now", help="override 'now' (UTC, YYYY-MM-DDTHH:MM:SSZ) for reproducible runs/tests")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    raw = _load_json(args.inp, required=True)
    rules = load_rules(args.rules)
    constituents = load_constituents(args.constituents)
    constituents_us = load_constituents_us(args.constituents_us)
    market = _load_json(args.market)
    archive = load_archive(args.archive)
    if args.archive:
        log.info("archive %s: %d items", args.archive, len(archive))
    now = parse_utc(args.now) if args.now else None
    if args.now and now is None:
        ap.error("--now must be UTC like 2026-09-09T12:00:00Z")
    api_key = None if args.no_llm else (os.environ.get("ANTHROPIC_API_KEY") or None)
    if api_key:
        log.info("LLM mode enabled (model=%s)", args.model)
    else:
        log.info("rules mode (no ANTHROPIC_API_KEY or --no-llm)")
    use_fallbacks = os.environ.get("NEWS_LLM_FALLBACKS", "1") not in ("0", "false", "no")

    result = analyze(raw, rules, constituents, market=market, api_key=api_key, model=args.model,
                     batch_size=args.batch, use_fallbacks=use_fallbacks, max_items=args.max, per_day=args.per_day,
                     min_relevance=args.min_relevance, constituents_us=constituents_us, archive=archive,
                     days=args.days, now=now)
    archive_doc = archive_document(result)
    result.pop("_archive", None)
    if args.archive:
        os.makedirs(os.path.dirname(os.path.abspath(args.archive)), exist_ok=True)
        with open(args.archive, "w", encoding="utf-8") as fh:
            json.dump(archive_doc, fh, ensure_ascii=False, indent=1)
        log.info("wrote %s (%d items over %d days)", args.archive, archive_doc["count"], args.days)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    log.info("wrote %s (%d items, mode=%s)", args.out, len(result["items"]), result["analysis_mode"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
