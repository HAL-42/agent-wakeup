"""Contract tests using real probe, codex, and agentapi subprocesses."""

import json
import os
import subprocess
import sys
import threading
import time
import typing as t
from dataclasses import dataclass, replace
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from agent_wakeup import cli, service
from agent_wakeup.model import Backend, Phase, WakeupError
from agent_wakeup.process import Result, execute, parse_probe
from agent_wakeup.runner import run_watch
from agent_wakeup.store import Store, file_lock
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


@dataclass
class Harness:
    """Isolated watch store, fake service manager, and simulated backend CLIs."""

    store: Store
    manager: FakeSystemd
    bin_dir: Path
    receipts: Path
    agy_control: Path

    def session_env(
        self,
        backend: Backend = "codex",
        *,
        session_id: str | None = None,
        ls_address: str = "localhost:33775",
        csrf_token: str = "secret-csrf-token-1",
    ) -> dict[str, str]:
        """Build an isolated session environment for codex or antigravity."""
        sid = session_id or str(uuid4())
        base = {
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "HOME": str(self.store.root.parent),
        }
        if backend == "codex":
            return base | {
                "CODEX_THREAD_ID": sid,
                "CODEX_HOME": str(self.store.root.parent / "codex"),
            }
        return base | {
            "ANTIGRAVITY_CONVERSATION_ID": sid,
            "ANTIGRAVITY_LS_ADDRESS": ls_address,
            "ANTIGRAVITY_CSRF_TOKEN": csrf_token,
        }

    def set_agy_server(
        self,
        *,
        ls_address: str = "localhost:33775",
        csrf_token: str = "secret-csrf-token-1",
        mode: str = "ok",
        missing_sessions: list[str] | None = None,
    ) -> None:
        """Update the simulated Antigravity language_server state."""
        self.agy_control.write_text(
            json.dumps(
                {
                    "ls_address": ls_address,
                    "csrf_token": csrf_token,
                    "mode": mode,
                    "missing_sessions": missing_sessions or [],
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Provide private state plus simulated codex and agentapi executables."""
    store = Store(tmp_path / "state")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    receipts = tmp_path / "receipts.jsonl"
    agy_control = tmp_path / "agy-server.json"

    codex = bin_dir / "codex"
    codex.write_text(
        dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        from pathlib import Path

        args = sys.argv[1:]
        if args == ['queue', '--help']:
            print('Usage: codex queue --thread <UUID> --message <TEXT>')
            raise SystemExit(0)
        thread = args[args.index('--thread') + 1]
        message = args[args.index('--message') + 1]
        with Path({str(receipts)!r}).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({{'backend': 'codex', 'session': thread, 'message': message}}) + '\\n')
        time.sleep(float(os.environ.get('QUEUE_SLEEP', '0')))
        if os.environ.get('QUEUE_BAD_ACK'):
            print('no confirmed receipt')
        else:
            print(f'Queued message test-message for thread {{thread}}.')
        """),
        encoding="utf-8",
    )
    codex.chmod(0o700)

    agentapi = bin_dir / "agentapi"
    agentapi.write_text(
        dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        from pathlib import Path

        control = json.loads(Path({str(agy_control)!r}).read_text(encoding='utf-8'))
        ls_addr = os.environ.get('ANTIGRAVITY_LS_ADDRESS', '')
        csrf = os.environ.get('ANTIGRAVITY_CSRF_TOKEN', '')
        if not ls_addr:
            print(json.dumps({{'error': 'ANTIGRAVITY_LS_ADDRESS is not set'}}))
            raise SystemExit(1)
        if ls_addr != control['ls_address'] or control['mode'] == 'down':
            print(json.dumps({{
                'response': {{}},
                'error': f'rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp {{ls_addr}}: connect: connection refused"',
            }}))
            raise SystemExit(1)
        if csrf != control['csrf_token']:
            print(json.dumps({{
                'response': {{}},
                'error': 'rpc error: code = Unauthenticated desc = unauthenticated: missing or invalid CSRF token',
            }}))
            raise SystemExit(1)

        args = sys.argv[1:]
        cmd = args[0]
        rest = args[1:]
        if rest[:1] == ['--']:
            rest = rest[1:]

        if cmd == 'get-conversation-metadata':
            sid = rest[0]
            if sid in control['missing_sessions']:
                print(json.dumps({{
                    'response': {{}},
                    'error': f'rpc error: code = NotFound desc = not_found: trajectory not found: {{sid}}',
                }}))
                raise SystemExit(1)
            print(json.dumps({{'response': {{'conversationMetadata': {{'metadata': {{'rootConversationId': sid}}}}}}}}))
            raise SystemExit(0)

        if cmd == 'send-message':
            sid, content = rest[0], rest[1]
            if control['mode'] == 'mid_rpc_drop':
                print(json.dumps({{
                    'response': {{}},
                    'error': 'rpc error: code = Unavailable desc = transport is closing',
                }}))
                raise SystemExit(1)
            with Path({str(receipts)!r}).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({{'backend': 'antigravity', 'session': sid, 'message': content}}) + '\\n')
            time.sleep(float(os.environ.get('QUEUE_SLEEP', '0')))
            if os.environ.get('QUEUE_BAD_ACK'):
                print('malformed non-json output')
                raise SystemExit(0)
            print(json.dumps({{'response': {{'sendMessage': {{'recipientId': sid, 'content': content}}}}}}))
            raise SystemExit(0)

        raise SystemExit(2)
        """),
        encoding="utf-8",
    )
    agentapi.chmod(0o700)

    instance = Harness(
        store, FakeSystemd(store, tmp_path / "units"), bin_dir, receipts, agy_control
    )
    instance.set_agy_server()
    return instance


def arm(
    harness: Harness,
    code: str,
    *,
    key: str = "example",
    backend: Backend = "codex",
    session_id: str | None = None,
    session_env: dict[str, str] | None = None,
    **overrides: t.Any,
) -> str:
    """Register a temporary probe with small test durations."""
    probe = harness.store.root.parent / f"{key}.py"
    probe.write_text(code, encoding="utf-8")
    active_session_env = session_env or harness.session_env(backend, session_id=session_id)
    options = {
        "key": key,
        "probe": probe,
        "interpreter": sys.executable,
        "probe_args": [],
        "cwd": harness.store.root.parent,
        "env": {},
        "session_env": active_session_env,
        "interval_sec": 0.01,
        "probe_timeout_sec": 2,
        "max_wait_sec": 30,
        "queue_timeout_sec": 2,
    } | overrides
    return service.register(harness.store, harness.manager, **options)["watch_id"]


def sequenced_probe(results: list[dict[str, str]]) -> str:
    """Build a probe that advances through results using its own local file."""
    return dedent(f"""\
        import json
        from pathlib import Path

        results = {results!r}
        counter = Path('probe-count')
        index = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(index + 1))
        print(json.dumps(results[index]))
        """)


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
def test_wait_then_wake_preserves_prompt_and_snapshot(harness: Harness, backend: Backend) -> None:
    """Polling is local, literal prompts survive edits, and secrets stay out of probes."""
    prompt = '- 完成！\n请读取 "路径"；`literal` $(touch unwanted) $HOME 与单引号\''
    watch_id = arm(
        harness,
        "import json, os\nfrom pathlib import Path\n"
        + "assert 'ANTIGRAVITY_CSRF_TOKEN' not in os.environ\n"
        + "counter = Path('counter')\ncount = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        + "counter.write_text(str(count))\n"
        + f"print(json.dumps({{'state': 'wake' if count == 3 else 'wait', 'summary': {prompt!r}}}))\n",
        backend=backend,
    )
    Path(harness.store.config(watch_id).probe_source).write_text(
        "raise RuntimeError('changed original')", encoding="utf-8"
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
        patch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
        run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 3, 1)
    assert state.queued_at is not None
    assert (state.queued_message_id is None) == (backend == "antigravity")
    recorded = json.loads(harness.receipts.read_text(encoding="utf-8"))
    assert (recorded["backend"], recorded["message"]) == (backend, prompt)
    assert not (harness.store.root.parent / "unwanted").exists()
    inspected = json.dumps(service.inspect_watch(harness.store, harness.manager, watch_id))
    assert "secret-csrf-token" not in inspected
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert len(harness.receipts.read_text(encoding="utf-8").splitlines()) == 1
    assert ("disable", harness.store.config(watch_id).unit) in harness.manager.calls


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
def test_notifications_continue_until_wake(harness: Harness, backend: Backend) -> None:
    """Each notify sends its literal prompt, including repeats, before the final wake."""
    first = '- 第一阶段完成。\n请分析 "结果"，保留 $(literal)。'
    second = "第二阶段完成，请继续分析。"
    final = "全部完成。"
    watch_id = arm(
        harness,
        sequenced_probe(
            [
                {"state": "wait", "summary": "任务启动"},
                {"state": "notify", "summary": first},
                {"state": "wait", "summary": "第二阶段运行中"},
                {"state": "notify", "summary": first},
                {"state": "notify", "summary": second},
                {"state": "wake", "summary": final},
            ]
        ),
        backend=backend,
    )
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    messages = [
        json.loads(line)["message"]
        for line in harness.receipts.read_text(encoding="utf-8").splitlines()
    ]
    assert messages == [first, first, second, final]
    assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 6, 4)
    assert state.reason == "probe"
    assert harness.manager.calls.count(("disable", harness.store.config(watch_id).unit)) == 1


def test_backend_autodetect_and_validation_errors(harness: Harness, tmp_path: Path) -> None:
    """Registration requires an unambiguous backend, valid session UUID, and live metadata."""
    probe = tmp_path / "probe.py"
    probe.write_text('print(\'{"state":"wait","summary":"ok"}\')\n', encoding="utf-8")
    base_args: dict[str, t.Any] = {
        "key": "check-env",
        "probe": probe,
        "interpreter": sys.executable,
        "probe_args": [],
        "cwd": tmp_path,
        "env": {},
    }
    with pytest.raises(WakeupError, match="--backend"):
        service.register(
            harness.store, harness.manager, session_env={"PATH": os.environ["PATH"]}, **base_args
        )

    both = harness.session_env("codex") | harness.session_env("antigravity")
    with pytest.raises(WakeupError, match="同时检测到"):
        service.register(harness.store, harness.manager, session_env=both, **base_args)

    missing_sid = str(uuid4())
    harness.set_agy_server(missing_sessions=[missing_sid])
    with pytest.raises(WakeupError, match="NotFound"):
        service.register(
            harness.store,
            harness.manager,
            session_env=harness.session_env("antigravity", session_id=missing_sid),
            **base_args,
        )
    assert not harness.store.root.exists()


@pytest.mark.parametrize(
    ("server_mode", "expected_phase"),
    [("down", "delivery_failed"), ("mid_rpc_drop", "delivery_unknown")],
)
def test_antigravity_error_classification_and_retry_refresh(
    harness: Harness, server_mode: str, expected_phase: str
) -> None:
    """Dial/auth rejections fail cleanly; retry_delivery refreshes rotated credentials."""
    sid = str(uuid4())
    watch_id = arm(
        harness,
        sequenced_probe(
            [
                {"state": "notify", "summary": "阶段一结果"},
                {"state": "wake", "summary": "最终结果"},
            ]
        ),
        backend="antigravity",
        session_id=sid,
    )
    harness.set_agy_server(
        ls_address="localhost:45123", csrf_token="rotated-token", mode=server_mode
    )
    if server_mode == "mid_rpc_drop":
        # Allow connection so mid-RPC drop occurs during send-message.
        harness.set_agy_server(
            ls_address="localhost:33775", csrf_token="secret-csrf-token-1", mode="mid_rpc_drop"
        )
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert state.phase == expected_phase
    assert (state.checks, state.delivery_attempts, state.summary) == (1, 1, "阶段一结果")

    harness.set_agy_server(ls_address="localhost:45123", csrf_token="rotated-token", mode="ok")
    new_env = harness.session_env(
        "antigravity", session_id=sid, ls_address="localhost:45123", csrf_token="rotated-token"
    )
    service.retry_delivery(harness.store, harness.manager, watch_id, session_env=new_env)
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 2, 3)
    assert [
        json.loads(line)["message"]
        for line in harness.receipts.read_text(encoding="utf-8").splitlines()
    ] == ["阶段一结果", "最终结果"]


def test_rebind_and_idempotent_register_across_server_restart(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Server port rotation does not break fingerprint reuse, and rebind updates active watches."""
    sid_ok, sid_gone = str(uuid4()), str(uuid4())
    code = sequenced_probe(
        [{"state": "wait", "summary": "waiting"}, {"state": "wake", "summary": "done"}]
    )
    watch_ok = arm(harness, code, key="ok-watch", backend="antigravity", session_id=sid_ok)
    watch_gone = arm(harness, code, key="gone-watch", backend="antigravity", session_id=sid_gone)
    before_fp = harness.store.config(watch_ok).fingerprint

    # Rotate server credentials and drop sid_gone.
    harness.set_agy_server(
        ls_address="localhost:55000",
        csrf_token="token-after-restart",
        missing_sessions=[sid_gone],
    )
    rotated_env = harness.session_env(
        "antigravity",
        session_id=sid_ok,
        ls_address="localhost:55000",
        csrf_token="token-after-restart",
    )

    # Idempotent register with same key/session succeeds and refreshes connection.json.
    reused = service.register(
        harness.store,
        harness.manager,
        key="ok-watch",
        probe=Path(harness.store.config(watch_ok).probe_source),
        interpreter=sys.executable,
        probe_args=[],
        cwd=harness.store.root.parent,
        env={},
        session_env=rotated_env,
        interval_sec=0.01,
        probe_timeout_sec=2,
        max_wait_sec=30,
        queue_timeout_sec=2,
    )
    assert reused["watch_id"] == watch_ok and reused["reused"] is True
    assert harness.store.config(watch_ok).fingerprint == before_fp

    # Bulk CLI rebind updates watch_ok, reports failure for watch_gone, and exits 1.
    for k, v in rotated_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(cli, "get_manager", lambda: harness.manager)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent-wakeup", "rebind", "--all", "--state_dir", str(harness.store.root), "--json"],
    )
    assert cli.main() == 1
    bulk = json.loads(capsys.readouterr().out)
    assert (bulk["total"], bulk["rebound"], bulk["failed"]) == (2, 1, 1)
    assert next(item["watch_id"] for item in bulk["results"] if not item["rebound"]) == watch_gone

    # watch_ok now delivers cleanly against the rotated server without restarting its unit first.
    run_watch(harness.store, watch_ok, harness.manager, threading.Event())
    assert harness.store.state(watch_ok).phase == "queued"
    assert json.loads(harness.receipts.read_text(encoding="utf-8"))["session"] == sid_ok

    # Finished watches reject rebind.
    with pytest.raises(WakeupError, match="已结束"):
        service.rebind(harness.store, watch_ok, session_env=rotated_env)


