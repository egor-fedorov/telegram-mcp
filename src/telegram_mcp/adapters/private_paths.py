"""Restrict local files and directories before writing private content."""

import sys
from pathlib import Path


def protect_private_path(path: Path, *, directory: bool = False) -> None:
    if path.is_symlink() or path.is_junction():
        raise OSError("Private storage cannot use a symbolic link or junction")
    if sys.platform == "win32":
        try:
            protect_windows_path(path)
        except Exception:
            raise OSError("Cannot restrict private storage to the current user") from None
    else:
        path.chmod(0o700 if directory else 0o600)


def protect_windows_path(path: Path) -> None:
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
