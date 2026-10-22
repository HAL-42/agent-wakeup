"""Contract tests using real probe and queue subprocesses."""

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_wakeup import service
from agent_wakeup.model import WakeupError
from agent_wakeup.process import Result, execute, parse_probe
from agent_wakeup.runner import run_watch
from agent_wakeup.store import Store
from agent_wakeup.systemd import Systemd


class FakeSystemd(Systemd):
    """Record service operations while exercising the real watch state."""

    def __init__(self, store: Store, directory: Path) -> None:
        """Bind the fake manager to a test state root."""
        super().__init__(directory)
        self.store = store
        self.calls: list[tuple[str, ...]] = []

    def control(self, *args: str) -> str:
        """Record an operation without contacting the host manager."""
        self.calls.append(args)
        return "ActiveState=inactive\nLoadState=loaded"

    def start(self, unit: str) -> None:
        """Acknowledge runner startup; tests decide when to execute it."""
        self.calls.append(("start", unit))
        watch_id = unit.split("@", 1)[1].removesuffix(".service")
        with self.store.edit(watch_id) as state:
            state.started_at = time.time()


@pytest.fixture
def context(tmp_path: Path) -> tuple[Store, FakeSystemd, Path, Path]:
    """Provide private state plus a queue executable that captures literal input."""
    store = Store(tmp_path / "state")
    queue = tmp_path / "fake-codex"
    queue.write_text(
        f"#!{sys.executable}\n"
        + "import json, os, sys, time\n"
        + "from pathlib import Path\n"
        + "args = sys.argv[1:]\n"
        + "thread = args[args.index('--thread') + 1]\n"
        + "message = args[args.index('--message') + 1]\n"
        + "with Path(os.environ['RECEIPTS']).open('a') as stream:\n"
        + "    stream.write(json.dumps({'thread': thread, 'message': message}) + '\\n')\n"
        + "time.sleep(float(os.environ.get('QUEUE_SLEEP', '0')))\n"
        + "if os.environ.get('QUEUE_BAD_ACK'): print('no confirmed receipt')\n"
        + "else: print(f'Queued message test-message for thread {thread}.')\n"
    )
    queue.chmod(0o700)
    return store, FakeSystemd(store, tmp_path / "units"), queue, tmp_path / "receipts"


def arm(
    context: tuple[Store, FakeSystemd, Path, Path],
    code: str,
    *,
    key: str = "example",
    thread: str | None = None,
    **overrides: object,
) -> str:
    """Register a real temporary probe with small test durations."""
    store, manager, queue, receipts = context
    probe = store.root.parent / f"{key}.py"
    probe.write_text(code)
    options = {
        "key": key,
        "probe": probe,
        "interpreter": sys.executable,
        "probe_args": [],
        "cwd": store.root.parent,
        "env": {"RECEIPTS": str(receipts)},
        "thread_id": thread or str(uuid4()),
        "codex_binary": str(queue),
        "codex_home": store.root.parent / "codex",
        "interval_sec": 0.01,
        "probe_timeout_sec": 2,
        "max_wait_sec": 30,
        "queue_timeout_sec": 2,
    } | overrides
    return service.register(store, manager, **options)["watch_id"]


def test_wait_then_wake_preserves_prompt_and_snapshot(
    context: tuple[Store, FakeSystemd, Path, Path],
) -> None:
    """Polling is local and one literal prompt survives original script edits."""
    store, manager, _, receipts = context
    prompt = '完成！\n请读取 "路径"；`literal` $(touch unwanted) $HOME 与单引号\''
    watch_id = arm(
        context,
        "import json\nfrom pathlib import Path\n"
        + "counter = Path('counter')\ncount = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        + "counter.write_text(str(count))\n"
        + f"print(json.dumps({{'state': 'wake' if count == 3 else 'wait', 'summary': {prompt!r}}}))\n",
    )
    Path(store.config(watch_id).probe_source).write_text("raise RuntimeError('changed original')")
    run_watch(store, watch_id, manager, threading.Event())
    state = store.state(watch_id)
    assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 3, 1)
    assert json.loads(receipts.read_text())["message"] == prompt
    assert not (store.root.parent / "unwanted").exists()
    run_watch(store, watch_id, manager, threading.Event())
    assert len(receipts.read_text().splitlines()) == 1
    assert ("disable", store.config(watch_id).unit) in manager.calls


