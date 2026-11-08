"""Find smart wallets ourselves — hundreds to thousands of them, from what actually made money.

  1. winners      coins trending on Solana right now (GeckoTerminal, free, no key)
  2. mine         each winner's recent swaps (Helius parsed history of its main pool):
                  per wallet, SOL spent vs SOL received ON THAT COIN
  3. repeat       wallets profitable on >= `min_tokens` different winners — repeat
                  winners, not one lucky trade
  4. score        each candidate's FULL history (win rate, PnL, hold time, flip rate,
                  rug rate) — the same quality + followability tests as every KOL
  5. keep         candidates that pass go into the registry (data/kols.json)
Run it daily: the registry compounds. Needs HELIUS_API_KEY (free tier works, slowly).
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from ctmc.kol.registry import Kol, Registry
from ctmc.kol.scorer import fetch_swaps, score_history, swap_deltas

GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
HELIUS = "https://api.helius.xyz/v0/addresses/{addr}/transactions"


@dataclass
class Winner:
    pool: str
    mint: str
    name: str
    change_24h: float
    volume_24h: float


@dataclass
class Candidate:
    wallet: str
    tokens: dict[str, float] = field(default_factory=dict)   # mint -> realized SOL profit on it

    @property
    def wins(self) -> int:
        return sum(1 for p in self.tokens.values() if p > 0)

    @property
    def profit_sol(self) -> float:
        return round(sum(self.tokens.values()), 3)


def parse_trending(payload: dict) -> list[Winner]:
    out = []
    for p in payload.get("data") or []:
        a = p.get("attributes") or {}
        base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        mint = base.split("_", 1)[1] if "_" in base else ""
        try:
            change = float((a.get("price_change_percentage") or {}).get("h24") or 0)
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
        except (TypeError, ValueError):
            continue
        if mint and a.get("address"):
            out.append(Winner(a["address"], mint, a.get("name", ""), change, vol))
    return out


def profits_on_token(txs: list[dict], mint: str) -> dict[str, float]:
    """Pure: a pool's parsed swaps -> {wallet: SOL received - SOL spent on `mint`}."""
    spent: dict[str, float] = defaultdict(float)
    got: dict[str, float] = defaultdict(float)
    for tx in txs:
        w = tx.get("feePayer")
        if not w:
            continue
        sol, tokens = swap_deltas(tx, w)
        d = tokens.get(mint, 0.0)
        if d > 0 and sol < 0:
            spent[w] += -sol
        elif d < 0 and sol > 0:
            got[w] += sol
    return {w: round(got[w] - spent[w], 4) for w in set(spent) | set(got) if spent[w] > 0}


def pick_candidates(per_token: dict[str, dict[str, float]], min_tokens: int, min_profit_sol: float) -> list[Candidate]:
    """Wallets that made money on several different winners, best first."""
    cands: dict[str, Candidate] = {}
    for mint, profits in per_token.items():
        for w, p in profits.items():
            cands.setdefault(w, Candidate(w)).tokens[mint] = p
    keep = [c for c in cands.values() if c.wins >= min_tokens and c.profit_sol >= min_profit_sol]
    return sorted(keep, key=lambda c: (-c.wins, -c.profit_sol))


async def trending_winners(http: httpx.AsyncClient, pages: int = 3, min_change: float = 50) -> list[Winner]:
    out: list[Winner] = []
    for page in range(1, pages + 1):
        r = await http.get(f"{GECKO}/trending_pools", params={"page": page}, headers={"accept": "application/json"})
        if r.status_code != 200:
            break
        out += parse_trending(r.json())
        await asyncio.sleep(2.1)                       # GeckoTerminal free tier: ~30 calls/min
    seen, uniq = set(), []
    for w in sorted(out, key=lambda w: -w.change_24h):
        if w.mint not in seen and w.change_24h >= min_change:
            seen.add(w.mint)
            uniq.append(w)
    return uniq


async def pool_swaps(http: httpx.AsyncClient, pool: str, pages: int) -> list[dict]:
    key, out, before = os.getenv("HELIUS_API_KEY", ""), [], None
    for _ in range(pages):
        params = {"api-key": key, "type": "SWAP", "limit": 100}
        if before:
            params["before"] = before
        r = await http.get(HELIUS.format(addr=pool), params=params)
        if r.status_code != 200:
            break
        page = r.json()
        if not page:
            break
        out += page
        before = page[-1].get("signature")
    return out


async def discover(registry: Registry, rules: dict, http: httpx.AsyncClient, *, pools: int = 30,
                   pages_per_pool: int = 10, min_tokens: int = 2, min_profit_sol: float = 1.0,
                   max_score: int = 300, sol_price: float = 0.0, log=print) -> dict:
    if not os.getenv("HELIUS_API_KEY"):
        raise RuntimeError("HELIUS_API_KEY not set — discovery reads trade history through Helius (free tier works)")
    winners = (await trending_winners(http))[:pools]
    log(f"{len(winners)} trending winners to mine")
    per_token: dict[str, dict[str, float]] = {}
    for w in winners:
        txs = await pool_swaps(http, w.pool, pages_per_pool)
        per_token[w.mint] = profits_on_token(txs, w.mint)
        log(f"  {w.name[:28]:28} +{w.change_24h:,.0f}% · {len(txs)} swaps · {sum(p > 0 for p in per_token[w.mint].values())} profitable wallets")
    cands = [c for c in pick_candidates(per_token, min_tokens, min_profit_sol)
             if c.wallet not in registry.by_wallet][:max_score]
    log(f"{len(cands)} new repeat winners — scoring each on its full history")

    sem, kept = asyncio.Semaphore(3), 0

    async def score(c: Candidate) -> Kol | None:
        async with sem:
            try:
                rep = score_history(await fetch_swaps(c.wallet, max_pages=5, http=http), c.wallet, sol_price)
            except (httpx.HTTPError, RuntimeError):
                return None
        kol = Kol(f"disc-{c.wallet[:6]}", [c.wallet], stats=rep.stats,
                  notes=f"profitable on {c.wins} trending coins (+{c.profit_sol} SOL)")
        kol.stats.source = "discovered"
        return kol if kol.quality(rules) > 0 else None

    for kol in await asyncio.gather(*(score(c) for c in cands)):
        if kol:
            registry.add(kol)
            kept += 1
    return {"winners": len(winners), "candidates": len(cands), "kept": kept}
