"""Registration, connection rebinding, and lifecycle operations shared by CLI and tests."""

import hashlib
import json
import os
import shutil
import subprocess
import time
import typing as t
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .delivery import (
    capture_connection,
    detect_backend,
    find_executable,
    has_live_connection_env,
    resolve_backend,
    resolve_session_id,
)
from .managers import ServiceManager
from .model import CATEGORY_PHASES, TERMINAL, Backend, Config, Connection, WakeupError
from .store import Store, file_lock

PROJECT_DIR = Path(__file__).resolve().parents[2]
executable = find_executable


def register(
    store: Store,
    manager: ServiceManager,
    *,
    key: str,
    probe: Path,
    interpreter: str,
    probe_args: list[str],
    cwd: Path,
    env: dict[str, str],
    backend: Backend | None = None,
    session_id: str | None = None,
    session_env: t.Mapping[str, str] | None = None,
    interval_sec: float = 300,
    probe_timeout_sec: float = 120,
    max_wait_sec: float = 604800,
    queue_timeout_sec: float = 60,
    max_probe_errors: int = 5,
) -> dict[str, t.Any]:
    """Register a probe, reusing an identical backend/session/key registration.

    Args:
        store: State root for this installation.
        manager: Platform service operations.
        key: Caller-selected key unique within the target session.
        probe: Single script to snapshot.
        interpreter: Executable used to run the snapshot.
        probe_args: Literal probe arguments.
        cwd: Probe and delivery working directory.
        env: Environment overrides kept for probe execution.
        backend: Optional explicit backend ('codex' or 'antigravity').
        session_id: Optional explicit session UUID.
        session_env: Optional environment mapping for resolving session and connection.
        interval_sec: Delay between successful checks.
        probe_timeout_sec: Per-check execution limit.
        max_wait_sec: Total waiting budget, retained across restarts.
        queue_timeout_sec: Delivery command timeout.
        max_probe_errors: Consecutive errors before notifying the agent.

    Returns:
        Registration identity and the observed runner state.
    """
    active_env = os.environ if session_env is None else session_env
    resolved_backend = resolve_backend(backend, active_env)
    resolved_session_id = resolve_session_id(resolved_backend, session_id, active_env)
    connection = capture_connection(
        resolved_backend,
        resolved_session_id,
        env=active_env,
        timeout_sec=queue_timeout_sec,
    )
    content = probe.read_bytes()
    values = {
        "key": key,
        "backend": resolved_backend,
        "session_id": resolved_session_id,
        "probe_source": str(probe.resolve()),
        "probe_sha256": hashlib.sha256(content).hexdigest(),
        "interpreter": executable(interpreter),
        "probe_args": probe_args,
        "cwd": str(cwd.resolve(strict=True)),
        "env": env,
        "project_dir": str(PROJECT_DIR),
        "uv_binary": executable("uv"),
        "interval_sec": float(interval_sec),
        "probe_timeout_sec": float(probe_timeout_sec),
        "max_wait_sec": float(max_wait_sec),
        "queue_timeout_sec": float(queue_timeout_sec),
        "max_probe_errors": max_probe_errors,
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    store.initialize()
    reused = False
    with file_lock(store.root / "registry.lock"):
        config = None
        for watch_id in store.ids():
            existing = store.config(watch_id)
            if (existing.backend, existing.session_id, existing.key) == (
                resolved_backend,
                resolved_session_id,
                key,
            ):
                if existing.fingerprint != fingerprint:
                    raise WakeupError(
                        f"key 已绑定不同配置：{key}（{watch_id}）；请使用另一个 key。"
                    )
                config, reused = existing, True
                if store.state(watch_id).phase not in CATEGORY_PHASES["finished"]:
                    store.save_connection(watch_id, connection)
                break
        if config is None:
            watch_id = str(uuid4())
            now = time.time()
            config = Config(
                **values,
                watch_id=watch_id,
                fingerprint=fingerprint,
                created_at=now,
                deadline=now + max_wait_sec,
                unit=manager.unit_name(PROJECT_DIR, store.root, watch_id),
            )
            store.create(config, connection, content)
        if store.state(config.watch_id).phase not in TERMINAL:
            manager.prepare(config, store.root)
    state = store.state(config.watch_id)
    if state.phase not in TERMINAL:
        try:
            if not manager.is_running(config.unit):
                with store.edit(config.watch_id) as current:
                    current.started_at = None
            manager.start(config.unit)
            deadline = time.monotonic() + 10
            while store.state(config.watch_id).started_at is None:
                if time.monotonic() >= deadline:
                    raise WakeupError(
                        f"运行器尚未确认启动；查看 status {config.watch_id} 和服务日志。"
                    )
                time.sleep(0.1)
        except (OSError, WakeupError, subprocess.SubprocessError) as error:
            # The saved registration makes a failed service start explicitly recoverable.
            detail = (
                error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
            )
            raise WakeupError(
                f"监控记录已保存为 {config.watch_id}，启动未完成：{detail}"
            ) from error
    return {
        "watch_id": config.watch_id,
        "key": key,
        "backend": config.backend,
        "session_id": config.session_id,
        "unit": config.unit,
        "phase": store.state(config.watch_id).phase,
        "reused": reused,
        "directory": str(store.directory(config.watch_id)),
    }


def inspect_watch(store: Store, manager: ServiceManager, watch_id: str) -> dict[str, t.Any]:
    """Describe a saved watch without disclosing environment or connection secrets."""
    config = store.config(watch_id)
    return {
        "watch_id": watch_id,
        "key": config.key,
        "backend": config.backend,
        "session_id": config.session_id,
        "unit": config.unit,
        "directory": str(store.directory(watch_id)),
        "probe_source": config.probe_source,
        "cwd": config.cwd,
        "deadline": config.deadline,
        "state": asdict(store.state(watch_id)),
        "service": manager.status(config.unit),
    }


def rebind(
    store: Store,
    watch_id: str,
    *,
    backend: Backend | None = None,
    session_env: t.Mapping[str, str] | None = None,
) -> dict[str, t.Any]:
    """Refresh one unfinished watch's delivery connection from the active environment.

    Args:
        store: State root containing the watch.
        watch_id: Target watch UUID.
        backend: Optional expected backend name.
        session_env: Optional environment mapping providing live connection credentials.

    Returns:
        Watch identity, current phase, and confirmation that connection was rebound.
    """
    active_env = os.environ if session_env is None else session_env
    config = store.config(watch_id)
    if backend is not None and config.backend != backend:
        raise WakeupError(f"监控 {watch_id} 属于 {config.backend}，与指定的 {backend} 不一致。")
    detected = detect_backend(active_env)
    if backend is None and detected is not None and detected != config.backend:
        raise WakeupError(
            f"当前会话环境属于 {detected}，无法刷新 {config.backend} 监控 {watch_id}。"
        )
    phase = store.state(watch_id).phase
    if phase in CATEGORY_PHASES["finished"]:
        raise WakeupError(f"监控已结束（{phase}），无需更新连接。")
    if phase == "sending":
        raise WakeupError("消息正在投递，请稍后再更新连接。")
    connection = capture_connection(
        config.backend,
        config.session_id,
        env=active_env,
        timeout_sec=config.queue_timeout_sec,
    )
    with store.edit(watch_id, connection=connection) as state:
        if state.phase in CATEGORY_PHASES["finished"]:
            raise WakeupError(f"监控已结束（{state.phase}），无需更新连接。")
        if state.phase == "sending":
            raise WakeupError("消息正在投递，请稍后再更新连接。")
        current_phase = state.phase
    return {
        "watch_id": watch_id,
        "backend": config.backend,
        "session_id": config.session_id,
        "phase": current_phase,
        "rebound": True,
    }


def rebind_all(
    store: Store,
    *,
    backend: Backend | None = None,
    session_env: t.Mapping[str, str] | None = None,
) -> dict[str, t.Any]:
    """Refresh connections for all unfinished watches of the active or specified backend.

    Args:
        store: State directory to scan.
        backend: Optional explicit backend; inferred from session_env when omitted.
        session_env: Optional environment mapping providing live connection credentials.

    Returns:
        Candidate, rebound, and failed counts plus per-watch outcomes.
    """
    active_env = os.environ if session_env is None else session_env
    target_backend = resolve_backend(backend, active_env)
    watch_ids = [
        watch_id
        for watch_id in store.ids()
        if store.config(watch_id).backend == target_backend
        and store.state(watch_id).phase not in CATEGORY_PHASES["finished"]
    ]
    results: list[dict[str, t.Any]] = []
    for watch_id in watch_ids:
        try:
            result = rebind(store, watch_id, backend=target_backend, session_env=active_env)
        except (WakeupError, OSError, ValueError, subprocess.SubprocessError) as error:
            detail = (
                error.stderr or str(error)
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
            results.append({"watch_id": watch_id, "rebound": False, "error": detail})
        else:
            results.append(result)
    rebound = sum(item["rebound"] for item in results)
    return {
        "backend": target_backend,
        "total": len(results),
        "rebound": rebound,
        "failed": len(results) - rebound,
        "results": results,
    }


def cancel(store: Store, manager: ServiceManager, watch_id: str) -> dict[str, str]:
    """Cancel pending monitoring; a delivery already started keeps its evidence."""
    config = store.config(watch_id)
    with store.edit(watch_id) as state:
        if state.phase == "sending":
            raise WakeupError("消息正在投递，请先查看投递结果；已发送的消息不能通过 cancel 撤回。")
        if state.phase not in TERMINAL:
            state.phase, state.reason = "cancelled", "user"
    manager.stop(config.unit)
    return {"watch_id": watch_id, "phase": store.state(watch_id).phase}


def retry_delivery(
    store: Store,
    manager: ServiceManager,
    watch_id: str,
    *,
    session_env: t.Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Retry a failed or unconfirmed delivery, refreshing live connection when available."""
    active_env = os.environ if session_env is None else session_env
    config = store.config(watch_id)
    with file_lock(store.directory(watch_id) / "run.lock", blocking=False):
        phase = store.state(watch_id).phase
        if phase not in CATEGORY_PHASES["delivery_error"]:
            raise WakeupError(f"当前状态不需要重试投递：{phase}")
        refreshed: Connection | None = None
        if has_live_connection_env(config.backend, active_env):
            refreshed = capture_connection(
                config.backend,
                config.session_id,
                env=active_env,
                timeout_sec=config.queue_timeout_sec,
            )
        # A runner may have released its lock just before its process exits. Unload
        # that instance before preparing the retry so start cannot reuse a dying job.
        manager.stop(config.unit)
        manager.prepare(config, store.root)
        with store.edit(watch_id, connection=refreshed) as state:
            state.phase, state.last_error, state.started_at = "triggered", "", None
    try:
        manager.start(config.unit)
    except (OSError, WakeupError, subprocess.SubprocessError) as error:
        with store.edit(watch_id) as state:
            # A start command can fail after the runner has already acknowledged
            # startup. Only restore a retryable phase if delivery has not begun.
            if state.phase == "triggered" and state.started_at is None:
                state.phase = phase
                state.last_error = f"启动重试失败：{error}"
        raise
    return {"watch_id": watch_id, "phase": store.state(watch_id).phase}


def retry_all_deliveries(
    store: Store,
    manager: ServiceManager,
    *,
    session_env: t.Mapping[str, str] | None = None,
) -> dict[str, t.Any]:
    """Request one retry per delivery error, retaining individual startup failures.

    Args:
        store: State directory whose delivery errors should be retried.
        manager: Platform service operations used by single-watch retries.
        session_env: Optional environment mapping providing live connection credentials.

    Returns:
        Candidate, started and failed counts plus each watch's startup result.
        Message delivery itself continues asynchronously in the background.
    """
    watch_ids = [
        watch_id
        for watch_id in store.ids()
        if store.state(watch_id).phase in CATEGORY_PHASES["delivery_error"]
    ]
    results: list[dict[str, t.Any]] = []
    for watch_id in watch_ids:
        try:
            result = retry_delivery(store, manager, watch_id, session_env=session_env)
        except (WakeupError, OSError, ValueError, subprocess.SubprocessError) as error:
            detail = (
                error.stderr or str(error)
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
            results.append({"watch_id": watch_id, "started": False, "error": detail})
        else:
            results.append({**result, "started": True})
    started = sum(result["started"] for result in results)
    return {
        "total": len(results),
        "started": started,
        "failed": len(results) - started,
        "results": results,
    }


def remove(store: Store, manager: ServiceManager, watch_id: str) -> dict[str, t.Any]:
    """Remove a finished watch's records after disabling its service."""
    with file_lock(store.root / "registry.lock"):
        directory = store.directory(watch_id)
        with file_lock(directory / "run.lock", blocking=False):
            if store.state(watch_id).phase not in TERMINAL:
                raise WakeupError("监控尚未结束，请先 cancel。")
            manager.stop(store.config(watch_id).unit)
            shutil.rmtree(directory)
    return {"watch_id": watch_id, "removed": True}
