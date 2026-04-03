"""Core functionality tests for the webapp."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (
    _build_rclone_bisync_args,
    _safe_env,
    app,
    load_config_env,
    load_json,
    save_json,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def tmp_json(tmp_path):
    """Provide a temp JSON file path."""
    return tmp_path / "test.json"


# ─── JSON Helpers ────────────────────────────────────────────────────────


class TestJsonHelpers:
    def test_load_json_missing_file(self, tmp_path):
        result = load_json(tmp_path / "nonexistent.json", [])
        assert result == []

    def test_load_json_default(self, tmp_path):
        result = load_json(tmp_path / "nonexistent.json")
        assert result == []

    def test_save_and_load_json(self, tmp_json):
        data = [{"id": "abc", "name": "test"}]
        save_json(tmp_json, data)
        loaded = load_json(tmp_json)
        assert loaded == data

    def test_load_json_corrupt(self, tmp_json):
        tmp_json.write_text("{invalid json")
        result = load_json(tmp_json, [])
        assert result == []


# ─── Config Env Parser ───────────────────────────────────────────────────


class TestLoadConfigEnv:
    def test_defaults(self):
        with patch("app.CONFIG_FILE", Path("/nonexistent/config.env")):
            config = load_config_env()
        assert config["RCLONE_REMOTE"] == "protondrive"
        assert "ProtonSync" in config["SYNC_DIR"]
        assert "ProtonDrive" in config["MOUNT_DIR"]

    def test_parses_config_file(self, tmp_path):
        config_file = tmp_path / "config.env"
        config_file.write_text(
            'RCLONE_REMOTE="myremote"\n'
            'SYNC_DIR="$HOME/MySync"\n'
            "# comment line\n"
            "\n"
            'LOG_LEVEL="DEBUG"\n'
        )
        with patch("app.CONFIG_FILE", config_file):
            config = load_config_env()
        assert config["RCLONE_REMOTE"] == "myremote"
        assert "MySync" in config["SYNC_DIR"]
        assert config["LOG_LEVEL"] == "DEBUG"

    def test_skips_comments_and_blanks(self, tmp_path):
        config_file = tmp_path / "config.env"
        config_file.write_text("# Only comments\n\n# More comments\n")
        with patch("app.CONFIG_FILE", config_file):
            config = load_config_env()
        # Should still have defaults
        assert "RCLONE_REMOTE" in config


# ─── Safe Environment ────────────────────────────────────────────────────


class TestSafeEnv:
    def test_contains_essential_vars(self):
        env = _safe_env()
        assert "HOME" in env
        assert "PATH" in env

    def test_excludes_secrets(self):
        with patch.dict(os.environ, {
            "SECRET_KEY": "hunter2",
            "AWS_SECRET_ACCESS_KEY": "abcdef",
            "DATABASE_URL": "postgres://...",
        }):
            env = _safe_env()
        assert "SECRET_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "DATABASE_URL" not in env


# ─── Bisync Args Builder ─────────────────────────────────────────────────


class TestBuildRcloneBisyncArgs:
    def test_basic_args(self):
        args = _build_rclone_bisync_args(
            "/home/user/sync",
            "remote:path",
            {"LOG_LEVEL": "INFO", "SYNC_CHECKERS": "4", "SYNC_TRANSFERS": "2"},
        )
        assert args[0] == "bisync"
        assert "/home/user/sync" in args
        assert "remote:path" in args
        assert "--log-level" in args
        assert "--stats" in args

    def test_excludes_parsed(self):
        args = _build_rclone_bisync_args(
            "/sync", "remote:",
            {"SYNC_EXCLUDE_PATTERNS": "*.tmp, .DS_Store, __pycache__"},
        )
        excludes = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
        assert "*.tmp" in excludes
        assert ".DS_Store" in excludes
        assert "__pycache__" in excludes

    def test_resync_flag(self):
        args = _build_rclone_bisync_args("/sync", "remote:", {}, resync=True)
        assert "--resync" in args

    def test_conflict_policy(self):
        args = _build_rclone_bisync_args(
            "/sync", "remote:",
            {"SYNC_CONFLICT_POLICY": "newer"},
        )
        assert "--conflict-resolve" in args
        idx = args.index("--conflict-resolve")
        assert args[idx + 1] == "newer"


# ─── API Route Tests ─────────────────────────────────────────────────────


class TestStatusEndpoint:
    def test_status_returns_json(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "rclone_installed" in data
        assert "remote_connected" in data
        assert "local_files" in data


class TestSyncConfigsCRUD:
    def test_get_returns_list(self, client):
        resp = client.get("/api/sync-configs")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_requires_body(self, client):
        resp = client.post(
            "/api/sync-configs",
            data="null",
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestSyncHistoryEndpoint:
    def test_returns_list(self, client):
        resp = client.get("/api/sync-history")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)


class TestPageRoutes:
    @pytest.mark.parametrize("path", [
        "/", "/folders", "/schedules", "/browser",
        "/connection", "/settings", "/logs",
    ])
    def test_pages_load(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200
        assert b"Proton Drive" in resp.data
