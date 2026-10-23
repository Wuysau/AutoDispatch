"""Kill-on-close Windows job; POSIX process-group cleanup."""
import os
import signal

from .model import Blocked


class ProcessTree:
    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("ProcessTime", c.c_longlong), ("JobTime", c.c_longlong),
                        ("LimitFlags", w.DWORD), ("MinWorkingSet", c.c_size_t),
                        ("MaxWorkingSet", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(c.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", c.c_size_t), ("JobMemoryLimit", c.c_size_t),
                        ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t)]

        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not self.kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)):
            if handle:
                self.kernel.CloseHandle(handle)
            raise Blocked("Cannot establish Windows process-tree job")
        if not self.kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.kernel.CloseHandle(handle)
            raise Blocked("Cannot attach subprocess to kill-on-close job")
        else:
            self.handle = handle

    def close(self):
        if os.name == "nt":
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
