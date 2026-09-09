#!/usr/bin/env python3
"""Fetch RSS/Atom news feeds for the Saudi + US market dashboard (Saudi-first priority).

Usage:
    python scripts/fetch_news.py --out data/news_raw.json [--hours 168] [--max 400]
                                 [--feeds feeds.json] [--timeout 15] [--us-reserve 120]

Feeds carry priority 1 (Saudi), 2 (Gulf) or 3 (global macro / US). US feeds are
tagged market_hint="us" (copied onto their items) so the analyzer can classify
generic English market headlines from CNBC/MarketWatch/... as US news.

Only stdlib + `requests`. Every feed failure is logged and skipped; the script
exits 0 if at least one feed was parsed successfully, 1 otherwise. The output
file is always written (possibly with an empty item list) so downstream steps
have a well-formed input.

Output schema (data/news_raw.json):
{
  "generated_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "window_hours": 168,
  "feeds": [{"name", "url", "priority", "ok", "count", "kept", "error",
             "effective_url"?, "notes"?}],   # notes: 403 retry / autodiscovery trail
  "items": [
    {"title", "link", "source", "published_utc", "published_riyadh",
     "precision": "second"|"minute"|"day", "summary", "fetched_at_utc",
     "lang": "ar"|"en", "priority": int, "relevance": 0..1, "raw_published": str|null,
     "market_hint"?: "us", "time_adjusted"?: "riyadh_local"|"clamped"}
  ],
  "dropped": int, "stats": {raw, deduped, kept, dropped_irrelevant, dropped_old_or_undated, dropped_cap, duplicates}
  ]
}
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

try:  # requests is provided by scripts/requirements.txt (owned elsewhere)
    import requests
except ImportError:  # pragma: no cover - keeps offline tests importable
    requests = None  # type: ignore

log = logging.getLogger("fetch_news")

RIYADH_TZ = timezone(timedelta(hours=3), name="Asia/Riyadh")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 MarsadNewsBot/1.0 (+https://github.com)"
)

# ---------------------------------------------------------------------------
# Feed list. priority: 1 = Saudi-specific (highest), 2 = Gulf/regional,
# 3 = global macro. Broken URLs are simply logged; several variants are listed
# for publishers whose RSS path is not stable. `lang` is a hint only.
# ---------------------------------------------------------------------------
def gnews(query: str, lang: str = "ar") -> str:
    """Google News RSS search URL (worked reliably from GitHub Actions)."""
    if lang == "ar":
        tail = "hl=ar&gl=SA&ceid=SA:ar"
    else:
        tail = "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&{tail}"


def us_feed(name: str, url: str, lang: str = "en") -> dict[str, Any]:
    """US-market feed (priority 3, tagged market_hint="us" for the analyzer)."""
    return {"name": name, "priority": 3, "lang": lang, "url": url, "market_hint": "us"}


DEFAULT_FEEDS: list[dict[str, Any]] = [
    # --- Google News, scoped to Saudi sources that block/omit direct RSS ---
    # (Google News worked from GitHub Actions; these guarantee Saudi coverage)
    {"name": "Google News – تداول/تاسي", "priority": 1, "lang": "ar",
     "url": "https://news.google.com/rss/search?q=Tadawul+OR+%D8%AA%D8%A7%D8%B3%D9%8A&hl=ar&gl=SA&ceid=SA:ar"},
    {"name": "Google News – أرقام", "priority": 1, "lang": "ar", "url": gnews("site:argaam.com when:2d")},
    {"name": "Google News – Saudi Exchange", "priority": 1, "lang": "ar", "url": gnews("site:saudiexchange.sa when:2d")},
    {"name": "Google News – مباشر السعودية", "priority": 1, "lang": "ar", "url": gnews("site:mubasher.info السعودية when:2d")},
    {"name": "Google News – الاقتصادية", "priority": 1, "lang": "ar", "url": gnews("site:aleqt.com when:2d")},
    {"name": "Google News – معال", "priority": 1, "lang": "ar", "url": gnews("site:maaal.com when:2d")},
    {"name": "Google News – Arab News Business", "priority": 1, "lang": "en", "url": gnews("site:arabnews.com business when:2d", "en")},
    {"name": "Google News – Reuters Saudi", "priority": 1, "lang": "en",
     "url": gnews("site:reuters.com (Saudi OR Tadawul OR Aramco) when:2d", "en")},
    {"name": "Google News – السوق السعودية", "priority": 1, "lang": "ar", "url": gnews("السوق السعودية أسهم when:2d")},
    {"name": "Google News – العربية أسواق", "priority": 2, "lang": "ar", "url": gnews("site:alarabiya.net أسواق when:2d")},
    {"name": "Google News – CNBC عربية", "priority": 2, "lang": "ar", "url": gnews("site:cnbcarabia.com when:2d")},
    {"name": "Google News – الشرق بلومبرغ", "priority": 2, "lang": "ar", "url": gnews("site:asharqbusiness.com السعودية when:2d")},
    # --- Saudi Exchange / official (403 -> alt browser profile retry) ------
    {"name": "Saudi Exchange – إعلانات الشركات", "priority": 1, "lang": "ar",
     "url": "https://www.saudiexchange.sa/wps/portal/saudiexchange/rss/announcements?locale=ar"},
    {"name": "Saudi Exchange – Announcements", "priority": 1, "lang": "en",
     "url": "https://www.saudiexchange.sa/wps/portal/saudiexchange/rss/announcements?locale=en"},
    {"name": "واس – RSS", "priority": 1, "lang": "ar", "url": "https://www.spa.gov.sa/rss?lang=ar"},
    {"name": "واس – الاقتصاد", "priority": 1, "lang": "ar", "url": "https://www.spa.gov.sa/rss/economy"},
    {"name": "SPA – RSS (EN)", "priority": 1, "lang": "en", "url": "https://www.spa.gov.sa/rss?lang=en"},
    # --- Saudi financial press (candidates; HTML answers go through autodiscovery) --
    {"name": "أرقام – الأحدث", "priority": 1, "lang": "ar", "url": "https://www.argaam.com/ar/rss/latest"},
    {"name": "أرقام", "priority": 1, "lang": "ar", "url": "https://www.argaam.com/ar/rss"},
    {"name": "Argaam (EN)", "priority": 1, "lang": "en", "url": "https://www.argaam.com/en/rss/latest"},
    {"name": "مباشر – السعودية", "priority": 1, "lang": "ar", "url": "https://www.mubasher.info/countries/sa/news"},
    {"name": "Mubasher (EN)", "priority": 1, "lang": "en", "url": "https://english.mubasher.info/countries/sa/news"},
    {"name": "الاقتصادية", "priority": 1, "lang": "ar", "url": "https://www.aleqt.com/rss.xml"},
    {"name": "الاقتصادية – feed", "priority": 1, "lang": "ar", "url": "https://www.aleqt.com/feed"},
    {"name": "معال", "priority": 1, "lang": "ar", "url": "https://maaal.com/feed/"},
    {"name": "Arab News", "priority": 1, "lang": "en", "url": "https://www.arabnews.com/rss.xml"},
    {"name": "Arab News – Business", "priority": 1, "lang": "en", "url": "https://www.arabnews.com/cat/2/rss.xml"},
    {"name": "Saudi Gazette – Business", "priority": 1, "lang": "en", "url": "https://saudigazette.com.sa/rssFeed/74"},
    # --- Gulf / regional -------------------------------------------------
    {"name": "العربية – أسواق", "priority": 2, "lang": "ar", "url": "https://www.alarabiya.net/.rss/ar/aswaq.xml"},
    {"name": "العربية – أسواق (feed)", "priority": 2, "lang": "ar", "url": "https://www.alarabiya.net/feed/rss2/ar/aswaq.xml"},
    {"name": "العربية – RSS tools", "priority": 2, "lang": "ar", "url": "https://www.alarabiya.net/tools/rss"},
    {"name": "الشرق بلومبرغ", "priority": 2, "lang": "ar", "url": "https://asharqbusiness.com/rss"},
    {"name": "CNBC عربية", "priority": 2, "lang": "ar", "url": "https://www.cnbcarabia.com/rss"},
    {"name": "Zawya – Saudi Arabia", "priority": 2, "lang": "en", "url": "https://www.zawya.com/en/rss/saudi-arabia"},
    # --- Global macro --------------------------------------------------
    {"name": "Google News – Oil/OPEC/Fed", "priority": 3, "lang": "en",
     "url": gnews("(Brent OR OPEC OR \"Federal Reserve\") when:2d", "en")},
    # --- US market (direct RSS first; Google News site-scoped queries as fallback) --
    us_feed("CNBC – Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    us_feed("CNBC – Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    us_feed("CNBC – Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    us_feed("MarketWatch – Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    us_feed("MarketWatch – Market Pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    us_feed("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    us_feed("Yahoo Finance – Headlines", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC,^IXIC,^DJI&region=US&lang=en-US"),
    us_feed("WSJ – Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    us_feed("Barron's – Markets", "https://feeds.content.dowjones.io/public/rss/RSSBarronsMarkets"),
    us_feed("Seeking Alpha – Market News", "https://seekingalpha.com/market_currents.xml"),
    us_feed("Investing.com – Stock Market News", "https://www.investing.com/rss/news_25.rss"),
    us_feed("Federal Reserve – Press Releases", "https://www.federalreserve.gov/feeds/press_all.xml"),
    us_feed("SEC – Press Releases", "https://www.sec.gov/news/pressreleases.rss"),
    us_feed("Google News – Reuters Business", gnews("site:reuters.com business markets when:2d", "en")),
    us_feed("Google News – Reuters Markets", gnews("site:reuters.com markets stocks when:2d", "en")),
    us_feed("Google News – Bloomberg Markets", gnews("site:bloomberg.com markets when:2d", "en")),
    us_feed("Google News – CNBC", gnews("site:cnbc.com stocks OR markets OR earnings when:2d", "en")),
    us_feed("Google News – MarketWatch", gnews("site:marketwatch.com stocks OR markets when:2d", "en")),
    us_feed("Google News – Wall Street", gnews("\"Wall Street\" stocks when:2d", "en")),
    us_feed("Google News – S&P Nasdaq Dow", gnews("\"S&P 500\" OR Nasdaq OR \"Dow Jones\" when:2d", "en")),
    us_feed("Google News – Fed rates", gnews("Fed rates OR FOMC OR \"Federal Reserve\" when:2d", "en")),
    us_feed("Google News – Earnings", gnews("\"earnings report\" OR \"quarterly results\" stock when:2d", "en")),
    us_feed("Google News – الأسهم الأمريكية", gnews("(وول ستريت OR ناسداك OR \"داو جونز\" OR الفيدرالي) when:2d"), "ar"),
]

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "rss1": "http://purl.org/rss/1.0/",
    "media": "http://search.yahoo.com/mrss/",
}


# ---------------------------------------------------------------------------
# HTML / text helpers
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(raw: str | None) -> str:
    """Remove tags/entities and collapse whitespace. Safe on malformed HTML."""
    if not raw:
        return ""
    text = unescape(raw)
    try:
        p = _TextExtractor()
        p.feed(text)
        p.close()
        text = "".join(p.parts)
    except Exception:  # pragma: no cover - HTMLParser is very tolerant
        text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_ARABIC_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = _ARABIC_DIACRITICS.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "utm_id", "fbclid", "gclid", "ref", "ocid", "cmpid", "mc_cid", "mc_eid"}


def normalize_link(link: str) -> str:
    if not link:
        return ""
    try:
        u = urlparse(link.strip())
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
        path = u.path.rstrip("/") or "/"
        return urlunparse((u.scheme.lower() or "https", u.netloc.lower(), path, "",
                           urlencode(q), ""))
    except Exception:
        return link.strip().lower()


def detect_lang(text: str) -> str:
    arabic = len(re.findall(r"[؀-ۿ]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return "ar" if arabic >= latin and arabic > 0 else "en"


# ---------------------------------------------------------------------------
# Relevance gate (finance/market terms; off-topic sections penalised)
# ---------------------------------------------------------------------------
def norm_text(text: str) -> str:
    """Lowercase + NFKC + strip Arabic diacritics + fold hamza/ta-marbuta (keeps punctuation)."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _ARABIC_DIACRITICS.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", t).strip()


