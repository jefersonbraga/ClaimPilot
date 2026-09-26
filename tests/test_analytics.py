"""First-party, anonymous usage analytics and the private admin dashboard."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.limits import UsageGuard
from app.main import app, get_analytics, get_audit_store, get_investigator, get_usage_guard
from app.observability.analytics import Analytics

CLAIM = json.loads((DATA_DIR / "claims" / "01_obvious_approval.json").read_text())
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"}
TOKEN = "t" * 40


@pytest.fixture
def analytics():
    return Analytics(":memory:")


@pytest.fixture
def client(make_investigator, audit, analytics, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    app.dependency_overrides[get_analytics] = lambda: analytics
    app.dependency_overrides[get_investigator] = lambda: make_investigator()
    app.dependency_overrides[get_audit_store] = lambda: audit
    app.dependency_overrides[get_usage_guard] = lambda: UsageGuard(per_minute=1000, daily_analyses=1000,
                                                                   daily_budget_usd=100, counts_spend=False)
    yield TestClient(app, base_url="https://claimpilot.example", headers=BROWSER)
    app.dependency_overrides.clear()


def stats(client, **kw):
    return client.get("/admin/stats", headers={"Authorization": f"Bearer {TOKEN}"}, **kw).json()


def test_visitors_views_analyses_and_sources_are_counted(client):
    client.get("/", headers={"Referer": "https://www.linkedin.com/feed/update/urn:li:activity:123?x=secret"})
    client.get("/")                                                   # same visitor, second view
    run = client.post("/claims/analyze", json=CLAIM).json()
    client.get(f"/executions/{run['execution_id']}")
    other = TestClient(app, base_url="https://claimpilot.example", headers=BROWSER)
    other.get(f"/?execution={run['execution_id']}")                    # someone opens a shared decision

    s = stats(client)
    assert s["unique_visitors"] == 2 and s["visitors_who_ran_an_analysis"] == 1
    assert (s["page_views"], s["analyses"], s["shared_link_opens"], s["replay_views"]) == (2, 1, 1, 1)
    assert s["referrers"] == [{"host": "linkedin.com", "visitors": 1}]   # host only: no path, no query
    assert s["analyses_by_model"] == [{"model": "mock · mock-analyst-v1", "count": 1}]


def test_bots_and_link_previews_never_count_as_visitors(client):
    client.get("/", headers={"User-Agent": "LinkedInBot/1.0 (compatible; Mozilla/5.0)"})
    client.get("/", headers={"User-Agent": "curl/8.7.1"})
    s = stats(client)
    assert s["unique_visitors"] == 0 and s["page_views"] == 0
    assert {b["agent"] for b in s["automated_traffic"]} == {"linkedinbot", "curl"}


def test_owner_can_stop_counting_their_own_browser(client):
    assert client.post("/admin/notrack", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    client.get("/")
    assert stats(client)["page_views"] == 0
    client.delete("/admin/notrack", headers={"Authorization": f"Bearer {TOKEN}"})
    client.get("/")
    assert stats(client)["page_views"] == 1


def test_admin_is_hidden_without_a_strong_token(client, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN")
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/stats", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", "short")
    assert client.get("/admin/stats", headers={"Authorization": "Bearer short"}).status_code == 404


def test_admin_requires_the_bearer_token(client):
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"Authorization": "Bearer wrong-token-wrong-token-xx"}).status_code == 401
    assert client.get(f"/admin/stats?token={TOKEN}").status_code == 401        # never via the URL
    page = client.get("/admin")
    assert page.status_code == 200 and page.headers["x-robots-tag"] == "noindex, nofollow"
    assert "no-store" in client.get("/admin/stats", headers={"Authorization": f"Bearer {TOKEN}"}).headers["cache-control"]
