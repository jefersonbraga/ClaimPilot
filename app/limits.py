"""Abuse and spend protection for a publicly reachable demo.

If ClaimPilot is deployed with a real LLM key, anyone who can reach POST /claims/analyze spends that key's budget.
Three cheap, dependency-free guards sit in front of the workflow:

  * per-client rate limit        (sliding window, requests per minute)
  * global daily analysis cap    (all clients combined)
  * global daily LLM spend cap   (only counted for real providers; uses the recorded estimated cost)

State is in-process memory, which is correct for the single-container MVP. Multiple replicas would need a shared
store (e.g. Redis) — and the provider-side spending limit remains the real backstop either way.
"""

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import HTTPException


class UsageGuard:
    def __init__(self, *, per_minute: int, daily_analyses: int, daily_budget_usd: float, counts_spend: bool):
        self.per_minute = per_minute
        self.daily_analyses = daily_analyses
        self.daily_budget_usd = daily_budget_usd
        self.counts_spend = counts_spend
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day = self._today()
        self._analyses = 0
        self._spent = 0.0

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _roll_day(self) -> None:
        if (today := self._today()) != self._day:
            self._day, self._analyses, self._spent = today, 0, 0.0

    def check(self, client: str) -> None:
        """Raise 429 before any work (or LLM spend) happens."""
        with self._lock:
            self._roll_day()
            if self._analyses >= self.daily_analyses:
                raise HTTPException(429, "Daily demo limit reached for this deployment. Try again tomorrow (UTC).")
            if self.counts_spend and self._spent >= self.daily_budget_usd:
                raise HTTPException(429, "Daily LLM budget for this demo deployment is exhausted. Try again tomorrow (UTC).")
            now = time.monotonic()
            window = self._hits[client]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                raise HTTPException(429, f"Rate limit: at most {self.per_minute} analyses per minute. Please wait a moment.",
                                    headers={"Retry-After": "60"})
            window.append(now)
            self._analyses += 1

    def record(self, cost_usd: float) -> None:
        with self._lock:
            self._roll_day()
            if self.counts_spend:
                self._spent += cost_usd

    def status(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                "per_minute_per_client": self.per_minute,
                "daily_analyses": {"used": self._analyses, "limit": self.daily_analyses},
                "daily_llm_budget_usd": {"spent": round(self._spent, 4), "limit": self.daily_budget_usd}
                if self.counts_spend else "not applicable (mock provider)",
            }
