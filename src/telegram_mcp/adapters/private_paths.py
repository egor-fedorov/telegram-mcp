"""Restrict local files and directories before writing private content."""

import sys
from pathlib import Path


def prepare_private_directory(path: Path) -> None:
    """Create private parents without following redirected storage paths."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise OSError("Private storage cannot use redirected paths")
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700, exist_ok=True)
        protect_private_path(candidate, directory=True)
    protect_private_path(path, directory=True)


def protect_private_path(path: Path, *, directory: bool = False) -> None:
    if path.is_symlink() or path.is_junction():
        raise OSError("Private storage cannot use a symbolic link or junction")
    if sys.platform == "win32":
        try:
            protect_windows_path(path, directory=directory)
        except Exception:
            raise OSError("Cannot restrict private storage to the current user") from None
    else:
        path.chmod(0o700 if directory else 0o600)


def protect_windows_path(path: Path, *, directory: bool = False) -> None:
    if sys.platform == "win32":
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            sid = win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
        finally:
            process_token.Close()
        dacl = win32security.ACL()
        if directory:
            # Preserve current-user access for existing and future children when
            # Windows replaces their inherited ACEs after this directory changes.
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS,
                sid,
            )
        else:
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
