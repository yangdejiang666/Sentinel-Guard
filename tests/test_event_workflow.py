from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backend.app.db import Database
from backend.app.models import EventRecord


def seed(client, event_id="workflow-event", **changes):
    record = EventRecord(event_id=event_id, site_id="test-site", source_mode="live",
                         event_type="anomaly", title="现场异常", message="需人工复核",
                         **changes).model_dump(mode="json")
    client.app.state.hub.db.insert_safety_event(record)
    return record


def action(client, headers, operation, revision, actor="handler", **changes):
    body = dict(action=operation, actor=actor, note="现场处理记录",
                evidence_ref="", expected_revision=revision, request_id=f"{operation}-{revision}")
    body.update(changes)
    return client.post("/api/v1/events/workflow-event/actions", json=body, headers=headers)


def test_workflow_review_idempotence_and_no_actuation(live_client, control_headers):
    original = seed(live_client)
    assert action(live_client, {}, "claim", 0).status_code == 401
    assert action(live_client, control_headers, "close", 0).status_code == 409
    claimed = action(live_client, control_headers, "claim", 0)
    assert claimed.status_code == 200
    assert claimed.json()["handling_status"] == "in_progress"
    assert action(live_client, control_headers, "claim", 0).json() == claimed.json()
    assert action(live_client, control_headers, "claim", 0, note="different").status_code == 409
    assert action(live_client, control_headers, "note", 0).status_code == 409
    assert action(live_client, control_headers, "submit_review", 1, actor="other").status_code == 409
    assert action(live_client, control_headers, "submit_review", 1).status_code == 200
    assert action(live_client, control_headers, "close", 2, evidence_ref="field-1").status_code == 409
    assert action(live_client, control_headers, "close", 2, actor="reviewer").status_code == 409
    closed = action(live_client, control_headers, "close", 2, actor="reviewer", evidence_ref="field-1")
    assert closed.json()["handling_status"] == "closed"
    assert len(closed.json()["handling_history"]) == 3
    ack = live_client.post("/api/v1/events/workflow-event/acknowledge", json={"acknowledged_by": "reader"}, headers=control_headers)
    assert ack.json()["revision"] == 3 and ack.json()["handling_status"] == "closed"
    retry, inserted = live_client.app.state.hub.db.insert_safety_event(original)
    assert not inserted and retry["acknowledged"] and retry["revision"] == 3
    reopened = action(live_client, control_headers, "reopen", 3)
    assert reopened.json()["handling_status"] == "open" and reopened.json()["assignee"] is None
    assert live_client.get("/api/v1/commands").json() == []


def test_action_scope_and_contract(live_client, control_headers):
    seed(live_client)
    assert action(live_client, control_headers, "claim", 0, actor=" ").status_code == 422
    assert action(live_client, control_headers, "claim", 0, note=" ").status_code == 422
    record = EventRecord(event_id="foreign", site_id="other", event_type="anomaly", title="x", message="x")
    live_client.app.state.hub.db.insert_safety_event(record.model_dump(mode="json"))
    response = live_client.post("/api/v1/events/foreign/actions", headers=control_headers,
                               json=dict(action="claim", actor="a", note="n", expected_revision=0, request_id="r"))
    assert response.status_code == 404


def test_full_history_search_pagination_scope_and_filters(live_client):
    for index in range(125):
        seed(live_client, f"history-{index:03d}", severity="critical" if index == 0 else "info",
             occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    other = EventRecord(event_id="other-site", site_id="other", event_type="anomaly", title="现场异常", message="x")
    live_client.app.state.hub.db.insert_safety_event(other.model_dump(mode="json"))
    first = live_client.get("/api/v1/events/search").json()
    assert first["total"] == 125 and len(first["items"]) == 20
    second = live_client.get("/api/v1/events/search?page=2").json()
    assert not set(x["event_id"] for x in first["items"]) & set(x["event_id"] for x in second["items"])
    assert live_client.get("/api/v1/events/search?page=999").json()["page"] == 7
    assert live_client.get("/api/v1/events/search?q=history-000").json()["items"][0]["event_id"] == "history-000"
    assert live_client.get("/api/v1/events/search?sort=priority").json()["items"][0]["severity"] == "critical"
    assert live_client.get("/api/v1/events/search?severity=critical").json()["total"] == 1
    assert live_client.get("/api/v1/events/search?handling=closed").json()["total"] == 0
    assert live_client.get("/api/v1/events/search?q=%").json()["total"] == 0
    assert live_client.get("/api/v1/events/search?severity=invalid").status_code == 422
    assert live_client.get("/api/v1/events/search?page_size=101").status_code == 422


def test_concurrent_claims_use_optimistic_revision(live_client):
    seed(live_client)
    stores = [Database(str(live_client.app.state.hub.db.path)) for _ in range(2)]
    def claim(index):
        try:
            return stores[index].act_on_event("workflow-event", dict(
                action="claim", actor=f"user-{index}", note="claim", evidence_ref="",
                expected_revision=0, request_id=f"claim-{index}"), "test-site", "live")
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(stores[0].get("events", "workflow-event")["handling_history"]) == 1
