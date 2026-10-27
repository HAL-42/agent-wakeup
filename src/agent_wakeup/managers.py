"""Platform-independent operations used to manage a watch's service."""

import hashlib
import sys
import typing as t
from pathlib import Path

from .model import Config, WakeupError


def installation_id(project: Path, state_root: Path) -> str:
    """Identify a project and state directory without changing existing Linux names."""
    return hashlib.sha256(f"{project}\0{state_root}".encode()).hexdigest()[:16]


class ServiceManager(t.Protocol):
    """Manage service lifetime while leaving monitoring state to the runner."""

    def unit_name(self, project: Path, state_root: Path, watch_id: str) -> str:
        """Return the platform's service identifier for a watch."""
        ...

    def prepare(self, config: Config, state_root: Path) -> None:
        """Write the configuration needed to start or restore a watch."""
        ...

    def start(self, unit: str) -> None:
        """Start a prepared service, preserving any existing running instance."""
        ...

    def is_running(self, unit: str) -> bool:
        """Report whether the service is running or already starting."""
        ...

    def finish(self, unit: str) -> None:
        """Remove automatic startup without terminating the caller."""
        ...

    def stop(self, unit: str) -> None:
        """Stop a service from outside the runner and remove automatic startup."""
        ...

    def status(self, unit: str) -> dict[str, t.Any]:
        """Return the platform's service details without starting it."""
        ...

    def diagnostics(self) -> dict[str, t.Any]:
        """Check access to the user's service manager without changing configuration."""
        ...


def get_manager() -> ServiceManager:
    """Select the service manager for the current operating system."""
    if sys.platform == "linux":
        from .systemd import Systemd

        return Systemd()
    if sys.platform == "darwin":
        from .launchd import Launchd

        return Launchd()
    raise WakeupError("需要 Linux 的 systemd 用户服务或 macOS 的 launchd 用户代理。")
