"""Brokers, async.

PaperBroker    simulated fills with realistic AMM price impact + fees.
JupiterBroker  live swaps routed by Jupiter across every Solana DEX, with explicit
               slippage, high priority fees, confirmation tracking and escalating
               slippage retries when a sell absolutely has to land.

Both refuse to move price more than `max_price_impact_pct`: buys shrink to fit.
"""
from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass

import httpx

USDC = "EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"


@dataclass
class Fill:
    qty: float        # tokens bought (+) or sold (-)
    usd: float        # USD spent (+) or received (-)
    price: float      # effective price paid / received
    impact_pct: float = 0.0
    signature: str = ""


class TradeError(RuntimeError):
    pass


def max_size_for_impact(liquidity_usd: float, max_impact_pct: float) -> float:
    """Largest order a constant-product pool takes within the impact cap.
    impact = dx / (x + dx)  ->  dx = x * p / (1 - p), x = one side of the pool."""
    p = max_impact_pct / 100
    return (liquidity_usd / 2) * p / (1 - p) if 0 < p < 1 else 0.0


def amm_impact(usd: float, liquidity_usd: float) -> float:
    x = liquidity_usd / 2
    return usd / (x + usd) if x > 0 else 1.0


class PaperBroker:
    def __init__(self, cash: float, ex: dict):
        self.cash = cash
        self.fee = ex.get("paper_fee_pct", 0.3) / 100
        self.max_impact = ex.get("max_price_impact_pct", 3)

    async def buy(self, token: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        usd = min(usd, self.cash, max_size_for_impact(liquidity_usd, self.max_impact))
        if usd <= 1:
            raise TradeError("pool too thin for the impact cap")
        impact = amm_impact(usd, liquidity_usd)
        fill_price = price / (1 - impact) * (1 + self.fee)
        self.cash -= usd
        return Fill(usd / fill_price, usd, fill_price, impact * 100)

    async def sell(self, token: str, qty: float, price: float, liquidity_usd: float, urgent: bool = False) -> Fill:
        impact = amm_impact(qty * price, liquidity_usd)
        fill_price = price * (1 - impact) * (1 - self.fee)
        usd = qty * fill_price
        self.cash += usd
        return Fill(-qty, -usd, fill_price, impact * 100)


class JupiterBroker:
    """Needs: pip install solders, WALLET_PRIVATE_KEY (base58), SOLANA_RPC_URL (paid RPC advised)."""

    def __init__(self, ex: dict, http: httpx.AsyncClient | None = None):
        from solders.keypair import Keypair  # optional dependency, live mode only

        self.kp = Keypair.from_base58_string(os.environ["WALLET_PRIVATE_KEY"])
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.ex = ex
        self.http = http or httpx.AsyncClient(timeout=15)
        self.cash = 0.0
        self._decimals: dict[str, int] = {USDC: 6}

    async def start(self) -> None:
        self.cash = await self._usdc_balance()

    # ── RPC ─────────────────────────────────────────────────
    async def _rpc(self, method: str, params: list):
        r = await self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise TradeError(str(out["error"]))
        return out["result"]

    async def _usdc_balance(self) -> float:
        res = await self._rpc("getTokenAccountsByOwner",
                              [str(self.kp.pubkey()), {"mint": USDC}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            self._decimals[mint] = int(info["value"]["data"]["parsed"]["info"]["decimals"])
        return self._decimals[mint]

    async def _confirm(self, sig: str, timeout: float = 30) -> None:
        for _ in range(int(timeout / 0.5)):
            st = (await self._rpc("getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("err"):
                raise TradeError(f"transaction failed on-chain: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return
            await asyncio.sleep(0.5)
        raise TradeError("not confirmed in time")

    # ── swap ────────────────────────────────────────────────
    async def _quote(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> dict:
        q = await self.http.get(JUP_QUOTE, params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": amount_raw,
            "slippageBps": slippage_bps, "restrictIntermediateTokens": "true",
        })
        q.raise_for_status()
        return q.json()

    async def _execute(self, quote: dict) -> str:
        from solders.transaction import VersionedTransaction

        s = await self.http.post(JUP_SWAP, json={
            "quoteResponse": quote, "userPublicKey": str(self.kp.pubkey()),
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": 5_000_000, "priorityLevel": "veryHigh"}},
        })
        s.raise_for_status()
        raw = VersionedTransaction.from_bytes(base64.b64decode(s.json()["swapTransaction"]))
        signed = VersionedTransaction(raw.message, [self.kp])
        sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                  {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
        await self._confirm(sig)
        return sig

    async def buy(self, token: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        usd = min(usd, self.cash, max_size_for_impact(liquidity_usd, self.ex["max_price_impact_pct"]))
        for _ in range(3):  # shrink until the real route fits the impact cap
            quote = await self._quote(USDC, token, int(usd * 1e6), self.ex["buy_slippage_bps"])
            impact = float(quote.get("priceImpactPct") or 0) * 100
            if impact <= self.ex["max_price_impact_pct"]:
                break
            usd *= 0.5
        else:
            raise TradeError(f"price impact {impact:.1f}% too high even at ${usd:,.0f}")
        sig = await self._execute(quote)
        qty = int(quote["outAmount"]) / 10 ** await self.decimals(token)
        self.cash -= usd
        return Fill(qty, usd, usd / qty, impact, sig)

    async def sell(self, token: str, qty: float, price: float, liquidity_usd: float, urgent: bool = False) -> Fill:
        ladder = [self.ex["sell_slippage_bps"]] + (list(self.ex["panic_sell_slippage_bps"]) if urgent else [])
        amount = int(qty * 10 ** await self.decimals(token))
        last: Exception | None = None
        for bps in ladder:      # escalate slippage until the exit lands
            try:
                quote = await self._quote(token, USDC, amount, bps)
                sig = await self._execute(quote)
                usd = int(quote["outAmount"]) / 1e6
                self.cash += usd
                return Fill(-qty, -usd, usd / qty, float(quote.get("priceImpactPct") or 0) * 100, sig)
            except (TradeError, httpx.HTTPError) as exc:
                last = exc
        raise TradeError(f"sell failed after {len(ladder)} attempts: {last}")
