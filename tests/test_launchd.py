"""LaunchAgent contracts exercised without contacting a macOS service manager."""

import plistlib
import subprocess
import sys
import time
import typing as t
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_wakeup import managers, service
from agent_wakeup.cli import doctor
from agent_wakeup.launchd import CONTROL_TIMEOUT_SEC, Launchd
from agent_wakeup.model import WakeupError
from agent_wakeup.store import Store
from agent_wakeup.systemd import Systemd


class FakeLaunchd(Launchd):
    """Model loaded and running jobs while retaining the real plist lifecycle."""

    def __init__(self, store: Store, directory: Path) -> None:
        """Bind a private agent directory and a simulated GUI domain."""
        super().__init__(directory, uid=501)
        self.store = store
        self.jobs: dict[str, int | None] = {}
        self.calls: list[tuple[str, ...]] = []

    def control(self, *args: str, timeout_sec: float = CONTROL_TIMEOUT_SEC) -> str:
        """Emulate launchctl and acknowledge startup without executing a real service."""
        self.calls.append(args)
        operation = args[0]
        if operation == "print":
            if args[1] == self.domain:
                return f"{self.domain} = {{}}"
            label = args[1].split("/", 2)[2]
            if label not in self.jobs:
                raise subprocess.CalledProcessError(
                    113,
                    args,
                    stderr=f'Could not find service "{label}" in domain for user gui: 501',
                )
            pid = self.jobs[label]
            output = f"{args[1]} = {{\n\tstate = {'running' if pid else 'not running'}\n"
            if pid:
                output += f"\tpid = {pid}\n"
            return output + "\tlast exit code = 0\n}\n"
        if operation == "bootstrap":
            label = plistlib.loads(Path(args[2]).read_bytes())["Label"]
        else:
            label = args[1].split("/", 2)[2]
        if operation == "bootout":
            if label not in self.jobs:
                raise subprocess.CalledProcessError(3, args, stderr="No such process")
            del self.jobs[label]
        elif operation in {"bootstrap", "kickstart"}:
            self.jobs[label] = 12345
            with self.store.edit(label.rsplit(".", 1)[1]) as state:
                state.started_at = time.time()
        else:
            raise AssertionError(args)
        return ""


@pytest.fixture
def launch_context(tmp_path: Path) -> tuple[Store, FakeLaunchd, dict[str, t.Any]]:
    """Supply the same registration inputs for initial and repeated registrations."""
    store = Store(tmp_path / "state")
    manager = FakeLaunchd(store, tmp_path / "LaunchAgents")
    probe = tmp_path / "probe.py"
    probe.write_text('print(\'{"state":"wait","summary":"waiting"}\')\n')
    arguments = {
        "key": "mac-example",
        "probe": probe,
        "interpreter": sys.executable,
        "probe_args": [],
        "cwd": tmp_path,
        "env": {"EXAMPLE_VARIABLE": "probe-only"},
        "thread_id": str(uuid4()),
        "codex_binary": sys.executable,
        "codex_home": tmp_path / "codex",
    }
    return store, manager, arguments


