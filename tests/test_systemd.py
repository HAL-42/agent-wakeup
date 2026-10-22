"""Opt-in live user-manager tests; all units belong to a temporary state root."""

import os
import subprocess
import sys
import threading
import time
import typing as t
from pathlib import Path
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
    """A restarted real service resumes polling and disables itself after delivery."""
    store, manager = Store(tmp_path / "state"), Systemd()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json\nfrom pathlib import Path\nprint(json.dumps({'state': 'wake' if Path('ready').exists() else 'wait', 'summary': 'live systemd result'}))\n"
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
        original_start = store.state(watch_id).started_at
        manager.control("restart", config.unit)
        until(lambda: store.state(watch_id).started_at != original_start)
        assert store.config(watch_id).deadline == config.deadline
        (tmp_path / "ready").touch()
        until(lambda: store.state(watch_id).phase == "queued")
        until(lambda: manager.status(config.unit)["ActiveState"] == "inactive")
        assert (tmp_path / "receipt").read_text().splitlines() == ["live systemd result"]
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


def test_systemd_cancel_active_probe(tmp_path: Path) -> None:
    """Cancelling an active service also stops the probe's child process."""
    store, manager = Store(tmp_path / "state"), Systemd()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        + "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; time.sleep(2); Path('leaked').touch()\"])\n"
        + "Path('running').touch()\ntime.sleep(60)\n"
    )
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
            codex_binary="/usr/bin/false",
            codex_home=tmp_path,
            max_wait_sec=60,
        )
        watch_id = registered["watch_id"]
        config = store.config(watch_id)
        until(lambda: (tmp_path / "running").exists())
        service.cancel(store, manager, watch_id)
        assert store.state(watch_id).phase == "cancelled"
        assert manager.status(config.unit)["ActiveState"] == "inactive"
        threading.Event().wait(2.1)
        assert not (tmp_path / "leaked").exists()
    finally:
        cleanup(store, manager)
