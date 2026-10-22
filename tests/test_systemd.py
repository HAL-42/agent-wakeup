"""Opt-in live user-manager tests; all units belong to a temporary state root."""

import os
import subprocess
import sys
import threading
import time
import typing as t
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from agent_wakeup import service
from agent_wakeup.store import Store
from agent_wakeup.systemd import Systemd

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_WAKEUP_SYSTEMD_TEST") != "1",
    reason="set AGENT_WAKEUP_SYSTEMD_TEST=1 for live systemd tests",
)


def until(condition: t.Callable[[], bool], timeout: float = 15) -> None:
    """Wait locally for a test condition with a fixed deadline."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.1)


def cleanup(store: Store, manager: Systemd) -> None:
    """Remove this fixture's units even when a registration failed to start."""
    for identity in store.ids():
        unit = store.config(identity).unit
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", unit], capture_output=True, timeout=30
        )
        (manager.unit_dir / (unit.split("@", 1)[0] + "@.service")).unlink(missing_ok=True)
    manager.control("daemon-reload")


def test_systemd_restart_delivery_and_cleanup(tmp_path: Path) -> None:
    """Notifications keep the service alive; restart resumes checks until the final wake."""
    store, manager = Store(tmp_path / "state"), Systemd()
    probe = tmp_path / "probe.py"
    probe.write_text(
        dedent("""\
        import json
        from pathlib import Path

        if Path('ready').exists():
            result = {'state': 'wake', 'summary': 'live systemd result'}
        elif Path('notify').exists():
            Path('notify').unlink()
            result = {'state': 'notify', 'summary': 'intermediate result'}
        else:
            result = {'state': 'wait', 'summary': 'waiting'}
        print(json.dumps(result))
        """)
    )
    queue = tmp_path / "codex"
    queue.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        + "with Path('receipt').open('a') as stream: stream.write(sys.argv[-1] + '\\n')\n"
        + "print('Queued message live-test for thread ' + sys.argv[sys.argv.index('--thread') + 1] + '.')\n"
    )
    queue.chmod(0o700)
    watch_id = None
    try:
        registered = service.register(
            store,
            manager,
            key="systemd-restart",
            probe=probe,
            interpreter=sys.executable,
            probe_args=[],
            cwd=tmp_path,
            env={},
            thread_id=str(uuid4()),
            codex_binary=str(queue),
            codex_home=tmp_path,
            interval_sec=0.2,
            max_wait_sec=60,
        )
        watch_id = registered["watch_id"]
        config = store.config(watch_id)
        until(lambda: store.state(watch_id).checks >= 1)
        assert not (tmp_path / "receipt").exists()
        original_pid = manager.control("show", config.unit, "--property=MainPID", "--value")
        (tmp_path / "notify").touch()
        until(
            lambda: (
                store.state(watch_id).queued_message_id == "live-test"
                and store.state(watch_id).phase == "waiting"
            )
        )
        assert manager.status(config.unit)["ActiveState"] == "active"
        assert manager.status(config.unit)["UnitFileState"] == "enabled"
        assert manager.control("show", config.unit, "--property=MainPID", "--value") == original_pid
        assert (tmp_path / "receipt").read_text().splitlines() == ["intermediate result"]
        state = store.state(watch_id)
        assert state.last_check_at is not None
        assert state.next_check_at - state.last_check_at == pytest.approx(
            config.interval_sec, abs=1e-6
        )
        original_checks = state.checks
        original_start = store.state(watch_id).started_at
        manager.control("restart", config.unit)
        until(lambda: store.state(watch_id).started_at != original_start)
        until(lambda: store.state(watch_id).checks > original_checks)
        assert (tmp_path / "receipt").read_text().splitlines() == ["intermediate result"]
        assert store.config(watch_id).deadline == config.deadline
        (tmp_path / "ready").touch()
        until(lambda: store.state(watch_id).phase == "queued")
        until(lambda: manager.status(config.unit)["ActiveState"] == "inactive")
        assert (tmp_path / "receipt").read_text().splitlines() == [
            "intermediate result",
            "live systemd result",
        ]
        assert manager.status(config.unit)["UnitFileState"] == "disabled"
        service.remove(store, manager, watch_id)
        assert probe.exists()
        assert not store.directory(watch_id).exists()
    finally:
        cleanup(store, manager)
        if watch_id:
            template = manager.unit_dir / (config.unit.split("@", 1)[0] + "@.service")
            template.unlink(missing_ok=True)
            manager.control("daemon-reload")


@pytest.mark.parametrize("notify_first", [False, True])
def test_systemd_cancel_active_probe(tmp_path: Path, notify_first: bool) -> None:
    """Cancelling an active service also stops the probe's child process."""
    store, manager = Store(tmp_path / "state"), Systemd()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, subprocess, sys, time\nfrom pathlib import Path\n"
        + (
            "if not Path('notified').exists():\n"
            + "    Path('notified').touch()\n"
            + "    print(json.dumps({'state': 'notify', 'summary': 'intermediate result'}))\n"
            + "    sys.exit(0)\n"
            if notify_first
            else ""
        )
        + "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; time.sleep(2); Path('leaked').touch()\"])\n"
        + "Path('running').touch()\ntime.sleep(60)\n"
    )
    queue = tmp_path / "codex"
    queue.write_text(
        f"#!{sys.executable}\nimport sys\n"
        + "print('Queued message cancel-test for thread ' + sys.argv[sys.argv.index('--thread') + 1] + '.')\n"
    )
    queue.chmod(0o700)
    watch_id = None
    try:
        registered = service.register(
            store,
            manager,
            key="cancel-active",
            probe=probe,
            interpreter=sys.executable,
            probe_args=[],
            cwd=tmp_path,
            env={},
            thread_id=str(uuid4()),
            codex_binary=str(queue),
            codex_home=tmp_path,
            interval_sec=0.2,
            max_wait_sec=60,
        )
        watch_id = registered["watch_id"]
        config = store.config(watch_id)
        until(lambda: (tmp_path / "running").exists())
        service.cancel(store, manager, watch_id)
        assert store.state(watch_id).phase == "cancelled"
        assert store.state(watch_id).delivery_attempts == (1 if notify_first else 0)
        assert manager.status(config.unit)["ActiveState"] == "inactive"
        threading.Event().wait(2.1)
        assert not (tmp_path / "leaked").exists()
    finally:
        cleanup(store, manager)
