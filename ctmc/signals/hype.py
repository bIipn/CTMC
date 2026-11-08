"""X / Twitter hype verification through the xAI Grok API (live X search).

Grok reads the live X conversation about a ticker and returns a strict JSON
verdict: is the hype organic, who is talking, are there bot/paid-shill signs.
Needs XAI_API_KEY (console.x.ai). No key -> hype check fails closed.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from ctmc.models import Verdict

XAI_URL = "https://api.x.ai/v1/chat/completions"

PROMPT = """You are a strict crypto due-diligence analyst with live X (Twitter) search.
Assess the CURRENT X conversation (last 24h) about the Solana token ${symbol} (mint {mint}).

Judge:
1. Is the hype ORGANIC (many distinct real accounts, real discussion) or MANUFACTURED
   (copy-paste shills, fresh/bot accounts, "paid promo" patterns, raid groups)?
2. Which credible, well-known accounts (large followings, track record, NOT paid callers)
   are discussing it? Name them.
3. Any warnings: dev dumping, rug accusations, bundle/sniper callouts, KOL dumping on followers.

Reply with ONLY this JSON, no prose:
{{"hype_score": 0-100, "organic": true/false,
  "credible_accounts": ["@handle", ...],
  "red_flags": ["...", ...],
  "summary": "one sentence"}}"""


class HypeChecker:
    def __init__(self, rules: dict, http: httpx.AsyncClient | None = None):
        self.r = rules
        self.http = http or httpx.AsyncClient(timeout=90)
        self.key = os.getenv("XAI_API_KEY", "")
        self._cache: dict[str, tuple[float, Verdict]] = {}

    async def check(self, symbol: str, mint: str) -> Verdict:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < self.r.get("cache_minutes", 30) * 60:
            return hit[1]
        verdict = await self._ask_grok(symbol, mint)
        self._cache[mint] = (time.time(), verdict)
        return verdict

    async def _ask_grok(self, symbol: str, mint: str) -> Verdict:
        if not self.key:
            return Verdict(False, ["XAI_API_KEY not set — cannot verify X hype (fail closed)"])
        body = {
            "model": self.r.get("model", "grok-4"),
            "messages": [{"role": "user", "content": PROMPT.format(symbol=symbol, mint=mint)}],
            "search_parameters": {"mode": "on", "sources": [{"type": "x"}]},
            "temperature": 0,
        }
        try:
            resp = await self.http.post(
                XAI_URL, json=body,
                headers={"Authorization": f"Bearer {self.key}"},
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return Verdict(False, [f"grok error ({exc.__class__.__name__}) — fail closed"])
        return self.evaluate(data)

    def evaluate(self, d: dict) -> Verdict:
        why = []
        score = float(d.get("hype_score") or 0)
        if not d.get("organic"):
            why.append("hype looks manufactured (bots / paid shills)")
        if score < self.r.get("min_score", 65):
            why.append(f"hype score {score:.0f} < {self.r.get('min_score', 65)}")
        names = d.get("credible_accounts") or []
        if len(names) < self.r.get("min_credible_accounts", 2):
            why.append(f"only {len(names)} credible accounts behind it")
        why += [f"X red flag: {f}" for f in d.get("red_flags") or []]
        return Verdict(not why, why or [d.get("summary", "")], score)
