"""Bounded subprocess execution for probes and delivery commands."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import typing as t
from contextlib import ExitStack
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
    with ExitStack() as resources:
        command = argv
        pass_fds: tuple[int, ...] = ()
        reader = None
        if sys.platform == "darwin":
            # Preserve the direct-exec error for a missing registered executable.
            if Path(argv[0]).is_absolute():
                Path(argv[0]).stat()
            # The guard retains this flock after a runner crash. A replacement runner
            # waits for cleanup before truncating output files or starting another command.
            execution_lock = resources.enter_context(
                (stdout_path.parent / "command.lock").open("a")
            )
            fcntl.flock(execution_lock.fileno(), fcntl.LOCK_EX)
            read_fd, write_fd = os.pipe()
            reader = resources.enter_context(os.fdopen(read_fd, "rb", buffering=0))
            resources.enter_context(os.fdopen(write_fd, "wb", buffering=0))
            pass_fds = (read_fd, execution_lock.fileno())
            command = [
                sys.executable,
                str(Path(__file__).with_name("_parent_guard.py")),
                str(read_fd),
                str(execution_lock.fileno()),
                *argv,
            ]
        output = resources.enter_context(stdout_path.open("wb"))
        errors = resources.enter_context(stderr_path.open("wb"))
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=os.environ | env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        if reader is not None:
            reader.close()
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
