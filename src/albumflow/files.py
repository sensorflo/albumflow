"""File helpers: drives, copies, progress."""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import string
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable


class AlbumflowError(RuntimeError):
    pass


def drives() -> list[Path]:
    """All mounted drive roots, without Windows' "insert a disk" popups."""
    kernel32 = ctypes.windll.kernel32
    old_mode = kernel32.SetErrorMode(0x0001)  # SEM_FAILCRITICALERRORS
    try:
        mask = kernel32.GetLogicalDrives()
        roots = [Path(f"{letter}:\\") for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1]
        return [root for root in roots if os.path.exists(root)]
    finally:
        kernel32.SetErrorMode(old_mode)


def volume_label(drive: Path) -> str:
    """The drive's name as shown in Explorer, "" if it has none."""
    buf = ctypes.create_unicode_buffer(261)
    if not ctypes.windll.kernel32.GetVolumeInformationW(drive.anchor, buf, len(buf), None, None, None, None, 0):
        return ""
    return buf.value


def volume_serial(drive: Path) -> int | None:
    """The volume serial number (changes when another card is inserted), None if unknown."""
    serial = wintypes.DWORD()
    if not ctypes.windll.kernel32.GetVolumeInformationW(drive.anchor, None, 0, ctypes.byref(serial),
                                                         None, None, None, 0):
        return None
    return serial.value


def poll_until(find: Callable[[], object], poll_seconds: float, stop=None):
    """Call find() every poll_seconds until it returns something truthy, and return that.

    With stop (a threading.Event), returns None as soon as stop is set.
    """
    while not (result := find()):
        if stop is not None:
            if stop.wait(poll_seconds):
                return None
        else:
            time.sleep(poll_seconds)
    return result


def eject(drive: Path, attempts: int = 5) -> None:
    """Safely eject the medium in drive, like Explorer's "Eject".

    Locks the volume (retrying while e.g. Explorer still has files open),
    dismounts it and ejects the medium. Raises AlbumflowError on failure.
    """
    if drive.drive.upper() == os.environ.get("SystemDrive", "C:").upper():
        raise AlbumflowError(f"Refusing to eject the system drive {drive}")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING = 1, 2, 3
    FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME = 0x00090018, 0x00090020
    IOCTL_STORAGE_MEDIA_REMOVAL, IOCTL_STORAGE_EJECT_MEDIA = 0x002D4804, 0x002D4808
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def ioctl(code: int, inbuf=None, insize: int = 0) -> bool:
        returned = wintypes.DWORD()
        return bool(k32.DeviceIoControl(handle, code, inbuf, insize, None, 0, ctypes.byref(returned), None))

    def fail(step: str):
        return AlbumflowError(f"Could not eject {drive} ({step}: {ctypes.FormatError(ctypes.get_last_error())})")

    handle = k32.CreateFileW(f"\\\\.\\{drive.drive}", GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise fail("open")
    try:
        for attempt in range(attempts):
            if ioctl(FSCTL_LOCK_VOLUME):
                break
            time.sleep(1)
        else:
            raise fail("still in use")
        if not ioctl(FSCTL_DISMOUNT_VOLUME):
            raise fail("dismount")
        allow_removal = ctypes.c_bool(False)  # PREVENT_MEDIA_REMOVAL.PreventMediaRemoval
        ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, ctypes.byref(allow_removal), ctypes.sizeof(allow_removal))
        if not ioctl(IOCTL_STORAGE_EJECT_MEDIA):
            raise fail("eject")
    finally:
        k32.CloseHandle(handle)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DeviceInterfaceData(ctypes.Structure):  # SP_DEVICE_INTERFACE_DATA
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t)]


class _DevinfoData(ctypes.Structure):  # SP_DEVINFO_DATA
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t)]


class _StorageDeviceNumber(ctypes.Structure):  # STORAGE_DEVICE_NUMBER
    _fields_ = [("DeviceType", wintypes.DWORD), ("DeviceNumber", wintypes.DWORD),
                ("PartitionNumber", wintypes.DWORD)]


_GUID_DEVINTERFACE_DISK = _GUID(0x53F56307, 0xB6BF, 0x11D0, (ctypes.c_ubyte * 8)(0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B))
_IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080


