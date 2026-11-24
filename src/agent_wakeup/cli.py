"""Command-line management for agent-wakeup."""

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import typing as t
from dataclasses import asdict
from pathlib import Path
from textwrap import dedent

from jsonschema import ValidationError
from loguru import logger

from . import service
from .delivery import backend_diagnostics, detect_backend
from .managers import ServiceManager, get_manager
from .model import BACKENDS, CATEGORY_PHASES, Backend, WakeupError
from .process import execute, parse_probe
from .runner import run_watch
from .store import Store


def positive(value: str) -> float:
    """Parse a finite positive duration."""
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("请输入大于 0 的有限数值。")
    return number


def positive_integer(value: str) -> int:
    """Parse a positive count."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("请输入正整数。")
    return number


def environment(values: list[str]) -> dict[str, str]:
    """Parse explicit NAME=VALUE environment overrides."""
    result = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ}
    for value in values:
        name, separator, content = value.partition("=")
        if not separator or not name or "\0" in value:
            raise WakeupError("--env 使用 NAME=VALUE 格式。")
        result[name] = content
    return result


def parser() -> argparse.ArgumentParser:
    """Build help as the authoritative reference for command options."""
    root = argparse.ArgumentParser(
        prog="agent-wakeup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=dedent("""\
            让 Agent 把长时间等待交给后台进程，条件满足后自动回到原会话继续工作。

            Agent 编写一个每次只检查一次条件的 probe，再用 register 注册监控。
            运行器通过本机的用户后台服务定期执行 probe；需要唤醒时，将 probe 返回的
            summary 发送到目标 Codex 或 Antigravity 会话。等待期间的轮询无需调用模型。
            probe 可选择发送中间通知后继续监控，或在最终唤醒后结束监控。
            Linux 使用 systemd，macOS 使用 launchd。
            """),
        epilog=dedent("""\
            从本地项目目录开始：
              ./%(prog)s register --key report-ready --probe /path/to/probe.py
              ./%(prog)s list
              ./%(prog)s status <watch_id>

            register 返回监控 ID 后，Agent 可以结束当前轮次。
            保持目标 Agent 服务运行，后台消息到达后便可唤醒会话继续工作。

            用法说明：
              ./%(prog)s <命令> --help  查看该命令的详细行为、参数和示例。
              每个命令都可用 --state_dir 选择记录目录，用 --json 输出单行 JSON。
              首次 register 自动准备用户服务配置；Python 和依赖由 uv 管理。
            """),
    )
    commands = root.add_subparsers(dest="command", title="命令", metavar="COMMAND", required=True)
    descriptions = {
        "register": (
            "保存 probe 和目标会话，启动后台监控，并返回可查询的监控 ID。",
            """\
            将一个 probe 注册为后台监控，供 Agent 结束当前轮次后继续等待条件。
            命令默认根据当前会话环境识别后端（codex 或 antigravity）并捕获连接信息，
            保存脚本副本、逻辑配置与私有连接，准备本机的用户服务并启动运行器。
            返回结果包括 watch_id、key、backend、session_id、服务名称、当前状态和记录目录。

            新监控启动后先检查一次。probe 的 state 决定本次如何处理：
              wait    保存检查结果，等待下一次检查。
              notify  原样发送 summary，消息入队成功后继续检查。
              wake    原样发送 summary，消息入队成功后结束监控。

            每次返回 notify 都会发送一条消息，通知频率和重复提醒由 probe 自己控制。
            通知成功后按原计划检查，无需等待 Agent 处理完消息，总等待期限也保持不变。

            同一后端和会话中，相同 key 和相同逻辑配置会复用原监控，输出 reused=true，
            并在监控尚未结束时用当前环境连接刷新投递配置。开始新一轮监控，或更换脚本、
            参数时，使用新的 key。
            """,
            """\
            示例：
              ./%(prog)s --key report-ready --probe /path/to/probe.py
              ./%(prog)s --key export-ready --probe /path/to/probe.py --interval_sec 600
              ./%(prog)s --key build-ready --probe /path/to/probe.sh --interpreter bash
              ./%(prog)s --key job-ready --probe /path/to/probe.py -- --job_id example-123

            -- 后面的参数原样交给 probe。其余选项应放在 -- 之前。
            注册成功后报告 watch_id 并结束当前轮次，等待后台消息唤醒原会话。
            """,
        ),
        "check": (
            "执行一次 probe 并验证输出，供编写或调试检查脚本时使用。",
            """\
            使用指定的解释器、工作目录和环境变量运行一次 probe，检查退出码和 JSON 格式。
            成功时返回 probe 的 state、summary、运行耗时，以及写入 stderr 的诊断信息。

            这是一次性的脚本检查。它不注册监控或启动后台服务，也不会向会话发送消息。
            wait、notify 和 wake 三种结果都会直接显示，检查完成后命令退出。
            可按需要用它调试 probe。
            """,
            """\
            示例：
              ./%(prog)s --probe /path/to/probe.py
              ./%(prog)s --probe /path/to/probe.py --cwd /path/to/work --env API_URL=https://example.com
              ./%(prog)s --probe /path/to/probe.py -- --job_id example-123

            检查成功返回退出码 0；脚本执行失败、超时或输出不符合协议时返回 1。
            probe 输出格式见项目中的 src/agent_wakeup/schema.json。
            """,
        ),
        "doctor": (
            "诊断本机是否具备运行条件，帮助定位命令或用户服务的环境问题。",
            """\
            检查 uv、用户服务管理器以及 Codex / Antigravity 投递环境是否可用。
            Linux 报告 systemd 用户管理器和 linger 设置；macOS 报告当前用户的
            launchd 图形登录域和 LaunchAgents 目录。

            未指定 --backend 时，若当前处于某个 Agent 会话则按该后端判定 healthy，
            否则只要至少一个后端工具可用即判定就绪。本命令不修改系统配置。
            """,
            """\
            示例：
              ./%(prog)s
              ./%(prog)s --backend antigravity --json
            """,
        ),
        "list": (
            "列出保存的监控记录，可按投递异常、监控中或已结束筛选。",
            """\
            列出所选状态目录中的全部监控，包括正在等待的监控和已经结束的记录。
            每项结果包含 watch_id、key、backend、session_id、当前状态和最近一次检查摘要。

            --category 只根据记录中的 phase 筛选：
              delivery_error  投递异常：delivery_failed、delivery_unknown。
              active          监控中：waiting、triggered、sending。
              finished        已结束或取消：queued、cancelled。
            不指定分类时列出全部记录。本命令只读取本地记录，不查询后台服务状态。
            """,
            """\
            示例：
              ./%(prog)s
              ./%(prog)s --category delivery_error
              ./%(prog)s --category active
              ./%(prog)s --category finished
              ./%(prog)s --state_dir /path/to/wakeup-state --json
            """,
        ),
        "status": (
            "查看一个监控的检查结果、消息投递结果和后台服务状态。",
            """\
            查看指定 watch_id 的详细记录，以及对应后台服务的当前状态。
            结果包括目标后端与会话 ID、脚本来源、工作目录、等待期限、检查次数、
            最近摘要、最近错误和消息投递回执，不暴露环境变量或连接凭据。
            """,
            """\
            示例：
              ./%(prog)s <watch_id>
              ./%(prog)s <watch_id> --json

            phase 的含义：
              waiting           正在检查条件，或等待下一次检查。
              triggered         唤醒消息已保存，准备发送。
              sending           正在执行消息投递命令。
              queued            用于结束监控的消息已被接受，监控结束。
              cancelled         监控已被取消，记录保留。
              delivery_failed   投递被明确拒绝或命令未能启动，监控已暂停，排除原因后可安全重试。
              delivery_unknown  无法确认投递结果，监控已暂停，需要结合日志和目标会话判断。
            """,
        ),
        "rebind": (
            "用当前会话环境刷新未结束监控的投递连接（例如 Antigravity 服务重启后）。",
            """\
            当 Agent 宿主服务重启导致连接端口或鉴权凭据更新时，在新的会话环境中执行本命令，
            可将未结束监控（waiting、triggered、delivery_failed、delivery_unknown）的
            connection.json 更新为当前环境连接，并预先校验目标会话可达。

            处于 waiting 状态的监控更新连接时无需重启后台服务，下次触发通知会自动使用新连接；
            处于投递异常状态的监控也可以直接执行 retry_delivery 自动完成连接刷新与重发。
            """,
            """\
            示例：
              ./%(prog)s <watch_id>
              ./%(prog)s --all
              ./%(prog)s --all --backend antigravity --json
            """,
        ),
        "cancel": (
            "提前停止后台监控及 probe 子进程，保留已有记录供后续查看。",
            """\
            在不再需要等待时，停止对应的后台服务、正在运行的 probe 及其子进程。
            尚未开始投递的监控会标记为 cancelled；已经结束的记录保留原有状态。

            取消的对象是本地监控。被检查的训练、转换或其他外部任务照常运行，已经
            发送到会话的消息也会保留。消息正在投递时，可先用 status 查看投递结果。
            """,
            """\
            示例：
              ./%(prog)s <watch_id>

            记录会继续出现在 list 和 status 中。需要清理记录时，再使用 remove。
            """,
        ),
        "retry_delivery": (
            "重试一个或全部投递异常的监控，重新发送已保存的消息。",
            """\
            用于状态为 delivery_failed 或 delivery_unknown 的监控。
            指定 watch_id 重试一项；--all 重试所选状态目录中这两类异常监控。
            若在具备对应会话连接变量的环境中执行（如新的 Antigravity 会话内），
            会自动用当前连接刷新 connection.json 后再重新启动服务发送已保存的 summary：
              notify  入队成功后恢复检查，沿用原来的检查计划和总等待期限。
              wake    入队成功后结束监控。

            delivery_unknown 表示缺少明确回执，消息可能已经到达，重试可能重复发送。
            可先用 list --category delivery_error 查看将重试的记录。
            """,
            """\
            示例：
              ./agent-wakeup status <watch_id>
              ./%(prog)s <watch_id>
              ./%(prog)s --all
              ./%(prog)s --all --state_dir /path/to/wakeup-state --json
            """,
        ),
        "remove": (
            "清理已结束监控的配置、连接、probe 副本、输出和投递记录。",
            """\
            删除一个已经结束的监控。命令会停止并禁用对应的服务实例，然后删除
            本工具保存的配置、连接、probe 副本、最近输出和投递回执。
            正在等待或投递的监控应先结束；需要提前停止等待时使用 cancel。
            """,
            """\
            示例：
              ./%(prog)s <watch_id>
            """,
        ),
        "run": (
            "运行或恢复一个监控实例的后台循环，供本机后台服务调用。",
            """\
            读取已有监控的配置、连接和状态，执行检查、等待以及消息发送。
            命令会持续运行，直到监控结束或进程收到停止信号。
            """,
            """\
            服务调用形式：
              ./%(prog)s --watch_id <watch_id> --state_dir /path/to/wakeup-state
            """,
        ),
    }
    for name, (summary, description, examples) in descriptions.items():
        command = commands.add_parser(
            name,
            help=summary,
            description=dedent(description),
            epilog=dedent(examples),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        command.add_argument(
            "--state_dir",
            type=Path,
            default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "agent-wakeup",
            help="保存监控记录的目录。 管理已有监控时使用注册时的同一目录。 默认：%(default)s。",
        )
        command.add_argument(
            "--json",
            action="store_true",
            help="将结果输出为单行 JSON，便于其他脚本读取。默认输出带缩进的 JSON。",
        )
        if name in {"status", "cancel", "remove"}:
            command.add_argument(
                "watch_id", help="监控的 UUID，可从 register 的返回结果或 list 中获取。"
            )
        if name == "list":
            command.add_argument(
                "--category",
                choices=tuple(CATEGORY_PHASES),
                help="按 phase 分类筛选：delivery_error 投递异常，active 监控中，finished 已结束或取消。",
            )
        if name == "doctor":
            command.add_argument(
                "--backend",
                choices=BACKENDS,
                help="指定要重点校验就绪状态的后端（codex 或 antigravity）。",
            )
        if name in {"retry_delivery", "rebind"}:
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("watch_id", nargs="?", help="目标监控 UUID，与 --all 二选一。")
            target.add_argument(
                "--all",
                dest="all_watches",
                action="store_true",
                help=(
                    "批量重试所选状态目录中的 delivery_failed 和 delivery_unknown。"
                    if name == "retry_delivery"
                    else "批量刷新所选状态目录中当前后端全部未结束监控的连接。"
                ),
            )
            if name == "rebind":
                command.add_argument(
                    "--backend",
                    choices=BACKENDS,
                    help="目标后端；省略时从当前会话环境自动识别。",
                )
        if name == "run":
            command.add_argument("--watch_id", required=True, help="要运行的已有监控 UUID。")
        if name in {"check", "register"}:
            probe_options = command.add_argument_group("probe 的运行方式")
            probe_options.add_argument(
                "--probe",
                type=Path,
                required=True,
                help="每次执行只检查一次条件的脚本路径。register 会保存脚本副本，check 执行原文件。",
            )
            probe_options.add_argument(
                "--interpreter",
                default=sys.executable,
                help="执行 probe 的解释器。 默认使用本工具 uv 环境中的 Python，也可指定其他 Python 或 Bash。",
            )
            probe_options.add_argument(
                "--cwd",
                type=Path,
                default=Path.cwd(),
                help="probe 的工作目录；注册监控后，消息投递命令也使用此目录。 默认是调用时的当前目录。",
            )
            probe_options.add_argument(
                "--env",
                action="append",
                default=[],
                metavar="NAME=VALUE",
                help=(
                    "给执行命令设置环境变量，可多次传入，例如 --env API_URL=https://example.com。 "
                    + (
                        "注册时会保存 PATH、LANG、LC_ALL 及这些显式值，供后台使用。"
                        if name == "register"
                        else "这些值会覆盖本次检查的当前环境。"
                    )
                ),
            )
            probe_options.add_argument(
                "--probe_timeout_sec",
                type=positive,
                default=120,
                help="单次检查最多允许的秒数，超时算作执行错误。 默认 %(default)s 秒；stdout 和 stderr 各最多 64 KiB。",
            )
            command.add_argument(
                "probe_args",
                nargs=argparse.REMAINDER,
                help="在 -- 后填写，作为参数原样传给 probe。",
            )
        if name == "register":
            target_options = command.add_argument_group("监控名称与目标会话")
            target_options.add_argument(
                "--key",
                required=True,
                help="监控在目标会话中的名称，用于识别重复注册，例如 report-ready。",
            )
            target_options.add_argument(
                "--backend",
                choices=BACKENDS,
                help="目标 Agent 后端（codex 或 antigravity）。省略时根据当前会话环境变量自动识别。",
            )
            target_options.add_argument(
                "--session_id",
                help="目标会话 UUID。省略时按后端分别读取 CODEX_THREAD_ID 或 ANTIGRAVITY_CONVERSATION_ID。",
            )
            timing_options = command.add_argument_group("检查间隔与结束条件")
            timing_options.add_argument(
                "--interval_sec",
                type=positive,
                default=300,
                help="一次有效检查结束后，到下一次检查开始前的等待秒数。默认 %(default)s 秒。",
            )
            timing_options.add_argument(
                "--max_wait_sec",
                type=positive,
                default=604800,
                help="从首次注册起允许等待的总秒数。 到期后向会话发送说明消息；服务重启沿用原期限。 默认 %(default)s 秒。",
            )
            timing_options.add_argument(
                "--max_probe_errors",
                type=positive_integer,
                default=5,
                help="超时、非零退出码或无效 JSON 均计为执行错误。 连续错误达到此数后，发送异常说明并停止检查。 错误时逐步延长重试间隔，有效结果会清零计数。 默认：%(default)s。",
            )
            timing_options.add_argument(
                "--queue_timeout_sec",
                type=positive,
                default=60,
                help="消息投递命令最多允许的秒数。 没有明确回执时保留 delivery_unknown 记录，供核对和重试。 默认 %(default)s 秒。",
            )
    return root


def doctor(
    store: Store,
    manager: ServiceManager,
    *,
    backend: Backend | None = None,
    session_env: t.Mapping[str, str] | None = None,
) -> dict[str, t.Any]:
    """Collect local service and backend capabilities without sending messages."""
    active_env = os.environ if session_env is None else session_env
    detected = detect_backend(active_env)
    backends = backend_diagnostics(active_env)
    target = backend or detected
    if target is not None:
        backend_ok = bool(backends[target]["ready"])
    else:
        backend_ok = bool(backends["codex"]["ready"] or backends["antigravity"]["binary"])
    checks: dict[str, t.Any] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "uv": shutil.which("uv"),
        "project_dir": str(service.PROJECT_DIR),
        "state_dir": str(store.root),
        "detected_backend": detected,
        "backends": backends,
    }
    checks.update(manager.diagnostics())
    checks["healthy"] = bool(checks["uv"] and checks.get("user_manager") and backend_ok)
    return checks


def dispatch(
    args: argparse.Namespace, store: Store, manager: ServiceManager
) -> dict[str, t.Any] | list[dict[str, t.Any]]:
    """Execute a parsed command and return its structured result."""
    if args.command in {"check", "register"}:
        probe_args = args.probe_args[1:] if args.probe_args[:1] == ["--"] else args.probe_args
        env = environment(args.env)
    if args.command == "register":
        return service.register(
            store,
            manager,
            key=args.key,
            probe=args.probe,
            interpreter=args.interpreter,
            probe_args=probe_args,
            cwd=args.cwd,
            env=env,
            backend=args.backend,
            session_id=args.session_id,
            interval_sec=args.interval_sec,
            probe_timeout_sec=args.probe_timeout_sec,
            max_wait_sec=args.max_wait_sec,
            queue_timeout_sec=args.queue_timeout_sec,
            max_probe_errors=args.max_probe_errors,
        )
    if args.command == "check":
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="agent-wakeup-check-") as name:
            result = execute(
                [service.executable(args.interpreter), str(args.probe.resolve()), *probe_args],
                cwd=str(args.cwd.resolve()),
                env=env,
                timeout=args.probe_timeout_sec,
                stdout_path=Path(name) / "stdout",
                stderr_path=Path(name) / "stderr",
            )
            value = parse_probe(result)
            return {
                "result": value,
                "duration_sec": round(time.monotonic() - started, 3),
                "stderr": result.stderr,
            }
    if args.command == "doctor":
        return doctor(store, manager, backend=args.backend)
    if args.command == "list":
        records = []
        for watch_id in store.ids():
            state = store.state(watch_id)
            if args.category is not None and state.phase not in CATEGORY_PHASES[args.category]:
                continue
            config = store.config(watch_id)
            records.append(
                {
                    "watch_id": watch_id,
                    "key": config.key,
                    "backend": config.backend,
                    "session_id": config.session_id,
                    **asdict(state),
                }
            )
        return records
    if args.command == "status":
        return service.inspect_watch(store, manager, args.watch_id)
    if args.command == "rebind":
        if args.all_watches:
            return service.rebind_all(store, backend=args.backend)
        return service.rebind(store, args.watch_id, backend=args.backend)
    if args.command == "cancel":
        return service.cancel(store, manager, args.watch_id)
    if args.command == "retry_delivery":
        if args.all_watches:
            return service.retry_all_deliveries(store, manager)
        return service.retry_delivery(store, manager, args.watch_id)
    if args.command == "remove":
        return service.remove(store, manager, args.watch_id)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    run_watch(store, args.watch_id, manager, stopped)
    return {"watch_id": args.watch_id, "phase": store.state(args.watch_id).phase}


def main() -> int:
    """Run the CLI with private files and concise actionable errors."""
    os.umask(0o077)
    logger.remove()
    logger.add(
        lambda message: sys.stderr.write(message),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        colorize=False,
    )
    args = parser().parse_args()
    try:
        result = dispatch(args, Store(args.state_dir), get_manager())
    except (WakeupError, OSError, ValueError, ValidationError, subprocess.SubprocessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        logger.error("{}", detail)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    if args.command == "doctor" and isinstance(result, dict) and not result["healthy"]:
        return 1
    if (
        args.command in {"retry_delivery", "rebind"}
        and args.all_watches
        and isinstance(result, dict)
        and result["failed"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
