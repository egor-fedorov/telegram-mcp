import sys
from pathlib import Path


def assert_windows_token_acl(path: Path, *, directory: bool = False) -> None:
    if sys.platform == "win32":
        import ntsecuritycon
        import win32api
        import win32security

        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY
        )
        try:
            expected_sid = win32security.GetTokenInformation(
                process_token, win32security.TokenUser
            )[0]
        finally:
            process_token.Close()
        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None and dacl.GetAceCount() == 1
        ace = dacl.GetAce(0)
        flags = (
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
            if directory
            else 0
        )
        assert ace[0] == (win32security.ACCESS_ALLOWED_ACE_TYPE, flags)
        assert ace[1] == ntsecuritycon.FILE_ALL_ACCESS
        assert ace[2] == expected_sid
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
