"""Local-object security: current-user SID and explicit DACLs.

The broker owns a named pipe and a named emergency-stop event. Both are created with an
explicit security descriptor rather than default permissions, because Windows named-pipe
defaults can grant read access to Everyone and anonymous users. The explicit descriptor
limits access to SYSTEM, the object owner, and the current user SID.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .contracts import DriverError

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_QUERY = 0x0008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TokenUser = 1
SDDL_REVISION_1 = 1
ERROR_INSUFFICIENT_BUFFER = 122


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.ULONG),
]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def current_user_sid() -> str:
    """SID string of the process token's user, e.g. ``S-1-5-21-...-1001``."""
    return _process_user_sid(kernel32.GetCurrentProcess())


def process_user_sid(pid: int) -> str | None:
    """SID string of another local process's token user, or None when it cannot be read."""
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        return _process_user_sid(process)
    except DriverError:
        return None
    finally:
        kernel32.CloseHandle(process)


def _process_user_sid(process: int) -> str:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        raise DriverError(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise DriverError("GetTokenInformation returned no size")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buffer, size.value, ctypes.byref(size)):
            raise DriverError(f"GetTokenInformation failed ({ctypes.get_last_error()})")
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
        string_ptr = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(string_ptr)):
            raise DriverError(f"ConvertSidToStringSidW failed ({ctypes.get_last_error()})")
        try:
            return string_ptr.value or ""
        finally:
            kernel32.LocalFree(string_ptr)
    finally:
        kernel32.CloseHandle(token)


def owner_dacl_sddl(sid: str | None = None) -> str:
    """Protective DACL granting full access to SYSTEM, the object owner, and the user SID."""
    user = sid or current_user_sid()
    return f"D:P(A;;GA;;;SY)(A;;GA;;;OW)(A;;GA;;;{user})"


class SecurityAttributes:
    """Keeps the security-descriptor buffer alive for the lifetime of the handle."""

    def __init__(self, sddl: str | None = None) -> None:
        self._sd = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl or owner_dacl_sddl(), SDDL_REVISION_1, ctypes.byref(self._sd), None
        ):
            raise DriverError(f"invalid security descriptor ({ctypes.get_last_error()})")
        self._attrs = SECURITY_ATTRIBUTES()
        self._attrs.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        self._attrs.lpSecurityDescriptor = self._sd
        self._attrs.bInheritHandle = False

    def __enter__(self) -> SECURITY_ATTRIBUTES:
        return self._attrs

    def __exit__(self, *_exc: object) -> None:
        if self._sd:
            kernel32.LocalFree(self._sd)
            self._sd = ctypes.c_void_p()


def session_id() -> int:
    """Terminal Services session id of the current process."""
    value = wintypes.DWORD(0)
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(value)):
        raise DriverError(f"ProcessIdToSessionId failed ({ctypes.get_last_error()})")
    return int(value.value)


def logon_session_id() -> int:
    """Logon session LUID of the process token as one 64-bit value (distinguishes RDP/local logons)."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise DriverError(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    try:
        # TokenStatistics is structurally stable and the LUID is the first field.
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 10, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise DriverError("GetTokenInformation(TokenStatistics) returned no size")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 10, buffer, size.value, ctypes.byref(size)):
            raise DriverError(f"GetTokenInformation(TokenStatistics) failed ({ctypes.get_last_error()})")
        # TOKEN_STATISTICS: LUID TokenId (8 bytes), LUID AuthenticationId (8 bytes) ...
        # A LUID is {DWORD LowPart; LONG HighPart}, so AuthenticationId.LowPart sits at offset 8.
        low = ctypes.c_uint32.from_buffer(buffer, 8).value
        high = ctypes.c_uint32.from_buffer(buffer, 12).value
        return (high << 32) | low
    finally:
        kernel32.CloseHandle(token)