@pytest.mark.parametrize("failure", ["missing", "no_ack"])
def test_codex_notification_retry_offline_preserves_saved_connection(
    harness: Harness, failure: str
) -> None:
    """Offline retry_delivery reuses the persisted connection when session env is absent."""
    watch_id = arm(
        harness,
        sequenced_probe(
            [
                {"state": "notify", "summary": "分析阶段结果"},
                {"state": "wake", "summary": "处理最终结果"},
            ]
        ),
    )
    codex = harness.bin_dir / "codex"
    healthy_codex = codex.read_bytes()
    if failure == "missing":
        codex.unlink()
    else:
        codex.write_text(f"#!{sys.executable}\nprint('no confirmed receipt')\n", encoding="utf-8")
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert harness.store.state(watch_id).phase == (
        "delivery_failed" if failure == "missing" else "delivery_unknown"
    )

    codex.write_bytes(healthy_codex)
    codex.chmod(0o700)
    service.retry_delivery(harness.store, harness.manager, watch_id, session_env={})
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 2, 3)


def test_notification_keeps_deadline(harness: Harness) -> None:
    """Notifying does not extend the deadline or turn its final message into a notification."""
    watch_id = arm(
        harness,
        'print(\'{"state":"notify","summary":"请检查任务状态"}\')',
        interval_sec=5,
        max_wait_sec=1,
    )
    deadline = harness.store.config(watch_id).deadline
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert (state.phase, state.reason, state.checks) == ("queued", "deadline", 1)
    assert harness.store.config(watch_id).deadline == deadline
    messages = [
        json.loads(line)["message"]
        for line in harness.receipts.read_text(encoding="utf-8").splitlines()
    ]
    assert len(messages) == 2 and messages[0] == "请检查任务状态" and "等待期限" in messages[1]


