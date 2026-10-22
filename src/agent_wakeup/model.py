"""Persisted watch configuration and execution state."""

import typing as t
from dataclasses import dataclass

Phase = t.Literal[
    "waiting",
    "triggered",
    "sending",
    "queued",
    "cancelled",
    "delivery_failed",
    "delivery_unknown",
]
TERMINAL = {"queued", "cancelled", "delivery_failed", "delivery_unknown"}
OUTPUT_LIMIT = 64 * 1024


class WakeupError(Exception):
    """An actionable CLI or monitoring error."""


@dataclass
class Config:
    """A watch's immutable execution and delivery configuration."""

    watch_id: str
    key: str
    fingerprint: str
    probe_source: str
    probe_sha256: str
    interpreter: str
    probe_args: list[str]
    cwd: str
    env: dict[str, str]
    thread_id: str
    codex_binary: str
    codex_home: str
    codex_remote: str | None
    project_dir: str
    uv_binary: str
    unit: str
    interval_sec: float
    probe_timeout_sec: float
    max_wait_sec: float
    queue_timeout_sec: float
    max_probe_errors: int
    created_at: float
    deadline: float


@dataclass
class State:
    """The current phase and latest observable result of a watch."""

    phase: Phase = "waiting"
    summary: str = ""
    checks: int = 0
    consecutive_errors: int = 0
    started_at: float | None = None
    last_check_at: float | None = None
    next_check_at: float = 0
    last_error: str = ""
    reason: str = ""
    delivery_attempts: int = 0
    queued_message_id: str | None = None
    queued_at: float | None = None
    updated_at: float = 0
    cleanup_error: str = ""
