"""Bounded subprocess execution for probes and delivery commands."""

import json
import os
import signal
import subprocess
import time
import typing as t
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from .model import OUTPUT_LIMIT, WakeupError

VALIDATOR = Draft202012Validator(
    json.loads(files("agent_wakeup").joinpath("schema.json").read_text())
)


@dataclass
class Result:
    """A command result with bounded output and a stop reason."""

    returncode: int
    stdout: str
    stderr: str
    reason: str = ""


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a command's process group and reap its direct child."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def execute(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    stopped: t.Callable[[], bool] = lambda: False,
) -> Result:
    """Execute argv without a shell and retain bounded diagnostic files.

    Args:
        argv: Executable and literal arguments.
        cwd: Command working directory.
        env: Explicit environment overrides.
        timeout: Maximum command lifetime in seconds.
        stdout_path: File for the latest command stdout.
        stderr_path: File for the latest command stderr.
        stopped: Whether the supervising runner has been asked to stop.

    Returns:
        Exit status, decoded output and any timeout/cancellation reason.
    """
    reason = ""
    with stdout_path.open("wb") as output, stderr_path.open("wb") as errors:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=os.environ | env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if stopped():
                    reason = "cancelled"
                elif time.monotonic() >= deadline:
                    reason = "timeout"
                elif (
                    max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size)
                    > OUTPUT_LIMIT
                ):
                    reason = "output_limit"
                if reason:
                    break
                time.sleep(0.05)
        finally:
            # A probe's descendants belong to that invocation, including after its parent exits.
            terminate_group(process)
    if max(stdout_path.stat().st_size, stderr_path.stat().st_size) > OUTPUT_LIMIT:
        reason = reason or "output_limit"
    for path in (stdout_path, stderr_path):
        with path.open("r+b") as stream:
            stream.truncate(min(path.stat().st_size, OUTPUT_LIMIT))
    return Result(
        process.returncode,
        stdout_path.read_text(encoding="utf-8", errors="replace"),
        stderr_path.read_text(encoding="utf-8", errors="replace"),
        reason,
    )


def parse_probe(result: Result) -> dict[str, str]:
    """Validate a successful command's JSON against the shared probe schema."""
    if result.reason or result.returncode:
        raise WakeupError(
            f"probe {result.reason or f'exit={result.returncode}'}: {result.stderr[-2000:]}"
        )
    value = json.loads(result.stdout)
    VALIDATOR.validate(value)
    return t.cast(dict[str, str], value)
