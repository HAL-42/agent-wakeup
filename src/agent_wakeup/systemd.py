"""Systemd user-service integration, isolated from monitoring logic."""

import hashlib
import os
import subprocess
from importlib.resources import files
from pathlib import Path

from .model import Config, WakeupError


def installation_id(project: Path, state_root: Path) -> str:
    """Identify one project and state-root installation on this host."""
    return hashlib.sha256(f"{project}\0{state_root}".encode()).hexdigest()[:16]


def quote_unit(value: str, *, command: bool = False) -> str:
    """Quote a systemd value, preserving literal percent and dollar characters."""
    value = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("%", "%%")
    )
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


class Systemd:
    """Manage only the user service units created for agent-wakeup."""

    def __init__(self, unit_dir: Path | None = None) -> None:
        """Select the standard user configuration directory or a test directory."""
        self.unit_dir = (
            unit_dir
            or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "systemd/user"
        )

    def control(self, *args: str) -> str:
        """Run a user-manager operation and expose failures to the caller."""
        return subprocess.run(
            ["systemctl", "--user", "--no-pager", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    def prepare(self, config: Config, state_root: Path) -> None:
        """Create or refresh this installation's service template when needed."""
        if "\n" in config.project_dir or "\r" in config.project_dir:
            raise WakeupError("systemd 的项目路径不能包含换行。")
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        template = self.unit_dir / (config.unit.split("@", 1)[0] + "@.service")
        argv = [
            config.uv_binary,
            "run",
            "--no-sync",
            "--offline",
            "--project",
            config.project_dir,
            "agent-wakeup",
            "run",
            "--state_dir",
            str(state_root),
        ]
        command = " ".join(quote_unit(value, command=True) for value in argv) + " --watch_id %i"
        content = (
            files("agent_wakeup")
            .joinpath("systemd.service.in")
            .read_text()
            .format(
                command=command,
                working_directory=config.project_dir.replace("%", "%%"),
            )
        )
        if not template.exists() or template.read_text() != content:
            temporary = template.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(template)
            self.control("daemon-reload")

    def start(self, unit: str) -> None:
        """Enable and start a watch, including recovery from a failed start."""
        if self.status(unit).get("ActiveState") == "failed":
            self.control("reset-failed", unit)
        self.control("enable", "--now", unit)

    def disable(self, unit: str) -> None:
        """Remove boot activation without stopping the caller's process."""
        self.control("disable", unit)

    def stop(self, unit: str) -> None:
        """Stop a watch and remove its boot activation."""
        self.control("disable", "--now", unit)

    def status(self, unit: str) -> dict[str, str]:
        """Read service state without running a probe."""
        output = self.control(
            "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState"
        )
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
