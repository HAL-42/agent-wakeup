"""Opt-in live user-manager tests; all units belong to a temporary state root."""

import json
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
from agent_wakeup.model import Backend
from agent_wakeup.store import Store
from agent_wakeup.systemd import Systemd

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AGENT_WAKEUP_SYSTEMD_TEST") != "1",
    reason="set AGENT_WAKEUP_SYSTEMD_TEST=1 on Linux for live systemd tests",
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


def make_fake_backends(tmp_path: Path) -> tuple[Path, Path]:
    """Create isolated codex and agentapi executables in tmp_path/bin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    receipt = tmp_path / "receipt"

    codex = bin_dir / "codex"
    codex.write_text(
        dedent(f"""\
        #!{sys.executable}
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        thread = args[args.index('--thread') + 1]
        with Path({str(receipt)!r}).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(args[-1]) + '\\n')
        print(f'Queued message live-test for thread {{thread}}.')
        """),
        encoding="utf-8",
    )
    codex.chmod(0o700)

    agentapi = bin_dir / "agentapi"
    agentapi.write_text(
        dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        from pathlib import Path
        expected = json.loads(Path({str(tmp_path / "agy.json")!r}).read_text(encoding='utf-8'))
        if (
            os.environ.get('ANTIGRAVITY_LS_ADDRESS') != expected['ls']
            or os.environ.get('ANTIGRAVITY_CSRF_TOKEN') != expected['token']
        ):
            print(json.dumps({{'response': {{}}, 'error': 'rpc error: code = Unauthenticated desc = bad token'}}))
            raise SystemExit(1)
        args = sys.argv[1:]
        cmd, rest = args[0], args[1:]
        if rest[:1] == ['--']:
            rest = rest[1:]
        if cmd == 'get-conversation-metadata':
            print(json.dumps({{'response': {{'conversationMetadata': {{'metadata': {{'rootConversationId': rest[0]}}}}}}}}))
            raise SystemExit(0)
        sid, prompt = rest[0], rest[1]
        with Path({str(receipt)!r}).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(prompt) + '\\n')
        print(json.dumps({{'response': {{'sendMessage': {{'recipientId': sid, 'content': prompt}}}}}}))
        """),
        encoding="utf-8",
    )
    agentapi.chmod(0o700)
    (tmp_path / "agy.json").write_text(
        json.dumps({"ls": "localhost:33775", "token": "tok-1"}), encoding="utf-8"
    )
    return bin_dir, receipt


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
def test_systemd_restart_rebind_delivery_and_cleanup(tmp_path: Path, backend: Backend) -> None:
    """Notifications keep the service alive; rebind and restart work until final wake."""
    store, manager = Store(tmp_path / "state"), Systemd()
    probe = Path(__file__).parent / "probes/lifecycle.py"
    bin_dir, receipt = make_fake_backends(tmp_path)
    sid = str(uuid4())
    session_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
    } | (
        {"CODEX_THREAD_ID": sid, "CODEX_HOME": str(tmp_path)}
        if backend == "codex"
        else {
            "ANTIGRAVITY_CONVERSATION_ID": sid,
            "ANTIGRAVITY_LS_ADDRESS": "localhost:33775",
            "ANTIGRAVITY_CSRF_TOKEN": "tok-1",
        }
    )
    watch_id = None
    try:
        registered = service.register(
            store,
            manager,
            key=f"systemd-{backend}",
            probe=probe,
            interpreter=sys.executable,
            probe_args=[str(tmp_path)],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            session_env=session_env,
            interval_sec=0.2,
            max_wait_sec=60,
        )
        watch_id = registered["watch_id"]
        config = store.config(watch_id)
        until(lambda: store.state(watch_id).checks >= 1)
        assert not receipt.exists()
        original_pid = manager.control("show", config.unit, "--property=MainPID", "--value")
        (tmp_path / "notify").touch()
        until(
            lambda: (
                store.state(watch_id).queued_at is not None
                and store.state(watch_id).phase == "waiting"
            )
        )
        assert manager.status(config.unit)["ActiveState"] == "active"
        assert manager.control("show", config.unit, "--property=MainPID", "--value") == original_pid
        first_messages = [
            json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()
        ]
        assert len(first_messages) == 1

        if backend == "antigravity":
            (tmp_path / "agy.json").write_text(
                json.dumps({"ls": "localhost:44999", "token": "tok-2"}), encoding="utf-8"
            )
            service.rebind(
                store,
                watch_id,
                session_env=session_env
                | {"ANTIGRAVITY_LS_ADDRESS": "localhost:44999", "ANTIGRAVITY_CSRF_TOKEN": "tok-2"},
            )
            assert (
                manager.control("show", config.unit, "--property=MainPID", "--value")
                == original_pid
            )

        original_checks = store.state(watch_id).checks
        original_start = store.state(watch_id).started_at
        manager.control("restart", config.unit)
        until(lambda: store.state(watch_id).started_at != original_start)
        until(lambda: store.state(watch_id).checks > original_checks)
        assert store.config(watch_id).deadline == config.deadline
        (tmp_path / "wake").touch()
        until(lambda: store.state(watch_id).phase == "queued")
        until(lambda: manager.status(config.unit)["ActiveState"] == "inactive")
        assert [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()] == [
            first_messages[0],
            store.state(watch_id).summary,
        ]
        assert manager.status(config.unit)["UnitFileState"] == "disabled"
        service.remove(store, manager, watch_id)
        assert probe.exists() and not store.directory(watch_id).exists()
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
        + "Path('running').touch()\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    bin_dir, _ = make_fake_backends(tmp_path)
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
            env={"PATH": os.environ["PATH"]},
            session_env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CODEX_THREAD_ID": str(uuid4()),
                "CODEX_HOME": str(tmp_path),
            },
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
