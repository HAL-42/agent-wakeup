"""Persisted watch configuration, backend connection, and execution state."""

import typing as t
from dataclasses import dataclass

Backend = t.Literal["codex", "antigravity"]
BACKENDS: tuple[Backend, ...] = t.get_args(Backend)

Phase = t.Literal[
    "waiting",
    "triggered",
    "sending",
    "queued",
    "cancelled",
    "delivery_failed",
    "delivery_unknown",
]
CATEGORY_PHASES: dict[str, set[Phase]] = {
    "delivery_error": {"delivery_failed", "delivery_unknown"},
    "active": {"waiting", "triggered", "sending"},
    "finished": {"queued", "cancelled"},
}
TERMINAL = CATEGORY_PHASES["delivery_error"] | CATEGORY_PHASES["finished"]
OUTPUT_LIMIT = 64 * 1024


class WakeupError(Exception):
    """An actionable CLI or monitoring error."""


@dataclass(frozen=True)
class CodexConnection:
    """Connection parameters for delivering messages to a Codex session."""

    backend: t.Literal["codex"]
    binary: str
    codex_home: str


@dataclass(frozen=True)
class AntigravityConnection:
    """Connection parameters for delivering messages to an Antigravity session."""

    backend: t.Literal["antigravity"]
    binary: str
    ls_address: str
    csrf_token: str


Connection = CodexConnection | AntigravityConnection


@dataclass
class Config:
    """A watch's immutable execution and session-binding configuration."""

    watch_id: str
    key: str
    backend: Backend
    session_id: str
    fingerprint: str
    probe_source: str
    probe_sha256: str
    interpreter: str
    probe_args: list[str]
    cwd: str
    env: dict[str, str]
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
