"""Deterministic polling and one-shot agent delivery."""

import subprocess
import threading
import time

from jsonschema import ValidationError
from loguru import logger

from .delivery import send_codex
from .model import TERMINAL, Config, WakeupError
from .process import execute, parse_probe
from .store import Store, file_lock
from .systemd import Systemd


def trigger(store: Store, watch_id: str, summary: str, reason: str) -> None:
    """Persist a prompt if the watch is still waiting."""
    with store.edit(watch_id) as state:
        if state.phase == "waiting":
            state.phase, state.summary, state.reason = "triggered", summary, reason


def deliver(store: Store, config: Config, stopped: threading.Event) -> None:
    """Send the persisted prompt and record an acknowledgement or uncertainty."""
    directory = store.directory(config.watch_id)
    with store.edit(config.watch_id) as state:
        if state.phase != "triggered" or stopped.is_set():
            return
        state.phase = "sending"
        state.delivery_attempts += 1
        attempt, prompt = state.delivery_attempts, state.summary
    receipt = send_codex(config, prompt, directory, attempt, stopped)
    with store.edit(config.watch_id) as state:
        state.phase = receipt.phase
        state.last_error = receipt.error
        if receipt.phase == "queued":
            state.phase, state.queued_message_id, state.queued_at = (
                "queued",
                receipt.message_id,
                time.time(),
            )
            logger.info("{} queued message {}", config.watch_id, state.queued_message_id)
        else:
            logger.error("{}: {}", config.watch_id, state.last_error)


def poll_once(store: Store, config: Config, stopped: threading.Event) -> None:
    """Execute one probe and persist its result or a recoverable error."""
    directory = store.directory(config.watch_id)
    error_message = ""
    result_value: dict[str, str] | None = None
    try:
        result = execute(
            [config.interpreter, str(directory / "probe"), *config.probe_args],
            cwd=config.cwd,
            env=config.env,
            timeout=min(config.probe_timeout_sec, max(0.01, config.deadline - time.time())),
            stdout_path=directory / "probe.stdout",
            stderr_path=directory / "probe.stderr",
            stopped=stopped.is_set,
        )
        if result.reason == "cancelled":
            return
        result_value = parse_probe(result)
    except (OSError, WakeupError, ValueError, ValidationError) as error:
        error_message = str(error)[:2000]
    if stopped.is_set():
        return
    with store.edit(config.watch_id) as state:
        if state.phase != "waiting":
            return
        now = time.time()
        state.checks += 1
        state.last_check_at = now
        if error_message:
            state.consecutive_errors += 1
            state.last_error = error_message
            state.next_check_at = now + min(
                config.interval_sec * 2 ** (state.consecutive_errors - 1), 900
            )
            logger.warning(
                "{} probe error {}/{}: {}",
                config.watch_id,
                state.consecutive_errors,
                config.max_probe_errors,
                error_message,
            )
            if state.consecutive_errors >= config.max_probe_errors:
                state.phase, state.reason = "triggered", "probe_error"
                state.summary = (
                    f"agent-wakeup 监控 {config.watch_id}（{config.key}）连续检查失败。\n"
                    + f"错误：{error_message}\n记录目录：{directory}\n"
                    + "请检查监控和 probe 日志，处理这一阻塞后继续原任务。"
                )
        else:
            assert result_value is not None
            state.summary = result_value["summary"]
            state.consecutive_errors, state.last_error = 0, ""
            state.next_check_at = now + config.interval_sec
            if result_value["state"] == "wake":
                state.phase, state.reason = "triggered", "probe"
                logger.info("{} probe triggered", config.watch_id)


def run_watch(store: Store, watch_id: str, manager: Systemd, stopped: threading.Event) -> None:
    """Run or recover one watch until delivery, cancellation or a stop signal.

    Args:
        store: Persistent state and locks.
        watch_id: Watch to run.
        manager: Service lifecycle operations.
        stopped: Signal-aware stop event; stopping preserves pending work.
    """
    directory = store.directory(watch_id)
    config = store.config(watch_id)
    with file_lock(directory / "run.lock", blocking=False):
        with store.edit(watch_id) as state:
            state.started_at = time.time()
            if state.phase == "sending":
                state.phase = "delivery_unknown"
                state.last_error = "运行器在发送回执落盘前退出；核对原会话后可显式重试。"
        logger.info("{} runner started", watch_id)
        while not stopped.is_set():
            state = store.state(watch_id)
            if state.phase in TERMINAL:
                break
            if state.phase == "triggered":
                deliver(store, config, stopped)
                continue
            if time.time() >= config.deadline:
                trigger(
                    store,
                    watch_id,
                    f"agent-wakeup 监控 {watch_id}（{config.key}）已到等待期限。\n"
                    + f"记录目录：{directory}\n请检查当前条件并决定后续处理。",
                    "deadline",
                )
                continue
            delay = min(state.next_check_at, config.deadline) - time.time()
            if delay > 0:
                stopped.wait(min(delay, 1))
                continue
            poll_once(store, config, stopped)
        if store.state(watch_id).phase in TERMINAL:
            try:
                manager.disable(config.unit)
            except subprocess.CalledProcessError as error:
                with store.edit(watch_id) as state:
                    state.cleanup_error = error.stderr or str(error)
                logger.warning("{} could not disable service: {}", watch_id, error.stderr)
        logger.info("{} runner stopped: {}", watch_id, store.state(watch_id).phase)