@pytest.mark.parametrize(
    "code",
    [
        "raise RuntimeError('network unavailable')",
        "print('invalid json')",
        "import time; time.sleep(3)",
    ],
)
def test_probe_errors_notify_once(harness: Harness, code: str) -> None:
    """Repeated execution failures send one diagnostic instead of a success prompt."""
    watch_id = arm(harness, code, max_probe_errors=2, probe_timeout_sec=0.1)
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    state = harness.store.state(watch_id)
    assert (state.phase, state.reason, state.checks) == ("queued", "probe_error", 2)
    assert "连续检查失败" in json.loads(harness.receipts.read_text(encoding="utf-8"))["message"]


def test_deadline_survives_restart(harness: Harness) -> None:
    """An elapsed persisted deadline wakes without executing the probe again."""
    watch_id = arm(harness, "raise AssertionError('must not run')", max_wait_sec=0.01)
    deadline = harness.store.config(watch_id).deadline
    time.sleep(0.02)
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert harness.store.config(watch_id).deadline == deadline
    assert harness.store.state(watch_id).checks == 0
    assert harness.store.state(watch_id).reason == "deadline"


@pytest.mark.parametrize("reason", ["", "probe_notify"])
def test_interrupted_delivery_is_not_replayed(harness: Harness, reason: str) -> None:
    """A persisted sending state is ambiguous after a runner crash."""
    watch_id = arm(
        harness,
        'print(\'{"state":"wake","summary":"finished"}\')'
        if reason == "probe_notify"
        else "raise AssertionError('must not run')",
    )
    with harness.store.edit(watch_id) as state:
        state.phase, state.summary, state.reason = "sending", "saved prompt", reason
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert harness.store.state(watch_id).phase == "delivery_unknown"
    assert not harness.receipts.exists()
    service.retry_delivery(harness.store, harness.manager, watch_id, session_env={})
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert harness.store.state(watch_id).phase == "queued"
    assert harness.store.state(watch_id).checks == (1 if reason == "probe_notify" else 0)
    with pytest.raises(WakeupError):
        service.retry_delivery(harness.store, harness.manager, watch_id, session_env={})


