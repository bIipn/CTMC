"""Real-time celebrity tweet feeds.

Two sources, both push-based (no polling):

  XFilteredStream  official X API v2 filtered stream. A persistent HTTP stream: X pushes
                   each matching tweet within about a second. Needs an X API plan that
                   includes filtered stream (Pro tier) -> X_BEARER_TOKEN.
  WebhookSource    tiny local HTTP server. Point any tweet-alert service at it
                   (twitterapi.io, TweetShift, a Telegram relay, ...) and it forwards
                   tweets as JSON: {"author_id", "text", "id", "created_at"}.

Every tweet is checked against the configured celebrity *user IDs*, so a renamed
or impersonating account can't trigger a buy.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Awaitable, Callable

import httpx

# Solana mints are base58, 32-44 chars. Excludes 0, O, I, l.
_B58 = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
_MINT_RE = re.compile(rf"(?<![1-9A-HJ-NP-Za-km-z])({_B58})(?![1-9A-HJ-NP-Za-km-z])")
_URL_MINT_RE = re.compile(
    rf"(?:pump\.fun/(?:coin/)?|dexscreener\.com/solana/|birdeye\.so/token/|solscan\.io/token/"
    rf"|jup\.ag/swap/[A-Za-z0-9]+-|gmgn\.ai/sol/token/|photon-sol\.tinyastro\.io/en/lp/)({_B58})"
)
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")

# Never treat these as a trade target.
_NOT_TARGETS = {"So11111111111111111111111111111111111111112", "EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc"}
_IGNORE_TICKERS = {"SOL", "BTC", "ETH", "USD", "USDC", "USDT"}


@dataclass
class Tweet:
    id: str
    author_id: str
    text: str
    created_at: float = field(default_factory=time.time)
    is_reply: bool = False
    received_at: float = field(default_factory=time.time)
    handle: str = ""


@dataclass
class CoinMention:
    mints: list[str]
    tickers: list[str]

    def __bool__(self) -> bool:
        return bool(self.mints or self.tickers)


def extract_coins(text: str) -> CoinMention:
    """Pull contract addresses (first priority) and $TICKERS out of tweet text."""
    mints: dict[str, None] = {}
    for m in _URL_MINT_RE.findall(text):
        mints.setdefault(m, None)
    for m in _MINT_RE.findall(text):
        if any(c.isdigit() for c in m) and any(c.isalpha() for c in m):  # real addresses mix both
            mints.setdefault(m, None)
    tickers = [t.upper() for t in _CASHTAG_RE.findall(text) if t.upper() not in _IGNORE_TICKERS]
    return CoinMention([m for m in mints if m not in _NOT_TARGETS], list(dict.fromkeys(tickers)))


class CelebrityFilter:
    def __init__(self, celebrities: list[dict], rules: dict):
        self.ids = {str(c["id"]): c["handle"] for c in celebrities if c.get("id")}
        self.unresolved = [c["handle"] for c in celebrities if not c.get("id")]
        self.max_age = rules.get("max_tweet_age_seconds", 20)
        self.ignore_replies = rules.get("ignore_replies", True)

    def accept(self, t: Tweet) -> tuple[bool, str]:
        if t.author_id not in self.ids:
            return False, f"author {t.author_id} is not a watched celebrity ID"
        t.handle = self.ids[t.author_id]
        if self.ignore_replies and t.is_reply:
            return False, "reply, not an original post"
        age = time.time() - t.created_at
        if age > self.max_age:
            return False, f"tweet is {age:.0f}s old"
        return True, "ok"


def _parse_time(s: str | None) -> float:
    if not s:
        return time.time()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def tweet_time_from_id(tweet_id: str) -> float | None:
    """Exact post time encoded in an X snowflake ID: ms = (id >> 22) + 1288834974657."""
    try:
        return ((int(tweet_id) >> 22) + 1288834974657) / 1000
    except (TypeError, ValueError):
        return None


def tweet_from_v2(payload: dict) -> Tweet | None:
    d = payload.get("data") or {}
    if not d.get("id"):
        return None
    refs = d.get("referenced_tweets") or []
    return Tweet(
        id=d["id"], author_id=str(d.get("author_id", "")), text=d.get("note_tweet", {}).get("text") or d.get("text", ""),
        created_at=tweet_time_from_id(d["id"]) or _parse_time(d.get("created_at")),   # ID time is exact to the ms
        is_reply=any(r.get("type") == "replied_to" for r in refs),
    )


OnTweet = Callable[[Tweet], Awaitable[None]]


def build_rules(handles: list[str], max_len: int = 512) -> list[str]:
    """'(from:a OR from:b ...) -is:retweet' rules, each within X's rule-length limit."""
    rules, cur = [], []
    for h in handles:
        trial = "(" + " OR ".join(f"from:{x}" for x in cur + [h]) + ") -is:retweet"
        if cur and len(trial) > max_len:
            rules.append("(" + " OR ".join(f"from:{x}" for x in cur) + ") -is:retweet")
            cur = [h]
        else:
            cur.append(h)
    if cur:
        rules.append("(" + " OR ".join(f"from:{x}" for x in cur) + ") -is:retweet")
    return rules


