from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.pilots.service import PilotOperationsService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, init_db, transaction


PROTOCOL = {
    "code": "gait-assist",
    "name": "外骨骼步态体验方案",
    "capability": "gait-assist",
    "parameter_schema": {
        "minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30},
        "assist_level": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "scene": {"type": "string", "required": True, "choices": ["stairs", "flat"]},
    },
    "default_parameters": {"assist_level": 0.4},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def submit_payload(key: str, *, user: str = "pilot-operator-1", priority: int = 50) -> dict:
    return {
        "protocol_code": "gait-assist",
        "project_code": "expo-health-a",
        "requested_by": user,
        "parameters": {"minutes": 8, "scene": "stairs"},
        "priority": priority,
        "idempotency_key": key,
    }


def create_protocol(client) -> None:
    response = client.post("/api/pilots/protocols?actor=administrator", json=PROTOCOL)
    assert response.status_code == 201, response.text


SECOND_PROTOCOL = {
    "code": "gait-assist-plus",
    "name": "外骨骼步态增强方案",
    "capability": "gait-assist",
    "parameter_schema": {
        "minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30},
        "assist_level": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "scene": {"type": "string", "required": True, "choices": ["stairs", "flat"]},
    },
    "default_parameters": {"assist_level": 0.4},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def create_second_protocol(client) -> None:
    response = client.post("/api/pilots/protocols?actor=administrator", json=SECOND_PROTOCOL)
    assert response.status_code == 201, response.text


def test_same_key_in_different_projects_creates_independent_sessions(client):
    create_protocol(client)
    expo = client.post("/api/pilots/sessions", json=submit_payload("request-100", )).json()
    hospital_payload = submit_payload("request-100")
    hospital_payload["project_code"] = "hospital-ward-b"
    hospital = client.post("/api/pilots/sessions", json=hospital_payload)
    assert hospital.status_code == 202, hospital.text
    assert hospital.json()["id"] != expo["id"]
    assert hospital.json()["project_code"] == "hospital-ward-b"
    # 展会侧重放仍返回展会场次，不会被医院侧的同键请求污染。
    replay = client.post("/api/pilots/sessions", json=submit_payload("request-100"))
    assert replay.json()["id"] == expo["id"]
    hospital_replay = client.post("/api/pilots/sessions", json=hospital_payload)
    assert hospital_replay.json()["id"] == hospital.json()["id"]
    scope = hospital_replay.json()["idempotency_scope_detail"]
    assert scope == {
        "scope": "project",
        "fields": ["project_code", "requested_by", "idempotency_key"],
        "project_code": "hospital-ward-b",
        "requested_by": "pilot-operator-1",
        "idempotency_key": "request-100",
    }


def test_replay_with_inconsistent_business_meaning_is_rejected(client):
    create_protocol(client)
    create_second_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-200"))
    assert first.status_code == 202

    different_parameters = submit_payload("request-200")
    different_parameters["parameters"] = {"minutes": 20, "scene": "flat"}
    response = client.post("/api/pilots/sessions", json=different_parameters)
    assert response.status_code == 409
    assert set(response.json()["error"]["context"]["mismatched_fields"]) == {"parameters"}

    different_protocol = submit_payload("request-200")
    different_protocol["protocol_code"] = "gait-assist-plus"
    response = client.post("/api/pilots/sessions", json=different_protocol)
    assert response.status_code == 409
    assert set(response.json()["error"]["context"]["mismatched_fields"]) == {"protocol_code"}

    different_priority = submit_payload("request-200", priority=10)
    response = client.post("/api/pilots/sessions", json=different_priority)
    assert response.status_code == 409
    assert set(response.json()["error"]["context"]["mismatched_fields"]) == {"priority"}

    # 被拒绝的请求没有创建新场次，原场次参数与优先级保持不变。
    details = client.get(f"/api/pilots/session-details/{first.json()['id']}").json()
    assert details["priority"] == 50
    assert details["protocol_code"] == "gait-assist"
    assert details["idempotency_scope"] == "project"


def test_session_detail_and_audit_show_effective_idempotency_scope(client):
    create_protocol(client)
    submitted = client.post("/api/pilots/sessions", json=submit_payload("request-300")).json()
    client.post("/api/pilots/sessions", json=submit_payload("request-300"))  # 重放
    rejected = submit_payload("request-300", priority=5)
    client.post("/api/pilots/sessions", json=rejected)

    detail = client.get(f"/api/pilots/session-details/{submitted['id']}").json()
    assert detail["idempotency_scope"] == "project"
    assert detail["idempotency_scope_detail"]["project_code"] == "expo-health-a"
    assert detail["idempotency_scope_detail"]["idempotency_key"] == "request-300"

    connection = get_connection()
    actions = {
        row["action"]: row["outcome"]
        for row in connection.execute(
            "SELECT action,outcome FROM audit_events WHERE resource_type='pilot_session' AND resource_id=?",
            (submitted["id"],),
        ).fetchall()
    }
    assert actions == {"pilot_session.submit": "success", "pilot_session.replay": "success", "pilot_session.submit_rejected": "denied"}
    scoped_events = connection.execute(
        "SELECT COUNT(*) FROM audit_events WHERE resource_type='pilot_session' AND json_extract(metadata_json,'$.idempotency_scope.scope')='project'"
    ).fetchone()[0]
    assert scoped_events == 3


def test_concurrent_first_submission_persists_single_session(tmp_path):
    import os

    os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = str(tmp_path / "concurrent.db")
    close_connection()
    init_db()
    service = PilotOperationsService(get_connection())
    service.create_protocol(PROTOCOL, "administrator")
    payload = submit_payload("request-concurrent-1")
    session_ids: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        local_service = PilotOperationsService(get_connection())
        barrier.wait()
        try:
            session_ids.append(local_service.submit(payload)["id"])
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert session_ids
    assert set(session_ids) == {session_ids[0]}
    total = get_connection().execute(
        "SELECT COUNT(*) FROM pilot_sessions WHERE project_code=? AND requested_by=? AND idempotency_key=?",
        (payload["project_code"], payload["requested_by"], payload["idempotency_key"]),
    ).fetchone()[0]
    assert total == 1
    close_connection()


def test_protocol_submission_idempotency_and_parameter_validation(client):
    create_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-000001"))
    second = client.post("/api/pilots/sessions", json=submit_payload("request-000001"))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    invalid = submit_payload("request-000002")
    invalid["parameters"]["minutes"] = 50
    rejected = client.post("/api/pilots/sessions", json=invalid)
    assert rejected.status_code == 422


def test_priority_capability_claim_and_observation_version(client):
    create_protocol(client)
    low = client.post("/api/pilots/sessions", json=submit_payload("priority-low", priority=10)).json()
    high = client.post("/api/pilots/sessions", json=submit_payload("priority-high", priority=90)).json()
    no_match = client.post("/api/pilots/sessions/claim", json={"site_code": "w0", "capabilities": ["other"], "lease_seconds": 60})
    assert no_match.status_code == 200 and no_match.json()["session"] is None
    claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "w1", "capabilities": ["gait-assist"], "lease_seconds": 60})
    assert claimed.status_code == 200
    assert claimed.json()["session"]["id"] == high["id"]
    completed = client.post(
        f"/api/pilots/sessions/{high['id']}/complete",
        json={"site_code": "w1", "observation": {"value": 3.14}, "metrics": {"seconds": 2}},
    )
    assert completed.status_code == 200
    details = client.get(f"/api/pilots/session-details/{high['id']}").json()
    assert details["status"] == "succeeded"
    assert details["current_observation_version"] == 1
    assert len(details["observations"]) == 1
    assert low["status"] == "queued"


def test_quota_cancel_retry_priority_and_batch_interventions(client):
    create_protocol(client)
    quota = client.put(
        "/api/pilots/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": "limited", "max_queued": 1, "max_running": 1, "daily_submissions": 2},
    )
    assert quota.status_code == 200
    one = client.post("/api/pilots/sessions", json=submit_payload("quota-one", user="limited")).json()
    blocked = client.post("/api/pilots/sessions", json=submit_payload("quota-two", user="limited"))
    assert blocked.status_code == 409
    cancelled = client.post(f"/api/pilots/sessions/{one['id']}/cancel", json={"actor": "administrator", "reason": "项目暂停"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/pilots/sessions/{one['id']}/retry", json={"actor": "administrator", "reason": "项目恢复", "priority": 95})
    assert retried.status_code == 200 and retried.json()["priority"] == 95
    other = client.post("/api/pilots/sessions", json=submit_payload("batch-other", user="other-user")).json()
    batch = client.post(
        "/api/pilots/sessions/batch",
        json={"session_ids": [one["id"], other["id"]], "operation": "priority", "actor": "administrator", "reason": "临床合作方临时到场", "priority": 99},
    )
    assert batch.status_code == 200
    assert len(batch.json()["succeeded"]) == 2
    details = client.get(f"/api/pilots/session-details/{one['id']}").json()
    assert [item["action"] for item in details["interventions"]] == ["cancel", "retry", "priority"]


def test_failure_backoff_and_expired_lease_recovery(client):
    from app.database import init_db

    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service = PilotOperationsService(get_connection(), clock)
    service.create_protocol(PROTOCOL, "administrator")
    first = service.submit(submit_payload("failure-000001"))
    claimed = service.claim("site-a", ["gait-assist"], 10)
    assert claimed and claimed["id"] == first["id"]
    failed = service.fail(first["id"], "site-a", "sensor_unstable", "步态传感器读数不稳定", True)
    assert failed["status"] == "queued"
    assert failed["available_at"] > failed["updated_at"]
    clock.advance(seconds=2)
    claimed_again = service.claim("site-a", ["gait-assist"], 10)
    assert claimed_again and claimed_again["attempt_count"] == 2
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["exhausted"] == [first["id"]]
    details = service.get_session(first["id"])
    assert details["status"] == "failed"
    assert details["interventions"][-1]["action"] == "lease_recovery"

