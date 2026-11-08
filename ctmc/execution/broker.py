"""Brokers, async.

PaperBroker    simulated fills with realistic AMM price impact + fees.
JupiterBroker  live swaps routed by Jupiter across every Solana DEX.

Live execution rules (learned the hard way by Hummingbot's Gateway and Nautilus):
  * a transaction is sent ONCE, then the same signed bytes are re-broadcast every ~2s
    until it confirms or its blockhash expires (the same signature can never execute twice);
  * a NEW transaction is only built after the old one is PROVEN dropped (block height past
    its lastValidBlockHeight and still not found) — never on a mere timeout, which is how
    bots sell the same bag twice;
  * slippage is widened only when the failure WAS slippage (Jupiter error 6001), and only
    on sells that must land;
  * fills are booked from the wallet's real on-chain balance changes, not from the quote;
  * sells are sized from the on-chain token balance; a SOL reserve is kept for fees;
  * buys abort if the price ran too far between the checks and the order (chase guard).
Both brokers refuse to move price more than `max_price_impact_pct`: buys shrink to fit.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass

import httpx

USDC = "EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc"
JUP_BASE = os.getenv("JUPITER_API_URL", "https://api.jup.ag")
JITO_URL = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"
SLIPPAGE_ERRORS = {6001, 0x1771}


@dataclass
class Fill:
    qty: float        # tokens bought (+) or sold (-)
    usd: float        # USD spent (+) or received (-)
    price: float      # effective price paid / received
    impact_pct: float = 0.0
    signature: str = ""
    quoted_usd: float = 0.0       # what the quote promised (for realized-vs-quoted slippage)
    confirm_ms: float = 0.0       # send -> confirmed
    fee_usd: float = 0.0          # network + priority + tip fees paid for this transaction


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


def jup_headers() -> dict:
    key = os.getenv("JUPITER_API_KEY", "")
    return {"x-api-key": key} if key else {}


class PaperBroker:
    def __init__(self, cash: float, ex: dict):
        self.cash = cash
        self.fee = ex.get("paper_fee_pct", 0.3) / 100
        self.net_fee = ex.get("network_fee_usd", 0.20)        # priority + base fee per transaction
        self.max_impact = ex.get("max_price_impact_pct", 3)

    async def buy(self, token: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        usd = min(usd, self.cash, max_size_for_impact(liquidity_usd, self.max_impact))
        if usd <= 1:
            raise TradeError("pool too thin for the impact cap")
        impact = amm_impact(usd, liquidity_usd)
        fill_price = price / (1 - impact) * (1 + self.fee)
        self.cash -= usd + self.net_fee
        return Fill(usd / fill_price, usd, fill_price, impact * 100, fee_usd=self.net_fee)

    async def sell(self, token: str, qty: float, price: float, liquidity_usd: float, urgent: bool = False) -> Fill:
        impact = amm_impact(qty * price, liquidity_usd)
        fill_price = price * (1 - impact) * (1 - self.fee)
        usd = qty * fill_price
        self.cash += usd - self.net_fee
        return Fill(-qty, -usd, fill_price, impact * 100, fee_usd=self.net_fee)


def slippage_error(err) -> bool:
    """True if an on-chain / simulation error is Jupiter's slippage-exceeded."""
    try:
        custom = err["InstructionError"][1]["Custom"]
        return int(custom) in SLIPPAGE_ERRORS
    except (KeyError, TypeError, IndexError, ValueError):
        return "slippage" in str(err).lower() or "0x1771" in str(err)