class XFilteredStream:
    """Official X API v2 filtered stream."""

    BASE = "https://api.x.com/2/tweets/search/stream"

    def __init__(self, celebrities: list[dict]):
        self.token = os.getenv("X_BEARER_TOKEN", "")
        self.handles = [c["handle"] for c in celebrities]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def sync_rules(self, http: httpx.AsyncClient) -> None:
        """Posts from every watched celebrity, split into as many rules as X's length limit needs."""
        want = build_rules(self.handles)
        r = await http.get(f"{self.BASE}/rules", headers=self._headers())
        existing = (r.json().get("data") or []) if r.status_code == 200 else []
        if sorted(x.get("value") for x in existing) == sorted(want):
            return
        if existing:
            await http.post(f"{self.BASE}/rules", headers=self._headers(),
                            json={"delete": {"ids": [x["id"] for x in existing]}})
        await http.post(f"{self.BASE}/rules", headers=self._headers(),
                        json={"add": [{"value": v, "tag": f"ctmc-celebs-{i}"} for i, v in enumerate(want)]})

    async def run(self, on_tweet: OnTweet) -> None:
        if not self.token:
            raise RuntimeError("X_BEARER_TOKEN not set — the X stream needs an API plan with filtered stream")
        backoff = 1
        params = {"tweet.fields": "author_id,created_at,referenced_tweets,note_tweet", "expansions": "author_id"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None)) as http:
            await self.sync_rules(http)
            while True:
                try:
                    async with http.stream("GET", self.BASE, headers=self._headers(), params=params) as resp:
                        if resp.status_code == 429:
                            raise httpx.HTTPError("rate limited")
                        resp.raise_for_status()
                        backoff = 1
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue                     # keep-alive heartbeat
                            t = tweet_from_v2(json.loads(line))
                            if t:
                                asyncio.create_task(on_tweet(t))
                except (httpx.HTTPError, json.JSONDecodeError):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)


class WebhookSource:
    """Local endpoint any tweet-alert service can POST to."""

    def __init__(self, port: int = 8787):
        self.port = port
        self.secret = os.getenv("WEBHOOK_SECRET", "")

    async def run(self, on_tweet: OnTweet) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            status = "400 Bad Request"
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode(errors="ignore")
                length = int(re.search(r"content-length:\s*(\d+)", head, re.I).group(1))
                auth = re.search(r"x-ctmc-secret:\s*(\S+)", head, re.I)
                body = json.loads(await reader.readexactly(length))
                if self.secret and (not auth or auth.group(1) != self.secret):
                    status = "401 Unauthorized"
                else:
                    t = tweet_from_v2({"data": body}) if "data" not in body else tweet_from_v2(body)
                    if t:
                        asyncio.create_task(on_tweet(t))
                        status = "200 OK"
            except Exception:
                pass
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()
            writer.close()

        # Only listen beyond this machine when a shared secret protects the endpoint.
        host = "0.0.0.0" if self.secret else "127.0.0.1"
        server = await asyncio.start_server(handle, host, self.port)
        async with server:
            await server.serve_forever()


async def resolve_ids(celebrities: list[dict], http: httpx.AsyncClient | None = None,
                      log=lambda msg: None) -> list[str]:
    """Look up every handle on X (needs X_BEARER_TOKEN): fill missing IDs and CORRECT wrong ones,
    so a typo can never silently mute a celebrity. Returns handles still without an ID."""
    token = os.getenv("X_BEARER_TOKEN", "")
    if not token or not celebrities:
        return [c["handle"] for c in celebrities if not c.get("id")]
    own = http is None
    http = http or httpx.AsyncClient(timeout=10)
    found: dict[str, str] = {}
    try:
        for i in range(0, len(celebrities), 100):                  # X allows 100 usernames per call
            batch = ",".join(c["handle"] for c in celebrities[i:i + 100])
            try:
                r = await http.get("https://api.x.com/2/users/by", params={"usernames": batch},
                                   headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200:
                    found.update({u["username"].lower(): u["id"] for u in (r.json().get("data") or [])})
            except (httpx.HTTPError, ValueError):
                continue
    finally:
        if own:
            await http.aclose()
    for c in celebrities:
        real = found.get(c["handle"].lower())
        if real and str(c.get("id") or "") not in ("", real):
            log(f"@{c['handle']}: configured id {c['id']} was wrong — corrected to {real}")
        if real:
            c["id"] = real
    return [c["handle"] for c in celebrities if not c.get("id")]


def make_source(cfg: dict):
    src = cfg["twitter"].get("source", "x_stream")
    if src == "webhook":
        return WebhookSource(cfg["twitter"].get("webhook_port", 8787))
    return XFilteredStream(cfg["celebrities"])
