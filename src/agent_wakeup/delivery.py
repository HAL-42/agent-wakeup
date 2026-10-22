"""Agent-specific message delivery, separate from watch scheduling."""

import re
import threading
import typing as t
from dataclasses import dataclass
from pathlib import Path

from .model import Config
from .process import execute


@dataclass
class Receipt:
    """Evidence returned by a delivery attempt."""

    phase: t.Literal["queued", "delivery_failed", "delivery_unknown"]
    message_id: str | None = None
    error: str = ""


def send_codex(
    config: Config, prompt: str, directory: Path, attempt: int, stopped: threading.Event
) -> Receipt:
    """Deliver one literal prompt through the configured Codex CLI.

    Args:
        config: Target account, executable and optional server endpoint.
        prompt: The persisted prompt, sent without modification.
        directory: Directory for per-attempt stdout and stderr.
        attempt: Monotonic attempt number for diagnostic filenames.
        stopped: Whether the runner has been asked to stop.

    Returns:
        An acknowledged message ID or an explicit failure/uncertainty record.
    """
    argv = [config.codex_binary]
    if config.codex_remote:
        argv += ["--remote", config.codex_remote]
    argv += ["queue", "--thread", config.thread_id, "--message", prompt]
    try:
        result = execute(
            argv,
            cwd=config.cwd,
            env=config.env | {"CODEX_HOME": config.codex_home},
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
        and receipts[0][1] == config.thread_id
    ):
        return Receipt("queued", receipts[0][0])
    return Receipt(
        "delivery_unknown",
        error=f"queue {result.reason or f'exit={result.returncode}'}; receipt not confirmed. {result.stderr[-2000:]}",
    )
