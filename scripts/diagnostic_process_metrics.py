"""Local process memory metrics with the same byte unit on macOS and Windows."""

import ctypes
import sys

try:
    import resource
except ModuleNotFoundError:
    resource = None  # Windows exposes process counters through Kernel32 instead.


class _ProcessMemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS uses fixed DWORDs and pointer-sized SIZE_T fields."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        *[(name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage",
        )],
    ]


def _windows_peak_rss_bytes() -> int:
    """Read only this process; no process enumeration or additional dependency is needed."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.K32GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_uint32,
    ]
    kernel.K32GetProcessMemoryInfo.restype = ctypes.c_int
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError("Local process memory metrics unavailable")
    return counters.PeakWorkingSetSize


def peak_rss_bytes() -> int:
    """Normalize POSIX high-water RSS and Windows peak working set to bytes."""
    if sys.platform == "win32":
        return _windows_peak_rss_bytes()
    if resource is None:
        raise OSError("Local process memory metrics unavailable")
    high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(high_water) * (1 if sys.platform == "darwin" else 1024)
