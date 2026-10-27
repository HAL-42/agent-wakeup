"""Per-user LaunchAgents for a logged-in macOS desktop session."""

import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import typing as t
from pathlib import Path

from .managers import installation_id
from .model import Config, WakeupError

LABEL_PREFIX = "io.github.hal-42.agent-wakeup."


class Launchd:
    """Manage this project's jobs in the current user's GUI launchd domain."""

    def __init__(self, agent_dir: Path | None = None, uid: int | None = None) -> None:
        """Select the user's LaunchAgents directory and login domain."""
        self.agent_dir = (agent_dir or Path.home() / "Library/LaunchAgents").absolute()
        self.domain = f"gui/{os.getuid() if uid is None else uid}"

    def control(self, *args: str) -> str:
        """Run launchctl without a shell and expose its diagnostic output."""
        return subprocess.run(
            ["/bin/launchctl", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    def unit_name(self, project: Path, state_root: Path, watch_id: str) -> str:
        """Return a label unique to this installation and watch."""
        return f"{LABEL_PREFIX}{installation_id(project, state_root)}.{watch_id}"

    def plist_path(self, unit: str) -> Path:
        """Locate a managed job's plist inside the user's LaunchAgents directory."""
        if not unit.startswith(LABEL_PREFIX) or Path(unit).name != unit:
            raise WakeupError(f"不是 agent-wakeup 的 launchd 服务标识：{unit}")
        return self.agent_dir / f"{unit}.plist"

    def prepare(self, config: Config, state_root: Path) -> None:
        """Write login startup configuration using the already prepared Python environment.

        Args:
            config: The saved watch configuration.
            state_root: Directory containing this watch's private records.
        """
        path = self.plist_path(config.unit)
        directory = state_root / config.watch_id
        value = {
            "Label": config.unit,
            "ProgramArguments": [
                str(Path(sys.executable).absolute()),
                "-m",
                "agent_wakeup.cli",
                "run",
                "--state_dir",
                str(state_root),
                "--watch_id",
                config.watch_id,
            ],
            "WorkingDirectory": config.project_dir,
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "LimitLoadToSessionType": "Aqua",
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "ExitTimeOut": 15,
            "Umask": 0o077,
            "StandardOutPath": str(directory / "runner.stdout"),
            "StandardErrorPath": str(directory / "runner.stderr"),
        }
        content = plistlib.dumps(value)
        if path.exists() and path.read_bytes() == content:
            return
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{config.unit}.", dir=self.agent_dir)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, unit: str) -> None:
        """Load a new job or start an idle loaded job without restarting a running one."""
        status = self.status(unit)
        if not status["loaded"]:
            try:
                self.control("bootstrap", self.domain, str(self.plist_path(unit)))
                return
            except subprocess.CalledProcessError:
                # Another registration may have loaded the same label in the meantime.
                if not self.status(unit)["loaded"]:
                    raise
        if not self.is_running(unit):
            self.control("kickstart", f"{self.domain}/{unit}")

    def is_running(self, unit: str) -> bool:
        """Include jobs already spawned or scheduled by launchd."""
        status = self.status(unit)
        return status["running"] or status["state"] in {"spawn scheduled", "spawned"}

    def finish(self, unit: str) -> None:
        """Remove next-login startup while allowing the runner to exit normally."""
        self.plist_path(unit).unlink(missing_ok=True)

    def stop(self, unit: str) -> None:
        """Remove login startup and unload the job from outside its process."""
        self.finish(unit)
        try:
            self.control("bootout", f"{self.domain}/{unit}")
        except subprocess.CalledProcessError:
            if self.status(unit)["loaded"]:
                raise

    def status(self, unit: str) -> dict[str, t.Any]:
        """Describe a loaded job, distinguishing absence from launchctl failures."""
        path = self.plist_path(unit)
        result: dict[str, t.Any] = {
            "manager": "launchd",
            "domain": self.domain,
            "label": unit,
            "plist_path": str(path),
            "plist_exists": path.exists(),
            "loaded": False,
            "running": False,
            "pid": None,
            "state": "unloaded",
            "last_exit_status": None,
        }
        try:
            output = self.control("print", f"{self.domain}/{unit}")
        except subprocess.CalledProcessError as error:
            if "Could not find service" in (error.stderr or ""):
                return result
            raise
        fields = re.findall(r"^([ \t]+)([a-z][a-z ]*) = (.*)$", output, re.MULTILINE)
        depth = min((len(indent) for indent, _, _ in fields), default=0)
        values = {key: value for indent, key, value in fields if len(indent) == depth}
        if "state" not in values:
            raise WakeupError(
                f"无法读取 launchd 状态，请检查 launchctl print {self.domain}/{unit}。"
            )
        pid = int(values["pid"]) if "pid" in values else None
        result.update(
            loaded=True,
            running=pid is not None and pid > 0,
            pid=pid,
            state=values["state"],
            last_exit_status=values.get("last exit code"),
        )
        return result

    def diagnostics(self) -> dict[str, t.Any]:
        """Check the GUI domain without loading, enabling or stopping jobs."""
        checks: dict[str, t.Any] = {
            "service_manager": "launchd",
            "launchd_domain": self.domain,
            "agent_dir": str(self.agent_dir),
            "macos_version": platform.mac_ver()[0],
        }
        try:
            self.control("print", self.domain)
            checks["user_manager"] = self.domain
        except (OSError, subprocess.SubprocessError) as error:
            checks["user_manager_error"] = (
                error.stderr or str(error)
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
        return checks