@pytest.mark.parametrize("backend", ["codex", "antigravity"])
@pytest.mark.parametrize("env_override", [{"QUEUE_SLEEP": "1"}, {"QUEUE_BAD_ACK": "1"}])
@pytest.mark.parametrize("probe_state", ["wake", "notify"])
def test_uncertain_receipt_stops_without_retry(
    harness: Harness, backend: Backend, env_override: dict[str, str], probe_state: str
) -> None:
    """Timeout and malformed receipts never cause an automatic duplicate."""
    watch_id = arm(
        harness,
        sequenced_probe([{"state": probe_state, "summary": "go"}]),
        backend=backend,
        env=env_override,
        queue_timeout_sec=0.2,
    )
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert harness.store.state(watch_id).phase == "delivery_unknown"
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert len(harness.receipts.read_text(encoding="utf-8").splitlines()) == 1


def test_duplicate_registration_and_conflict(harness: Harness) -> None:
    """A backend/session/key triple identifies one immutable logical registration."""
    sid = str(uuid4())
    code = 'print(\'{"state":"wait","summary":"waiting"}\')'
    watch_id = arm(harness, code, session_id=sid, interval_sec=300)
    assert arm(harness, code, session_id=sid, interval_sec=300.0) == watch_id
    with pytest.raises(WakeupError, match="不同配置"):
        arm(harness, code, session_id=sid, interval_sec=1)
    # Same UUID/key under the other backend does not collide.
    other_id = arm(harness, code, backend="antigravity", session_id=sid, interval_sec=300)
    assert other_id != watch_id
    assert len(harness.store.ids()) == 2