def _disk_number(device_path: str) -> int | None:
    """Disk number of a drive (\\\\.\\F:) or disk device path, None if unknown."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(device_path, 0, 3, None, 3, 0, None)  # no access needed; share read/write
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        number, returned = _StorageDeviceNumber(), wintypes.DWORD()
        ok = k32.DeviceIoControl(handle, _IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0, ctypes.byref(number),
                                 ctypes.sizeof(number), ctypes.byref(returned), None)
        return number.DeviceNumber if ok else None
    finally:
        k32.CloseHandle(handle)


def _disk_devinst(number: int) -> int | None:
    """Device instance of the disk with that number, among all present disks."""
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.POINTER(_GUID),
                                                     wintypes.DWORD, ctypes.POINTER(_DeviceInterfaceData)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_DeviceInterfaceData),
                                                          wintypes.LPVOID, wintypes.DWORD,
                                                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_DevinfoData)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    devinst = None
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(_GUID_DEVINTERFACE_DISK), None, None, 0x02 | 0x10)
    try:
        index = 0
        while devinst is None:
            iface = _DeviceInterfaceData(cbSize=ctypes.sizeof(_DeviceInterfaceData))
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(_GUID_DEVINTERFACE_DISK),
                                                         index, ctypes.byref(iface)):
                break
            index += 1
            detail = ctypes.create_string_buffer(1024)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: 8 on 64-bit Windows, 6 on 32-bit
            ctypes.c_uint32.from_buffer(detail).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            info = _DevinfoData(cbSize=ctypes.sizeof(_DevinfoData))
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), detail, len(detail),
                                                              None, ctypes.byref(info)):
                continue
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            if _disk_number(path) == number:
                devinst = info.DevInst
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return devinst


def remove_device(drive: Path, attempts: int = 5) -> None:
    """Safely remove the USB device holding drive, like "Safely Remove Hardware".

    For external disks such as the backup SSD; for a card in a reader, use
    eject(), which releases the card but keeps the reader.
    """
    if drive.drive.upper() == os.environ.get("SystemDrive", "C:").upper():
        raise AlbumflowError(f"Refusing to remove the system drive {drive}")
    number = _disk_number(f"\\\\.\\{drive.drive}")
    if number is None:
        raise AlbumflowError(f"Could not remove {drive} (disk not found)")

    devinst = _disk_devinst(number)
    if devinst is None:
        raise AlbumflowError(f"Could not remove {drive} (device not found)")

    cfgmgr = ctypes.WinDLL("cfgmgr32")
    # The disk's parent is the USB device; asking it to eject removes the disk with it.
    parent = wintypes.DWORD()
    if cfgmgr.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != 0:
        raise AlbumflowError(f"Could not remove {drive} (no parent device)")
    for attempt in range(attempts):
        veto_type, veto_name = wintypes.DWORD(), ctypes.create_unicode_buffer(260)
        result = cfgmgr.CM_Request_Device_EjectW(parent.value, ctypes.byref(veto_type), veto_name, 260, 0)
        if result == 0 and veto_type.value == 0:
            return
        time.sleep(1)
    reason = f"in use by {veto_name.value}" if veto_name.value else f"refused, code {result}/{veto_type.value}"
    raise AlbumflowError(f"Could not remove {drive} ({reason})")


def sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def copy_file(src: Path, dest: Path, on_bytes: Callable[[int], None] | None = None) -> None:
    """Copy src to dest (keeping its timestamps), then call on_bytes(size).

    Writes to a temporary name first, so dest only ever exists complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    if on_bytes:
        on_bytes(src.stat().st_size)


def same_content(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and sha256(a) == sha256(b)


# progress(done_bytes, total_bytes, current_filename)
Progress = Callable[[int, int, str], None]


class ByteCounter:
    """Turns per-chunk byte counts into overall progress calls."""

    def __init__(self, total: int, progress: Progress | None):
        self.total, self.done, self.progress, self.name = total, 0, progress, ""

    def start(self, name: str) -> None:
        self.name = name
        self.add(0)

    def add(self, n: int) -> None:
        self.done += n
        if self.progress:
            self.progress(self.done, self.total, self.name)