def balance_delta(meta: dict, wallet: str, mint: str) -> float:
    def total(rows):
        return sum(float((r.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for r in rows or [] if r.get("owner") == wallet and r.get("mint") == mint)
    return total(meta.get("postTokenBalances")) - total(meta.get("preTokenBalances"))


class JupiterBroker:
    """Needs: pip install solders, WALLET_PRIVATE_KEY (base58), SOLANA_RPC_URL (paid RPC advised),
    JUPITER_API_KEY (free at portal.jup.ag)."""

    def __init__(self, ex: dict, http: httpx.AsyncClient | None = None, store=None):
        from solders.keypair import Keypair  # optional dependency, live mode only

        self.kp = Keypair.from_base58_string(os.environ["WALLET_PRIVATE_KEY"])
        self.wallet = str(self.kp.pubkey())
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.ex = ex
        self.http = http or httpx.AsyncClient(timeout=15)
        self.store = store                    # ctmc.storage.Store: intents are written before sending
        self.cash = 0.0
        self.sol = 0.0
        self.sol_usd = 150.0                  # kept current from the SOL leader feed (for fee accounting)
        self._decimals: dict[str, int] = {USDC: 6}

    async def start(self) -> None:
        self.cash = await self._usdc_balance()
        self.sol = await self.sol_balance()

    # ── RPC ─────────────────────────────────────────────────
    async def _rpc(self, method: str, params: list, url: str | None = None):
        r = await self.http.post(url or self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise TradeError(str(out["error"]))
        return out["result"]

    async def _usdc_balance(self) -> float:
        return await self.token_balance(USDC)

    async def sol_balance(self) -> float:
        return float((await self._rpc("getBalance", [self.wallet]))["value"]) / 1e9

    async def token_balance(self, mint: str) -> float:
        res = await self._rpc("getTokenAccountsByOwner", [self.wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            self._decimals[mint] = int(info["value"]["data"]["parsed"]["info"]["decimals"])
        return self._decimals[mint]

    # ── Jupiter ─────────────────────────────────────────────
    async def _get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            r = await self.http.get(f"{JUP_BASE}{path}", params=params, headers=jup_headers())
            if r.status_code == 429:                       # rate limited: back off, never call it "no route"
                await asyncio.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        raise TradeError("Jupiter rate limit — try again shortly")

    async def _quote(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> dict:
        return await self._get("/swap/v1/quote", {
            "inputMint": in_mint, "outputMint": out_mint, "amount": amount_raw,
            "slippageBps": slippage_bps, "restrictIntermediateTokens": "true"})

    async def _build(self, quote: dict) -> dict:
        jito = int(self.ex.get("jito_tip_lamports") or 0)
        fee = ({"jitoTipLamports": jito} if jito else
               {"priorityLevelWithMaxLamports": {"maxLamports": int(self.ex.get("max_priority_fee_lamports", 3_000_000)),
                                                 "priorityLevel": "veryHigh"}})
        r = await self.http.post(f"{JUP_BASE}/swap/v1/swap", headers=jup_headers(), json={
            "quoteResponse": quote, "userPublicKey": self.wallet,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": fee})
        r.raise_for_status()
        built = r.json()
        if built.get("simulationError"):                   # failed in simulation: nothing was sent
            err = built["simulationError"]
            raise TradeError(f"{'SLIPPAGE' if slippage_error(err) else 'SIMULATION'}: {err}")
        return built

    def _sign(self, built: dict) -> str:
        from solders.transaction import VersionedTransaction

        raw = VersionedTransaction.from_bytes(base64.b64decode(built["swapTransaction"]))
        return base64.b64encode(bytes(VersionedTransaction(raw.message, [self.kp]))).decode()

    async def _broadcast(self, tx_b64: str) -> str:
        params = [tx_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]
        if self.ex.get("jito_tip_lamports"):
            try:                                              # private path: not visible to sandwich bots
                return await self._rpc("sendTransaction", params, url=JITO_URL)
            except (TradeError, httpx.HTTPError):
                pass
        return await self._rpc("sendTransaction", params)

    async def send_and_confirm(self, built: dict, side: str, mint: str, reason: str = "") -> tuple[str, str, dict]:
        """-> (status, signature, meta). status: CONFIRMED | FAILED | DROPPED | UNRESOLVED."""
        tx = self._sign(built)
        last_valid = int(built.get("lastValidBlockHeight") or 0)
        sig = await self._broadcast(tx)
        intent = self.store.fill(mint, "", side, 0, 0, 0, 0, sig, reason, status="pending") if self.store else None
        started, last_send, misses_after_expiry = time.monotonic(), time.monotonic(), 0
        result = "UNRESOLVED"
        try:
            while time.monotonic() - started < 150:
                st = (await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]))["value"][0]
                if st and st.get("err"):
                    result = "FAILED"
                    return result, sig, {"err": st["err"]}
                if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                    tx_full = await self._rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
                    meta = (tx_full or {}).get("meta") or {}
                    meta["_confirm_ms"] = (time.monotonic() - started) * 1000
                    result = "CONFIRMED"
                    return result, sig, meta
                if last_valid:
                    height = await self._rpc("getBlockHeight", [{"commitment": "confirmed"}])
                    if height > last_valid:
                        misses_after_expiry += 1
                        if misses_after_expiry >= 3:               # expired AND repeatedly not found
                            result = "DROPPED"
                            return result, sig, {}
                if time.monotonic() - last_send >= 2:              # same bytes: can't double-execute
                    try:
                        await self._broadcast(tx)
                    except (TradeError, httpx.HTTPError):
                        pass
                    last_send = time.monotonic()
                await asyncio.sleep(0.5)
            return result, sig, {}
        finally:
            if intent is not None:
                self.store.set_fill_status(intent, result.lower())

    def fee_usd(self, meta: dict) -> float:
        """What this transaction cost in network fees: base + priority (meta.fee) + any Jito tip."""
        lamports = int(meta.get("fee") or 0) + int(self.ex.get("jito_tip_lamports") or 0)
        return lamports / 1e9 * self.sol_usd

    # ── broker API ──────────────────────────────────────────
    async def buy(self, token: str, usd: float, price: float, liquidity_usd: float) -> Fill:
        if self.ex.get("min_sol_reserve", 0.1) and await self.sol_balance() < self.ex.get("min_sol_reserve", 0.1):
            raise TradeError(f"SOL balance below the {self.ex.get('min_sol_reserve', 0.1)} SOL fee reserve — top up")
        usd = min(usd, self.cash, max_size_for_impact(liquidity_usd, self.ex["max_price_impact_pct"]))
        dec = await self.decimals(token)
        for _ in range(3):  # shrink until the real route fits the impact cap
            quote = await self._quote(USDC, token, int(usd * 1e6), self.ex["buy_slippage_bps"])
            impact = float(quote.get("priceImpactPct") or 0) * 100
            if impact <= self.ex["max_price_impact_pct"]:
                break
            usd *= 0.5
        else:
            raise TradeError(f"price impact {impact:.1f}% too high even at ${usd:,.0f}")
        quoted_tokens = int(quote["outAmount"]) / 10 ** dec
        drift = (usd / quoted_tokens) / price - 1 if quoted_tokens and price else 0
        if drift * 100 > self.ex.get("max_entry_drift_pct", 25):
            raise TradeError(f"price ran {drift * 100:.0f}% since the checks — not chasing it")
        status, sig, meta = await self.send_and_confirm(await self._build(quote), "buy", token)
        if status != "CONFIRMED":
            raise TradeError(f"buy {status.lower()}: {meta.get('err', '')} ({sig})")
        got = balance_delta(meta, self.wallet, token) or quoted_tokens
        spent = -balance_delta(meta, self.wallet, USDC) or usd
        self.cash -= spent
        return Fill(got, spent, spent / got, impact, sig, quoted_usd=usd, confirm_ms=meta.get("_confirm_ms", 0),
                    fee_usd=self.fee_usd(meta))

    async def sell(self, token: str, qty: float, price: float, liquidity_usd: float, urgent: bool = False) -> Fill:
        held = await self.token_balance(token)
        qty = min(qty, held) if qty < held * 0.98 else held        # selling (nearly) all -> sell exactly what's there
        if qty <= 0:
            raise TradeError("no tokens left in the wallet for this coin")
        dec = await self.decimals(token)
        rungs = [self.ex["sell_slippage_bps"]] + (list(self.ex["panic_sell_slippage_bps"]) if urgent else [])
        i, last, drops = 0, "", 0
        while i < len(rungs) and drops < 3:
            quote = await self._quote(token, USDC, int(qty * 10 ** dec), rungs[i])
            try:
                built = await self._build(quote)
            except TradeError as exc:                        # simulation said no: nothing was sent
                last = str(exc)
                if "SLIPPAGE" in last:
                    i += 1
                    continue
                raise
            status, sig, meta = await self.send_and_confirm(built, "sell", token)
            if status == "CONFIRMED":
                usd = balance_delta(meta, self.wallet, USDC) or int(quote["outAmount"]) / 1e6
                sold = -balance_delta(meta, self.wallet, token) or qty
                self.cash += usd
                return Fill(-sold, -usd, usd / sold, float(quote.get("priceImpactPct") or 0) * 100, sig,
                            quoted_usd=int(quote["outAmount"]) / 1e6, confirm_ms=meta.get("_confirm_ms", 0),
                            fee_usd=self.fee_usd(meta))
            if status == "FAILED" and slippage_error(meta.get("err")):
                i += 1                                     # landed and failed on slippage: widen
                continue
            if status == "DROPPED":
                drops += 1                                 # proven never executed: safe to try again
                continue
            raise TradeError(f"sell {status.lower()} ({sig}) — not resending, to avoid selling twice")
        raise TradeError(f"sell failed after {len(rungs)} slippage levels: {last}")
