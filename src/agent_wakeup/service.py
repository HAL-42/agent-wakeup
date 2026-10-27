"""Registration and lifecycle operations shared by the CLI and tests."""

import hashlib
import json
import shutil
import subprocess
import time
import typing as t
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from .managers import ServiceManager
from .model import CATEGORY_PHASES, TERMINAL, Config, WakeupError
from .store import Store, file_lock

PROJECT_DIR = Path(__file__).resolve().parents[2]


def executable(name: str) -> str:
    """Find an executable without resolving its version-selecting symlink."""
    path = shutil.which(name)
    if path is None:
        raise WakeupError(f"找不到可执行文件：{name}")
    return str(Path(path).absolute())


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
    thread_id: str,
    codex_binary: str,
    codex_home: Path,
    codex_remote: str | None = None,
    interval_sec: float = 300,
    probe_timeout_sec: float = 120,
    max_wait_sec: float = 604800,
    queue_timeout_sec: float = 60,
    max_probe_errors: int = 5,
) -> dict[str, t.Any]:
    """Register a probe, reusing an identical session/key registration.

    Args:
        store: State root for this installation.
        manager: Platform service operations.
        key: Caller-selected key unique within the target session.
        probe: Single script to snapshot.
        interpreter: Executable used to run the snapshot.
        probe_args: Literal probe arguments.
        cwd: Probe and delivery working directory.
        env: Environment overrides, kept in the private configuration.
        thread_id: Exact Codex thread UUID.
        codex_binary: Installed Codex CLI executable.
        codex_home: Codex account data directory.
        codex_remote: Optional existing app-server endpoint.
        interval_sec: Delay between successful checks.
        probe_timeout_sec: Per-check execution limit.
        max_wait_sec: Total waiting budget, retained across restarts.
        queue_timeout_sec: Delivery command timeout.
        max_probe_errors: Consecutive errors before notifying the agent.

    Returns:
        Registration identity and the observed runner state.
    """
    store.initialize()
    content = probe.read_bytes()
    values = {
        "key": key,
        "probe_source": str(probe.resolve()),
        "probe_sha256": hashlib.sha256(content).hexdigest(),
        "interpreter": executable(interpreter),
        "probe_args": probe_args,
        "cwd": str(cwd.resolve(strict=True)),
        "env": env,
        "thread_id": str(UUID(thread_id)),
        "codex_binary": executable(codex_binary),
        "codex_home": str(codex_home.expanduser().absolute()),
        "codex_remote": codex_remote,
        "project_dir": str(PROJECT_DIR),
        "uv_binary": executable("uv"),
        "interval_sec": float(interval_sec),
        "probe_timeout_sec": float(probe_timeout_sec),
        "max_wait_sec": float(max_wait_sec),
        "queue_timeout_sec": float(queue_timeout_sec),
        "max_probe_errors": max_probe_errors,
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    reused = False
    with file_lock(store.root / "registry.lock"):
        config = None
        for watch_id in store.ids():
            existing = store.config(watch_id)
            if (existing.thread_id, existing.key) == (values["thread_id"], key):
                if existing.fingerprint != fingerprint:
                    raise WakeupError(
                        f"key 已绑定不同配置：{key}（{watch_id}）；请使用另一个 key。"
                    )
                config, reused = existing, True
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
            store.create(config, content)
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
        "unit": config.unit,
        "phase": store.state(config.watch_id).phase,
        "reused": reused,
        "directory": str(store.directory(config.watch_id)),
    }


def inspect_watch(store: Store, manager: ServiceManager, watch_id: str) -> dict[str, t.Any]:
    """Describe a saved watch without disclosing environment override values."""
    config = store.config(watch_id)
    return {
        "watch_id": watch_id,
        "key": config.key,
        "thread_id": config.thread_id,
        "unit": config.unit,
        "directory": str(store.directory(watch_id)),
        "probe_source": config.probe_source,
        "cwd": config.cwd,
        "deadline": config.deadline,
        "state": asdict(store.state(watch_id)),
        "service": manager.status(config.unit),
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


def retry_delivery(store: Store, manager: ServiceManager, watch_id: str) -> dict[str, str]:
    """Retry only a failed or unconfirmed delivery, using its saved prompt."""
    config = store.config(watch_id)
    with file_lock(store.directory(watch_id) / "run.lock", blocking=False):
        phase = store.state(watch_id).phase
        if phase not in CATEGORY_PHASES["delivery_error"]:
            raise WakeupError(f"当前状态不需要重试投递：{phase}")
        # A runner may have released its lock just before its process exits. Unload
        # that instance before preparing the retry so start cannot reuse a dying job.
        manager.stop(config.unit)
        manager.prepare(config, store.root)
        with store.edit(watch_id) as state:
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


def retry_all_deliveries(store: Store, manager: ServiceManager) -> dict[str, t.Any]:
    """Request one retry per delivery error, retaining individual startup failures.

    Args:
        store: State directory whose delivery errors should be retried.
        manager: Platform service operations used by single-watch retries.

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
            result = retry_delivery(store, manager, watch_id)
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
