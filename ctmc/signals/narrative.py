"""Celebrity-name narrative plays.

When a celebrity is in the news, coins named after them (or after what they just said)
can run even without their backing. This module:
  1. marks a celebrity "hot" whenever they post, and pulls the stand-out words from the
     post (names, hashtags, $tags, quoted phrases, Capitalised Phrases -> "topg", "kekiusmaximus");
  2. matches new and recent tokens whose name/symbol fits a hot keyword;
  3. puts matches on a RADAR. A radar coin is only bought once proven wallets pile in
     (KOL cluster) and it passes every safety check — name-match alone is never enough,
     because copycat scams use the same names.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

_STOP = {
    "the", "and", "for", "this", "that", "with", "you", "your", "are", "was", "will", "have", "has", "just",
    "not", "but", "all", "can", "its", "it's", "what", "when", "who", "how", "why", "they", "them", "their",
    "our", "out", "now", "new", "big", "get", "got", "one", "two", "today", "tomorrow", "yes", "no", "lol",
    "very", "much", "more", "most", "great", "good", "best", "true", "false", "about", "from", "into", "over",
    "after", "before", "there", "here", "some", "any", "every", "people", "world", "time", "year", "day",
    "crypto", "coin", "token", "solana", "bitcoin", "ethereum", "sol", "btc", "eth", "usd", "usdc", "pump",
    "https", "http", "amp", "rt", "via", "breaking", "update", "thank", "thanks", "love", "like", "make",
}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def keywords_from_tweet(text: str, min_len: int = 3, limit: int = 6) -> list[str]:
    """Stand-out words a narrative coin would be named after, strongest first."""
    text = re.sub(r"https?://\S+", " ", text)
    found: list[str] = []
    found += re.findall(r"[#$]([A-Za-z][A-Za-z0-9_]{1,30})", text)                  # #tags and $tags
    found += re.findall(r"[\"“']([^\"”']{3,30})[\"”']", text)                     # "quoted names"
    for run in re.findall(r"(?:\b[A-Z][a-zA-Z0-9]*\b(?:\s+|$)){2,4}", text):        # Capitalised Phrases ("Top G")
        found.append(run.strip())
    found += [w for w in re.findall(r"\b[A-Z][a-zA-Z0-9]{3,}\b", text)]             # single proper nouns
    out: list[str] = []
    for f in found:
        k = norm(f)
        if len(k) >= min_len and k not in _STOP and k not in out:
            out.append(k)
    return out[:limit]


@dataclass
class RadarCoin:
    mint: str
    name: str
    symbol: str
    handle: str            # which celebrity's narrative
    keyword: str
    since: float = field(default_factory=time.time)
    source: str = ""


class Narrative:
    def __init__(self, celebrities: list[dict], rules: dict):
        self.r = rules
        self.base = {c["handle"]: {norm(k) for k in (c.get("keywords") or [])} | {norm(c["handle"])}
                     for c in celebrities}
        self.hot: dict[str, tuple[float, set[str]]] = {}          # handle -> (hot until, keywords)
        self.radar: dict[str, RadarCoin] = {}

    def on_tweet(self, handle: str, text: str) -> list[str]:
        """Celebrity posted: they're hot for a while, with this post's words added."""
        fresh = keywords_from_tweet(text, self.r.get("min_keyword_len", 3), self.r.get("max_keywords_per_tweet", 4))
        until = time.time() + self.r.get("hot_hours", 12) * 3600
        _, old = self.hot.get(handle, (0, set()))
        self.hot[handle] = (until, old | self.base.get(handle, set()) | set(fresh))
        return fresh

    def hot_keywords(self) -> dict[str, str]:
        now, out = time.time(), {}
        for handle, (until, kws) in list(self.hot.items()):
            if until < now:
                del self.hot[handle]
                continue
            for k in kws:
                out.setdefault(k, handle)
        return out

    def match(self, name: str, symbol: str) -> tuple[str, str] | None:
        """(handle, keyword) if a token's name/symbol fits a hot keyword."""
        n, s = norm(name), norm(symbol)
        words = {norm(w) for w in re.split(r"\s+", name or "") if w}
        for kw, handle in sorted(self.hot_keywords().items(), key=lambda kv: -len(kv[0])):
            if len(kw) < self.r.get("min_keyword_len", 3):
                continue
            if s == kw or n == kw or kw in words or (len(kw) >= 5 and (kw in n or kw in s)):
                return handle, kw
        return None

    def consider(self, mint: str, name: str, symbol: str, created_ts: float | None = None, source: str = "") -> RadarCoin | None:
        if mint in self.radar:
            return self.radar[mint]
        if created_ts and time.time() - created_ts > self.r.get("lookback_hours", 72) * 3600:
            return None
        m = self.match(name, symbol)
        if not m:
            return None
        coin = RadarCoin(mint, name, symbol, m[0], m[1], source=source)
        self.radar[mint] = coin
        self._expire()
        return coin

    def _expire(self) -> None:
        ttl = self.r.get("radar_hours", 24) * 3600
        for mint in [m for m, c in self.radar.items() if time.time() - c.since > ttl]:
            del self.radar[mint]
        while len(self.radar) > self.r.get("radar_max", 200):
            del self.radar[min(self.radar, key=lambda m: self.radar[m].since)]
