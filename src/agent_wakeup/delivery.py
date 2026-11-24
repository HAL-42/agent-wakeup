"""Agent-specific session binding, diagnostics, and message delivery."""

import json
import os
import re
import shutil
import subprocess
import threading
import typing as t
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .model import (
    AntigravityConnection,
    Backend,
    CodexConnection,
    Config,
    Connection,
    WakeupError,
)
from .process import execute

SESSION_ENV_VARS: dict[Backend, str] = {
    "codex": "CODEX_THREAD_ID",
    "antigravity": "ANTIGRAVITY_CONVERSATION_ID",
}

ANTIGRAVITY_DEFINITE_ERRORS: tuple[str, ...] = (
    "ANTIGRAVITY_LS_ADDRESS is not set",
    "code = Unauthenticated",
    "code = PermissionDenied",
    "code = NotFound",
    "code = InvalidArgument",
)


@dataclass
class Receipt:
    """Evidence returned by a delivery attempt."""

    phase: t.Literal["queued", "delivery_failed", "delivery_unknown"]
    message_id: str | None = None
    error: str = ""


def find_executable(name: str, env: t.Mapping[str, str] | None = None) -> str:
    """Find an executable without resolving its version-selecting symlink.

    Args:
        name: Command name or filesystem path.
        env: Optional environment mapping providing PATH.

    Returns:
        Absolute path to the executable without resolving symlinks.
    """
    search_path = env["PATH"] if env is not None and "PATH" in env else None
    found = shutil.which(name, path=search_path)
    if found is None:
        raise WakeupError(f"找不到可执行文件：{name}")
    return str(Path(found).absolute())


def detect_backend(env: t.Mapping[str, str]) -> Backend | None:
    """Infer the active backend when unambiguously identified by environment.

    Args:
        env: Environment variables to inspect.

    Returns:
        The inferred backend name, or None when neither or both are present.
    """
    codex_active = bool(env.get("CODEX_THREAD_ID", "").strip())
    antigravity_active = bool(
        env.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        or env.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    )
    if codex_active and not antigravity_active:
        return "codex"
    if antigravity_active and not codex_active:
        return "antigravity"
    return None


def resolve_backend(backend: Backend | None, env: t.Mapping[str, str]) -> Backend:
    """Return an explicit backend or infer one from the active session environment.

    Args:
        backend: Optional caller-specified backend.
        env: Environment variables to inspect when backend is omitted.

    Returns:
        Resolved backend identifier.
    """
    if backend is not None:
        return backend
    detected = detect_backend(env)
    if detected is not None:
        return detected
    if env.get("CODEX_THREAD_ID", "").strip() and (
        env.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        or env.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    ):
        raise WakeupError("同时检测到 Codex 与 Antigravity 环境，请用 --backend 显式指定。")
    raise WakeupError("未检测到会话环境变量，请用 --backend 指定 codex 或 antigravity。")


def resolve_session_id(backend: Backend, session_id: str | None, env: t.Mapping[str, str]) -> str:
    """Validate an explicit or environment-supplied session UUID.

    Args:
        backend: Target backend owning the session.
        session_id: Optional explicit UUID string.
        env: Environment variables supplying the default session ID.

    Returns:
        Canonical hyphenated UUID string.
    """
    env_var = SESSION_ENV_VARS[backend]
    raw = (session_id or env.get(env_var, "")).strip()
    if not raw:
        raise WakeupError(f"当前环境未设置 {env_var}，请用 --session_id 指定目标会话 UUID。")
    try:
        return str(UUID(raw))
    except ValueError as error:
        raise WakeupError(f"无效的目标会话 UUID：{raw}") from error


def has_live_connection_env(backend: Backend, env: t.Mapping[str, str]) -> bool:
    """Report whether env provides live session connection settings for backend.

    Args:
        backend: Watch backend type.
        env: Current caller environment.

    Returns:
        True when live connection settings for backend are present in env.
    """
    if backend == "antigravity":
        return bool(
            env.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
            or env.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
        )
    return bool(env.get("CODEX_THREAD_ID", "").strip() or env.get("CODEX_HOME", "").strip())


