"""Opt-in macOS tests using private watch records and a fake Codex executable."""

import json
import os
import subprocess
import sys
import time
import typing as t
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from agent_wakeup import service
from agent_wakeup.launchd import Launchd
from agent_wakeup.store import Store

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("AGENT_WAKEUP_LAUNCHD_TEST") != "1",
    reason="set AGENT_WAKEUP_LAUNCHD_TEST=1 on a logged-in Mac for live launchd tests",
)


def until(condition: t.Callable[[], bool], timeout: float = 15) -> None:
    """Wait for an observable service transition with a bounded test deadline."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("launchd test condition was not reached")
        time.sleep(0.1)


def is_running(pid: int) -> bool:
    """Exclude exited children that are briefly waiting to be reaped."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.fixture
def native_watch(
    tmp_path: Path,
) -> t.Iterator[tuple[Store, Launchd, t.Callable[..., str], Path]]:
    """Register isolated LaunchAgents and unload only jobs created by this fixture."""
    store, manager = Store(tmp_path / "state"), Launchd()
    queue = tmp_path / "codex"
    queue.write_text(
        dedent("""\
        #!/usr/bin/env python3
        import json, sys, time
        from pathlib import Path

        thread = sys.argv[sys.argv.index('--thread') + 1]
        prompt = sys.argv[sys.argv.index('--message') + 1]
        with Path('receipts').open('a') as stream:
            stream.write(json.dumps(prompt) + '\\n')
        if Path('bad-ack').exists():
            print('receipt unavailable')
        else:
            print(f'Queued message mac-test-{time.time_ns()} for thread {thread}.')
        """)
    )
    queue.chmod(0o700)
    thread = str(uuid4())

    def register(code: str, **options: t.Any) -> str:
        """Start a watch with short test intervals and a fake message destination."""
        probe = tmp_path / "probe.py"
        probe.write_text(code)
        return service.register(
            store,
            manager,
            **(
                {
                    "key": "launchd-test",
                    "probe": probe,
                    "interpreter": sys.executable,
                    "probe_args": [],
                    "cwd": tmp_path,
                    "env": {"PATH": os.environ["PATH"]},
                    "thread_id": thread,
                    "codex_binary": str(queue),
                    "codex_home": tmp_path,
                    "interval_sec": 0.2,
                    "probe_timeout_sec": 10,
                    "queue_timeout_sec": 10,
                    "max_wait_sec": 120,
                }
                | options
            ),
        )["watch_id"]

    try:
        yield store, manager, register, tmp_path
    finally:
        for watch_id in store.ids():
            manager.stop(store.config(watch_id).unit)


PHASE_PROBE = (Path(__file__).parent / "probes/lifecycle.py").read_text()


def test_launchd_lifecycle(native_watch: tuple[Store, Launchd, t.Callable[..., str], Path]) -> None:
    """A real job survives notifications and resumes a saved wait after a graceful stop."""
    store, manager, register, directory = native_watch
    watch_id = register(PHASE_PROBE, probe_args=[str(directory)])
    config = store.config(watch_id)
    until(lambda: store.state(watch_id).checks >= 1)
    first_pid = manager.status(config.unit)["pid"]
    first_started_at = store.state(watch_id).started_at
    assert register(PHASE_PROBE, probe_args=[str(directory)]) == watch_id
    assert manager.status(config.unit)["pid"] == first_pid

    (directory / "notify").touch()
    until(
        lambda: (
            store.state(watch_id).queued_message_id is not None
            and store.state(watch_id).phase == "waiting"
        )
    )
    assert manager.status(config.unit)["pid"] == first_pid
    assert manager.plist_path(config.unit).exists()
    first_messages = [
        json.loads(line) for line in (directory / "receipts").read_text().splitlines()
    ]
    assert len(first_messages) == 1
    original_checks = store.state(watch_id).checks

    manager.control("kill", "SIGTERM", f"{manager.domain}/{config.unit}")
    until(lambda: not manager.is_running(config.unit))
    assert store.state(watch_id).phase == "waiting"
    assert manager.plist_path(config.unit).exists()
    manager.start(config.unit)
    until(lambda: store.state(watch_id).checks > original_checks)
    assert manager.status(config.unit)["pid"] != first_pid
    assert store.state(watch_id).started_at != first_started_at
    assert store.config(watch_id).deadline == config.deadline
    assert [
        json.loads(line) for line in (directory / "receipts").read_text().splitlines()
    ] == first_messages

    (directory / "wake").touch()
    until(lambda: store.state(watch_id).phase == "queued")
    until(lambda: not manager.is_running(config.unit))
    assert not manager.plist_path(config.unit).exists()
    assert [json.loads(line) for line in (directory / "receipts").read_text().splitlines()] == [
        first_messages[0],
        store.state(watch_id).summary,
    ]
    assert register(PHASE_PROBE, probe_args=[str(directory)]) == watch_id
    assert not manager.plist_path(config.unit).exists()
    service.remove(store, manager, watch_id)
    assert not manager.status(config.unit)["loaded"]
    assert not store.directory(watch_id).exists()


