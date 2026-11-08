"""Rug / bundle / honeypot screening, async.  Fail-closed: no data = no trade.

Solana -> rugcheck.xyz report (mint & freeze authority, LP lock, holder concentration,
          insider/bundle networks) + bubble-map funder clustering (clusters.py).
"""
from __future__ import annotations

import httpx

from ctmc.models import Verdict
from ctmc.safety.clusters import ClusterAnalyzer

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

_NON_HOLDER_HINTS = ("raydium", "pump", "meteora", "orca", "lp", "exchange", "binance", "okx", "bybit")


def known_pool_owners(rep: dict) -> set[str]:
    owners = set()
    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                owners.add(m[k])
    return owners


def people_holders(rep: dict) -> list[dict]:
    pools = known_pool_owners(rep)
    return [h for h in rep.get("topHolders") or []
            if h.get("owner") not in pools
            and not any(k in str(h.get("owner", "")).lower() for k in _NON_HOLDER_HINTS)]


class SafetyChecker:
    def __init__(self, rules: dict, cluster_rules: dict | None = None,
                 http: httpx.AsyncClient | None = None, clusters: ClusterAnalyzer | None = None):
        self.r = rules
        self.cr = cluster_rules or {"enabled": False}
        self.http = http or httpx.AsyncClient(timeout=10)
        self.clusters = clusters or (ClusterAnalyzer(self.cr) if self.cr.get("enabled") else None)

    async def report(self, mint: str) -> dict:
        r = await self.http.get(RUGCHECK.format(mint=mint))
        r.raise_for_status()
        return r.json()

    async def check(self, mint: str) -> Verdict:
        try:
            rep = await self.report(mint)
        except (httpx.HTTPError, ValueError) as exc:
            return Verdict(False, [f"rugcheck unavailable ({exc.__class__.__name__}) — fail closed"])
        v = self.evaluate_solana(rep)
        if not v or not self.clusters:
            return v
        cv = await self.clusters.analyze(rep.get("topHolders") or [], rep.get("creator"), known_pool_owners(rep))
        return Verdict(cv.ok, (v.reasons if cv.ok else []) + cv.reasons, v.score)

    def evaluate_solana(self, rep: dict) -> Verdict:
        r, why = self.r, []
        if r.get("require_mint_authority_revoked") and rep.get("mintAuthority"):
            why.append("mint authority NOT revoked (dev can print supply)")
        if r.get("require_freeze_authority_revoked") and rep.get("freezeAuthority"):
            why.append("freeze authority NOT revoked (dev can freeze your tokens)")
        if rep.get("rugged"):
            why.append("flagged as rugged")
        score = rep.get("score_normalised", rep.get("score"))
        if score is not None and score > r.get("max_rugcheck_score", 1000):
            why.append(f"rugcheck risk score {score} > {r['max_rugcheck_score']}")
        for risk in rep.get("risks") or []:
            if (risk.get("level") or "").lower() == "danger":
                why.append(f"danger: {risk.get('name')}")

        markets = rep.get("markets") or []
        if markets:
            lp_pct = max(float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in markets)
            if lp_pct < r.get("min_lp_locked_or_burned_pct", 90):
                why.append(f"only {lp_pct:.0f}% of LP locked/burned")
        else:
            why.append("no market / LP data")

        people = people_holders(rep)
        pcts = [float(h.get("pct") or 0) for h in people]
        if pcts:
            if max(pcts) > r.get("max_single_holder_pct", 8):
                why.append(f"single wallet holds {max(pcts):.1f}%")
            top10 = sum(sorted(pcts, reverse=True)[:10])
            if top10 > r.get("max_top10_holders_pct", 30):
                why.append(f"top-10 wallets hold {top10:.1f}%")

        insider_pct = sum(float(h.get("pct") or 0) for h in people if h.get("insider"))
        for net in rep.get("insiderNetworks") or []:
            insider_pct = max(insider_pct, float(net.get("tokenAmountPct") or net.get("pct") or 0))
        if insider_pct > r.get("max_insider_cluster_pct", 15):
            why.append(f"bundled/insider wallets control {insider_pct:.1f}%")
        return Verdict(not why, why, score)
