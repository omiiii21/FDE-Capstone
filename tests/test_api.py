"""The API surface, including the kill switch.

Skipped where FastAPI is not installed, so the suite still runs on a bare
interpreter. It is in requirements.txt and CI installs it, so these do run where
it matters. They were written after a traceability review pointed out that the
admin endpoints, which are the mechanism the governance framework names as the
kill switch, were exercised by hand and nowhere else.

Covers FR-16 at the surface level, and A11 for the health endpoint, which has to
answer even when the model provider does not.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is optional for the core system")
pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-admin-token"

TICKET = {
    "ticket_id": "API-1",
    "channel": "chat",
    "subject": "",
    "body": "We shipped a release this morning and need to get back to the previous revision.",
    "customer_tier": "business",
}


@pytest.fixture()
def client():
    """The app as it is actually configured.

    Deliberately no reloading of src.config. Settings is read once at import and
    frozen, so reloading it produces a second object while src.route and
    src.pipeline keep the first, and a halt written through one is invisible to
    the other. conftest sets the environment before the first import instead,
    which is the only arrangement where this test means anything.
    """
    from src.config import settings
    import src.api

    settings.kill_switch_path.unlink(missing_ok=True)
    with TestClient(src.api.app) as test_client:
        yield test_client
    settings.kill_switch_path.unlink(missing_ok=True)


def test_health_answers_even_with_no_provider(client):
    # A11. A health endpoint that fails when the model provider is down reports
    # the outage by joining it.
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["classifier_loaded"] is True
    assert body["provider"]["available"] is False
    assert any("provider" in reason for reason in body["degraded_because"])


def test_a_ticket_is_processed_and_cited(client):
    response = client.post("/tickets", json=TICKET)

    assert response.status_code == 200
    body = response.json()
    assert body["action"] in {"auto_respond", "escalate", "blocked"}
    if body["action"] == "auto_respond":
        assert body["citations"]
        assert "drafted automatically" in body["body"]


def test_a_decision_can_be_reconstructed_afterwards(client):
    # A distinct id, because the decision log is append-only and shared across
    # the session. Reusing one would return every earlier run of it as well.
    client.post("/tickets", json=dict(TICKET, ticket_id="API-TRACE"))

    response = client.get("/decisions/API-TRACE")

    assert response.status_code == 200
    stages = [row["stage"] for row in response.json()["decisions"]]
    assert stages == ["classification", "retrieval", "routing", "generation", "validation"]


def test_an_unknown_ticket_is_a_404_not_an_empty_list(client):
    assert client.get("/decisions/NOT-A-TICKET").status_code == 404


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, 401), ({"X-Admin-Token": "wrong"}, 401), ({"X-Admin-Token": TOKEN}, 200)],
)
def test_the_admin_endpoints_check_the_token(client, headers, expected):
    assert client.post("/admin/halt", headers=headers).status_code == expected


def test_the_kill_switch_stops_automation_and_resumes_it(client):
    # FR-16. The whole point is that this needs no deployment, so the check is
    # that the very next ticket routes differently.
    before = client.post("/tickets", json=dict(TICKET, ticket_id="API-BEFORE")).json()

    halted = client.post("/admin/halt", headers={"X-Admin-Token": TOKEN})
    assert halted.status_code == 200

    during = client.post("/tickets", json=dict(TICKET, ticket_id="API-DURING")).json()
    assert during["action"] == "escalate"
    assert during["routing"]["rule"] == "R-00-kill-switch"
    assert "switched off" in during["routing"]["reason"]

    health = client.get("/healthz").json()
    assert health["kill_switch"]["engaged"] is True

    client.post("/admin/resume", headers={"X-Admin-Token": TOKEN})
    after = client.post("/tickets", json=dict(TICKET, ticket_id="API-AFTER")).json()
    assert after["routing"]["rule"] == before["routing"]["rule"]


def test_a_malformed_ticket_does_not_return_a_500(client):
    # Ingest repairs rather than rejects, so an empty body is a ticket that
    # escalates, not an error.
    response = client.post("/tickets", json={"ticket_id": "API-EMPTY", "channel": "chat", "body": ""})

    assert response.status_code in {200, 422}
    if response.status_code == 200:
        assert response.json()["action"] == "escalate"
