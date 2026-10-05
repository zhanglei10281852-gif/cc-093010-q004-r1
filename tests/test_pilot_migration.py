from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.database import close_connection, init_db, get_connection
from app.pilots.service import PilotOperationsService


LEGACY_DDL = """
CREATE TABLE pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, capability TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, parameter_schema_json TEXT NOT NULL, default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL, max_attempts INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL, requested_by TEXT NOT NULL, parameters_json TEXT NOT NULL, parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50, idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL, lease_owner TEXT NOT NULL DEFAULT '', lease_expires_at TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER, last_error_code TEXT NOT NULL DEFAULT '', last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at);
CREATE TABLE pilot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, observation_json TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, version)
);
CREATE TABLE pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX idx_pilot_interventions ON pilot_interventions(session_id,id);
"""


def _build_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(LEGACY_DDL)
    connection.execute(
        "INSERT INTO pilot_protocols VALUES(1,'gait-assist','方案','gait-assist',1,'{}','{}',300,2,1,'admin','t','t')"
    )
    connection.execute(
        "INSERT INTO pilot_sessions(id,protocol_id,project_code,requested_by,parameters_json,parameter_digest,"
        "idempotency_key,status,max_attempts,available_at,created_at,updated_at) "
        "VALUES(1,1,'expo-legacy','operator-a','{}','digest-a','request-001','succeeded',2,'t','t','t')"
    )
    connection.execute(
        "INSERT INTO pilot_observations VALUES(1,1,1,'{}','{}','obs-digest','site-a','t')"
    )
    connection.execute(
        "INSERT INTO pilot_interventions VALUES(1,1,'admin','cancel','项目暂停','{}','{}','','t')"
    )
    connection.commit()
    connection.close()


def test_legacy_database_migrates_scope_and_keeps_history_queryable(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_database(db_path)
    os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = str(db_path)
    close_connection()

    init_db()
    connection = get_connection()
    assert connection.execute("PRAGMA user_version").fetchone()[0] >= 3
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(pilot_sessions)").fetchall()}
    assert "idempotency_scope" in columns
    # 唯一约束已升级为项目作用域三元组。
    unique_column_groups = []
    for index in connection.execute("PRAGMA index_list(pilot_sessions)").fetchall():
        if index["unique"] == 1:
            columns_in_index = tuple(
                row["name"] for row in connection.execute(f"PRAGMA index_info({index['name']})").fetchall()
            )
            if columns_in_index:
                unique_column_groups.append(columns_in_index)
    assert ("project_code", "requested_by", "idempotency_key") in unique_column_groups

    # 历史场次、观察版本和干预记录全部保留，id 不变。
    session = connection.execute("SELECT * FROM pilot_sessions WHERE id=1").fetchone()
    assert session["project_code"] == "expo-legacy"
    assert session["idempotency_scope"] == "project"
    assert connection.execute("SELECT COUNT(*) FROM pilot_observations WHERE session_id=1").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM pilot_interventions WHERE session_id=1").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # 升级后历史场次仍可通过详情接口查询，并展示实际作用域。
    detail = PilotOperationsService(connection).get_session(1)
    assert detail["status"] == "succeeded"
    assert len(detail["observations"]) == 1
    assert len(detail["interventions"]) == 1
    assert detail["idempotency_scope_detail"]["scope"] == "project"
    assert detail["idempotency_scope_detail"]["project_code"] == "expo-legacy"

    # 升级后跨项目复用历史键不再串单：同名键在医院项目创建独立场次。
    service = PilotOperationsService(connection)
    hospital = service.submit({
        "protocol_code": "gait-assist",
        "project_code": "hospital-ward-a",
        "requested_by": "operator-a",
        "parameters": {},
        "priority": 50,
        "idempotency_key": "request-001",
    })
    assert hospital["id"] != 1
    assert hospital["project_code"] == "hospital-ward-a"
    replay = service.submit({
        "protocol_code": "gait-assist",
        "project_code": "hospital-ward-a",
        "requested_by": "operator-a",
        "parameters": {},
        "priority": 50,
        "idempotency_key": "request-001",
    })
    assert replay["id"] == hospital["id"]

    close_connection()
