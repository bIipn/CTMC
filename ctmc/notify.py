"""Phone alerts + remote control.

Notifier         queues events and sends them in the background (never inside the buy path).
                 Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) and/or a Discord webhook
                 (DISCORD_WEBHOOK_URL). Duplicates are suppressed for a minute; CRITICAL alerts
                 (rug alerts, pauses, kill switch) are never suppressed.        (DSA / freqtrade / jesse)
TelegramControl  commands from YOUR chat only:
                 /status /profit /pause /resume /sellall /sell SYMBOL /help    (freqtrade RPC)
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

ICON = {"BUY": "🟢", "SELL": "🟣", "HALT": "⛔", "TWEET": "🐦", "KOL": "🧠", "RADAR": "📡", "SKIP": "⚪"}


def severity(ev) -> str:
    if ev.kind == "HALT" or "RUG ALERT" in ev.detail:
        return "critical"
    return "info" if ev.kind in ("BUY", "SELL") else "low"


def format_event(ev) -> str:
    icon = "🚨" if "RUG ALERT" in ev.detail else ICON.get(ev.kind, "•")
    pnl = f"  {'+' if ev.pnl_usd >= 0 else '-'}${abs(ev.pnl_usd):,.2f}" if ev.pnl_usd else ""
    return f"{icon} {ev.kind} {ev.symbol}{pnl}\n{ev.detail}"


class Notifier:
    def __init__(self, rules: dict, http: httpx.AsyncClient | None = None):
        self.r = rules
        self.http = http or httpx.AsyncClient(timeout=10)
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
        self.discord = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.kinds = set(rules.get("kinds", ["BUY", "SELL", "HALT"]))
        self.q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._recent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chat) or self.discord)

    def publish(self, ev) -> None:
        """Non-blocking. Called from Trader.log for every event."""
        if not self.enabled:
            return
        sev = severity(ev)
        if sev != "critical" and ev.kind not in self.kinds:
            return
        key = f"{ev.kind}|{ev.symbol}|{ev.detail[:40]}"
        now = time.time()
        if sev != "critical" and now - self._recent.get(key, 0) < self.r.get("dedup_seconds", 60):
            return
        self._recent[key] = now
        try:
            self.q.put_nowait(format_event(ev))
        except asyncio.QueueFull:
            pass

    async def send(self, text: str) -> None:
        if self.tg_token and self.tg_chat:
            for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
                await self._post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                 {"chat_id": self.tg_chat, "text": chunk, "disable_web_page_preview": True})
        if self.discord:
            await self._post(self.discord, {"content": text[:1900]})

    async def _post(self, url: str, payload: dict) -> None:
        for attempt in range(3):                      # retry server errors / timeouts, never 4xx
            try:
                r = await self.http.post(url, json=payload)
                if r.status_code < 500 and r.status_code != 429:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5 * 2 ** attempt)

    async def run(self) -> None:
        while True:
            text = await self.q.get()
            try:
                await self.send(text)
            except Exception:
                pass                                  # alerts must never take the bot down


class TelegramControl:
    """Long-polls Telegram for commands from the configured chat only."""

    HELP = ("/status — positions & equity\n/profit — performance summary\n/pause — stop new buys (exits keep running)\n"
            "/resume — allow buys again\n/sellall — sell every position now\n/sell SYMBOL — sell one position now")

    def __init__(self, trader, protect, reporter, http: httpx.AsyncClient | None = None):
        self.trader, self.protect, self.reporter = trader, protect, reporter
        self.http = http or httpx.AsyncClient(timeout=40)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.getenv("TELEGRAM_CHAT_ID", "")
        self.offset = 0

    async def handle(self, text: str) -> str:
        t = self.trader
        cmd, *args = (text.strip().split() or [""])
        cmd = cmd.split("@")[0].lower()
        if cmd == "/status":
            lines = [f"equity ${t.equity():,.2f} · cash ${t.broker.cash:,.2f} · "
                     f"{'PAUSED' if self.protect and self.protect.halted() else 'active'}"]
            for p in t.positions.values():
                px = t.last_px.get(p.token, p.entry_price)
                lines.append(f"{p.symbol}: {p.multiple(px):.2f}x · bag ${p.value(px):,.2f} · banked ${p.realized_usd:,.2f}")
            return "\n".join(lines) if len(lines) > 1 else lines[0] + "\nno open positions"
        if cmd == "/profit":
            return self.reporter() if self.reporter else "no journal yet"
        if cmd == "/pause" and self.protect:
            self.protect.halt("telegram /pause")
            return "⛔ paused: no new buys. Exits keep running. /resume to trade again."
        if cmd == "/resume" and self.protect:
            self.protect.resume()
            return "🟢 resumed: buys allowed again."
        if cmd == "/sellall":
            n = len(t.positions)
            for p in list(t.positions.values()):
                await t.close(p, "manual: /sellall from Telegram", urgent=True)
            return f"selling {n} position(s)."
        if cmd == "/sell" and args:
            hits = [p for p in t.positions.values() if p.symbol.lower() == args[0].lower().lstrip("$")]
            for p in hits:
                await t.close(p, "manual: /sell from Telegram", urgent=True)
            return f"selling {hits[0].symbol}." if hits else f"no open position called {args[0]}"
        return self.HELP

    async def run(self) -> None:
        if not (self.token and self.chat):
            await asyncio.Event().wait()
        base = f"https://api.telegram.org/bot{self.token}"
        while True:
            try:
                r = await self.http.get(f"{base}/getUpdates", params={"timeout": 30, "offset": self.offset})
                for u in (r.json() or {}).get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(self.chat) or not msg.get("text"):
                        continue                         # only YOUR chat can control the bot
                    reply = await self.handle(msg["text"])
                    await self.http.post(f"{base}/sendMessage", json={"chat_id": self.chat, "text": reply})
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(5)
