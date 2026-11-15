"""Tests for the digital secretary web UI (feature 014 / web_ui)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from assistant.config import load
from assistant.state import State
from assistant.web_ui import create_web_app


@pytest.fixture
def test_app(tmp_path: Path):
    db_path = tmp_path / "assistant.db"
    state = State(db_path)
    state.kv_set("model", "groq")
    state.schedule_upsert("test-routine", "daily 08:00", "briefing", True, None, "test")

    cfg = load()
    # Override state_dir for isolated testing
    cfg.assistant.state_dir = str(tmp_path)

    app = create_web_app(cfg)
    return TestClient(app)


def test_index_route(test_app: TestClient):
    res = test_app.get("/")
    assert res.status_code == 200
    assert "Digital Secretary" in res.text
    assert "Secretary Console" in res.text
    assert "Action Guardrail" in res.text


def test_api_status(test_app: TestClient):
    res = test_app.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "assistant_name" in data
    assert "timezone" in data
    assert "budget_today" in data
    assert "active_model" in data


def test_api_models_and_switch(test_app: TestClient):
    res = test_app.get("/api/models")
    assert res.status_code == 200
    data = res.json()
    assert "models" in data
    assert len(data["models"]) > 0

    # Switch model
    target = data["models"][0]["alias"]
    res_switch = test_app.post("/api/models/switch", json={"model": target})
    assert res_switch.status_code == 200
    assert res_switch.json()["active"] == target


def test_api_approvals_flow(test_app: TestClient, tmp_path: Path):
    res = test_app.get("/api/approvals")
    assert res.status_code == 200
    data = res.json()
    assert "pending" in data
    assert "history" in data


def test_api_knowledge_base(test_app: TestClient):
    res = test_app.get("/api/knowledge-base")
    assert res.status_code == 200
    assert "documents" in res.json()


def test_api_schedules_toggle(test_app: TestClient):
    res = test_app.get("/api/schedules")
    assert res.status_code == 200
    schedules = res.json().get("schedules", [])
    if schedules:
        name = schedules[0]["name"]
        t_res = test_app.post(f"/api/schedules/{name}/toggle")
        assert t_res.status_code == 200
        assert "enabled" in t_res.json()


def test_api_memory(test_app: TestClient):
    res = test_app.get("/api/memory")
    assert res.status_code == 200
    assert "facts" in res.json()


def test_api_audit(test_app: TestClient):
    res = test_app.get("/api/audit")
    assert res.status_code == 200
    assert "records" in res.json()
