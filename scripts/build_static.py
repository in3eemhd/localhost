#!/usr/bin/env python3
"""Build site/static.html: site/index.html with the real data/market.json and
data/news.json embedded as the snapshot constants and the loader forced into
static mode (no network fetch, mode pill "لقطة").

Usage: python3 scripts/build_static.py [--index site/index.html] [--out site/static.html]
                                       [--market data/market.json] [--news data/news.json]
Stdlib only.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "/*SNAPSHOT_START*/", "/*SNAPSHOT_END*/"
STATIC_FLAG = "/*STATIC_MODE*/false"


def js_literal(obj) -> str:
    """JSON that is safe inside an inline <script>: no '</script', no U+2028/2029."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return s


def build(index: Path, out: Path, market: Path, news: Path) -> None:
    html = index.read_text(encoding="utf-8")
    m = json.loads(market.read_text(encoding="utf-8"))
    n = json.loads(news.read_text(encoding="utf-8"))
    if not isinstance(m, dict) or not isinstance(n, dict):
        sys.exit("market.json / news.json must be JSON objects")

    a, b = html.find(START), html.find(END)
    if a < 0 or b < 0 or b < a:
        sys.exit(f"snapshot markers {START} ... {END} not found in {index}")
    block = (
        f"{START}\n"
        f"const SNAPSHOT_MARKET={js_literal(m)};\n"
        f"const SNAPSHOT_NEWS={js_literal(n)};\n"
        f"{END}"
    )
    html = html[:a] + block + html[b + len(END):]

    if STATIC_FLAG not in html:
        sys.exit(f"static flag {STATIC_FLAG} not found in {index}")
    html = html.replace(STATIC_FLAG, "/*STATIC_MODE*/true", 1)

    # Sanity: the markers must not appear inside the data itself.
    if html.count(START) != 1 or html.count(END) != 1:
        sys.exit("marker collision inside data")

    out.write_text(html, encoding="utf-8")
    gen = m.get("generated_at_riyadh") or m.get("generated_at_utc") or "?"
    print(f"wrote {out} ({len(html.encode('utf-8'))} bytes) · market generated {gen} · "
          f"{len(m.get('indices') or [])} indices · {len(m.get('stocks') or [])} stocks · "
          f"{len(n.get('items') or [])} news items")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--index", default=ROOT / "site" / "index.html", type=Path)
    ap.add_argument("--out", default=ROOT / "site" / "static.html", type=Path)
    ap.add_argument("--market", default=ROOT / "data" / "market.json", type=Path)
    ap.add_argument("--news", default=ROOT / "data" / "news.json", type=Path)
    args = ap.parse_args()
    build(args.index, args.out, args.market, args.news)


if __name__ == "__main__":
    main()