def test_cancel_remove_keeps_original(harness: Harness) -> None:
    """Cancellation and record removal leave the original script and external files."""
    watch_id = arm(harness, 'print(\'{"state":"wake","summary":"go"}\')')
    original = Path(harness.store.config(watch_id).probe_source)
    with pytest.raises(WakeupError, match="先 cancel"):
        service.remove(harness.store, harness.manager, watch_id)
    service.cancel(harness.store, harness.manager, watch_id)
    run_watch(harness.store, watch_id, harness.manager, threading.Event())
    assert not harness.receipts.exists()
    service.remove(harness.store, harness.manager, watch_id)
    assert not harness.store.directory(watch_id).exists()
    assert original.exists()
    assert list(harness.manager.unit_dir.glob("*@.service"))


def test_doctor_reports_both_backends_without_leaking_token(harness: Harness) -> None:
    """Doctor validates active Antigravity sessions and omits secret token values."""
    agy_env = harness.session_env("antigravity")
    report = cli.doctor(harness.store, harness.manager, session_env=agy_env)
    assert report["detected_backend"] == "antigravity"
    assert report["backends"]["antigravity"]["session_reachable"] is True
    assert report["backends"]["codex"]["ready"] is True
    assert "secret-csrf-token" not in json.dumps(report)

    bad_env = agy_env | {"ANTIGRAVITY_CSRF_TOKEN": "wrong"}
    bad_report = cli.doctor(harness.store, harness.manager, session_env=bad_env)
    assert bad_report["backends"]["antigravity"]["ready"] is False
    assert bad_report["healthy"] is False


