"""Tests for agents.config validation."""

import os
from unittest.mock import patch

from agents import config


def test_validate_catches_missing_target():
    with patch.object(config, "TARGET_HOST", ""):
        problems = config.validate()
    assert any("target host" in p.lower() for p in problems)


def test_validate_catches_missing_settings():
    with patch.object(config, "SETTINGS_FILE", ""):
        problems = config.validate()
    assert any("settings file" in p.lower() for p in problems)


def test_validate_catches_missing_vertex_project():
    with patch.object(config, "VERTEX_PROJECT", ""):
        problems = config.validate()
    assert any("ANTHROPIC_VERTEX_PROJECT_ID" in p for p in problems)


def test_validate_catches_missing_model():
    with patch.object(config, "LLM_MODEL", ""):
        problems = config.validate()
    assert any("model" in p.lower() for p in problems)


def test_validate_catches_missing_pubsub_project():
    with patch.object(config, "GCP_PUBSUB_PROJECT_ID", ""):
        problems = config.validate()
    assert any("GCP_PUBSUB_PROJECT_ID" in p for p in problems)


def test_validate_catches_missing_sa_key_file(tmp_path):
    with patch.dict(os.environ, {"PUBSUB_SA_KEY_PATH": str(tmp_path / "nonexistent.json")}, clear=False):
        problems = config._validate_common()
    assert any("does not exist" in p for p in problems)


def test_validate_catches_no_sa_key_env():
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(config, "GCP_PUBSUB_PROJECT_ID", "test-project"):
            problems = config._validate_common()
    assert any("No Pub/Sub credentials" in p for p in problems)


def test_validate_mcp_is_lighter():
    """validate_mcp() should not check Vertex or target settings."""
    with patch.object(config, "TARGET_HOST", ""), \
         patch.object(config, "VERTEX_PROJECT", ""), \
         patch.object(config, "LLM_MODEL", ""), \
         patch.object(config, "SETTINGS_FILE", ""), \
         patch.object(config, "GCP_PUBSUB_PROJECT_ID", "test"), \
         patch.dict(os.environ, {"PUBSUB_SA_KEY_PATH": ""}, clear=False), \
         patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": ""}, clear=False):
        problems = config.validate_mcp()
    # Should NOT complain about target, vertex, model, or settings
    for p in problems:
        assert "target host" not in p.lower()
        assert "ANTHROPIC_VERTEX_PROJECT_ID" not in p
        assert "model" not in p.lower() or "world model" in p.lower()
        assert "settings file" not in p.lower()
