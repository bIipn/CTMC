"""Hacked-account defence.

The classic X rug: a hacker takes over a famous account, launches a token, posts its
contract address, dumps on everyone who buys, and the post is deleted minutes later.

Before a buy (per post):
  scam wording     presale / airdrop / claim / "connect wallet" / telegram invite links / giveaways
  out of character a celebrity who has NEVER posted a coin suddenly posts one
                   (accounts marked posts_coins — callers, founders — are exempt)
  burst            the same account posts two DIFFERENT coins inside an hour -> compromised
  fresh launch     the coin's pool is minutes old AND the post is out of character
  compromised      the account was flagged recently -> nothing it posts is bought
Levels: ok (normal) · caution (quarter size, watched closely) · block (no buy).

After a buy:
  admission        the account then posts "hacked" / "not me" / "don't buy" / "fake token"
                   -> sell every position that came from it, now
  deletion         the trigger post disappears (ctmc/signals/postbuy.py) -> sell now
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

SCAM_PATTERNS: list[tuple[str, int, str]] = [
    (r"\bpre-?sale\b", 3, "presale"),
    (r"\bair-?drop\b", 3, "airdrop"),
    (r"\bclaim (your|now|here|free)\b|\bclaim\b.*\btokens?\b", 3, "claim your tokens"),
    (r"connect (your )?wallet|wallet ?connect", 4, "connect your wallet"),
    (r"\bwhite-?list\b", 2, "whitelist"),
    (r"\bgive-?away\b", 2, "giveaway"),
    (r"send (me )?\d*\s*(sol|eth|usdc)\b", 4, "asks you to send crypto"),
    (r"t\.me/|telegram\.me/|discord\.gg/", 2, "telegram/discord invite link"),
    (r"first \d{2,5} (holders|buyers|people)", 2, "first-N-holders bait"),
    (r"\b(my|our) (official )?(token|coin|memecoin) is (now )?live\b", 2, "\"my token is live\""),
    (r"\bofficial (token|coin|memecoin)\b", 1, "\"official token\""),
    (r"\b(dm|message) me\b", 2, "DM me"),
    (r"\b(bit\.ly|tinyurl\.com|cutt\.ly|rb\.gy)/", 2, "link shortener"),
]
# FIRST-PERSON only: callers and celebrities talk about OTHER accounts getting hacked all the time
ADMISSIONS = re.compile(
    r"\b(i was hacked|i got hacked|we (were|got) hacked|hacked my|"
    r"(my|our) (x |twitter )?account (was|got|has been|is|had been) (hacked|compromised)|"
    r"not me|wasn'?t me|don'?t buy|do not buy|fake (token|coin)|scam (token|coin)|"
    r"ignore (the|my) (last|previous)|unauthori[sz]ed (post|tweet|access)|lost access|"
    r"someone (got|gained) access)\b", re.I)


@dataclass
class HackVerdict:
    level: str = "ok"                 # ok | caution | block
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def scale(self) -> float:
        return {"ok": 1.0, "caution": 0.25, "block": 0.0}[self.level]


def scam_score(text: str) -> tuple[int, list[str]]:
    score, why = 0, []
    for pattern, weight, label in SCAM_PATTERNS:
        if re.search(pattern, text, re.I):
            score += weight
            why.append(label)
    return score, why


class HackGuard:
    def __init__(self, rules: dict, store=None, clock=time.time):
        self.r = rules
        self.store = store
        self.clock = clock
        self._posts: dict[str, list[tuple[float, str]]] = {}     # handle -> [(ts, mint)] coin posts
        self._compromised: dict[str, float] = {}                 # handle -> flagged until

    # ── memory (SQLite when available, so it survives restarts) ──
    def _history(self, handle: str) -> list[tuple[float, str]]:
        if self.store is not None:
            return [tuple(x) for x in self.store.get(f"coinposts:{handle.lower()}", [])]
        return self._posts.get(handle.lower(), [])

    def record_coin_post(self, handle: str, mint: str) -> None:
        hist = (self._history(handle) + [(self.clock(), mint)])[-50:]
        if self.store is not None:
            self.store.put(f"coinposts:{handle.lower()}", [list(x) for x in hist])
        else:
            self._posts[handle.lower()] = hist

    def mark_compromised(self, handle: str, reason: str) -> None:
        until = self.clock() + self.r.get("compromised_hours", 24) * 3600
        if self.store is not None:
            self.store.put(f"compromised:{handle.lower()}", [until, reason])
        self._compromised[handle.lower()] = until

    def compromised(self, handle: str) -> bool:
        until = self._compromised.get(handle.lower())
        if until is None and self.store is not None:
            until = (self.store.get(f"compromised:{handle.lower()}") or [0])[0]
        return bool(until and until > self.clock())

    # ── every post from a watched account ────────────────────
    def admission(self, handle: str, text: str) -> bool:
        """True if the post says the account was hacked / the coin is fake. Marks it compromised."""
        if ADMISSIONS.search(text or ""):
            self.mark_compromised(handle, "the account itself said it was hacked / the coin is fake")
            return True
        return False

    # ── a coin post: buy it or not? ──────────────────────────
    def assess(self, handle: str, text: str, mint: str, posts_coins: bool = False,
               pool_age_minutes: float | None = None) -> HackVerdict:
        v = HackVerdict()
        now = self.clock()
        if self.compromised(handle):
            v.level, v.reasons = "block", [f"@{handle} looks compromised — not buying anything it posts for now"]
            return v
        others = {m for ts, m in self._history(handle)
                  if now - ts <= self.r.get("burst_window_minutes", 60) * 60 and m != mint}
        limit = self.r.get("caller_burst_coins", 4) if posts_coins else 1   # callers post many coins normally
        if len(others) >= limit:
            self.mark_compromised(handle, "posted several different coins within an hour")
            v.level = "block"
            v.reasons.append(f"@{handle} posted {len(others) + 1} different coins within an hour — classic hacked-account pattern")
            return v
        v.score, found = scam_score(text)
        v.reasons += [f"scam wording: {', '.join(found)}"] if found else []
        first_ever = not posts_coins and not self._history(handle)
        if first_ever:
            v.score += self.r.get("first_coin_post_points", 2)
            v.reasons.append(f"first coin @{handle} has ever posted (out of character)")
            if pool_age_minutes is not None and pool_age_minutes < self.r.get("fresh_pool_minutes", 60):
                v.score += self.r.get("fresh_pool_points", 1)
                v.reasons.append(f"coin launched {pool_age_minutes:.0f} min ago")
        if v.score >= self.r.get("block_score", 5):
            v.level = "block"
        elif v.score >= self.r.get("caution_score", 2):
            v.level = "caution"
        return v