def _parse_json_object(raw: str) -> dict[str, t.Any] | None:
    """Parse stdout as a JSON object, returning None on non-object or malformed text."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _antigravity_subprocess_env(
    base_env: t.Mapping[str, str], connection: AntigravityConnection
) -> dict[str, str]:
    """Build a minimal environment for running agentapi commands."""
    merged = {key: base_env[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in base_env}
    if "HOME" not in merged and "HOME" in os.environ:
        merged["HOME"] = os.environ["HOME"]
    if "PATH" not in merged and "PATH" in os.environ:
        merged["PATH"] = os.environ["PATH"]
    merged["ANTIGRAVITY_LS_ADDRESS"] = connection.ls_address
    merged["ANTIGRAVITY_CSRF_TOKEN"] = connection.csrf_token
    return merged


def verify_antigravity_session(
    connection: AntigravityConnection,
    session_id: str,
    env: t.Mapping[str, str],
    timeout_sec: float = 20,
) -> None:
    """Verify Antigravity server reachability, authentication, and conversation existence.

    Args:
        connection: Captured Antigravity connection parameters.
        session_id: Target Antigravity conversation UUID.
        env: Base environment for invoking agentapi.
        timeout_sec: Maximum seconds to wait for metadata verification.
    """
    try:
        result = subprocess.run(
            [connection.binary, "get-conversation-metadata", session_id],
            env=_antigravity_subprocess_env(env, connection),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise WakeupError(f"校验 Antigravity 会话超时（{session_id}）。") from error
    payload = _parse_json_object(result.stdout)
    error_text = str(payload.get("error", "")).strip() if payload is not None else ""
    if (
        result.returncode == 0
        and payload is not None
        and not error_text
        and isinstance(payload.get("response"), dict)
        and "conversationMetadata" in payload["response"]
    ):
        return
    detail = (
        error_text or result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
    )
    raise WakeupError(f"Antigravity 会话校验失败（{session_id}）：{detail[:500]}")


def capture_connection(
    backend: Backend,
    session_id: str,
    *,
    env: t.Mapping[str, str],
    timeout_sec: float = 60,
) -> Connection:
    """Capture and validate delivery connection settings from the active environment.

    Args:
        backend: Target delivery backend.
        session_id: Validated target session UUID.
        env: Environment variables to read connection parameters from.
        timeout_sec: Timeout in seconds for pre-flight validation.

    Returns:
        Validated immutable connection record ready for persistence.
    """
    if backend == "codex":
        binary = find_executable("codex", env)
        default_home = Path(env.get("HOME", str(Path.home()))) / ".codex"
        codex_home = Path(env.get("CODEX_HOME", str(default_home))).expanduser().absolute()
        return CodexConnection(backend="codex", binary=binary, codex_home=str(codex_home))

    binary = find_executable("agentapi", env)
    ls_address = env.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf_token = env.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if not ls_address or not csrf_token:
        raise WakeupError(
            "缺少 Antigravity 连接环境变量（ANTIGRAVITY_LS_ADDRESS / ANTIGRAVITY_CSRF_TOKEN），"
            + "请在 Antigravity 会话内执行该命令。"
        )
    connection = AntigravityConnection(
        backend="antigravity",
        binary=binary,
        ls_address=ls_address,
        csrf_token=csrf_token,
    )
    verify_antigravity_session(connection, session_id, env, timeout_sec=min(timeout_sec, 20))
    return connection


def backend_diagnostics(env: t.Mapping[str, str]) -> dict[str, dict[str, t.Any]]:
    """Inspect local availability and session readiness for all supported backends.

    Args:
        env: Environment variables to inspect.

    Returns:
        Per-backend diagnostic records without exposing secret values.
    """
    search_path = env["PATH"] if "PATH" in env else None
    codex_bin = shutil.which("codex", path=search_path)
    queue_supported = False
    if codex_bin:
        result = subprocess.run(
            [codex_bin, "queue", "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        queue_supported = result.returncode == 0 and "--thread" in result.stdout
    codex_info: dict[str, t.Any] = {
        "binary": codex_bin,
        "session_id_set": bool(env.get("CODEX_THREAD_ID", "").strip()),
        "queue_supported": queue_supported,
        "ready": bool(codex_bin and queue_supported),
    }

    agentapi_bin = shutil.which("agentapi", path=search_path)
    ls_address = env.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf_token = env.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    agy_session = env.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
    agy_info: dict[str, t.Any] = {
        "binary": agentapi_bin,
        "session_id_set": bool(agy_session),
        "ls_address_set": bool(ls_address),
        "csrf_token_set": bool(csrf_token),
    }
    if agentapi_bin and ls_address and csrf_token and agy_session:
        try:
            validated_id = str(UUID(agy_session))
            verify_antigravity_session(
                AntigravityConnection(
                    backend="antigravity",
                    binary=str(Path(agentapi_bin).absolute()),
                    ls_address=ls_address,
                    csrf_token=csrf_token,
                ),
                validated_id,
                env,
                timeout_sec=10,
            )
        except (ValueError, WakeupError, OSError, subprocess.SubprocessError) as error:
            agy_info["session_reachable"] = False
            agy_info["error"] = str(error)
            agy_info["ready"] = False
        else:
            agy_info["session_reachable"] = True
            agy_info["ready"] = True
    else:
        agy_info["ready"] = bool(agentapi_bin and ls_address and csrf_token)

    return {"codex": codex_info, "antigravity": agy_info}


def send_codex(
    config: Config,
    connection: CodexConnection,
    prompt: str,
    directory: Path,
    attempt: int,
    stopped: threading.Event,
) -> Receipt:
    """Deliver one literal prompt through the configured Codex CLI.

    Args:
        config: Watch configuration containing target session and timeout.
        connection: Codex executable and home directory.
        prompt: The persisted prompt, sent without modification.
        directory: Directory for per-attempt stdout and stderr.
        attempt: Monotonic attempt number for diagnostic filenames.
        stopped: Whether the runner has been asked to stop.

    Returns:
        An acknowledged message ID or an explicit failure/uncertainty record.
    """
    argv = [connection.binary, "queue", "--thread", config.session_id, "--message", prompt]
    try:
        result = execute(
            argv,
            cwd=config.cwd,
            env=config.env | {"CODEX_HOME": connection.codex_home},
            timeout=config.queue_timeout_sec,
            stdout_path=directory / f"queue-{attempt}.stdout",
            stderr_path=directory / f"queue-{attempt}.stderr",
            stopped=stopped.is_set,
        )
    except FileNotFoundError as error:
        return Receipt("delivery_failed", error=str(error))
    except OSError as error:
        return Receipt("delivery_unknown", error=str(error))
    receipts = re.findall(
        r"^Queued message (\S+) for thread ([0-9a-f-]+)\.$", result.stdout, re.MULTILINE
    )
    if (
        not result.reason
        and result.returncode == 0
        and len(receipts) == 1
        and receipts[0][1] == config.session_id
    ):
        return Receipt("queued", receipts[0][0])
    return Receipt(
        "delivery_unknown",
        error=(
            f"queue {result.reason or f'exit={result.returncode}'}; "
            + f"receipt not confirmed. {result.stderr[-2000:]}"
        ),
    )


def _is_definite_antigravity_rejection(error_text: str) -> bool:
    """Identify pre-delivery errors where no message could have been queued."""
    if any(marker in error_text for marker in ANTIGRAVITY_DEFINITE_ERRORS):
        return True
    return "code = Unavailable" in error_text and "Error while dialing" in error_text


def send_antigravity(
    config: Config,
    connection: AntigravityConnection,
    prompt: str,
    directory: Path,
    attempt: int,
    stopped: threading.Event,
) -> Receipt:
    """Deliver one literal prompt through Antigravity's agentapi CLI.

    Args:
        config: Watch configuration containing target conversation and timeout.
        connection: Antigravity agentapi path, server address, and CSRF token.
        prompt: The persisted prompt, sent without modification.
        directory: Directory for per-attempt stdout and stderr.
        attempt: Monotonic attempt number for diagnostic filenames.
        stopped: Whether the runner has been asked to stop.

    Returns:
        Delivery confirmation or classified failure/uncertainty record.
    """
    argv = [connection.binary, "send-message", "--", config.session_id, prompt]
    try:
        result = execute(
            argv,
            cwd=config.cwd,
            env=config.env
            | {
                "ANTIGRAVITY_LS_ADDRESS": connection.ls_address,
                "ANTIGRAVITY_CSRF_TOKEN": connection.csrf_token,
            },
            timeout=config.queue_timeout_sec,
            stdout_path=directory / f"queue-{attempt}.stdout",
            stderr_path=directory / f"queue-{attempt}.stderr",
            stopped=stopped.is_set,
        )
    except FileNotFoundError as error:
        return Receipt("delivery_failed", error=str(error))
    except OSError as error:
        return Receipt("delivery_unknown", error=str(error))

    payload = _parse_json_object(result.stdout)
    error_text = str(payload.get("error", "")).strip() if payload is not None else ""
    sent = (
        payload.get("response", {}).get("sendMessage")
        if payload is not None and isinstance(payload.get("response"), dict)
        else None
    )
    if (
        not result.reason
        and result.returncode == 0
        and not error_text
        and isinstance(sent, dict)
        and sent.get("recipientId") == config.session_id
    ):
        return Receipt("queued", None)
    if (
        not result.reason
        and result.returncode != 0
        and _is_definite_antigravity_rejection(error_text)
    ):
        return Receipt("delivery_failed", error=f"agentapi rejected delivery: {error_text[:1000]}")
    detail = error_text or result.stderr[-2000:].strip()
    return Receipt(
        "delivery_unknown",
        error=(
            f"agentapi {result.reason or f'exit={result.returncode}'}; "
            + f"receipt not confirmed. {detail}"
        ).strip(),
    )


def deliver_message(
    config: Config,
    connection: Connection,
    prompt: str,
    directory: Path,
    attempt: int,
    stopped: threading.Event,
) -> Receipt:
    """Dispatch message delivery to the watch's configured backend.

    Args:
        config: Persisted watch configuration.
        connection: Latest persisted backend connection parameters.
        prompt: Literal prompt string to deliver.
        directory: Watch state directory for diagnostic output files.
        attempt: Monotonic delivery attempt counter.
        stopped: Runner stop event.

    Returns:
        Receipt describing whether delivery was queued, failed, or uncertain.
    """
    if isinstance(connection, CodexConnection):
        return send_codex(config, connection, prompt, directory, attempt, stopped)
    return send_antigravity(config, connection, prompt, directory, attempt, stopped)
