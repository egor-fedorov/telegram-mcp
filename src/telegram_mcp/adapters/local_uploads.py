"""Read a bounded regular file under an explicitly allowed, non-redirected directory."""

import os
import stat
import sys
from pathlib import Path

from telegram_mcp.domain.models import ToolError


def read_allowed_file(path: Path, directories: tuple[Path, ...], maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ToolError(
            "FILE_ACCESS_DENIED", "Use an absolute path inside allowed_upload_directories."
        )
    if not any(path != root and path.is_relative_to(root) for root in directories):
        raise ToolError("FILE_ACCESS_DENIED", "Path is outside allowed_upload_directories.")
    try:
        if sys.platform == "win32":
            return _windows_read(path, maximum)
        return _posix_read(path, maximum)
    except OSError:
        raise ToolError(
            "FILE_ACCESS_DENIED", "Cannot read this regular file without following redirects."
        ) from None


def _read(fd: int, maximum: int) -> bytes:
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ToolError("FILE_ACCESS_DENIED", "Only regular files can be registered.")
        data = stream.read(maximum + 1)
        if not data or len(data) > maximum:
            raise ToolError("FILE_TOO_LARGE", "Use a nonempty file no larger than 20 MiB.")
        return data


def _posix_read(path: Path, maximum: int) -> bytes:
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    return _read(fd, maximum)


def _windows_read(path: Path, maximum: int) -> bytes:
    # The final handle path also detects a redirect introduced between checks and open.
    if sys.platform == "win32":
        import msvcrt

        import win32file

        for candidate in (path, *path.parents):
            if candidate.is_symlink() or candidate.is_junction():
                raise OSError("Redirected upload")
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            final = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(fd), 0)
            if os.path.normcase(final.removeprefix("\\\\?\\")) != os.path.normcase(str(path)):
                raise OSError("Redirected upload")
        except BaseException:
            os.close(fd)
            raise
        return _read(fd, maximum)
    raise OSError("Windows handles unavailable")
