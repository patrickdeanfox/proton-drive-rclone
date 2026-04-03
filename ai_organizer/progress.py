"""
Unified progress tracking system with WebSocket broadcasting.

Every long-running operation creates a ProgressTracker, which:
 - tracks start time, elapsed, ETA, speed
 - stores snapshots in the database (progress_snapshots table)
 - emits real-time updates over Socket.IO
"""

import time
import threading
import logging
import uuid
from datetime import datetime

log = logging.getLogger(__name__)

# Will be set by app.py after socketio is created
_socketio = None

def set_socketio(sio):
    """Called once at app startup to inject the SocketIO instance."""
    global _socketio
    _socketio = sio


def emit_progress(event, data, room=None):
    """Safely emit a Socket.IO event."""
    if _socketio is None:
        return
    try:
        if room:
            _socketio.emit(event, data, room=room, namespace="/progress")
        else:
            _socketio.emit(event, data, namespace="/progress")
    except Exception as e:
        log.debug("emit_progress error: %s", e)


class ProgressTracker:
    """
    Tracks progress for a single operation.

    Usage:
        tracker = ProgressTracker(operation_id, "scan", total=1000)
        for i in range(1000):
            tracker.update(i + 1, message=f"Scanning file {i+1}")
        tracker.complete(result={"files": 1000})
    """

    def __init__(self, operation_id=None, operation_type="generic",
                 total=0, unit="items", job_id=None):
        self.operation_id = operation_id or str(uuid.uuid4())[:12]
        self.operation_type = operation_type
        self.total = total
        self.current = 0
        self.unit = unit  # "items", "bytes", "files"
        self.job_id = job_id
        self.message = ""
        self.status = "running"
        self.start_time = time.time()
        self.last_emit_time = 0
        self._lock = threading.Lock()
        self.result = None
        self.error = None
        self._emit_interval = 0.25  # min seconds between emits

        # Announce start
        emit_progress("operation_started", self._snapshot())

    def update(self, current=None, total=None, message=None,
               increment=0, extra=None):
        """Update progress. Emits at most every _emit_interval seconds."""
        with self._lock:
            if current is not None:
                self.current = current
            elif increment:
                self.current += increment
            if total is not None:
                self.total = total
            if message is not None:
                self.message = message

        now = time.time()
        if now - self.last_emit_time >= self._emit_interval:
            self.last_emit_time = now
            snap = self._snapshot()
            if extra:
                snap.update(extra)
            emit_progress("progress_update", snap)

    def complete(self, result=None, message=None):
        with self._lock:
            self.status = "completed"
            self.current = self.total if self.total else self.current
            self.result = result
            if message:
                self.message = message
        emit_progress("operation_completed", self._snapshot())

    def fail(self, error=None, message=None):
        with self._lock:
            self.status = "failed"
            self.error = str(error) if error else None
            if message:
                self.message = message
        emit_progress("operation_failed", self._snapshot())

    def _snapshot(self):
        elapsed = time.time() - self.start_time
        percent = 0
        eta = None
        speed = 0

        if self.total and self.total > 0:
            percent = min(100, round((self.current / self.total) * 100, 1))
            if self.current > 0:
                rate = self.current / elapsed if elapsed > 0 else 0
                remaining = self.total - self.current
                eta = remaining / rate if rate > 0 else None
                speed = rate

        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "job_id": self.job_id,
            "status": self.status,
            "current": self.current,
            "total": self.total,
            "percent": percent,
            "unit": self.unit,
            "message": self.message,
            "elapsed": round(elapsed, 1),
            "eta": round(eta, 1) if eta else None,
            "speed": round(speed, 2),
            "error": self.error,
            "result": self.result,
            "timestamp": datetime.now().isoformat(),
        }


# Global registry of active operations
_active_operations = {}
_ops_lock = threading.Lock()


def register_operation(tracker: ProgressTracker):
    with _ops_lock:
        _active_operations[tracker.operation_id] = tracker


def unregister_operation(operation_id: str):
    with _ops_lock:
        _active_operations.pop(operation_id, None)


def get_active_operations():
    with _ops_lock:
        return {k: v._snapshot() for k, v in _active_operations.items()}


def get_operation(operation_id: str):
    with _ops_lock:
        t = _active_operations.get(operation_id)
        return t._snapshot() if t else None
