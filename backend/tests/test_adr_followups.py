"""Regression tests for the follow-up improvements ADR (PR #11):
Decision 2 (environment-configured CORS origins) and Decision 4
(seen row recorded on the add-from-link path)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, cors_origin_list, settings
from app.db import SessionLocal
from app.models import Application, JobMatch, Run, SeenJob


# ---------------------------------------------------------------------------
# Decision 2: CORS origins come from the CORS_ORIGINS setting.
# ---------------------------------------------------------------------------


def test_cors_setting_defaults_to_localhost():
    assert Settings.model_fields["cors_origins"].default == (
        "http://localhost:5173,http://127.0.0.1:5173"
    )


def test_cors_origin_list_parses_configured_value(monkeypatch):
    monkeypatch.setattr(
        settings, "cors_origins", " https://jobapplylive.com ,https://staging.example.com ,,"
    )
    assert cors_origin_list() == ["https://jobapplylive.com", "https://staging.example.com"]


def test_cors_headers_use_the_default_allowlist():
    client = TestClient(main.app)
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    blocked = client.get("/api/health", headers={"Origin": "https://unconfigured.example"})
    assert "access-control-allow-origin" not in blocked.headers