FINANCE_TERMS = [
    # Arabic
    "سوق", "اسواق", "اسهم", "سهم", "تداول", "تاسي", "مؤشر", "بورصه", "ارباح", "ربح", "شركه", "شركات",
    "بنك", "بنوك", "مصرف", "مصارف", "نفط", "خام", "برميل", "اوبك", "الفيدرالي", "فائده", "سندات", "صكوك",
    "اكتتاب", "ادراج", "توزيعات", "استثمار", "استثمارات", "صندوق", "اقتصاد", "اقتصادي", "تضخم", "ريال",
    "دولار", "مليار", "مليون", "عقد", "ترسيه", "استحواذ", "نتائج", "ايرادات", "بتروكيماويات", "تمويل",
    "قرض", "تصنيف", "الناتج المحلي", "صادرات", "عمله", "ذهب", "ارامكو", "هيئه السوق", "ساما", "حقوق اولويه",
    "راس المال", "مساهمين", "طرح", "غاز", "مصفاه", "مشتقات", "اسمنت", "تأمين", "تامين", "عقار", "عقاري",
    "سابك", "معادن", "الراجحي", "اس تي سي", "الصادرات", "واردات", "ميزانيه", "عجز", "فائض", "تجاره", "تجاري",
    # English
    "stock", "stocks", "shares", "share price", "market", "markets", "index", "tadawul", "tasi", "earnings",
    "profit", "profits", "revenue", "revenues", "bank", "banks", "oil", "crude", "barrel", "barrels", "opec",
    "rates", "rate cut", "rate hike", "interest rate", "bond", "bonds", "sukuk", "ipo", "dividend", "dividends",
    "investor", "investors", "investment", "fund", "economy", "economic", "inflation", "gdp", "riyal", "dollar",
    "billion", "million", "contract", "acquisition", "merger", "listing", "exchange", "aramco", "sabic", "pif",
    "treasury", "yields", "tariff", "tariffs", "central bank", "sama", "fomc", "cpi", "exports", "refinery",
    "petrochemical", "petrochemicals", "lng", "cement", "insurer", "insurance", "real estate", "reit",
    "wall street", "wall st", "nasdaq", "s&p", "s&p 500", "dow jones", "وول ستريت", "ناسداك", "داو جونز",
    "nyse", "dow", "futures", "rally", "rallies", "sell-off", "selloff", "buyback", "guidance", "wall street",
    "semiconductor", "chipmaker", "payrolls", "jobs report", "sec", "الأسهم الأمريكية", "الأسواق الأمريكية",
    "brent", "برنت", "fed",  # ambiguous: count only with context (AMBIGUOUS_CONTEXT)
]
OFFTOPIC_TERMS = [
    "sport", "sports", "football", "soccer", "nba", "nfl", "mlb", "nhl", "cricket", "tennis", "golf", "f1",
    "formula 1", "entertainment", "celebrity", "celebrities", "movie", "movies", "film", "music", "recipe",
    "recipes", "gaming", "video game", "video games", "esports", "fashion", "horoscope", "lifestyle",
    "رياضه", "رياضي", "رياضيه", "كره القدم", "دوري روشن", "مباراه", "فنان", "فنانه", "مشاهير", "طبخ", "وصفه",
    "العاب", "مسلسل", "سينما", "ازياء", "نجوم الفن",
]
# Ambiguous words that count only with a nearby context term.
AMBIGUOUS_CONTEXT = {
    "brent": ["oil", "crude", "barrel", "barrels", "opec", "خام", "نفط", "برميل", "$", "dollar", "دولار", "price", "prices", "سعر"],
    "برنت": ["خام", "نفط", "برميل", "دولار", "سعر", "اوبك", "oil", "crude", "$"],
    "fed": ["rate", "rates", "interest", "fomc", "powell", "فائده", "inflation", "basis points", "central bank", "الفيدرالي"],
    "كيان": ["سهم", "اسهم", "شركه", "الشركه", "2350", "تداول", "سابك", "بتروكيماويات"],
    "kayan": ["stock", "shares", "share", "company", "2350", "tadawul", "sabic", "petrochemical"],
    "علم": ["سهم", "اسهم", "شركه", "الشركه", "7203", "تداول", "التقنيه", "رقمي"],
    "elm": ["stock", "shares", "share", "company", "7203", "tadawul", "digital", "tech"],
    # US-market words that are common outside finance
    "sec": ["securities", "charges", "charged", "filing", "regulator", "exchange commission", "chair", "enforcement", "fraud"],
    "rally": ["stock", "stocks", "shares", "market", "markets", "index", "nasdaq", "s&p", "dow", "wall street", "oil", "gold", "bond", "dollar"],
    "rallies": ["stock", "stocks", "shares", "market", "markets", "index", "nasdaq", "s&p", "dow", "wall street", "oil", "gold", "bond", "dollar"],
    "guidance": ["earnings", "revenue", "forecast", "quarter", "stock", "shares", "profit", "outlook", "sales"],
    "dow": ["jones", "points", "stocks", "s&p", "nasdaq", "index", "futures", "wall street"],
}
RELEVANCE_MIN = 0.35
FUTURE_TOLERANCE = timedelta(minutes=5)  # beyond this a publish time is treated as mislabeled


