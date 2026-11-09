"""Process-lifetime Store ownership backed by a kernel file lock."""

from pathlib import Path
import sys

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class StoreLease:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._file = (directory / ".writer.lock").open("a+b")
        try:
            self._file.seek(0, 2)
            if self._file.tell() == 0:
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._file.close()
            raise RuntimeError("Store write ownership is unavailable; another process may own this Store.") from error

    def close(self) -> None:
        # Closing the descriptor releases the kernel lock, including after process death.
        # The persistent file itself carries no liveness meaning and is never unlinked.
        self._file.close()
