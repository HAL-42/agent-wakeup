"""Clean a macOS command's process group if its supervising runner disappears."""

import os
import signal
import sys
import time


def watch_parent(pipe_fd: int, lock_fd: int) -> None:
    """Wait for the runner's pipe to close, then terminate this command's group.

    Args:
        pipe_fd: Read end of a pipe whose only writer is the runner.
        lock_fd: Inherited execution lock, retained until this process group is gone.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with os.fdopen(lock_fd, "a"), os.fdopen(pipe_fd, "rb", buffering=0) as stream:
        stream.read()
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(3)
        os.killpg(os.getpgrp(), signal.SIGKILL)


def main() -> None:
    """Start the guard before replacing this process with the requested command."""
    pipe_fd = int(sys.argv[1])
    lock_fd = int(sys.argv[2])
    command = sys.argv[3:]
    if os.fork() == 0:
        watch_parent(pipe_fd, lock_fd)
        os._exit(0)
    os.close(pipe_fd)
    os.close(lock_fd)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
