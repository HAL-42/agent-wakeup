"""Exercise the Darwin parent-death guard using real POSIX subprocesses."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

from agent_wakeup import process


def is_running(pid: int) -> bool:
    """Treat an exited process awaiting reaping as no longer running."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def test_guard_keeps_exit_status_and_cleans_normal_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard preserves stdout and exit status, and serializes subsequent commands."""
    with monkeypatch.context() as patch:
        patch.setattr(process.sys, "platform", "darwin")
        result = process.execute(
            [sys.executable, "-c", "print('result'); raise SystemExit(7)"],
            cwd=str(tmp_path),
            env={},
            timeout=3,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
        )
        assert result.returncode == 7 and result.stdout == "result\n"
        result = process.execute(
            [sys.executable, "-c", "print('next')"],
            cwd=str(tmp_path),
            env={},
            timeout=3,
            stdout_path=tmp_path / "stdout",
            stderr_path=tmp_path / "stderr",
        )
    assert result.returncode == 0 and result.stdout == "next\n"


def test_guard_cleans_after_runner_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed runner cannot leave a probe alive or overlap a replacement command."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        dedent("""\
        import json, os, signal, subprocess, sys, time
        from pathlib import Path

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([
            sys.executable, '-c',
            'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',
        ])
        Path('running.json').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid}))
        time.sleep(60)
        """)
    )
    controller = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from agent_wakeup import process; "
            + "process.sys.platform = 'darwin'; "
            + "process.execute([sys.executable, 'probe.py'], cwd=str(Path.cwd()), env={}, "
            + "timeout=60, stdout_path=Path('stdout'), stderr_path=Path('stderr'))",
        ],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    pids: dict[str, int] = {}
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "running.json").exists():
            if controller.poll() is not None:
                raise AssertionError(controller.communicate()[1])
            if time.monotonic() > deadline:
                raise AssertionError("probe did not start")
            time.sleep(0.05)
        pids = json.loads((tmp_path / "running.json").read_text())
        controller.kill()
        controller.wait(timeout=5)

        # This command must wait for the old group before sharing its output files.
        with monkeypatch.context() as patch:
            patch.setattr(process.sys, "platform", "darwin")
            result = process.execute(
                [
                    sys.executable,
                    "-c",
                    "import json, subprocess; "
                    + "print(json.dumps([subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], "
                    + f"capture_output=True, text=True).stdout.strip() for pid in {list(pids.values())!r}]))",
                ],
                cwd=str(tmp_path),
                env={},
                timeout=5,
                stdout_path=tmp_path / "stdout",
                stderr_path=tmp_path / "stderr",
            )
        assert result.returncode == 0, result.stderr
        assert all(not state or state.startswith("Z") for state in json.loads(result.stdout))
        assert not any(is_running(pid) for pid in pids.values())
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=5)
        if pids:
            try:
                os.killpg(pids["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