def test_launchd_retry_saved_notification(
    native_watch: tuple[Store, Launchd, t.Callable[..., str], Path],
) -> None:
    """A paused notification is resent before polling resumes and finds the final result."""
    store, manager, register, directory = native_watch
    (directory / "bad-ack").touch()
    (directory / "notify").touch()
    watch_id = register(PHASE_PROBE, probe_args=[str(directory)])
    config = store.config(watch_id)
    until(lambda: store.state(watch_id).phase == "delivery_unknown")
    until(lambda: not manager.is_running(config.unit))
    assert not manager.plist_path(config.unit).exists()
    saved_message = store.state(watch_id).summary
    (directory / "bad-ack").unlink()
    (directory / "wake").touch()
    service.retry_delivery(store, manager, watch_id)
    until(lambda: store.state(watch_id).phase == "queued")
    until(lambda: not manager.is_running(config.unit))
    assert store.config(watch_id).deadline == config.deadline
    assert [json.loads(line) for line in (directory / "receipts").read_text().splitlines()] == [
        saved_message,
        saved_message,
        store.state(watch_id).summary,
    ]


BLOCKING_PROBE = dedent("""\
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    if Path('ready').exists():
        print(json.dumps({'state': 'wake', 'summary': 'finished after restart'}))
        sys.exit(0)
    if not Path('notified').exists():
        Path('notified').touch()
        print(json.dumps({'state': 'notify', 'summary': 'before blocking check'}))
        sys.exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([
        sys.executable, '-c',
        'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',
    ])
    Path('running.tmp').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid}))
    Path('running.tmp').replace('running.json')
    time.sleep(60)
    """)


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_launchd_stops_probe_children(
    native_watch: tuple[Store, Launchd, t.Callable[..., str], Path], action: str
) -> None:
    """Cancellation and a timed-out check reap even children that ignore SIGTERM."""
    store, manager, register, directory = native_watch
    watch_id = register(
        BLOCKING_PROBE, probe_timeout_sec=2 if action == "timeout" else 60, max_probe_errors=1
    )
    config = store.config(watch_id)
    until(lambda: (directory / "running.json").exists())
    pids = json.loads((directory / "running.json").read_text())
    if action == "cancel":
        service.cancel(store, manager, watch_id)
        expected = "cancelled"
    else:
        expected = "queued"
    until(lambda: store.state(watch_id).phase == expected)
    until(lambda: not manager.is_running(config.unit))
    until(lambda: not any(is_running(pid) for pid in pids.values()))
    assert not manager.plist_path(config.unit).exists()


def test_launchd_recovers_from_sigkill(
    native_watch: tuple[Store, Launchd, t.Callable[..., str], Path],
) -> None:
    """An abnormal runner exit cleans its probe group and launchd restores the saved watch."""
    store, manager, register, directory = native_watch
    watch_id = register(BLOCKING_PROBE, probe_timeout_sec=60)
    config = store.config(watch_id)
    until(lambda: (directory / "running.json").exists())
    pids = json.loads((directory / "running.json").read_text())
    started = store.state(watch_id).started_at
    manager.control("kill", "SIGKILL", f"{manager.domain}/{config.unit}")
    (directory / "ready").touch()
    until(lambda: not any(is_running(pid) for pid in pids.values()))
    until(lambda: store.state(watch_id).phase == "queued", timeout=45)
    until(lambda: not manager.is_running(config.unit))
    assert store.state(watch_id).started_at != started
    assert store.config(watch_id).deadline == config.deadline
    assert not manager.plist_path(config.unit).exists()
