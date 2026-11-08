"""Brokers: PaperBroker (default, simulated) and JupiterBroker (live Solana swaps)."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import requests

USDC = "EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"


@dataclass
class Fill:
    qty: float        # tokens bought (+) or sold (-)
    usd: float        # USD spent (+) or received (-)
    price: float


class PaperBroker:
    """Fills at the observed price with slippage + fee haircut."""

    def __init__(self, cash: float, slippage_pct: float = 1.0, fee_pct: float = 0.3):
        self.cash = cash
        self.cost = (slippage_pct + fee_pct) / 100

    def buy(self, token: str, usd: float, price: float) -> Fill:
        usd = min(usd, self.cash)
        fill_price = price * (1 + self.cost)
        self.cash -= usd
        return Fill(usd / fill_price, usd, fill_price)

    def sell(self, token: str, qty: float, price: float) -> Fill:
        fill_price = price * (1 - self.cost)
        usd = qty * fill_price
        self.cash += usd
        return Fill(-qty, -usd, fill_price)


class JupiterBroker:
    """Live swaps USDC <-> token through Jupiter. Signs with WALLET_PRIVATE_KEY.

    Requires:  pip install solders   and env WALLET_PRIVATE_KEY (base58),
               SOLANA_RPC_URL (a paid RPC like Helius is strongly advised).
    """

    def __init__(self, slippage_bps: int = 150):
        from solders.keypair import Keypair  # imported lazily: optional dependency

        key = os.environ["WALLET_PRIVATE_KEY"]
        self.kp = Keypair.from_base58_string(key)
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.slippage_bps = slippage_bps
        self.http = requests.Session()
        self.cash = self._usdc_balance()

    # -- helpers ------------------------------------------------
    def _rpc(self, method: str, params: list) -> dict:
        r = self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    def _usdc_balance(self) -> float:
        res = self._rpc("getTokenAccountsByOwner", [str(self.kp.pubkey()), {"mint": USDC}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])

    def _swap(self, in_mint: str, out_mint: str, amount_raw: int) -> dict:
        from solders.transaction import VersionedTransaction

        q = self.http.get(JUP_QUOTE, params={
            "inputMint": in_mint, "outputMint": out_mint,
            "amount": amount_raw, "slippageBps": self.slippage_bps,
        }, timeout=15)
        q.raise_for_status()
        quote = q.json()
        s = self.http.post(JUP_SWAP, json={
            "quoteResponse": quote, "userPublicKey": str(self.kp.pubkey()),
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto",
        }, timeout=15)
        s.raise_for_status()
        raw = VersionedTransaction.from_bytes(base64.b64decode(s.json()["swapTransaction"]))
        signed = VersionedTransaction(raw.message, [self.kp])
        sig = self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64", "maxRetries": 3}])
        quote["signature"] = sig
        return quote

    def _decimals(self, mint: str) -> int:
        info = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        return int(info["value"]["data"]["parsed"]["info"]["decimals"])

    # -- broker API ---------------------------------------------
    def buy(self, token: str, usd: float, price: float) -> Fill:
        usd = min(usd, self.cash)
        q = self._swap(USDC, token, int(usd * 1e6))
        qty = int(q["outAmount"]) / 10 ** self._decimals(token)
        self.cash -= usd
        return Fill(qty, usd, usd / qty if qty else price)

    def sell(self, token: str, qty: float, price: float) -> Fill:
        q = self._swap(token, USDC, int(qty * 10 ** self._decimals(token)))
        usd = int(q["outAmount"]) / 1e6
        self.cash += usd
        return Fill(-qty, -usd, usd / qty if qty else price)