def _term_re(term: str) -> re.Pattern:
    t = re.escape(norm_text(term))
    if re.search(r"[\u0600-\u06FF]", term):  # Arabic: allow clitic prefixes and pronoun/plural suffixes
        return re.compile(r"(?<![\w])(?:و|ب|ل|ك|ف|ال|وال|بال|فال|لل)?" + t
                          + r"(?:ها|ه|هم|هن|هما|نا|كم|ك|ي|ات|ان|ين|ون|تها|ته|تهم|تنا|تي)?(?![\w])")
    return re.compile(r"(?<![\w])" + t + r"(?![\w])")


_FINANCE_RE = [(t, _term_re(t)) for t in FINANCE_TERMS]
_OFFTOPIC_RE = [(t, _term_re(t)) for t in OFFTOPIC_TERMS]
_OFFTOPIC_PATH_RE = re.compile(r"/(sport|sports|entertainment|celebrity|lifestyle|recipes?|gaming|games|fashion|"
                               r"music|movies?|film|رياضه|رياضة|فن|منوعات)(/|$|\?)", re.I)


def ambiguous_ok(term: str, text_norm: str) -> bool:
    """True when `term` is not ambiguous, or its required context is present in the text."""
    ctx = AMBIGUOUS_CONTEXT.get(norm_text(term))
    if ctx is None:
        return True
    return any(norm_text(c) in text_norm for c in ctx)


