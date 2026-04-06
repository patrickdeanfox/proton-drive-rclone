"""Multi-step agentic workflow orchestration.

Each workflow is a sequence of steps. Each step calls an existing Python
engine and optionally an Ollama LLM for reasoning / narrative. Steps emit
ProgressTracker updates so the frontend can stream live status.

Available workflows:
  sync_health         — check all remotes, compare counts, generate health report
  file_comparison     — diff two directories or remotes, summarise differences
  file_manager        — propose organization, apply if safe, generate report
  migration_assistant — pre-dedup check, dry-run, execute migration

Usage:
    runner = AgentRunner(db_module=db)
    result = runner.run_workflow("sync_health", {}, job_id)
"""

import logging
import uuid
from typing import Optional

from ..progress import ProgressTracker, register_operation, unregister_operation

log = logging.getLogger(__name__)


class AgentRunner:
    """Orchestrates multi-step agentic workflows."""

    def __init__(self, db_module=None):
        self._db = db_module

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run_workflow(self, workflow_name: str, params: dict,
                     job_id: str = None) -> dict:
        """Execute a workflow by name and return its result dict."""
        if job_id is None:
            job_id = str(uuid.uuid4())

        handler = {
            "sync_health": self._workflow_sync_health,
            "file_comparison": self._workflow_file_comparison,
            "file_manager": self._workflow_file_manager,
            "migration_assistant": self._workflow_migration_assistant,
        }.get(workflow_name)

        if handler is None:
            return {"error": f"Unknown workflow: {workflow_name}",
                    "available": list(WORKFLOW_SCHEMAS.keys())}

        tracker = ProgressTracker(
            operation_type=f"agent_{workflow_name}",
            total=100,
            unit="%",
            job_id=job_id,
        )
        register_operation(tracker)
        try:
            result = handler(params, tracker, job_id)
            tracker.complete(result=result)
        except Exception as e:
            log.error("Workflow %s error: %s", workflow_name, e)
            result = {"error": str(e)}
            tracker.fail(error=str(e))
        finally:
            unregister_operation(tracker.operation_id)

        return {"job_id": job_id, "workflow": workflow_name, **result}

    # ------------------------------------------------------------------
    # Workflow: sync_health
    # ------------------------------------------------------------------

    def _workflow_sync_health(self, params: dict,
                               tracker: ProgressTracker,
                               job_id: str) -> dict:
        """Check all configured remotes and produce a health report."""
        import subprocess

        tracker.update(current=10, message="Listing configured remotes…")

        # 1. List all remotes
        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True, text=True, timeout=10
            )
            remotes = [r.strip().rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
        except Exception as e:
            remotes = []
            log.warning("sync_health: could not list remotes: %s", e)

        tracker.update(current=30, message=f"Testing {len(remotes)} remote(s)…")

        # 2. Test each remote
        from ..engines.migration_engine import test_remote_connection
        remote_statuses = {}
        for i, remote in enumerate(remotes):
            status = test_remote_connection(remote)
            remote_statuses[remote] = status
            pct = 30 + int((i + 1) / max(len(remotes), 1) * 40)
            tracker.update(current=pct, message=f"Tested {remote}: {'ok' if status['ok'] else 'FAIL'}")

        tracker.update(current=75, message="Querying local file counts…")

        # 3. Local file counts from DB
        local_stats = {}
        if self._db:
            try:
                local_stats = self._db.get_stats()
            except Exception:
                pass

        tracker.update(current=90, message="Generating health narrative…")

        # 4. LLM narrative (optional)
        healthy = sum(1 for s in remote_statuses.values() if s["ok"])
        failed = len(remotes) - healthy
        summary_ctx = {
            "remotes_checked": len(remotes),
            "remotes_healthy": healthy,
            "remotes_failed": failed,
            "local_files": local_stats.get("total_files", "unknown"),
        }
        narrative = self._llm_narrative(
            "Summarize the sync health status in 2-3 sentences.", summary_ctx
        )

        return {
            "remotes": remote_statuses,
            "local_stats": local_stats,
            "summary": narrative or (
                f"{healthy}/{len(remotes)} remotes healthy. "
                f"{local_stats.get('total_files', '?')} local files indexed."
            ),
        }

    # ------------------------------------------------------------------
    # Workflow: file_comparison
    # ------------------------------------------------------------------

    def _workflow_file_comparison(self, params: dict,
                                   tracker: ProgressTracker,
                                   job_id: str) -> dict:
        """Compare two directories or remotes and report differences."""
        import subprocess
        path_a = params.get("path_a", "")
        path_b = params.get("path_b", "")
        if not path_a or not path_b:
            return {"error": "path_a and path_b are required"}

        tracker.update(current=20, message=f"Comparing {path_a} ↔ {path_b}…")

        try:
            result = subprocess.run(
                ["rclone", "check", path_a, path_b, "--one-way", "-v"],
                capture_output=True, text=True, timeout=300,
            )
            output = result.stderr or result.stdout
        except Exception as e:
            return {"error": str(e)}

        tracker.update(current=70, message="Parsing comparison output…")

        # Parse rclone check output
        only_in_a, only_in_b, differ = [], [], []
        for line in output.split("\n"):
            if "ERROR" in line and "not found in" in line:
                only_in_a.append(line.strip())
            elif "NOTICE" in line and "sizes differ" in line:
                differ.append(line.strip())

        tracker.update(current=90, message="Generating comparison summary…")

        narrative = self._llm_narrative(
            "Summarize a file comparison result.",
            {"only_in_a": len(only_in_a), "differ": len(differ), "path_a": path_a, "path_b": path_b}
        )

        return {
            "path_a": path_a,
            "path_b": path_b,
            "only_in_a_count": len(only_in_a),
            "differ_count": len(differ),
            "only_in_a": only_in_a[:50],
            "differ": differ[:50],
            "summary": narrative or (
                f"{len(only_in_a)} files only in {path_a}; "
                f"{len(differ)} files differ."
            ),
        }

    # ------------------------------------------------------------------
    # Workflow: file_manager
    # ------------------------------------------------------------------

    def _workflow_file_manager(self, params: dict,
                                tracker: ProgressTracker,
                                job_id: str) -> dict:
        """Propose and optionally apply file organization."""
        tracker.update(current=20, message="Proposing organization…")

        try:
            from ..engines.rule_engine import propose_organization
            proposals = propose_organization()
        except Exception as e:
            return {"error": f"propose_organization failed: {e}"}

        if not isinstance(proposals, list):
            proposals = list(proposals) if proposals else []

        tracker.update(current=60, message=f"Generated {len(proposals)} proposals…")

        # Filter by safety threshold
        threshold = 0.85
        if self._db:
            try:
                settings = self._db.get_safety_settings()
                threshold = settings.get("organize_certainty_threshold", 0.85)
            except Exception:
                pass

        safe = [p for p in proposals if float(p.get("confidence", 1.0)) >= threshold]

        tracker.update(current=85, message="Generating report…")

        narrative = self._llm_narrative(
            "Summarize file organization proposals.",
            {"total": len(proposals), "safe": len(safe), "threshold": threshold}
        )

        return {
            "total_proposals": len(proposals),
            "safe_proposals": len(safe),
            "threshold": threshold,
            "proposals": safe[:100],
            "summary": narrative or (
                f"{len(safe)} safe proposals (out of {len(proposals)} total) "
                f"above {threshold:.0%} confidence."
            ),
        }

    # ------------------------------------------------------------------
    # Workflow: migration_assistant
    # ------------------------------------------------------------------

    def _workflow_migration_assistant(self, params: dict,
                                       tracker: ProgressTracker,
                                       job_id: str) -> dict:
        """Pre-flight check + dry-run for a migration."""
        source_remote = params.get("source_remote", "dropbox")
        source_path = params.get("source_path", "")
        dest_remote = params.get("dest_remote", "protondrive")
        dest_path = params.get("dest_path", "")

        tracker.update(current=10, message="Validating endpoints…")

        from ..engines.migration_engine import validate_endpoints, dry_run_migration
        check = validate_endpoints(source_remote, dest_remote)
        if not check["ok"]:
            return {"error": check["error"], "step": "validate"}

        tracker.update(current=30, message="Running dry-run…")

        dry = dry_run_migration(source_remote, source_path, dest_remote, dest_path)
        if not dry["ok"]:
            return {"error": dry["error"], "step": "dry_run"}

        tracker.update(current=70, message="Checking for potential duplicates…")

        dedup_warning = None
        if self._db:
            try:
                with self._db.get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT count(*) FROM duplicate_groups WHERE status = 'pending'"
                    )
                    pending_dups = cur.fetchone()[0]
                if pending_dups:
                    dedup_warning = (
                        f"{pending_dups} pending duplicate group(s) detected. "
                        "Consider resolving duplicates before migration."
                    )
            except Exception:
                pass

        tracker.update(current=90, message="Generating pre-flight report…")

        narrative = self._llm_narrative(
            "Summarize pre-migration analysis.",
            {
                "files_to_transfer": dry.get("count", 0),
                "source": f"{source_remote}:{source_path}",
                "dest": f"{dest_remote}:{dest_path}",
                "dedup_warning": dedup_warning or "none",
            }
        )

        return {
            "validate": check,
            "dry_run": dry,
            "dedup_warning": dedup_warning,
            "summary": narrative or (
                f"Ready to transfer {dry.get('count', 0)} files from "
                f"{source_remote}:{source_path} → {dest_remote}:{dest_path}."
                + (f" Warning: {dedup_warning}" if dedup_warning else "")
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _llm_narrative(self, prompt: str, context: dict = None) -> Optional[str]:
        """Generate a narrative via Ollama. Returns None if unavailable."""
        try:
            from ..llm.task_router import get_router
            return get_router().run_task("agent_narrative", prompt, context)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Workflow metadata (for /api/agents/workflows endpoint)
# ---------------------------------------------------------------------------

WORKFLOW_SCHEMAS = {
    "sync_health": {
        "name": "Sync Health Check",
        "description": "Tests all configured remotes and generates a health report.",
        "params": {},
    },
    "file_comparison": {
        "name": "File Comparison",
        "description": "Diffs two directories or remotes and reports differences.",
        "params": {
            "path_a": {"type": "string", "required": True, "description": "First path (local or remote:path)"},
            "path_b": {"type": "string", "required": True, "description": "Second path"},
        },
    },
    "file_manager": {
        "name": "File Manager",
        "description": "Proposes file organization based on rules and AI analysis.",
        "params": {},
    },
    "migration_assistant": {
        "name": "Migration Assistant",
        "description": "Pre-flight check + dry-run for a Dropbox → Proton migration.",
        "params": {
            "source_remote": {"type": "string", "default": "dropbox"},
            "source_path": {"type": "string", "default": ""},
            "dest_remote": {"type": "string", "default": "protondrive"},
            "dest_path": {"type": "string", "default": ""},
        },
    },
}
