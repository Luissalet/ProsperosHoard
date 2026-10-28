"""Stop a process and everything it started without waiting on external tools.

On Windows the shared ``kill_tree`` runs ``taskkill`` twice (a polite pass that cannot stop a process without a window,
then a forced one). ``taskkill`` enumerates every process through WMI, which takes seconds on a machine with many
processes, so cancelling a browser worker or an engine took longer than the work it interrupted. Here the descendants
are found with a ToolHelp snapshot and terminated directly; other systems keep the shared implementation.
"""

from __future__ import annotations

import os
import subprocess

from .hoard_link import proc as hlproc


def _windows_descendants(root: int) -> list[int]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    found, queue = [], [root]
    while queue:
        for child in children.get(queue.pop(), ()):
            if child != root and child not in found:
                found.append(child)
                queue.append(child)
    return found


def _terminate(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if handle:
        try:
            k32.TerminateProcess(handle, 1)
        finally:
            k32.CloseHandle(handle)


def kill_tree(proc: subprocess.Popen, *, grace_s: float = 1.0) -> bool:
    """Stop ``proc`` and its descendants; True when ``proc`` is gone."""
    if os.name != "nt":
        return hlproc.kill_tree(proc, grace_s=grace_s)
    if proc.poll() is not None:
        return True
    try:
        descendants = _windows_descendants(proc.pid)  # taken first: they lose their parent link once it dies
    except Exception:  # noqa: BLE001 - ToolHelp unavailable: fall back to the shared tree kill
        return hlproc.kill_tree(proc, grace_s=grace_s)
    try:
        proc.kill()
    except OSError:
        pass
    for pid in descendants:
        _terminate(pid)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return False
    return True