def relevance_score(title: str, summary: str = "", source: str = "", link: str = "") -> float:
    """0..1 finance/market relevance. 0 = no finance term at all.

    base = 0.4 for the first finance term, +0.15 per extra distinct term (max 3),
    +0.15 if a term is in the title; x0.25 if an off-topic marker is found in the
    title/summary, source name, or URL path (sports, entertainment, recipes...).
    Deterministic; used as a gate with RELEVANCE_MIN.
    """
    tn = norm_text(title)
    full = norm_text(f"{title} . {summary}")
    hits = [t for t, rx in _FINANCE_RE if rx.search(full) and ambiguous_ok(t, full)]
    if not hits:
        return 0.0
    score = 0.4 + 0.15 * min(len(hits) - 1, 3)
    if any(rx.search(tn) for t, rx in _FINANCE_RE if t in hits):
        score += 0.15
    score = min(1.0, score)
    src = norm_text(source)
    path = urlparse(link or "").path
    offtopic = (any(rx.search(full) or rx.search(src) for _, rx in _OFFTOPIC_RE)
                or bool(_OFFTOPIC_PATH_RE.search(path)))
    if offtopic:
        score *= 0.25
    return round(score, 2)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
_TZ_ABBREV = {
    "AST": 3 * 3600, "KSA": 3 * 3600, "GST": 4 * 3600, "EET": 2 * 3600, "EEST": 3 * 3600,
    "CET": 3600, "CEST": 2 * 3600, "BST": 3600, "IST": 5 * 3600 + 1800,
    "EST": -5 * 3600, "EDT": -4 * 3600, "CST": -6 * 3600, "CDT": -5 * 3600,
    "PST": -8 * 3600, "PDT": -7 * 3600, "UTC": 0, "GMT": 0, "Z": 0,
}


