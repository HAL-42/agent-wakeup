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

from jsonschema import ValidationError
from loguru import logger

from . import service
from .model import WakeupError
from .process import execute, parse_probe
from .runner import run_watch
from .store import Store
from .systemd import Systemd


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
        description="后台执行条件检查，在触发时向 agent 会话投递 prompt。",
        epilog="首次 register 自动登记 systemd 用户服务。代码和环境留在本项目；状态保存在用户目录。",
    )
    commands = root.add_subparsers(dest="command", required=True)
    descriptions = {
        "register": "保存 probe 快照和会话绑定，启动后台监控；相同会话/key 的相同配置复用原实例。",
        "check": "按给定环境执行一次 probe 并验证 JSON；即使返回 wake 也不投递。",
        "doctor": "检查 uv、Codex 和 systemd 用户服务条件；不运行 probe 或发送消息。",
        "list": "列出本状态目录的监控记录，不查询被监控的数据源。",
        "status": "查看一个监控的最近结果、期限、错误、回执和 systemd 状态。",
        "cancel": "停止监控及 probe 子进程，保留记录；外部任务和已入队消息不受影响。",
        "retry_delivery": "核对原会话后，显式重投未确认/失败的 prompt；不重新运行 probe。",
        "remove": "删除已结束监控的本地记录；运行中的监控先 cancel。",
        "run": "内部入口：由 systemd 运行或恢复一个监控实例。",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(
            name,
            help=description,
            description=description,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        command.add_argument(
            "--state_dir",
            type=Path,
            default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "agent-wakeup",
            help="监控状态目录",
        )
        command.add_argument("--json", action="store_true", help="输出可供程序读取的 JSON")
        if name in {"status", "cancel", "retry_delivery", "remove"}:
            command.add_argument("watch_id", help="register 返回的 UUID")
        if name == "run":
            command.add_argument("--watch_id", required=True, help="要运行的 UUID")
        if name in {"check", "register"}:
            command.add_argument(
                "--probe", type=Path, required=True, help="单次检查脚本；注册时保存一份快照"
            )
            command.add_argument(
                "--interpreter",
                default=sys.executable,
                help="运行 probe 的解释器，可指定其他项目的 Python 或 Bash",
            )
            command.add_argument(
                "--cwd", type=Path, default=Path.cwd(), help="probe 和 queue 的工作目录"
            )
            command.add_argument(
                "--env",
                action="append",
                default=[],
                metavar="NAME=VALUE",
                help="环境覆盖，可重复；私有配置中保存，不复制整个终端环境",
            )
            command.add_argument(
                "--probe_timeout_sec",
                type=positive,
                default=120,
                help="一次 probe 的最长运行时间；stdout/stderr 各最多 64 KiB",
            )
            command.add_argument(
                "probe_args", nargs=argparse.REMAINDER, help="在 -- 后填写传给 probe 的参数"
            )
        if name == "register":
            command.add_argument("--key", required=True, help="目标会话内的注册去重键")
            command.add_argument(
                "--thread_id",
                default=os.environ.get("CODEX_THREAD_ID"),
                help="目标 Codex UUID；默认来自 CODEX_THREAD_ID",
            )
            command.add_argument(
                "--codex_binary", default="codex", help="Codex CLI 程序；注册时解析绝对路径"
            )
            command.add_argument(
                "--codex_home",
                type=Path,
                default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
                help="目标 Codex 的数据目录",
            )
            command.add_argument(
                "--codex_remote", help="可选的现有 app-server 地址，例如 unix:///path/server.sock"
            )
            command.add_argument(
                "--interval_sec",
                type=positive,
                default=300,
                help="一次成功检查结束到下一次检查的间隔",
            )
            command.add_argument(
                "--max_wait_sec",
                type=positive,
                default=604800,
                help="从注册开始的总等待期限；重启不会重置",
            )
            command.add_argument(
                "--max_probe_errors",
                type=positive_integer,
                default=5,
                help="连续执行错误达到此数后发送异常 prompt；退避上限 15 分钟",
            )
            command.add_argument(
                "--queue_timeout_sec",
                type=positive,
                default=60,
                help="投递命令超时；结果不明确时保留记录，等待显式重试",
            )
    return root


def doctor(store: Store, manager: Systemd) -> dict[str, t.Any]:
    """Collect local capabilities without sending a completion message."""
    checks: dict[str, t.Any] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "uv": shutil.which("uv"),
        "codex": shutil.which("codex"),
        "project_dir": str(service.PROJECT_DIR),
        "state_dir": str(store.root),
    }
    try:
        checks["user_manager"] = manager.control("show", "--property=Version", "--value")
    except (OSError, subprocess.CalledProcessError) as error:
        checks["user_manager_error"] = str(error)
    if checks["codex"]:
        result = subprocess.run(
            [checks["codex"], "queue", "--help"], capture_output=True, text=True, timeout=20
        )
        checks["queue_supported"] = result.returncode == 0 and "--thread" in result.stdout
    if shutil.which("loginctl"):
        result = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["linger"] = result.stdout.strip() if result.returncode == 0 else "unknown"
    checks["healthy"] = bool(
        checks["uv"] and checks.get("queue_supported") and checks.get("user_manager")
    )
    return checks


def dispatch(
    args: argparse.Namespace, store: Store, manager: Systemd
) -> dict[str, t.Any] | list[dict[str, t.Any]]:
    """Execute a parsed command and return its structured result."""
    if args.command in {"check", "register"}:
        probe_args = args.probe_args[1:] if args.probe_args[:1] == ["--"] else args.probe_args
        env = environment(args.env)
    if args.command == "register":
        if not args.thread_id:
            raise WakeupError("当前环境没有 CODEX_THREAD_ID，请用 --thread_id 指定目标会话 UUID。")
        return service.register(
            store,
            manager,
            key=args.key,
            probe=args.probe,
            interpreter=args.interpreter,
            probe_args=probe_args,
            cwd=args.cwd,
            env=env,
            thread_id=args.thread_id,
            codex_binary=args.codex_binary,
            codex_home=args.codex_home,
            codex_remote=args.codex_remote,
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
        return doctor(store, manager)
    if args.command == "list":
        return [
            {
                "watch_id": watch_id,
                "key": store.config(watch_id).key,
                "thread_id": store.config(watch_id).thread_id,
                **asdict(store.state(watch_id)),
            }
            for watch_id in store.ids()
        ]
    if args.command == "status":
        return service.inspect_watch(store, manager, args.watch_id)
    if args.command == "cancel":
        return service.cancel(store, manager, args.watch_id)
    if args.command == "retry_delivery":
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
        sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", colorize=False
    )
    args = parser().parse_args()
    try:
        if sys.platform != "linux":
            raise WakeupError("首版需要 Linux 和 systemd 用户服务。")
        result = dispatch(args, Store(args.state_dir), Systemd())
    except (WakeupError, OSError, ValueError, ValidationError, subprocess.SubprocessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        logger.error("{}", detail)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    if args.command == "doctor" and isinstance(result, dict) and not result["healthy"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