def test_plist_and_registration_reuse(
    launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Paths remain literal and a repeated registration preserves the running job."""
    store, manager, arguments = launch_context
    registered = service.register(store, manager, **arguments)
    watch_id = registered["watch_id"]
    config = store.config(watch_id)
    path = manager.plist_path(config.unit)
    before = path.stat().st_mtime_ns
    repeated = service.register(store, manager, **arguments)
    assert repeated["watch_id"] == watch_id and repeated["reused"]
    assert path.stat().st_mtime_ns == before
    assert len([call for call in manager.calls if call[0] == "bootstrap"]) == 1
    assert not any(call[0] == "kickstart" for call in manager.calls)
    manager.jobs[config.unit] = None
    manager.start(config.unit)
    assert ("kickstart", f"gui/501/{config.unit}") in manager.calls

    python = str(tmp_path / 'project with spaces & quotes "' / ".venv/bin/python")
    monkeypatch.setattr(sys, "executable", python)
    config = replace(config, project_dir=str(tmp_path / 'project % $ & " 中文'))
    manager.prepare(config, store.root)
    value = plistlib.loads(path.read_bytes())
    assert value["ProgramArguments"] == [
        python,
        "-m",
        "agent_wakeup.cli",
        "run",
        "--state_dir",
        str(store.root),
        "--watch_id",
        watch_id,
    ]
    assert value["WorkingDirectory"] == config.project_dir
    assert value["RunAtLoad"] is True
    assert value["KeepAlive"] == {"SuccessfulExit": False}
    assert value["LimitLoadToSessionType"] == "Aqua"
    assert value["Umask"] == 0o077
    assert value["StandardOutPath"] == str(store.directory(watch_id) / "runner.stdout")
    assert value["StandardErrorPath"] == str(store.directory(watch_id) / "runner.stderr")
    assert "EXAMPLE_VARIABLE" not in value["EnvironmentVariables"]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "delayed_operation,delay_sec,starts",
    [("kickstart", 31, True), ("kickstart", 61, False), ("print", 31, False)],
    ids=["restart-after-throttle", "restart-deadline", "status-deadline"],
)
def test_start_waits_for_throttle_with_bounded_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delayed_operation: str,
    delay_sec: float,
    starts: bool,
) -> None:
    """A throttled restart can complete while stalled commands still time out."""
    manager = Launchd(tmp_path / "LaunchAgents", uid=501)
    unit = manager.unit_name(tmp_path, tmp_path / "state", str(uuid4()))
    running = False
    kickstarts = 0

    def run_launchctl(
        command: list[str], *, timeout: float, **options: t.Any
    ) -> subprocess.CompletedProcess[str]:
        """Model launchctl waiting for a spawn without delaying the unit test."""
        nonlocal running, kickstarts
        operation = command[1]
        if operation == delayed_operation and delay_sec > timeout:
            raise subprocess.TimeoutExpired(command, timeout)
        if operation == "print":
            output = f"job = {{\n\tstate = {'running' if running else 'not running'}\n"
            if running:
                output += "\tpid = 12345\n"
            output += "}\n"
        elif operation == "kickstart":
            running = True
            kickstarts += 1
            output = ""
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(subprocess, "run", run_launchctl)
    if starts:
        manager.start(unit)
        assert manager.status(unit)["running"]
        manager.start(unit)
        assert kickstarts == 1
    else:
        with pytest.raises(subprocess.TimeoutExpired) as error:
            manager.start(unit)
        assert error.value.cmd[1] == delayed_operation
        assert not running


def test_finish_does_not_stop_runner_or_reenable_finished_watch(
    launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]],
) -> None:
    """Completion removes login startup; repeated register leaves completed records alone."""
    store, manager, arguments = launch_context
    registered = service.register(store, manager, **arguments)
    watch_id, unit = registered["watch_id"], registered["unit"]
    with store.edit(watch_id) as state:
        state.phase = "queued"
    manager.finish(unit)
    manager.finish(unit)
    assert not manager.plist_path(unit).exists()
    assert manager.jobs[unit] == 12345
    assert not any(call[0] == "bootout" for call in manager.calls)
    before = list(manager.calls)
    assert service.register(store, manager, **arguments)["reused"]
    assert manager.calls == before
    assert not manager.plist_path(unit).exists()


def test_retry_cancel_and_remove(
    launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]],
) -> None:
    """Retry reloads a paused job's config; cancellation and removal unload its label."""
    store, manager, arguments = launch_context
    registered = service.register(store, manager, **arguments)
    watch_id, unit = registered["watch_id"], registered["unit"]
    with store.edit(watch_id) as state:
        state.phase, state.summary, state.reason = (
            "delivery_unknown",
            "saved prompt",
            "probe_notify",
        )
    manager.finish(unit)
    # Keep the old PID visible to emulate a process just finishing its exit.
    service.retry_delivery(store, manager, watch_id)
    assert manager.plist_path(unit).exists()
    assert ("bootout", f"gui/501/{unit}") in manager.calls
    assert len([call for call in manager.calls if call[0] == "bootstrap"]) == 2
    state = store.state(watch_id)
    assert (state.phase, state.reason, state.summary) == (
        "triggered",
        "probe_notify",
        "saved prompt",
    )
    service.cancel(store, manager, watch_id)
    assert unit not in manager.jobs
    assert not manager.plist_path(unit).exists()
    assert store.state(watch_id).phase == "cancelled"
    service.remove(store, manager, watch_id)
    assert not store.directory(watch_id).exists()
    assert arguments["probe"].exists()


def test_launchd_status_distinguishes_nested_fields_and_missing_jobs(
    launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service state comes from top-level job fields, and absence is a normal status."""
    store, manager, arguments = launch_context
    unit = service.register(store, manager, **arguments)["unit"]
    monkeypatch.setattr(
        manager,
        "control",
        lambda *args: (
            "job = {\n\tstate = running\n\tpid = 4321\n"
            + "\tchild = {\n\t\tstate = stopped\n\t\tpid = 9876\n\t}\n}\n"
        ),
    )
    status = manager.status(unit)
    assert status["running"] and status["pid"] == 4321
    assert status["domain"] == "gui/501"
    monkeypatch.undo()
    del manager.jobs[unit]
    assert manager.status(unit)["loaded"] is False


def test_launchd_reports_manager_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Permission or login-domain failures are not treated as missing services."""
    manager = Launchd(tmp_path / "LaunchAgents", uid=501)

    def denied(*args: str) -> str:
        """Simulate a launchctl authorization failure."""
        raise subprocess.CalledProcessError(1, args, stderr="Operation not permitted")

    monkeypatch.setattr(manager, "control", denied)
    unit = manager.unit_name(tmp_path, tmp_path / "state", str(uuid4()))
    with pytest.raises(subprocess.CalledProcessError):
        manager.status(unit)
    checks = manager.diagnostics()
    assert "user_manager" not in checks
    assert checks["user_manager_error"] == "Operation not permitted"


@pytest.mark.parametrize("loaded_on_error", [False, True])
def test_bootstrap_failure_is_recoverable(
    launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]],
    monkeypatch: pytest.MonkeyPatch,
    loaded_on_error: bool,
) -> None:
    """Only a confirmed concurrent load can turn a bootstrap error into success."""
    store, manager, arguments = launch_context
    control = manager.control

    def bootstrap_error(*args: str) -> str:
        """Model both a genuine bootstrap failure and a concurrent registration."""
        if args[0] == "bootstrap":
            if loaded_on_error:
                control(*args)
            raise subprocess.CalledProcessError(
                5, args, stderr="Bootstrap failed: Input/output error"
            )
        return control(*args)

    monkeypatch.setattr(manager, "control", bootstrap_error)
    if loaded_on_error:
        assert service.register(store, manager, **arguments)["phase"] == "waiting"
    else:
        with pytest.raises(WakeupError, match="启动未完成"):
            service.register(store, manager, **arguments)
        assert len(store.ids()) == 1
        monkeypatch.setattr(manager, "control", control)
        assert service.register(store, manager, **arguments)["reused"]


def test_finish_rejects_foreign_paths(tmp_path: Path) -> None:
    """A malformed saved identifier cannot delete another file."""
    manager = Launchd(tmp_path / "LaunchAgents", uid=501)
    other = tmp_path / "other.plist"
    other.write_text("unrelated")
    with pytest.raises(WakeupError):
        manager.finish("../other")
    assert other.read_text() == "unrelated"


@pytest.mark.parametrize("platform_name,expected", [("linux", Systemd), ("darwin", Launchd)])
def test_platform_selection(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, expected: type
) -> None:
    """The CLI selects a backend without installing or loading services."""
    monkeypatch.setattr(managers.sys, "platform", platform_name)
    assert isinstance(managers.get_manager(), expected)


def test_launchd_doctor(launch_context: tuple[Store, FakeLaunchd, dict[str, t.Any]]) -> None:
    """Platform diagnostics query the GUI domain without starting any jobs."""
    store, manager, _ = launch_context
    checks = doctor(store, manager)
    assert checks["user_manager"] == "gui/501"
    assert checks["service_manager"] == "launchd"
    assert "linger" not in checks
    assert manager.calls == [("print", "gui/501")]
