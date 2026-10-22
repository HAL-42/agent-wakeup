"""Private, atomic watch records with process-level locks."""

import fcntl
import json
import os
import shutil
import tempfile
import time
import typing as t
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from .model import Config, State, WakeupError


def atomic_json(path: Path, value: dict[str, t.Any]) -> None:
    """Replace a JSON record and flush it before returning.

    Args:
        path: Destination within an existing directory.
        value: JSON-serializable record.
    """
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> t.Iterator[None]:
    """Hold an advisory exclusive lock for a filesystem operation.

    Args:
        path: Lock file, kept in place to preserve inode identity.
        blocking: Whether to wait for another holder.
    """
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise WakeupError("该监控仍由运行器占用，请稍后重试。") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Store:
    """Read and update watch records under one state root."""

    def __init__(self, root: Path) -> None:
        """Retain the absolute state directory without creating it."""
        self.root = root.expanduser().absolute()

    def initialize(self) -> None:
        """Create the private state root when a command writes records."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, watch_id: str) -> Path:
        """Return the directory of a UUID-addressed watch."""
        return self.root / str(UUID(watch_id))

    def ids(self) -> list[str]:
        """List complete registrations, excluding staging directories."""
        if not self.root.exists():
            return []
        return sorted(
            path.name
            for path in self.root.iterdir()
            if (path / "config.json").is_file() and not path.name.startswith(".")
        )

    def config(self, watch_id: str) -> Config:
        """Read a watch's immutable configuration."""
        return Config(**json.loads((self.directory(watch_id) / "config.json").read_text()))

    def state(self, watch_id: str) -> State:
        """Read the latest complete state record."""
        return State(**json.loads((self.directory(watch_id) / "state.json").read_text()))

    @contextmanager
    def edit(self, watch_id: str) -> t.Iterator[State]:
        """Serialize a short state transition and persist it on success."""
        directory = self.directory(watch_id)
        with file_lock(directory / "state.lock"):
            state = self.state(watch_id)
            yield state
            state.updated_at = time.time()
            atomic_json(directory / "state.json", asdict(state))

    def create(self, config: Config, probe: bytes) -> None:
        """Publish a complete registration under the caller's registry lock."""
        directory = self.directory(config.watch_id)
        staging = Path(tempfile.mkdtemp(prefix=".register-", dir=self.root))
        try:
            atomic_json(staging / "config.json", asdict(config))
            atomic_json(staging / "state.json", asdict(State(updated_at=config.created_at)))
            snapshot = staging / "probe"
            with snapshot.open("wb") as stream:
                stream.write(probe)
                stream.flush()
                os.fsync(stream.fileno())
            snapshot.chmod(0o600)
            staging.rename(directory)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
