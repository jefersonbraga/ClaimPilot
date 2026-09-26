"""First-party, anonymous usage analytics.

No third-party scripts, no IP addresses, no personal data: visitors are counted by the same hashed anonymous cookie
that scopes execution history. Only the referrer's host is kept (e.g. "linkedin.com"), never the full URL.
Automated traffic (crawlers, link-preview bots, scripts) is recorded separately so it never inflates visitor counts,
which also reveals when the demo link was shared (e.g. a LinkedIn preview fetch).
"""

import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

RETENTION_DAYS = 400
_BOT = re.compile(
    r"(linkedinbot|slackbot|twitterbot|facebookexternalhit|discordbot|telegrambot|whatsapp|googlebot|bingbot|"
    r"applebot|duckduckbot|yandex|baiduspider|bot\b|bot/|crawl|spider|slurp|preview|embedly|headless|lighthouse|"
    r"curl|wget|python|httpx|go-http|java/|okhttp|uptime|monitor)",
    re.IGNORECASE,
)


def classify_agent(user_agent: str | None) -> str | None:
    """Returns the matched automation marker (e.g. 'linkedinbot'), or None for a regular browser."""
    if not user_agent:
        return "no-user-agent"
    m = _BOT.search(user_agent)
    return m.group(1).lower() if m else None


def referrer_host(referer: str | None, own_host: str) -> str | None:
    if not referer:
        return None
    host = (urlsplit(referer).hostname or "").lower().removeprefix("www.")
    return None if not host or host == own_host.lower().removeprefix("www.") else host


class Analytics:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_events (
                   ts TEXT NOT NULL, day TEXT NOT NULL, visitor TEXT NOT NULL,
                   kind TEXT NOT NULL, detail TEXT, referrer TEXT)"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_events(day, kind)")
        self._conn.commit()

    def record(self, kind: str, visitor: str, detail: str | None = None, referrer: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), now.date().isoformat(), visitor, kind,
                 (detail or "")[:80] or None, referrer),
            )
            self._conn.commit()

    def summary(self, days: int = 30) -> dict:
        since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        q = lambda sql, *a: self._conn.execute(sql, (since, *a)).fetchall()  # noqa: E731
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).date().isoformat()
            self._conn.execute("DELETE FROM usage_events WHERE day < ?", (cutoff,))
            human = "kind != 'automated'"
            totals = dict(q(f"SELECT kind, COUNT(*) FROM usage_events WHERE day >= ? AND {human} GROUP BY kind"))
            unique = q(f"SELECT COUNT(DISTINCT visitor) FROM usage_events WHERE day >= ? AND {human}")[0][0]
            engaged = q("SELECT COUNT(DISTINCT visitor) FROM usage_events WHERE day >= ? AND kind = 'analysis'")[0][0]
            daily = q(f"""SELECT day, COUNT(DISTINCT visitor), SUM(kind = 'page_view'), SUM(kind = 'analysis')
                          FROM usage_events WHERE day >= ? AND {human} GROUP BY day ORDER BY day DESC""")
            referrers = q(f"""SELECT referrer, COUNT(DISTINCT visitor) FROM usage_events
                              WHERE day >= ? AND {human} AND referrer IS NOT NULL
                              GROUP BY referrer ORDER BY 2 DESC LIMIT 15""")
            by_detail = lambda kind: q(  # noqa: E731
                "SELECT detail, COUNT(*) FROM usage_events WHERE day >= ? AND kind = ? GROUP BY detail ORDER BY 2 DESC LIMIT 15",
                kind)
            recent = self._conn.execute(
                "SELECT ts, kind, detail, referrer, visitor FROM usage_events ORDER BY ts DESC LIMIT 25").fetchall()
        return {
            "window_days": days,
            "unique_visitors": unique,
            "visitors_who_ran_an_analysis": engaged,
            "page_views": totals.get("page_view", 0),
            "analyses": totals.get("analysis", 0),
            "shared_link_opens": totals.get("shared_link_open", 0),
            "replay_views": totals.get("replay_view", 0),
            "daily": [{"day": d, "visitors": v, "page_views": p or 0, "analyses": a or 0} for d, v, p, a in daily],
            "referrers": [{"host": h, "visitors": n} for h, n in referrers],
            "analyses_by_model": [{"model": d, "count": n} for d, n in by_detail("analysis")],
            "automated_traffic": [{"agent": d, "hits": n} for d, n in by_detail("automated")],
            "recent": [{"ts": t, "kind": k, "detail": d, "referrer": r, "visitor": v[:8]} for t, k, d, r, v in recent],
        }
