"""Keep only the subprocesses owned by this reader tied to its lifetime."""
import ctypes
import os
from ctypes import wintypes


class ProcessGroup:
    class Basic(ctypes.Structure):
        _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64), ("Flags", wintypes.DWORD),
                    ("Minimum", ctypes.c_size_t), ("Maximum", ctypes.c_size_t), ("Active", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadCount", "WriteCount", "OtherCount", "ReadBytes", "WriteBytes", "OtherBytes")]

    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", ProcessGroup.Basic), ("IO", ProcessGroup.IO), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.Basic.Flags = 0x2000
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.kernel, self.handle = kernel, handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