def detect_precision(raw: str) -> str:
    """What the feed actually provided: seconds, minutes or only a date."""
    s = raw.strip()
    if re.search(r"\d{1,2}:\d{2}:\d{2}", s):
        return "second"
    if re.search(r"\d{1,2}:\d{2}", s):
        return "minute"
    return "day"


def parse_date(raw: str | None) -> tuple[datetime | None, str]:
    """Parse RFC 822 / ISO 8601 / loose date strings to an aware UTC datetime.

    Returns (datetime_utc_or_None, precision).
    """
    if not raw or not raw.strip():
        return None, "day"
    s = raw.strip()
    precision = detect_precision(s)
    dt: datetime | None = None

    # Regional tz abbreviations: email.utils reads "AST" as Atlantic (-4); for
    # Gulf feeds it means Arabia Standard Time (+3). Rewrite to numeric offsets.
    m_abbr = re.search(r"\s([A-Z]{1,5})$", s)
    if m_abbr and m_abbr.group(1) in _TZ_ABBREV and m_abbr.group(1) not in ("UTC", "GMT", "Z"):
        off = _TZ_ABBREV[m_abbr.group(1)]
        sign = "+" if off >= 0 else "-"
        off = abs(off)
        s = s[:m_abbr.start()] + f" {sign}{off // 3600:02d}{(off % 3600) // 60:02d}"

    # 1) RFC 822 (RSS pubDate, dc:date sometimes)
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None

    # 2) ISO 8601 (Atom updated/published, dc:date)
    if dt is None:
        iso = s.replace("Z", "+00:00").replace("z", "+00:00")
        iso = re.sub(r"(\.\d{3})\d+", r"\1", iso)  # trim >6 fractional digits
        if re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
            iso += "T00:00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = None

    # 3) Loose formats with a trailing tz abbreviation or none
    if dt is None:
        m = re.match(r"^(?:[A-Za-z]{3,9},?\s+)?(.+?)(?:\s+([A-Z]{1,5}))?$", s)
        body, abbr = (m.group(1), m.group(2)) if m else (s, None)
        for fmt in ("%d %b %Y %H:%M:%S", "%d %b %Y %H:%M", "%d %B %Y %H:%M:%S",
                    "%d %B %Y %H:%M", "%d %b %Y", "%d %B %Y", "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(body, fmt)
                if abbr and abbr.upper() in _TZ_ABBREV:
                    dt = dt.replace(tzinfo=timezone(timedelta(seconds=_TZ_ABBREV[abbr.upper()])))
                break
            except ValueError:
                continue

    if dt is None:
        return None, precision
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)  # assume UTC when the feed omits tz
    return dt.astimezone(timezone.utc), precision


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_riyadh(dt: datetime) -> str:
    return dt.astimezone(RIYADH_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Google News link handling
# ---------------------------------------------------------------------------
_GN_ARTICLE = re.compile(r"news\.google\.com/(?:rss/)?articles/([A-Za-z0-9_\-]+)")


def resolve_google_news_link(link: str) -> str:
    """Best-effort, offline decode of the legacy base64 Google News article id.

    The older `CBMi...` ids embed the target URL in a protobuf; newer `AU_yqL...`
    ids need a network round-trip (not cheap) so we keep the Google link.
    """
    m = _GN_ARTICLE.search(link or "")
    if not m:
        return link
    token = m.group(1)
    try:
        pad = "=" * (-len(token) % 4)
        blob = base64.urlsafe_b64decode(token + pad)
    except Exception:
        return link
    urls = re.findall(rb"https?://[\x21-\x7e]+", blob)
    for u in urls:
        cand = u.decode("ascii", "ignore").rstrip("\x00")
        # protobuf length bytes may trail; cut at first non-URL char
        cand = re.split(r"[\s\"'<>\\\x00-\x1f]", cand)[0]
        if len(cand) > 12 and "google.com" not in cand:
            return cand
    return link


# ---------------------------------------------------------------------------
# Feed parsing (RSS 2.0, RSS 1.0/RDF, Atom)
# ---------------------------------------------------------------------------
_XML_DECL = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", re.I)


def _decode(raw: bytes) -> str:
    m = _XML_DECL.match(raw)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    for e in (enc, "utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _clean_xml(text: str) -> str:
    # Remove control chars that ElementTree rejects; keep tab/newline/CR.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # Common broken-entity fix (`&` not followed by an entity) outside CDATA.
    return re.sub(r"&(?!#?\w+;)", "&amp;", text)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(el: ET.Element, *names: str) -> str | None:
    """First non-empty text among children whose local name matches."""
    for child in el:
        if _local(child.tag) in names:
            txt = (child.text or "").strip()
            if txt:
                return txt
    return None


def _atom_link(el: ET.Element) -> str | None:
    alt = None
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = child.get("rel") or "alternate"
        if href and rel == "alternate":
            return href
        if href and alt is None:
            alt = href
        if child.text and child.text.strip().startswith("http"):
            return child.text.strip()
    return alt


def parse_feed(raw: bytes, feed_name: str) -> list[dict[str, Any]]:
    """Return list of dicts: title, link, summary, date_raw, source."""
    text = _decode(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        root = ET.fromstring(_clean_xml(text))

    rtag = _local(root.tag)
    entries: list[ET.Element] = []
    if rtag == "feed":  # Atom
        entries = [e for e in root if _local(e.tag) == "entry"]
    else:  # RSS 2.0 (rss/channel/item) or RSS 1.0 (rdf:RDF/item)
        entries = [e for e in root.iter() if _local(e.tag) == "item"]

    out: list[dict[str, Any]] = []
    for e in entries:
        title = strip_html(_child_text(e, "title") or "")
        link = _child_text(e, "link", "guid", "id") if rtag != "feed" else _atom_link(e)
        if rtag != "feed":
            # `<link/>` empty but `<guid isPermaLink="true">` set
            if not link or not link.startswith("http"):
                guid = _child_text(e, "guid")
                link = guid if guid and guid.startswith("http") else (link or "")
        link = (link or "").strip()
        summary = (_child_text(e, "description", "summary", "content", "encoded") or "")
        date_raw = _child_text(e, "pubDate", "published", "updated", "date", "issued",
                               "created", "modified", "pubdate")
        source = feed_name
        src_el = next((c for c in e if _local(c.tag) == "source"), None)
        if src_el is not None and (src_el.text or "").strip():
            source = f"{src_el.text.strip()} (عبر {feed_name})"
        if not title:
            continue
        out.append({
            "title": title,
            "link": link,
            "summary": strip_html(summary)[:600],
            "date_raw": date_raw,
            "source": source,
        })
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
# Two realistic desktop browser profiles. A 403 with the first is retried once
# with the second (some Saudi CDNs block anything that names a bot).
UA_PROFILES: list[dict[str, str]] = [
    {"User-Agent": USER_AGENT,
     "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
     "Accept-Language": "ar,en;q=0.8"},
    {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"),
     "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
     "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
     "Cache-Control": "no-cache",
     "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
     "Upgrade-Insecure-Requests": "1"},
]


def looks_like_feed(body: bytes) -> bool:
    head = body[:8192].lower()
    return b"<" in head and any(m in head for m in (b"<rss", b"<feed", b"<rdf:rdf", b"<channel"))


class _FeedLinkFinder(HTMLParser):
    """Collects RSS/Atom autodiscovery links from an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[int, str]] = []  # (rank, href) lower rank = better

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        href = a.get("href", "").strip()
        if not href:
            return
        if tag == "link":
            rel = a.get("rel", "").lower()
            typ = a.get("type", "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
                self.links.append((0 if "rss" in typ else 1, href))
        elif tag == "a":
            h = href.lower()
            if re.search(r"(/rss(\.xml|/|$|\?)|\.rss($|\?)|/feed(/|$|\?)|feed\.xml|rss\.xml|/atom(\.xml)?($|\?))", h):
                self.links.append((2, href))


def discover_feed_url(html: bytes, base_url: str) -> str | None:
    """Return the best RSS/Atom URL advertised by an HTML page, or None."""
    try:
        p = _FeedLinkFinder()
        p.feed(_decode(html))
        p.close()
    except Exception:  # pragma: no cover
        return None
    if not p.links:
        return None
    p.links.sort(key=lambda x: x[0])
    href = p.links[0][1]
    if href.startswith("//"):
        href = urlparse(base_url).scheme + ":" + href
    return urljoin(base_url, href)


def _http_get(url: str, timeout: float, profile: dict[str, str]):
    """Thin wrapper (mocked in tests)."""
    if requests is None:
        raise RuntimeError("requests is not installed")
    return requests.get(url, headers=profile, timeout=timeout, allow_redirects=True)


def fetch_feed(url: str, timeout: float, retries: int = 1, discover: bool = True,
               notes: list[str] | None = None) -> tuple[bytes, str]:
    """Fetch a feed. Returns (bytes, effective_url).

    * local paths / file:// are read directly (used by tests)
    * HTTP 403 -> one retry with a different browser profile
    * HTML instead of a feed -> RSS/Atom autodiscovery, capped to one hop
    * connection errors / 5xx -> `retries` extra attempts with backoff
    * 4xx other than 403 is treated as permanent (no retry)
    """
    notes = notes if notes is not None else []
    if url.startswith("file://"):
        with open(url[len("file://"):], "rb") as fh:
            return fh.read(), url
    if not re.match(r"^https?://", url) and os.path.exists(url):
        with open(url, "rb") as fh:
            return fh.read(), url

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _http_get(url, timeout, UA_PROFILES[0])
            if r.status_code == 403:
                notes.append("403 -> retried with alternate browser profile")
                r = _http_get(url, timeout, UA_PROFILES[1])
            if r.status_code >= 500 or r.status_code in (408, 429):
                raise RuntimeError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise PermissionError(f"HTTP {r.status_code}")
            body = r.content or b""
            final_url = getattr(r, "url", None) or url
            if looks_like_feed(body):
                return body, final_url
            ctype = (r.headers.get("Content-Type") or "").lower()
            if discover and (b"<html" in body[:8192].lower() or "html" in ctype):
                found = discover_feed_url(body, final_url)
                if found and normalize_link(found) != normalize_link(url):
                    notes.append(f"autodiscovered {found}")
                    log.info("autodiscovery %s -> %s", url, found)
                    return fetch_feed(found, timeout, retries=retries, discover=False, notes=notes)
            raise PermissionError(f"not a feed (content-type={ctype or '?'})")
        except PermissionError as exc:  # permanent: 4xx / not a feed
            raise RuntimeError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - transient
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def fetch_bytes(url: str, timeout: float, retries: int = 1) -> bytes:
    """Backwards-compatible helper: bytes only."""
    return fetch_feed(url, timeout, retries)[0]


def adjust_future_time(dt: datetime, now: datetime, precision: str) -> tuple[datetime, str, str | None]:
    """Correct publish times that are ahead of `now` by more than FUTURE_TOLERANCE.

    Returns (dt, precision, note). note is None (untouched), "riyadh_local"
    (source labelled a Riyadh-local time as UTC: shifted back 3h) or "clamped"
    (still in the future: clamped to `now`, precision downgraded to "minute").
    """
    if dt <= now + FUTURE_TOLERANCE:
        return dt, precision, None
    shifted = dt - timedelta(hours=3)
    if shifted <= now + FUTURE_TOLERANCE:
        return shifted, precision, "riyadh_local"
    return now, ("minute" if precision == "second" else precision), "clamped"


def build_items(entries: Iterable[dict[str, Any]], feed: dict[str, Any], now: datetime,
                window_hours: float, min_relevance: float = RELEVANCE_MIN) -> tuple[list[dict[str, Any]], int, int]:
    """Returns (items, dropped_by_date, dropped_by_relevance)."""
    cutoff = now - timedelta(hours=window_hours)
    fetched_at = fmt_utc(now)
    items, dropped, irrelevant = [], 0, 0
    for e in entries:
        dt, precision = parse_date(e.get("date_raw"))
        if dt is None:
            dropped += 1
            log.debug("[%s] no parseable date, dropped: %r", feed["name"], e.get("title", "")[:60])
            continue
        if dt < cutoff:
            dropped += 1
            continue
        # Future timestamps: some feeds label Riyadh-local times as UTC/GMT.
        # First assume the source meant Riyadh local (UTC+3); if that is still
        # in the future, clamp to fetch time and downgrade precision.
        dt, precision, time_adjusted = adjust_future_time(dt, now, precision)
        if time_adjusted:
            log.debug("[%s] future time %s -> %s (%s): %s", feed["name"], e.get("date_raw"), fmt_utc(dt),
                      time_adjusted, e["title"][:60])
        link = e.get("link") or ""
        if "news.google.com" in link:
            link = resolve_google_news_link(link)
        text_for_lang = f"{e['title']} {e.get('summary', '')}"
        rel = relevance_score(e["title"], e.get("summary", ""), e.get("source") or feed["name"], link)
        if rel < min_relevance:
            irrelevant += 1
            log.debug("[%s] irrelevant (%.2f): %s", feed["name"], rel, e["title"][:80])
            continue
        item = {
            "title": e["title"],
            "link": link,
            "source": e.get("source") or feed["name"],
            "published_utc": fmt_utc(dt),
            "published_riyadh": fmt_riyadh(dt),
            "precision": precision,
            "summary": e.get("summary", ""),
            "fetched_at_utc": fetched_at,
            "lang": detect_lang(text_for_lang),
            "priority": int(feed.get("priority", 3)),
            "relevance": rel,
            "raw_published": e.get("date_raw"),
        }
        if feed.get("market_hint"):
            item["market_hint"] = str(feed["market_hint"])
        if time_adjusted:
            item["time_adjusted"] = time_adjusted
        items.append(item)
    return items, dropped, irrelevant


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop items whose normalized title OR normalized link was already seen.

    Input order matters: callers pass Saudi-priority feeds first so the
    higher-priority copy survives.
    """
    seen_titles: set[str] = set()
    seen_links: set[str] = set()
    out = []
    for it in items:
        t = normalize_title(it["title"])
        l = normalize_link(it["link"])
        if (t and t in seen_titles) or (l and l in seen_links):
            continue
        if t:
            seen_titles.add(t)
        if l:
            seen_links.add(l)
        out.append(it)
    return out


DEFAULT_WINDOW_HOURS = 168  # 7-day rolling archive downstream
DEFAULT_MAX_ITEMS = 400
US_RESERVE = 120  # slots reserved for market_hint=="us" feeds when the cap binds


def run(feeds: list[dict[str, Any]], window_hours: float = DEFAULT_WINDOW_HOURS, max_items: int = DEFAULT_MAX_ITEMS,
        timeout: float = 15, now: datetime | None = None, retries: int = 1,
        budget: float = 300, us_reserve: int = US_RESERVE) -> dict[str, Any]:
    """Fetch every feed (Saudi-priority first). `budget` is a wall-clock cap in
    seconds after which remaining feeds are skipped (logged) so a broad outage
    cannot stall the CI job. When the cap binds, up to `us_reserve` slots are
    kept for US-market feeds (market_hint=="us") so Saudi feeds cannot crowd
    them out entirely."""
    now = now or datetime.now(timezone.utc)
    feeds = sorted(feeds, key=lambda f: int(f.get("priority", 3)))
    all_items: list[dict[str, Any]] = []
    report = []
    ok_count = 0
    dropped_relevance = dropped_date = 0
    started = time.monotonic()
    for feed in feeds:
        name, url = feed["name"], feed["url"]
        entry = {"name": name, "url": url, "priority": int(feed.get("priority", 3)),
                 "ok": False, "count": 0, "kept": 0, "error": None}
        if time.monotonic() - started > budget:
            entry["error"] = "skipped: time budget exhausted"
            log.warning("SKIP %-40s time budget (%.0fs) exhausted", name, budget)
            report.append(entry)
            continue
        notes: list[str] = []
        try:
            raw, effective = fetch_feed(url, timeout=timeout, retries=retries, notes=notes)
            entries = parse_feed(raw, name)
            items, dropped, irrelevant = build_items(entries, feed, now, window_hours)
            dropped_relevance += irrelevant
            dropped_date += dropped
            entry.update(ok=True, count=len(entries), kept=len(items), dropped_irrelevant=irrelevant)
            if effective != url:
                entry["effective_url"] = effective
            if notes:
                entry["notes"] = notes
            ok_count += 1
            all_items.extend(items)
            log.info("OK   %-40s entries=%d kept=%d old/undated=%d irrelevant=%d",
                     name, len(entries), len(items), dropped, irrelevant)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            if notes:
                entry["notes"] = notes
            log.warning("FAIL %-40s %s%s", name, entry["error"], f"  [{'; '.join(notes)}]" if notes else "")
        report.append(entry)

    # Sort by priority first so dedupe keeps the Saudi-source copy, then newest.
    all_items.sort(key=lambda i: i["priority"])
    deduped = dedupe(all_items)
    # Cap with a Saudi-first reservation: priority-1 feeds fill the cap before
    # regional/global ones; the surviving set is then ordered newest first.
    deduped.sort(key=lambda i: (i["priority"], -_ts(i["published_utc"])))
    final = apply_cap(deduped, max_items, us_reserve)
    dropped_cap = len(deduped) - len(final)
    final.sort(key=lambda i: i["published_utc"], reverse=True)
    log.info("feeds ok=%d/%d  items raw=%d deduped=%d final=%d  dropped: irrelevant=%d old/undated=%d cap=%d",
             ok_count, len(feeds), len(all_items), len(deduped), len(final), dropped_relevance, dropped_date, dropped_cap)
    return {
        "generated_at_utc": fmt_utc(now),
        "window_hours": window_hours,
        "feeds_ok": ok_count,
        "feeds_total": len(feeds),
        "dropped": dropped_relevance + dropped_date + dropped_cap,
        "stats": {"raw": len(all_items), "deduped": len(deduped), "kept": len(final),
                  "dropped_irrelevant": dropped_relevance, "dropped_old_or_undated": dropped_date,
                  "dropped_cap": dropped_cap, "duplicates": len(all_items) - len(deduped)},
        "feeds": report,
        "items": final,
    }


def apply_cap(ordered: list[dict[str, Any]], max_items: int, us_reserve: int = US_RESERVE) -> list[dict[str, Any]]:
    """Cap `ordered` (already priority-then-newest sorted) at `max_items`,
    reserving up to `us_reserve` slots for market_hint=="us" items. Saudi/regional
    items fill the remaining slots first; the returned list keeps input order."""
    if len(ordered) <= max_items:
        return list(ordered)
    us = [i for i in ordered if i.get("market_hint") == "us"]
    rest = [i for i in ordered if i.get("market_hint") != "us"]
    us_slots = min(len(us), max(0, us_reserve), max_items)
    rest_slots = max_items - us_slots
    keep = rest[:rest_slots]
    us_slots = max_items - len(keep)  # give back any unused Saudi slots
    keep_ids = {id(i) for i in keep + us[:us_slots]}
    return [i for i in ordered if id(i) in keep_ids]


def _ts(published_utc: str) -> float:
    try:
        return datetime.strptime(published_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def load_feeds(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return DEFAULT_FEEDS
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feeds = data["feeds"] if isinstance(data, dict) else data
    for f in feeds:
        f.setdefault("priority", 3)
        f.setdefault("name", f["url"])
    return feeds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--feeds", help="JSON file with a list of {name,url,priority} (default: built-in list)")
    ap.add_argument("--hours", type=float, default=DEFAULT_WINDOW_HOURS,
                    help=f"keep items newer than N hours ({DEFAULT_WINDOW_HOURS} = 7 days, matches the archive)")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_ITEMS, help=f"cap on items ({DEFAULT_MAX_ITEMS})")
    ap.add_argument("--us-reserve", type=int, default=US_RESERVE,
                    help=f"slots reserved for US-market feeds when the cap binds ({US_RESERVE})")
    ap.add_argument("--timeout", type=float, default=15, help="per-request timeout seconds")
    ap.add_argument("--retries", type=int, default=1, help="extra attempts per feed on failure (1)")
    ap.add_argument("--budget", type=float, default=300, help="total seconds before remaining feeds are skipped (300)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    feeds = load_feeds(args.feeds)
    result = run(feeds, window_hours=args.hours, max_items=args.max, timeout=args.timeout,
                 retries=args.retries, budget=args.budget, us_reserve=args.us_reserve)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    log.info("wrote %s (%d items)", args.out, len(result["items"]))

    if result["feeds_ok"] == 0:
        log.error("no feed succeeded")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
