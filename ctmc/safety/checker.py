"""Rug / bundle / honeypot screening, async.  Fail-closed: no data = no trade.

Solana -> rugcheck.xyz report:
  authorities     mint + freeze authority revoked
  Token-2022      no permanent delegate / transfer hook / non-transferable / frozen-by-default,
                  no transfer fee ("tax token")
  liquidity       share of LP locked or burned
  holders         single-wallet and top-10 concentration (pools, lockers, exchanges excluded
                  using rugcheck's own account labels), total holder count
  dev             creator's share of supply, how many tokens the creator has launched
  insiders        bundled / insider wallet networks
  + bubble-map funder clustering of top holders (clusters.py)
"""
from __future__ import annotations

import httpx

from ctmc.models import Verdict
from ctmc.safety.clusters import ClusterAnalyzer

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

# rugcheck knownAccounts types that are infrastructure, not people
_NOT_PEOPLE = {"AMM", "LOCKER", "BURN", "EXCHANGE", "CEX", "PROGRAM", "BONDING_CURVE", "POOL", "VAULT"}

# Token-2022 extensions that let someone other than you control your tokens
_DANGEROUS_EXTENSIONS = {
    "permanentdelegate": "permanent delegate (someone can move or burn YOUR tokens)",
    "transferhook": "transfer hook (custom code runs on every transfer — can block sells)",
    "nontransferable": "non-transferable token (you can't sell)",
    "defaultaccountstate": "accounts frozen by default",
}


def known_pool_owners(rep: dict) -> set[str]:
    """Addresses that belong to pools, lockers, exchanges or programs."""
    owners: set[str] = set()
    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                owners.add(m[k])
    for addr, info in (rep.get("knownAccounts") or {}).items():
        if str((info or {}).get("type", "")).upper() in _NOT_PEOPLE:
            owners.add(addr)
    for locker in (rep.get("lockers") or {}).values() if isinstance(rep.get("lockers"), dict) else []:
        if isinstance(locker, dict) and locker.get("owner"):
            owners.add(locker["owner"])
    return owners


def people_holders(rep: dict) -> list[dict]:
    pools = known_pool_owners(rep)
    return [h for h in rep.get("topHolders") or []
            if h.get("owner") not in pools and h.get("address") not in pools]


def _extension_names(rep: dict) -> set[str]:
    """Normalised Token-2022 extension names, whatever shape rugcheck returns them in."""
    ext = rep.get("token_extensions") or rep.get("tokenExtensions") or {}
    names: set[str] = set()
    if isinstance(ext, dict):
        for k, v in ext.items():
            if v not in (None, False, {}, []):
                names.add(k.replace("_", "").lower())
    elif isinstance(ext, list):
        for e in ext:
            n = e.get("extension") or e.get("name") if isinstance(e, dict) else e
            if n:
                names.add(str(n).replace("_", "").lower())
    return names


class SafetyChecker:
    def __init__(self, rules: dict, cluster_rules: dict | None = None,
                 http: httpx.AsyncClient | None = None, clusters: ClusterAnalyzer | None = None):
        self.r = rules
        self.cr = cluster_rules or {"enabled": False}
        self.http = http or httpx.AsyncClient(timeout=10)
        self.clusters = clusters or (ClusterAnalyzer(self.cr) if self.cr.get("enabled") else None)
        self.last_report: dict[str, dict] = {}   # mint -> rugcheck report (reused by the rug watch)

    async def report(self, mint: str) -> dict:
        r = await self.http.get(RUGCHECK.format(mint=mint))
        r.raise_for_status()
        return r.json()

    async def check(self, mint: str) -> Verdict:
        try:
            rep = await self.report(mint)
        except (httpx.HTTPError, ValueError) as exc:
            return Verdict(False, [f"rugcheck unavailable ({exc.__class__.__name__}) — fail closed"])
        self.last_report[mint] = rep
        v = self.evaluate_solana(rep)
        if not v or not self.clusters:
            return v
        cv = await self.clusters.analyze(rep.get("topHolders") or [], rep.get("creator"), known_pool_owners(rep))
        return Verdict(cv.ok, (v.reasons if cv.ok else []) + cv.reasons, v.score)

    def evaluate_solana(self, rep: dict) -> Verdict:
        r, why, notes = self.r, [], []
        token = rep.get("token") or {}

        # ── authorities ──
        if r.get("require_mint_authority_revoked") and (rep.get("mintAuthority") or token.get("mintAuthority")):
            why.append("mint authority NOT revoked (dev can print supply)")
        if r.get("require_freeze_authority_revoked") and (rep.get("freezeAuthority") or token.get("freezeAuthority")):
            why.append("freeze authority NOT revoked (dev can freeze your tokens)")
        if rep.get("rugged"):
            why.append("flagged as rugged")

        # ── Token-2022 traps ──
        blocked = {e.replace("_", "").lower() for e in r.get("block_token2022_extensions", list(_DANGEROUS_EXTENSIONS))}
        for name in _extension_names(rep) & blocked:
            why.append(_DANGEROUS_EXTENSIONS.get(name, f"Token-2022 extension: {name}"))
        fee = float((rep.get("transferFee") or {}).get("pct") or 0)
        if fee > r.get("max_transfer_fee_pct", 0):
            why.append(f"{fee:g}% transfer fee on every buy/sell (tax token)")

        # ── metadata ──
        meta = rep.get("tokenMeta") or {}
        if meta.get("mutable"):
            (why if r.get("reject_mutable_metadata") else notes).append("metadata is mutable (name/image can change)")

        # ── rugcheck's own risk list ──
        score = rep.get("score_normalised", rep.get("score"))
        if score is not None and score > r.get("max_rugcheck_score", 1000):
            why.append(f"rugcheck risk score {score} > {r['max_rugcheck_score']}")
        for risk in rep.get("risks") or []:
            if (risk.get("level") or "").lower() == "danger":
                why.append(f"danger: {risk.get('name')}")

        # ── liquidity lock ──
        markets = rep.get("markets") or []
        if markets:
            lp_pct = max(float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in markets)
            if lp_pct < r.get("min_lp_locked_or_burned_pct", 90):
                why.append(f"only {lp_pct:.0f}% of LP locked/burned")
        else:
            why.append("no market / LP data")

        # ── holders ──
        total_holders = int(rep.get("totalHolders") or 0)
        if total_holders and total_holders < r.get("min_holders", 0):
            why.append(f"only {total_holders} holders")
        people = people_holders(rep)
        pcts = [float(h.get("pct") or 0) for h in people]
        if pcts:
            if max(pcts) > r.get("max_single_holder_pct", 8):
                why.append(f"single wallet holds {max(pcts):.1f}%")
            top10 = sum(sorted(pcts, reverse=True)[:10])
            if top10 > r.get("max_top10_holders_pct", 30):
                why.append(f"top-10 wallets hold {top10:.1f}%")

        # ── dev ──
        creator = rep.get("creator")
        dev_pct = float(rep.get("creatorBalancePct") or 0) or sum(
            float(h.get("pct") or 0) for h in rep.get("topHolders") or [] if creator and h.get("owner") == creator)
        if dev_pct > r.get("max_dev_holding_pct", 5):
            why.append(f"dev wallet still holds {dev_pct:.1f}%")
        launched = rep.get("creatorTokens") or []
        if isinstance(launched, list) and len(launched) > r.get("max_creator_tokens", 10):
            why.append(f"creator has launched {len(launched)} tokens (serial launcher)")

        # ── insiders / bundles ──
        insider_pct = sum(float(h.get("pct") or 0) for h in people if h.get("insider"))
        for net in rep.get("insiderNetworks") or []:
            insider_pct = max(insider_pct, float(net.get("tokenAmountPct") or net.get("pct") or 0))
        if insider_pct > r.get("max_insider_cluster_pct", 15):
            why.append(f"bundled/insider wallets control {insider_pct:.1f}%")

        return Verdict(not why, why or notes, score)
