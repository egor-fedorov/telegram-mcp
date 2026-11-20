"""Read a bounded regular file under an explicitly allowed, non-redirected directory."""

import os
import stat
import sys
from pathlib import Path

from telegram_mcp.domain.models import ToolError


def read_allowed_file(
    path: Path, directories: tuple[Path, ...], maximum: int, private_paths: tuple[Path, ...] = ()
) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ToolError(
            "FILE_ACCESS_DENIED", "Use an absolute path inside allowed_upload_directories."
        )
    if not any(path != root and path.is_relative_to(root) for root in directories):
        raise ToolError("FILE_ACCESS_DENIED", "Path is outside allowed_upload_directories.")
    try:
        if _is_private_upload(path, private_paths):
            raise ToolError(
                "FILE_ACCESS_DENIED", "Server credentials and private storage cannot be registered."
            )
        if sys.platform == "win32":
            return _windows_read(path, maximum)
        return _posix_read(path, maximum)
    except (OSError, RuntimeError):
        raise ToolError(
            "FILE_ACCESS_DENIED", "Cannot read this regular file without following redirects."
        ) from None


def _is_private_upload(path: Path, private_paths: tuple[Path, ...]) -> bool:
    # File identities also cover filesystem-specific aliases and case-insensitive volumes.
    ancestors = (candidate.lstat() for candidate in (path, *path.parents))
    identities = {(info.st_dev, info.st_ino) for info in ancestors if info.st_ino}
    for private in private_paths:
        if path.is_relative_to(private.resolve()):
            return True
        try:
            info = private.stat()
        except FileNotFoundError:
            continue
        if info.st_ino and (info.st_dev, info.st_ino) in identities:
            return True
    return False


def _read(fd: int, maximum: int) -> bytes:
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ToolError("FILE_ACCESS_DENIED", "Use a regular file without hard links.")
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
