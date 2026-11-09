"""Private local files: the current account, SYSTEM and administrators only."""

import os
from pathlib import Path


def _windows_acl():
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    allowed = [
        user,
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid),
    ]
    acl = win32security.ACL()
    for sid in allowed:
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    return win32security, allowed, acl


def secure_path(path: Path, directory=False):
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        ws, _, acl = _windows_acl()
        ws.SetNamedSecurityInfo(
            str(path),
            ws.SE_FILE_OBJECT,
            ws.DACL_SECURITY_INFORMATION | ws.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


def require_private(path: Path):
    if not path.exists():
        raise ValueError("Private file/directory missing")
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise ValueError("Private file/directory permissions are too broad")
        return
    ws, allowed, _ = _windows_acl()
    acl = ws.GetNamedSecurityInfo(
        str(path), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION
    ).GetSecurityDescriptorDacl()
    if acl is None:
        raise ValueError("Private file has an unrestricted DACL")
    allowed_strings = {ws.ConvertSidToStringSid(sid) for sid in allowed}
    for i in range(acl.GetAceCount()):
        ace = acl.GetAce(i)
        if (
            ace[0][0] == ws.ACCESS_ALLOWED_ACE_TYPE
            and ws.ConvertSidToStringSid(ace[2]) not in allowed_strings
        ):
            raise ValueError("Private file/directory is accessible to another account")


def pipe_security():
    import pywintypes

    ws, allowed, _ = _windows_acl()
    acl = ws.ACL()
    for sid in allowed:
        acl.AddAccessAllowedAce(ws.ACL_REVISION, 0x001F01FF, sid)
    descriptor = ws.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes
