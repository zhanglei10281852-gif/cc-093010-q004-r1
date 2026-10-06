from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime

from app.pilots.service import PilotOperationsService
from app.core.clock import FrozenClock
from app.database import PROJECT_SCOPE, get_connection, init_db, session_request_digest


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


def submit_payload(key: str, *, user: str = "pilot-operator-1", project: str = "expo-health-a", priority: int = 50) -> dict:
    return {
        "protocol_code": "gait-assist",
        "project_code": project,
        "requested_by": user,
        "parameters": {"minutes": 8, "scene": "stairs"},
        "priority": priority,
        "idempotency_key": key,
    }


def create_protocol(client) -> None:
    response = client.post("/api/pilots/protocols?actor=administrator", json=PROTOCOL)
    assert response.status_code == 201, response.text


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



def test_same_key_in_different_projects_creates_independent_sessions(client):
    create_protocol(client)
    expo = client.post("/api/pilots/sessions", json=submit_payload("request-001", project="expo-public"))
    clinical = client.post("/api/pilots/sessions", json=submit_payload("request-001", project="hospital-clinical"))
    assert expo.status_code == clinical.status_code == 202
    assert expo.json()["id"] != clinical.json()["id"]
    assert expo.json()["project_code"] == "expo-public"
    assert clinical.json()["project_code"] == "hospital-clinical"
    for body in (expo.json(), clinical.json()):
        assert body["idempotency_scope"] == PROJECT_SCOPE
        assert body["idempotency_key"] == "request-001"


def test_same_key_for_different_submitters_splits_sessions(client):
    create_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-007", user="operator-expo"))
    second = client.post("/api/pilots/sessions", json=submit_payload("request-007", user="operator-hospital"))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


def test_replay_within_same_project_returns_original_session(client):
    create_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-010", project="p-1"))
    second = client.post("/api/pilots/sessions", json=submit_payload("request-010", project="p-1"))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    listing = client.get("/api/pilots/sessions?project_code=p-1").json()["items"]
    assert len(listing) == 1


def test_different_submitter_plus_project_replay_conflict_surface(client):
    create_protocol(client)
    client.post("/api/pilots/sessions", json=submit_payload("request-020", project="p-1"))
    # 跨项目同键不冲突，但跨项目重放一个已存在于另一项目的请求也应独立
    other = client.post("/api/pilots/sessions", json=submit_payload("request-020", project="p-2"))
    assert other.status_code == 202
    assert other.json()["id"] != client.get("/api/pilots/sessions?project_code=p-1").json()["items"][0]["id"]


def test_replay_with_different_parameters_is_rejected(client):
    create_protocol(client)
    first = client.post("/api/pilots/sessions", json=submit_payload("request-100", project="p-1"))
    assert first.status_code == 202
    changed = submit_payload("request-100", project="p-1")
    changed["parameters"] = {"minutes": 12, "scene": "stairs"}
    conflict = client.post("/api/pilots/sessions", json=changed)
    assert conflict.status_code == 409
    detail = conflict.json()["error"]
    assert detail["context"]["idempotency_scope"] == PROJECT_SCOPE
    assert detail["context"]["differences"] == ["parameters"]
    # 原场次未被静默改写
    original = client.get(f"/api/pilots/session-details/{first.json()['id']}").json()
    assert original["parameters_json"] == first.json()["parameters_json"]


def test_replay_with_different_priority_is_rejected(client):
    create_protocol(client)
    client.post("/api/pilots/sessions", json=submit_payload("request-101", project="p-1", priority=50))
    changed = submit_payload("request-101", project="p-1", priority=80)
    conflict = client.post("/api/pilots/sessions", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["differences"] == ["priority"]


def test_replay_with_different_protocol_is_rejected(client):
    create_protocol(client)
    second_protocol = {**PROTOCOL, "code": "gait-assist-pro", "capability": "gait-assist"}
    response = client.post("/api/pilots/protocols?actor=administrator", json=second_protocol)
    assert response.status_code == 201
    client.post("/api/pilots/sessions", json=submit_payload("request-102", project="p-1"))
    changed = submit_payload("request-102", project="p-1")
    changed["protocol_code"] = "gait-assist-pro"
    conflict = client.post("/api/pilots/sessions", json=changed)
    assert conflict.status_code == 409
    assert "protocol_code" in conflict.json()["error"]["context"]["differences"]


def test_session_details_expose_idempotency_scope(client):
    create_protocol(client)
    submitted = client.post("/api/pilots/sessions", json=submit_payload("request-200", project="p-9"))
    details = client.get(f"/api/pilots/session-details/{submitted.json()['id']}").json()
    assert details["idempotency_scope"] == PROJECT_SCOPE
    assert details["project_code"] == "p-9"
    assert details["request_digest"] == submitted.json()["request_digest"]


def test_concurrent_first_submissions_create_single_session(client):
    create_protocol(client)
    barrier = threading.Barrier(3)
    results: list[dict] = []
    errors: list[Exception] = []

    def submit() -> None:
        try:
            service = PilotOperationsService(get_connection())
            barrier.wait(timeout=10)
            results.append(service.submit(submit_payload("request-concurrent-1", project="p-c")))
        except Exception as exc:  # noqa: BLE001 - 测试需要收集任意失败
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors, errors
    assert len({row["id"] for row in results}) == 1
    stored = get_connection().execute("SELECT COUNT(*) AS amount FROM pilot_sessions WHERE idempotency_key='request-concurrent-1'").fetchone()
    assert stored["amount"] == 1


def _create_legacy_v2_database(path) -> None:
    """构造 user_version=2 的旧库：场次表内嵌旧约束 UNIQUE(requested_by,idempotency_key)。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=2")
    connection.executescript(
        """
        CREATE TABLE pilot_protocols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            capability TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            parameter_schema_json TEXT NOT NULL,
            default_parameters_json TEXT NOT NULL DEFAULT '{}',
            max_runtime_seconds INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE pilot_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id),
            project_code TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            parameter_digest TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            current_observation_version INTEGER,
            last_error_code TEXT NOT NULL DEFAULT '',
            last_error_message TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(requested_by, idempotency_key)
        );
        CREATE TABLE pilot_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            observation_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            observation_digest TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, version)
        );
        CREATE TABLE pilot_interventions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            batch_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    now = "2026-09-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO pilot_protocols(id,code,name,capability,parameter_schema_json,default_parameters_json,"
        "max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(1,?,?,?,?,?,?,?,1,?,?,?)",
        (
            PROTOCOL["code"], PROTOCOL["name"], PROTOCOL["capability"],
            '{"minutes":{"type":"integer"},"assist_level":{"type":"number"},"scene":{"type":"string"}}',
            '{"assist_level":0.4}', PROTOCOL["max_runtime_seconds"], PROTOCOL["max_attempts"],
            "administrator", now, now,
        ),
    )
    connection.execute(
        "INSERT INTO pilot_sessions(id,protocol_id,project_code,requested_by,parameters_json,parameter_digest,"
        "priority,idempotency_key,status,max_attempts,available_at,created_at,updated_at) "
        "VALUES(1,1,'expo-public','shared-operator',?,?,50,'request-001','queued',2,?,?,?)",
        ('{"assist_level":0.4,"minutes":8,"scene":"stairs"}', "legacy-digest", now, now, now),
    )
    connection.execute(
        "INSERT INTO pilot_observations(session_id,version,observation_json,observation_digest,created_by,created_at) "
        "VALUES(1,1,'{\"ok\":true}','obs-digest','site-a',?)",
        (now,),
    )
    connection.commit()
    connection.close()


def test_legacy_v2_database_upgrades_and_keeps_history_queryable(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _create_legacy_v2_database(db_path)
    monkeypatch.setenv("HEALTH_INNOVATION_DATABASE_PATH", str(db_path))
    from app.database import close_connection
    close_connection()
    init_db()
    try:
        connection = get_connection()
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        service = PilotOperationsService(connection)
        details = service.get_session(1)
        assert details["project_code"] == "expo-public"
        assert details["idempotency_scope"] == PROJECT_SCOPE
        assert details["request_digest"] == session_request_digest("gait-assist", {"assist_level": 0.4, "minutes": 8, "scene": "stairs"}, 50)
        assert len(details["observations"]) == 1
        # 同一作用域内用原请求重放，返回历史场次
        replay = service.submit(submit_payload("request-001", user="shared-operator", project="expo-public"))
        assert replay["id"] == 1
        # 升级后跨项目复用相同键，创建独立记录
        clinical = service.submit(submit_payload("request-001", user="shared-operator", project="hospital-clinical"))
        assert clinical["id"] == 2
        # 迁移可重复执行
        init_db()
        assert int(get_connection().execute("SELECT COUNT(*) FROM pilot_sessions").fetchone()[0]) == 2
    finally:
        close_connection()


def test_submit_audit_events_record_effective_idempotency_scope(client, admin):
    create_protocol(client)
    client.post("/api/pilots/sessions", json=submit_payload("request-300", project="p-1"))
    client.post("/api/pilots/sessions", json=submit_payload("request-300", project="p-1"))
    changed = submit_payload("request-300", project="p-1", priority=90)
    rejected = client.post("/api/pilots/sessions", json=changed)
    assert rejected.status_code == 409
    events = client.get("/api/audit?action=pilot_session.submit&size=20", headers=admin["headers"]).json()["data"]
    results = [json.loads(event["metadata_json"])["result"] for event in events]
    assert results == ["rejected_conflict", "replayed", "created"]
    for event in events:
        metadata = json.loads(event["metadata_json"])
        assert metadata["idempotency_scope"] == PROJECT_SCOPE
        assert metadata["project_code"] == "p-1"