@pytest.mark.parametrize(
    "value",
    [
        {"state": "other", "summary": "x"},
        {"state": "wake", "summary": ""},
        {"state": "notify", "summary": ""},
        {"state": "wait"},
        {"state": "wait", "summary": 1},
        {"state": "wake", "summary": "x", "extra": True},
        {"state": "notify", "summary": "x", "event_id": "stage-1"},
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
        + "time.sleep(5)\n",
        encoding="utf-8",
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
    assert result.reason == "timeout" and not marker.exists()


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


@pytest.mark.parametrize("probe_state", ["wake", "notify"])
def test_cli_check_forwards_literal_arguments(tmp_path: Path, probe_state: str) -> None:
    """The optional check command reports results without registering or sending messages."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        + f"print(json.dumps({{'state': {probe_state!r}, 'summary': sys.argv[1]}}))\n",
        encoding="utf-8",
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
    assert json.loads(result.stdout)["result"] == {"state": probe_state, "summary": message}
    assert not (tmp_path / "state").exists()


def test_unit_rendering_and_reuse(harness: Harness, tmp_path: Path) -> None:
    """Dynamic paths are quoted for systemd; unchanged templates are reused."""
    watch_id = arm(harness, "print('{}')")
    config = replace(
        harness.store.config(watch_id),
        project_dir=str(tmp_path / 'space % $ " path'),
        uv_binary=str(tmp_path / "uv executable"),
    )
    harness.manager.prepare(config, harness.store.root)
    content = next(harness.manager.unit_dir.glob("*@.service")).read_text(encoding="utf-8")
    assert '"' + str(tmp_path / "uv executable") + '"' in content
    assert "%%" in content and "$$" in content and '\\"' in content
    assert "--watch_id %i" in content
    before = len(harness.manager.calls)
    harness.manager.prepare(config, harness.store.root)
    assert len(harness.manager.calls) == before


@pytest.fixture
def watches_by_phase(harness: Harness) -> dict[str, str]:
    """Save all lifecycle phases without running probes or sending messages."""
    watches = {}
    for phase in t.get_args(Phase):
        watch_id = arm(harness, "raise AssertionError('probe must not run')", key=phase)
        with harness.store.edit(watch_id) as state:
            state.phase = phase
            state.summary = f"saved message for {phase}"
            state.reason = "probe"
        watches[phase] = watch_id
    return watches


@pytest.mark.parametrize(
    ("category", "phases"),
    [
        (None, set(t.get_args(Phase))),
        ("delivery_error", {"delivery_failed", "delivery_unknown"}),
        ("active", {"waiting", "triggered", "sending"}),
        ("finished", {"queued", "cancelled"}),
    ],
)
def test_cli_list_categories_use_only_saved_phases(
    harness: Harness,
    watches_by_phase: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    category: str | None,
    phases: set[str],
) -> None:
    """Filtering works even when services are inactive and never queries the manager."""
    argv = ["agent-wakeup", "list", "--state_dir", str(harness.store.root), "--json"]
    if category is not None:
        argv += ["--category", category]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "get_manager", lambda: harness.manager)
    harness.manager.calls.clear()

    assert cli.main() == 0
    records = json.loads(capsys.readouterr().out)
    assert {record["watch_id"] for record in records} == {
        watches_by_phase[phase] for phase in phases
    }
    assert all(record["backend"] == "codex" and record["session_id"] for record in records)
    assert harness.manager.calls == []
    assert not harness.receipts.exists()


@pytest.mark.parametrize("all_watches", [False, True])
def test_cli_retry_preserves_messages_and_excludes_other_phases(
    harness: Harness,
    watches_by_phase: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    all_watches: bool,
) -> None:
    """Single and bulk retries send saved messages once and leave other watches intact."""
    for var in (
        "ANTIGRAVITY_LS_ADDRESS",
        "ANTIGRAVITY_CSRF_TOKEN",
        "CODEX_THREAD_ID",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    phases = {"delivery_failed", "delivery_unknown"} if all_watches else {"delivery_failed"}
    selected = {watches_by_phase[phase] for phase in phases}
    before = {watch_id: harness.store.state(watch_id) for watch_id in harness.store.ids()}
    deadlines = {watch_id: harness.store.config(watch_id).deadline for watch_id in selected}
    target = ["--all"] if all_watches else [watches_by_phase["delivery_failed"]]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-wakeup",
            "retry_delivery",
            *target,
            "--state_dir",
            str(harness.store.root),
            "--json",
        ],
    )
    monkeypatch.setattr(cli, "get_manager", lambda: harness.manager)

    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    if all_watches:
        assert (result["total"], result["started"], result["failed"]) == (2, 2, 0)
        assert {item["watch_id"] for item in result["results"]} == selected
    else:
        assert result == {"watch_id": target[0], "phase": "triggered"}

    for watch_id in selected:
        run_watch(harness.store, watch_id, harness.manager, threading.Event())
        state = harness.store.state(watch_id)
        assert (state.phase, state.checks, state.delivery_attempts) == ("queued", 0, 1)
        assert state.summary == before[watch_id].summary
        assert harness.store.config(watch_id).deadline == deadlines[watch_id]
    for watch_id in set(harness.store.ids()) - selected:
        assert harness.store.state(watch_id) == before[watch_id]


def test_retry_all_does_not_take_over_a_locked_watch(
    harness: Harness, watches_by_phase: dict[str, str]
) -> None:
    """An occupied watch is reported without changing it or blocking other retries."""
    watch_id = watches_by_phase["delivery_unknown"]
    before = harness.store.state(watch_id)
    with file_lock(harness.store.directory(watch_id) / "run.lock"):
        result = service.retry_all_deliveries(harness.store, harness.manager, session_env={})
    assert (result["total"], result["started"], result["failed"]) == (2, 1, 1)
    assert harness.store.state(watch_id) == before
    assert "占用" in next(item["error"] for item in result["results"] if not item["started"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["retry_delivery"],
        ["retry_delivery", str(uuid4()), "--all"],
        ["rebind"],
        ["rebind", str(uuid4()), "--all"],
        ["list", "--category", "running"],
        ["register", "--key", "x", "--probe", "p.py", "--thread_id", str(uuid4())],
    ],
)
def test_cli_rejects_invalid_or_removed_arguments(arguments: list[str]) -> None:
    """Callers must select valid targets, categories, and current option names."""
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["list", "--category", "delivery_error"],
        ["retry_delivery", "--all"],
        ["rebind", "--all", "--backend", "antigravity"],
    ],
)
def test_cli_empty_state_directory_is_a_successful_noop(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    """Empty listings, batch retries, and batch rebinds succeed without creating records."""
    monkeypatch.setattr(cli, "get_manager", lambda: harness.manager)
    monkeypatch.setattr(
        sys, "argv", ["agent-wakeup", *arguments, "--state_dir", str(harness.store.root)]
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    if arguments[0] == "list":
        assert result == []
    elif arguments[0] == "retry_delivery":
        assert result == {"total": 0, "started": 0, "failed": 0, "results": []}
    else:
        assert result == {
            "backend": "antigravity",
            "total": 0,
            "rebound": 0,
            "failed": 0,
            "results": [],
        }
    assert harness.manager.calls == []
    assert not harness.store.root.exists()
