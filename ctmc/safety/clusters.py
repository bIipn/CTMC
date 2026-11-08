"""Bubble-map style wallet clustering.

For each top holder, find the wallet that first funded it with SOL. Holders that
share a funder are one "bubble" (likely one person or team with many wallets).
Holders funded by the token creator, or that funded the creator, are dev-linked.

Needs a Solana RPC (SOLANA_RPC_URL; a Helius/Triton key makes this fast).
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict

import httpx

from ctmc.models import Verdict

# Exchange hot wallets fund thousands of unrelated users; never count them as a cluster.
KNOWN_EXCHANGES = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance 2",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Coinbase 2",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
}


class Rpc:
    def __init__(self, url: str | None = None, http: httpx.AsyncClient | None = None):
        self.url = url or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.http = http or httpx.AsyncClient(timeout=10)

    async def call(self, method: str, params: list):
        r = await self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]


class ClusterAnalyzer:
    def __init__(self, rules: dict, rpc: Rpc | None = None):
        self.r = rules
        self.rpc = rpc or Rpc()
        self._funder_cache: dict[str, str | None] = {}

    async def funder_of(self, wallet: str) -> str | None:
        """The account that sent this wallet its first SOL (None if too old/busy to tell)."""
        if wallet in self._funder_cache:
            return self._funder_cache[wallet]
        funder = None
        try:
            sigs = await self.rpc.call("getSignaturesForAddress", [wallet, {"limit": self.r.get("max_signatures_scan", 1000)}])
            # A full page means a long history: an established wallet, not a fresh bundle wallet.
            if sigs and len(sigs) < self.r.get("max_signatures_scan", 1000):
                tx = await self.rpc.call("getTransaction", [sigs[-1]["signature"],
                                         {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                funder = _first_sol_sender(tx, wallet)
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
            funder = None
        self._funder_cache[wallet] = funder
        return funder

    async def analyze(self, holders: list[dict], creator: str | None, pools: set[str]) -> Verdict:
        """holders: [{'owner': wallet, 'pct': float}] (rugcheck topHolders format)."""
        people = [h for h in holders if h.get("owner") and h["owner"] not in pools][: self.r.get("top_holders", 20)]
        funders = await asyncio.gather(*(self.funder_of(h["owner"]) for h in people))
        creator_funder = await self.funder_of(creator) if creator else None

        bubbles: dict[str, list[dict]] = defaultdict(list)
        dev_linked = 0.0
        for h, f in zip(people, funders):
            pct = float(h.get("pct") or 0)
            if creator and (h["owner"] == creator or f == creator or (creator_funder and f == creator_funder)):
                dev_linked += pct
            if f and f not in KNOWN_EXCHANGES:
                bubbles[f].append(h)
        # wallets that funded each other also chain together
        owners = {h["owner"] for h in people}
        for h, f in zip(people, funders):
            if f in owners:
                bubbles[f].append(h)

        why = []
        sizes = []
        for funder, group in bubbles.items():
            members = {h["owner"]: float(h.get("pct") or 0) for h in group}
            if funder in owners:            # the funder is itself a holder: it's in the bubble too
                members[funder] = next(float(h.get("pct") or 0) for h in people if h["owner"] == funder)
            if len(members) >= 2:
                sizes.append(sum(members.values()))
        biggest = max(sizes, default=0.0)
        if biggest > self.r.get("max_cluster_pct", 12):
            why.append(f"connected-wallet bubble holds {biggest:.1f}% (same funder)")
        if dev_linked > self.r.get("max_dev_linked_pct", 5):
            why.append(f"dev-linked wallets hold {dev_linked:.1f}%")
        return Verdict(not why, why or [f"largest bubble {biggest:.1f}%, dev-linked {dev_linked:.1f}%"], biggest)


def _first_sol_sender(tx: dict | None, wallet: str) -> str | None:
    if not tx:
        return None
    instrs = (tx.get("transaction") or {}).get("message", {}).get("instructions") or []
    for ins in instrs:
        p = ins.get("parsed") or {}
        info = p.get("info") or {}
        if p.get("type") in ("transfer", "createAccount") and info.get("destination", info.get("newAccount")) == wallet:
            src = info.get("source")
            if src and src != wallet:
                return src
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
    payer = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
    return payer if payer and payer != wallet else None