@pytest.mark.parametrize(
    "code",
    [
        "raise RuntimeError('network unavailable')",
        "print('invalid json')",
        "import time; time.sleep(3)",
    ],
)
def test_probe_errors_notify_once(
    context: tuple[Store, FakeSystemd, Path, Path], code: str
) -> None:
    """Repeated execution failures send one diagnostic instead of a success prompt."""
    store, manager, _, receipts = context
    watch_id = arm(context, code, max_probe_errors=2, probe_timeout_sec=0.1)
    run_watch(store, watch_id, manager, threading.Event())
    state = store.state(watch_id)
    assert (state.phase, state.reason, state.checks) == ("queued", "probe_error", 2)
    assert "连续检查失败" in json.loads(receipts.read_text())["message"]


def test_deadline_survives_restart(context: tuple[Store, FakeSystemd, Path, Path]) -> None:
    """An elapsed persisted deadline wakes without executing the probe again."""
    store, manager, _, receipts = context
    watch_id = arm(context, "raise AssertionError('must not run')", max_wait_sec=0.01)
    deadline = store.config(watch_id).deadline
    time.sleep(0.02)
    run_watch(store, watch_id, manager, threading.Event())
    assert store.config(watch_id).deadline == deadline
    assert store.state(watch_id).checks == 0
    assert store.state(watch_id).reason == "deadline"
    assert "等待期限" in json.loads(receipts.read_text())["message"]


def test_interrupted_delivery_is_not_replayed(
    context: tuple[Store, FakeSystemd, Path, Path],
) -> None:
    """A persisted sending state is ambiguous after a runner crash."""
    store, manager, _, receipts = context
    watch_id = arm(context, "raise AssertionError('must not run')")
    with store.edit(watch_id) as state:
        state.phase, state.summary = "sending", "saved prompt"
    run_watch(store, watch_id, manager, threading.Event())
    assert store.state(watch_id).phase == "delivery_unknown"
    assert not receipts.exists()
    service.retry_delivery(store, manager, watch_id)
    run_watch(store, watch_id, manager, threading.Event())
    assert store.state(watch_id).phase == "queued"
    assert store.state(watch_id).checks == 0
    assert json.loads(receipts.read_text())["message"] == "saved prompt"
    with pytest.raises(WakeupError):
        service.retry_delivery(store, manager, watch_id)


@pytest.mark.parametrize("env", [{"QUEUE_SLEEP": "1"}, {"QUEUE_BAD_ACK": "1"}])
def test_uncertain_receipt_stops_without_retry(
    context: tuple[Store, FakeSystemd, Path, Path], env: dict[str, str]
) -> None:
    """Timeout and malformed receipts never cause an automatic duplicate."""
    store, manager, _, receipts = context
    watch_id = arm(
        context,
        'print(\'{"state":"wake","summary":"go"}\')',
        env={"RECEIPTS": str(receipts)} | env,
        queue_timeout_sec=0.2,
    )
    run_watch(store, watch_id, manager, threading.Event())
    assert store.state(watch_id).phase == "delivery_unknown"
    run_watch(store, watch_id, manager, threading.Event())
    assert len(receipts.read_text().splitlines()) == 1


def test_duplicate_registration_and_conflict(
    context: tuple[Store, FakeSystemd, Path, Path],
) -> None:
    """A session/key pair identifies one immutable registration."""
    thread = str(uuid4())
    code = 'print(\'{"state":"wait","summary":"waiting"}\')'
    watch_id = arm(context, code, thread=thread)
    assert arm(context, code, thread=thread) == watch_id
    with pytest.raises(WakeupError, match="不同配置"):
        arm(context, code, thread=thread, interval_sec=1)
    assert len(context[0].ids()) == 1


