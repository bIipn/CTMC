"""Rug / bundle / honeypot screening.  Fail-closed: no data = no trade.

Solana  -> rugcheck.xyz report (mint & freeze authority, LP lock, holders,
           insider/bundled wallet networks).
EVM     -> honeypot.is (can you actually sell? what is the tax?).
"""
from __future__ import annotations

import requests

from ctmc.models import Verdict

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
HONEYPOT = "https://api.honeypot.is/v2/IsHoneypot"
TIMEOUT = 15

# Holder accounts that are pools / exchanges, not people. Excluded from concentration.
_NON_HOLDER_HINTS = ("raydium", "pump", "meteora", "orca", "lp", "exchange", "binance", "okx", "bybit")


class SafetyChecker:
    def __init__(self, rules: dict, session: requests.Session | None = None):
        self.r = rules
        self.http = session or requests.Session()

    # ── Solana ───────────────────────────────────────────────
    def check_solana(self, mint: str) -> Verdict:
        try:
            resp = self.http.get(RUGCHECK.format(mint=mint), timeout=TIMEOUT)
            resp.raise_for_status()
            report = resp.json()
        except (requests.RequestException, ValueError) as exc:
            return Verdict(False, [f"rugcheck unavailable ({exc.__class__.__name__}) — fail closed"])
        return self.evaluate_solana(report)

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

        # LP locked / burned
        markets = rep.get("markets") or []
        if markets:
            lp_pct = max(float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in markets)
            if lp_pct < r.get("min_lp_locked_or_burned_pct", 90):
                why.append(f"only {lp_pct:.0f}% of LP locked/burned")
        else:
            why.append("no market / LP data")

        # Holder concentration, ignoring pools & exchanges
        pools = _known_pool_owners(rep)
        people = [
            h for h in rep.get("topHolders") or []
            if not any(k in str(h.get("owner", "")).lower() for k in _NON_HOLDER_HINTS)
            and h.get("owner") not in pools
        ]
        pcts = [float(h.get("pct") or 0) for h in people]
        if pcts:
            if max(pcts) > r.get("max_single_holder_pct", 8):
                why.append(f"single wallet holds {max(pcts):.1f}%")
            top10 = sum(sorted(pcts, reverse=True)[:10])
            if top10 > r.get("max_top10_holders_pct", 30):
                why.append(f"top-10 wallets hold {top10:.1f}%")

        # Bundles / connected wallets (same funder, sniped in launch block)
        insider_pct = sum(float(h.get("pct") or 0) for h in people if h.get("insider"))
        for net in rep.get("insiderNetworks") or []:
            insider_pct = max(insider_pct, float(net.get("tokenAmountPct") or net.get("pct") or 0))
        if insider_pct > r.get("max_insider_cluster_pct", 15):
            why.append(f"bundled/insider wallets control {insider_pct:.1f}%")

        return Verdict(not why, why, score)

    # ── EVM ──────────────────────────────────────────────────
    def check_evm(self, address: str, chain_id: int = 1) -> Verdict:
        try:
            resp = self.http.get(HONEYPOT, params={"address": address, "chainID": chain_id}, timeout=TIMEOUT)
            resp.raise_for_status()
            d = resp.json()
        except (requests.RequestException, ValueError) as exc:
            return Verdict(False, [f"honeypot.is unavailable ({exc.__class__.__name__}) — fail closed"])
        why = []
        if (d.get("honeypotResult") or {}).get("isHoneypot"):
            why.append("HONEYPOT — cannot sell")
        tax = float((d.get("simulationResult") or {}).get("sellTax") or 0)
        if tax > self.r.get("max_sell_tax_pct", 5):
            why.append(f"sell tax {tax:.1f}%")
        if not (d.get("contractCode") or {}).get("openSource", False):
            why.append("contract source not verified")
        return Verdict(not why, why)

    def check(self, token: str, chain: str) -> Verdict:
        return self.check_solana(token) if chain == "solana" else self.check_evm(token)


def _known_pool_owners(rep: dict) -> set[str]:
    owners = set()
    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                owners.add(m[k])
    return owners
