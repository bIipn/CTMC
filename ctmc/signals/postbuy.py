"""Checks that run AFTER a buy — too slow for the instant path, so they can only ever reduce risk.

  engagement   ~60 s after the celebrity's post: likes / reposts / replies / quotes, scored
               like last30days does (log-weighted), against THIS celebrity's own baseline.
               A post that isn't catching on -> sell half.
  grok         Grok's live-X read of the hype (10-90 s). Manufactured hype / red flags ->
               sell half. Never adds size.                       (Vibe-Trading advisory pattern)
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
import time

import httpx


def tweet_gone(status: int, body: dict | None) -> bool:
    """Did X say the post no longer exists? (Rate limits / errors are NOT deletions.)"""
    if status == 404:
        return True
    if status != 200 or not isinstance(body, dict) or body.get("data"):
        return False
    return any("not found" in str(e.get("title", "")).lower() or str(e.get("type", "")).endswith("resource-not-found")
               for e in body.get("errors") or [])


def engagement_score(m: dict) -> float:
    """0..100 from X public_metrics (last30days weights: likes .55, reposts .25, replies .15, quotes .05)."""
    raw = (0.55 * math.log1p(m.get("like_count", 0)) + 0.25 * math.log1p(m.get("retweet_count", 0))
           + 0.15 * math.log1p(m.get("reply_count", 0)) + 0.05 * math.log1p(m.get("quote_count", 0)))
    return round(min(100.0, raw / 9.2 * 100), 1)


class PostBuyReview:
    def __init__(self, rules: dict, store=None, hype=None, http: httpx.AsyncClient | None = None):
        self.r = rules
        self.store = store
        self.hype = hype
        self.http = http or httpx.AsyncClient(timeout=10)

    async def run(self, trader, pos, tweet) -> None:
        jobs = []
        if tweet is not None and os.getenv("X_BEARER_TOKEN") and self.r.get("engagement_check", True):
            jobs.append(self._engagement(trader, pos, tweet))
        if self.hype is not None and self.r.get("grok_review", True) and getattr(self.hype, "key", ""):
            jobs.append(self._grok(trader, pos))
        if tweet is not None and os.getenv("X_BEARER_TOKEN") and self.r.get("deletion_watch", True):
            jobs.append(self._deletion_watch(trader, pos, tweet))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _engagement(self, trader, pos, tweet) -> None:
        wait = self.r.get("engagement_after_s", 60) - (time.time() - tweet.created_at)
        if wait > 0:
            await asyncio.sleep(wait)
        r = await self.http.get(f"https://api.x.com/2/tweets/{tweet.id}", params={"tweet.fields": "public_metrics"},
                                headers={"Authorization": f"Bearer {os.getenv('X_BEARER_TOKEN', '')}"})
        if r.status_code != 200:
            return
        score = engagement_score(((r.json() or {}).get("data") or {}).get("public_metrics") or {})
        key = f"engagement:{tweet.handle or tweet.author_id}"
        history = (self.store.get(key, []) if self.store else []) or []
        verdict = self.judge(score, history)
        if self.store:
            self.store.put(key, (history + [score])[-30:])
        if verdict and pos.token in trader.positions:
            await trader.trim(pos, 0.5, verdict)
        else:
            trader.log("KOL", pos.symbol, f"tweet engagement {score:.0f}/100 at ~1 min"
                                          + (f" (their usual {statistics.median(history):.0f})" if history else ""))

    def judge(self, score: float, history: list[float]) -> str:
        """A trim reason if this post is clearly under-performing this celebrity's norm, else ''."""
        if len(history) < self.r.get("engagement_min_history", 5):
            return ""
        usual = statistics.median(history)
        if usual > 0 and score < self.r.get("engagement_weak_ratio", 0.5) * usual:
            return f"tweet isn't catching on: engagement {score:.0f} vs their usual {usual:.0f} — taking half off"
        return ""

    async def _deletion_watch(self, trader, pos, tweet) -> None:
        """Hacked posts get deleted within minutes. If the trigger post vanishes, get out."""
        until = time.time() + self.r.get("deletion_watch_minutes", 20) * 60
        while time.time() < until and pos.token in trader.positions:
            await asyncio.sleep(self.r.get("deletion_poll_s", 45))
            try:
                r = await self.http.get(f"https://api.x.com/2/tweets/{tweet.id}",
                                        headers={"Authorization": f"Bearer {os.getenv('X_BEARER_TOKEN', '')}"})
                body = r.json() if r.content else None
            except (httpx.HTTPError, ValueError):
                continue
            if tweet_gone(r.status_code, body) and pos.token in trader.positions:
                who = f"@{tweet.handle}" if tweet.handle else "the"
                await trader.close(pos, f"RUG ALERT — {who} post that triggered this was deleted (hacked or scam)",
                                   urgent=True)
                return

    async def _grok(self, trader, pos) -> None:
        v = await self.hype.check(pos.symbol, pos.token)
        if v.ok:
            trader.log("KOL", pos.symbol, f"Grok: hype looks real ({v.score:.0f}/100)" if v.score else "Grok: hype looks real")
        elif pos.token in trader.positions and not any("unavailable" in r or "not set" in r or "error" in r
                                                         for r in v.reasons):
            await trader.trim(pos, 0.5, "Grok: " + "; ".join(v.reasons[:2]) + " — taking half off")
