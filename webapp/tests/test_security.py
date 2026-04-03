"""Security-focused tests for the webapp."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add webapp to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (
    _sanitize_path,
    _validate_cron_expression,
    _validate_path_component,
    _validate_remote_name,
    app,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# ─── Path Sanitization ──────────────────────────────────────────────────


class TestSanitizePath:
    def test_normal_path(self):
        assert _sanitize_path("/home/user/Documents") is not None

    def test_tilde_expansion(self):
        result = _sanitize_path("~/Documents")
        assert result is not None
        assert "~" not in result

    def test_traversal_attack(self):
        result = _sanitize_path("/home/user/../../../etc/passwd")
        # Should resolve to /etc/passwd which isn't blocked, but traversal is resolved
        assert result is not None
        assert ".." not in result

    def test_blocks_proc(self):
        assert _sanitize_path("/proc/self/environ") is None

    def test_blocks_sys(self):
        assert _sanitize_path("/sys/kernel") is None

    def test_blocks_dev(self):
        assert _sanitize_path("/dev/sda") is None

    def test_blocks_boot(self):
        assert _sanitize_path("/boot/vmlinuz") is None

    def test_empty_path(self):
        assert _sanitize_path("") is None

    def test_none_input(self):
        assert _sanitize_path(None) is None


# ─── Remote Name Validation ─────────────────────────────────────────────


class TestValidateRemoteName:
    def test_valid_names(self):
        assert _validate_remote_name("protondrive")
        assert _validate_remote_name("my-remote")
        assert _validate_remote_name("remote_123")
        assert _validate_remote_name("ProtonDrive")

    def test_invalid_names(self):
        assert not _validate_remote_name("")
        assert not _validate_remote_name("remote;rm -rf /")
        assert not _validate_remote_name("remote`whoami`")
        assert not _validate_remote_name("remote$(cmd)")
        assert not _validate_remote_name("remote name")
        assert not _validate_remote_name("remote:path")
        assert not _validate_remote_name("../../etc")


# ─── Path Component Validation ───────────────────────────────────────────


class TestValidatePathComponent:
    def test_valid_paths(self):
        assert _validate_path_component("")
        assert _validate_path_component("Documents")
        assert _validate_path_component("path/to/folder")
        assert _validate_path_component("my folder/sub dir")

    def test_rejects_shell_metacharacters(self):
        assert not _validate_path_component("path;rm -rf /")
        assert not _validate_path_component("path`whoami`")
        assert not _validate_path_component("path$(cmd)")
        assert not _validate_path_component("path|cat /etc/passwd")
        assert not _validate_path_component("path&background")

    def test_rejects_null_bytes(self):
        assert not _validate_path_component("path\0evil")


# ─── Cron Validation ────────────────────────────────────────────────────


class TestValidateCronExpression:
    def test_valid_expressions(self):
        assert _validate_cron_expression("0 * * * *")
        assert _validate_cron_expression("*/5 * * * *")
        assert _validate_cron_expression("0 2 * * 1-5")
        assert _validate_cron_expression("30 4 1,15 * *")

    def test_invalid_expressions(self):
        assert not _validate_cron_expression("")
        assert not _validate_cron_expression("* * *")  # too few fields
        assert not _validate_cron_expression("* * * * * *")  # too many fields
        assert not _validate_cron_expression("0 * * * * extra")
        assert not _validate_cron_expression("$(whoami) * * * *")  # injection


# ─── API Security Tests ─────────────────────────────────────────────────


class TestAPIPathTraversal:
    def test_browse_local_traversal(self, client):
        resp = client.get("/api/browse/local?path=/proc/self/environ")
        assert resp.status_code == 400

    def test_browse_local_dev(self, client):
        resp = client.get("/api/browse/local?path=/dev")
        assert resp.status_code == 400

    def test_browse_local_tree_traversal(self, client):
        resp = client.get("/api/browse/local/tree?path=/proc")
        assert resp.status_code == 400

    def test_browse_remote_injection(self, client):
        resp = client.get("/api/browse/remote?path=;rm+-rf+/")
        assert resp.status_code == 400


class TestAPIRemoteValidation:
    def test_test_remote_injection(self, client):
        # URL-encode the semicolon to avoid path routing issues
        resp = client.post("/api/remotes/%3Brm%20-rf/test")
        assert resp.status_code in (400, 404)

    def test_about_remote_injection(self, client):
        resp = client.get("/api/remotes/$(whoami)/about")
        assert resp.status_code in (400, 404)

    def test_set_active_injection(self, client):
        resp = client.post("/api/remotes/;evil/set-active")
        assert resp.status_code in (400, 404)


class TestAPIConfigInjection:
    @patch("app.CONFIG_FILE")
    def test_rejects_unknown_keys(self, mock_file, client):
        resp = client.put(
            "/api/config",
            data=json.dumps({"EVIL_KEY": "value"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "not allowed" in data.get("error", "")


class TestAPISyncConfigValidation:
    def test_create_missing_local_path(self, client):
        resp = client.post(
            "/api/sync-configs",
            data=json.dumps({"name": "test"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_invalid_direction(self, client):
        resp = client.post(
            "/api/sync-configs",
            data=json.dumps({
                "name": "test",
                "local_path": "/tmp",
                "direction": "evil",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_blocked_path(self, client):
        resp = client.post(
            "/api/sync-configs",
            data=json.dumps({
                "name": "test",
                "local_path": "/proc/self",
                "direction": "push",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_shell_injection_remote_path(self, client):
        resp = client.post(
            "/api/sync-configs",
            data=json.dumps({
                "name": "test",
                "local_path": "/tmp",
                "remote_path": ";rm -rf /",
                "direction": "push",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestAPIScheduleValidation:
    def test_create_missing_config_id(self, client):
        resp = client.post(
            "/api/schedules",
            data=json.dumps({"name": "test"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_invalid_schedule_type(self, client):
        resp = client.post(
            "/api/schedules",
            data=json.dumps({
                "name": "test",
                "config_id": "abc123",
                "schedule_type": "evil",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_invalid_cron(self, client):
        resp = client.post(
            "/api/schedules",
            data=json.dumps({
                "name": "test",
                "config_id": "abc123",
                "schedule_type": "cron",
                "cron_expression": "$(whoami)",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestAPILogsBounds:
    def test_log_lines_bounded(self, client):
        resp = client.get("/api/logs?lines=999999999")
        # Should not crash — lines is capped at 5000
        assert resp.status_code == 200

    def test_log_lines_invalid(self, client):
        resp = client.get("/api/logs?lines=notanumber")
        # Should fallback to 200 default
        assert resp.status_code == 200

    def test_sync_status_since_invalid(self, client):
        resp = client.get("/api/sync-configs/fake/status?since=notanumber")
        assert resp.status_code == 200