def test_equivalent_duration_registration(context: tuple[Store, FakeSystemd, Path, Path]) -> None:
    """Explicit numeric defaults keep the same registration identity."""
    thread = str(uuid4())
    first = arm(context, "print('{}')", thread=thread, interval_sec=300)
    assert arm(context, "print('{}')", thread=thread, interval_sec=300.0) == first


def test_cancel_remove_keeps_original(context: tuple[Store, FakeSystemd, Path, Path]) -> None:
    """Cancellation and record removal leave the original script and external files."""
    store, manager, _, receipts = context
    watch_id = arm(context, 'print(\'{"state":"wake","summary":"go"}\')')
    original = Path(store.config(watch_id).probe_source)
    with pytest.raises(WakeupError, match="先 cancel"):
        service.remove(store, manager, watch_id)
    service.cancel(store, manager, watch_id)
    run_watch(store, watch_id, manager, threading.Event())
    assert not receipts.exists()
    service.remove(store, manager, watch_id)
    assert not store.directory(watch_id).exists()
    assert original.exists()
    assert list(manager.unit_dir.glob("*@.service"))


@pytest.mark.parametrize(
    "value",
    [
        {"state": "other", "summary": "x"},
        {"state": "wake", "summary": ""},
        {"state": "wait"},
        {"state": "wait", "summary": 1},
        {"state": "wake", "summary": "x", "extra": True},
    ],
)
def test_schema_contract(value: dict[str, object]) -> None:
    """The published two-field schema also governs runtime acceptance."""
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        parse_probe(Result(0, json.dumps(value), ""))
    assert parse_probe(Result(0, '{"state":"wait","summary":""}', ""))["state"] == "wait"


def test_timeout_cleans_descendants(tmp_path: Path) -> None:
    """A timed-out probe cannot leave a child writing later output."""
    marker = tmp_path / "unexpected"
    script = tmp_path / "probe.py"
    script.write_text(
        "import subprocess, sys, time\n"
        + f"subprocess.Popen([sys.executable, '-c', {('import time; from pathlib import Path; time.sleep(0.4); Path(' + repr(str(marker)) + ').touch()')!r}])\n"
        + "time.sleep(5)\n"
    )
    result = execute(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        env={},
        timeout=0.15,
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
    )
    time.sleep(0.5)
    assert result.reason == "timeout"
    assert not marker.exists()


def test_output_limit(tmp_path: Path) -> None:
    """Large output is bounded and treated as an execution error."""
    result = execute(
        [sys.executable, "-c", "print('x' * 100000)"],
        cwd=str(tmp_path),
        env={},
        timeout=2,
        stdout_path=tmp_path / "out",
        stderr_path=tmp_path / "err",
    )
    assert result.reason == "output_limit"
    assert (tmp_path / "out").stat().st_size == 65536


def test_cli_check_forwards_literal_arguments(tmp_path: Path) -> None:
    """The optional check command reports wake without registering a watch."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\nprint(json.dumps({'state': 'wake', 'summary': sys.argv[1]}))\n"
    )
    message = '参数含空格、引号 " 与 $(literal)'
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_wakeup.cli",
            "check",
            "--probe",
            str(probe),
            "--state_dir",
            str(tmp_path / "state"),
            "--json",
            "--",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["result"] == {"state": "wake", "summary": message}
    assert not (tmp_path / "state").exists()


def test_unit_rendering_and_reuse(
    context: tuple[Store, FakeSystemd, Path, Path], tmp_path: Path
) -> None:
    """Dynamic paths are quoted for systemd; unchanged templates are reused."""
    store, manager, _, _ = context
    watch_id = arm(context, "print('{}')")
    config = replace(
        store.config(watch_id),
        project_dir=str(tmp_path / 'space % $ " path'),
        uv_binary=str(tmp_path / "uv executable"),
    )
    manager.prepare(config, store.root)
    content = next(manager.unit_dir.glob("*@.service")).read_text()
    assert '"' + str(tmp_path / "uv executable") + '"' in content
    assert "%%" in content and "$$" in content and '\\"' in content
    assert "--watch_id %i" in content
    before = len(manager.calls)
    manager.prepare(config, store.root)
    assert len(manager.calls) == before
