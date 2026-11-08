"""Import KOL wallets + stats from kolexplorer.com (public KOL pages; robots.txt allows /kol/).

Runs only when you ask (`python -m ctmc kols import`), one page at a time with a pause
between requests. Each KOL page carries schema.org JSON-LD with the wallet address,
X profile, 7d PnL / win rate, 30d profit, trade counts and average trade size, plus a
stat grid with overall win rate, median entry market cap and median hold.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from ctmc.kol.registry import Kol, KolStats, Registry, parse_duration, parse_money, stamp

SITE = "https://kolexplorer.com"
HEADERS = {"User-Agent": "ctmc-personal-bot/1.0 (+manual import, low rate)"}
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _pct(text) -> float:
    try:
        return float(str(text).replace("%", "").strip())
    except ValueError:
        return 0.0


def parse_kol_page(html: str, slug: str) -> Kol | None:
    """Pure parser: KOL page HTML -> Kol (None if no wallet found)."""
    stats = KolStats()
    name, x_handle, wallets = slug, "", []

    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if m:
        try:
            graph = json.loads(m.group(1)).get("@graph", [])
        except ValueError:
            graph = []
        for node in graph:
            types = node.get("@type")
            if "Person" not in (types if isinstance(types, list) else [types]):
                continue
            name = node.get("name") or name
            for url in node.get("sameAs") or []:
                hm = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", url)
                if hm:
                    x_handle = hm.group(1)
            for pv in node.get("additionalProperty") or []:
                key, val = (pv.get("name") or "").lower(), pv.get("value")
                if "wallet address" in key and _B58.match(str(val)):
                    wallets.append(str(val))
                elif "7-day realized pnl" in key:
                    stats.pnl_7d_usd = parse_money(val)
                elif "7-day win rate" in key:
                    stats.win_rate = _pct(val)
                elif "30-day profit" in key:
                    stats.pnl_30d_usd = parse_money(val)
                elif key.startswith("trades (7d)"):
                    stats.trades_7d = int(val or 0)
                elif "unique tokens traded" in key:
                    stats.tokens_7d = int(val or 0)
                elif "average trade size" in key:
                    stats.avg_position_usd = parse_money(val)

    # stat grid: <span><b>39.0%</b>Win rate</span> <span><b>$286.9K</b>Median entry MC</span> ...
    for value, label in re.findall(r"<span><b[^>]*>(.*?)</b>\s*([^<]+)</span>", html):
        value = re.sub(r"<[^>]+>", "", value).strip()
        label = label.strip().lower()
        if label.startswith("win rate"):
            stats.win_rate = _pct(value) or stats.win_rate          # broader sample than 7d
        elif label.startswith("median entry mc"):
            stats.median_entry_mcap_usd = parse_money(value)
        elif label.startswith("median hold"):
            stats.median_hold_s = parse_duration(value)
    cm = re.search(r"(\d[\d,]*)\s+closed trades", html)
    if cm:
        stats.closed_trades = int(cm.group(1).replace(",", ""))

    if not wallets:
        return None
    return Kol(name=name, wallets=wallets, x_handle=x_handle, stats=stamp(stats, "kolexplorer"))


def parse_kol_slugs(text: str) -> list[str]:
    """KOL slugs from the sitemap XML or the leaderboard HTML, in page order."""
    slugs = re.findall(r"kolexplorer\.com/kol/([a-z0-9][a-z0-9\-_]*)", text, re.I)
    return list(dict.fromkeys(s.lower() for s in slugs))


async def import_kolexplorer(registry: Registry, limit: int = 60, pause_s: float = 1.5,
                             http: httpx.AsyncClient | None = None, log=print) -> int:
    own = http is None
    http = http or httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True)
    try:
        board = await http.get(f"{SITE}/")                    # leaderboard order = best realized PnL first
        slugs = parse_kol_slugs(board.text)
        if len(slugs) < limit:
            sm = await http.get(f"{SITE}/sitemap.xml")
            slugs += [s for s in parse_kol_slugs(sm.text) if s not in slugs]
        added = 0
        for slug in slugs[:limit]:
            try:
                r = await http.get(f"{SITE}/kol/{slug}")
                kol = parse_kol_page(r.text, slug) if r.status_code == 200 else None
            except httpx.HTTPError as exc:
                log(f"  {slug}: {exc.__class__.__name__}")
                kol = None
            if kol:
                registry.add(kol)
                added += 1
            await asyncio.sleep(pause_s)
        return added
    finally:
        if own:
            await http.aclose()
