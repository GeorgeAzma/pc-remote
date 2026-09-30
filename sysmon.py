"""Live system monitor for the Controls tab: per-thread CPU load with the
core topology (performance vs efficiency cores), clocks, memory, GPU, disks,
network and the busiest processes.

Everything comes from Windows itself through ctypes (NtQuerySystemInformation,
GetLogicalProcessorInformationEx, PDH performance counters, psapi) and NVML
for NVIDIA GPUs, so it needs no extra packages. A sampler thread runs once a
second only while someone is looking, and stops a minute after the last
request. (CPU temperature has no dependable Windows API without admin
rights or vendor drivers, so it isn't shown; the GPU's is.)"""
import ctypes
import os
import string
import threading
import time
from collections import deque
from ctypes import wintypes

import win32

_ntdll = ctypes.WinDLL("ntdll")
_k32 = ctypes.WinDLL("kernel32")
_psapi = ctypes.WinDLL("psapi")
_pdh = ctypes.WinDLL("pdh")
HISTORY = 60  # samples (seconds) kept for the graphs
IDLE_STOP = 60.0


# ---- CPU: per logical processor times, and which core each belongs to -----
class _PROC_PERF(ctypes.Structure):  # SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION
    _fields_ = [("Idle", ctypes.c_longlong), ("Kernel", ctypes.c_longlong), ("User", ctypes.c_longlong),
                ("Dpc", ctypes.c_longlong), ("Interrupt", ctypes.c_longlong), ("Count", ctypes.c_ulong)]


_k32.GetActiveProcessorCount.restype = wintypes.DWORD
NCPU = min(64, _k32.GetActiveProcessorCount(0xFFFF))


def _cpu_times():
    buf = (_PROC_PERF * NCPU)()
    if _ntdll.NtQuerySystemInformation(8, buf, ctypes.sizeof(buf), None) != 0:
        return None
    return [(p.Idle, p.Kernel + p.User) for p in buf]


def topology() -> list[dict]:
    """Physical cores: {'kind': 'P'|'E'|'C', 'threads': [logical indices]}.
    On hybrid CPUs the higher efficiency class is the performance core."""
    need = wintypes.DWORD(0)
    _k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(need))  # RelationProcessorCore
    buf = ctypes.create_string_buffer(need.value)
    if not _k32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(need)):
        return [{"kind": "C", "threads": [i]} for i in range(NCPU)]
    raw, off, cores = buf.raw, 0, []
    while off < need.value:
        size = int.from_bytes(raw[off + 4:off + 8], "little")
        eff = raw[off + 9]
        groups = int.from_bytes(raw[off + 30:off + 32], "little")
        threads = []
        for g in range(groups):
            base = off + 32 + g * 16
            mask = int.from_bytes(raw[base:base + 8], "little")
            if int.from_bytes(raw[base + 8:base + 10], "little") == 0:  # group 0 only (<= 64 CPUs)
                threads += [i for i in range(64) if mask >> i & 1]
        cores.append({"eff": eff, "threads": threads})
        off += size
    classes = {c["eff"] for c in cores}
    top = max(classes)
    for c in cores:
        c["kind"] = "C" if len(classes) == 1 else "P" if c.pop("eff") == top else "E"
        c.pop("eff", None)
    # P cores first, then E, each in hardware order
    return sorted(cores, key=lambda c: ("PCE".index(c["kind"]), c["threads"][0] if c["threads"] else 0))


def _cpu_reg(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


# ---- PDH performance counters (clocks, disks, network, processes) --------
class _PDH_VALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("double", ctypes.c_double)]


class _PDH_ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PDH_VALUE)]


_pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
PDH_FMT_DOUBLE, PDH_FMT_NOCAP100, PDH_MORE_DATA = 0x200, 0x8000, 0x800007D2


class Counters:
    PATHS = {
        "perf": r"\Processor Information(*)\% Processor Performance",
        "disk_r": r"\PhysicalDisk(_Total)\Disk Read Bytes/sec",
        "disk_w": r"\PhysicalDisk(_Total)\Disk Write Bytes/sec",
        "disk_idle": r"\PhysicalDisk(_Total)\% Idle Time",
        "net_rx": r"\Network Interface(*)\Bytes Received/sec",
        "net_tx": r"\Network Interface(*)\Bytes Sent/sec",
        "proc_cpu": r"\Process(*)\% Processor Time",
        "proc_mem": r"\Process(*)\Working Set - Private",
        "gpu_eng": r"\GPU Engine(*)\Utilization Percentage",
        "gpu_mem": r"\GPU Adapter Memory(*)\Dedicated Usage",
    }

    def __init__(self):
        self.q = ctypes.c_void_p()
        self.c = {}
        if _pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q)) != 0:
            return
        for k, path in self.PATHS.items():
            h = ctypes.c_void_p()
            if _pdh.PdhAddEnglishCounterW(self.q, path, None, ctypes.byref(h)) == 0:
                self.c[k] = h
        _pdh.PdhCollectQueryData(self.q)

    def collect(self):
        return _pdh.PdhCollectQueryData(self.q) == 0

    def items(self, key) -> dict:
        h = self.c.get(key)
        if not h:
            return {}
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        if (_pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF) != PDH_MORE_DATA:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return {}
        arr = ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM))
        return {arr[i].szName: arr[i].FmtValue.double for i in range(count.value) if arr[i].FmtValue.CStatus in (0, 1)}

    def close(self):
        if self.q:
            _pdh.PdhCloseQuery(self.q)


# ---- memory / system counts ------------------------------------------------
class _PERF_INFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


class _POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def _memory():
    pi = _PERF_INFO(cb=ctypes.sizeof(_PERF_INFO))
    _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb)
    pg = pi.PageSize
    total, avail = pi.PhysicalTotal * pg, pi.PhysicalAvailable * pg
    return {"total": total, "used": total - avail, "cached": min(avail, pi.SystemCache * pg),
            "commit": pi.CommitTotal * pg, "commit_limit": pi.CommitLimit * pg,
            "paged": pi.KernelPaged * pg, "nonpaged": pi.KernelNonpaged * pg}, \
        {"processes": pi.ProcessCount, "threads": pi.ThreadCount, "handles": pi.HandleCount}


def _disks():
    out, mask = [], _k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask >> i & 1:
            continue
        root = f"{letter}:\\"
        if _k32.GetDriveTypeW(root) != 3:  # fixed disks only
            continue
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not _k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free)):
            continue
        label = ctypes.create_unicode_buffer(64)
        _k32.GetVolumeInformationW(root, label, 64, None, None, None, None, 0)
        out.append({"name": f"{letter}:", "label": label.value, "total": total.value, "used": total.value - free.value})
    return out


def _battery():
    ps = _POWER_STATUS()
    if not _k32.GetSystemPowerStatus(ctypes.byref(ps)) or ps.BatteryFlag & 0x80 or ps.BatteryFlag == -1:
        return None  # no battery
    return {"percent": ps.BatteryLifePercent & 0xFF, "charging": ps.ACLineStatus == 1}


# ---- NVIDIA GPU ---------------------------------------------------------------
class _NV_MEM(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NV_UTIL(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _nvidia():
    nv, dev = win32.STATS._nvml, win32.STATS._gpu
    if not nv:
        return None
    u, v, p = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()

    def val(fn, *args):
        return u.value if getattr(nv, fn)(dev, *args, ctypes.byref(u)) == 0 else None
    g = {"name": win32.STATS.gpu_name}
    ut = _NV_UTIL()
    if nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(ut)) == 0:
        g["util"], g["mem_util"] = ut.gpu, ut.memory
    m = _NV_MEM()
    if nv.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m)) == 0:
        g["vram_total"], g["vram_used"] = m.total, m.used
    g["temp"] = val("nvmlDeviceGetTemperature", 0)
    power = val("nvmlDeviceGetPowerUsage")
    g["power_w"] = power / 1000 if power is not None else None
    limit = val("nvmlDeviceGetEnforcedPowerLimit")
    g["power_limit_w"] = limit / 1000 if limit is not None else None
    g["clock"], g["clock_max"] = val("nvmlDeviceGetClockInfo", 0), val("nvmlDeviceGetMaxClockInfo", 0)
    g["mem_clock"] = val("nvmlDeviceGetClockInfo", 2)
    g["fan"] = val("nvmlDeviceGetFanSpeed")
    for key, fn in (("encoder", "nvmlDeviceGetEncoderUtilization"), ("decoder", "nvmlDeviceGetDecoderUtilization")):
        if getattr(nv, fn)(dev, ctypes.byref(v), ctypes.byref(p)) == 0:
            g[key] = v.value
    g["pcie_rx"], g["pcie_tx"] = val("nvmlDeviceGetPcieThroughput", 1), val("nvmlDeviceGetPcieThroughput", 0)  # KB/s
    return g


# ---- sampler ------------------------------------------------------------------
class Monitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._asked = 0.0
        self._snap = None
        self._hist = {k: deque(maxlen=HISTORY) for k in ("cpu", "mem", "gpu", "net_rx", "net_tx", "disk_r", "disk_w")}
        self._cores = topology()
        self._base = _cpu_reg("~MHz")
        self._name = " ".join((_cpu_reg("ProcessorNameString") or "").replace("(R)", "").replace("(TM)", "").split()) or None
        self._disks_at, self._disks = 0.0, []

    def get(self) -> dict:
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="sysmon")
                self._thread.start()
            snap = self._snap
        if snap is None:  # first request: wait for the first sample
            for _ in range(30):
                time.sleep(0.05)
                if self._snap is not None:
                    break
            snap = self._snap
        return snap or {"warming": True}

    def _run(self):
        ctr = Counters()
        prev = _cpu_times()
        try:
            while time.monotonic() - self._asked < IDLE_STOP:
                time.sleep(1.0)
                ctr.collect()
                cur = _cpu_times()
                threads = []
                if cur and prev:
                    for (i0, t0), (i1, t1) in zip(prev, cur):
                        dt = t1 - t0
                        threads.append(round(100 * max(0.0, 1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
                prev = cur
                snap = self._sample(ctr, threads)
                with self._lock:
                    self._snap = snap
        finally:
            ctr.close()
            with self._lock:
                self._thread = None

    def _sample(self, ctr: Counters, threads: list) -> dict:
        cpu = round(sum(threads) / len(threads), 1) if threads else None
        # effective clock per logical processor: base clock x "% Processor Performance"
        perf = ctr.items("perf")
        mhz = []
        if self._base:
            for i in range(NCPU):
                p = perf.get(f"0,{i}")
                mhz.append(round(self._base * p / 100) if p is not None else None)
        clocks = [m for m in mhz if m]
        mem, counts = _memory()
        now = time.monotonic()
        if now - self._disks_at > 10:
            self._disks_at, self._disks = now, _disks()
        net_rx = sum(ctr.items("net_rx").values())
        net_tx = sum(ctr.items("net_tx").values())
        disk_r = ctr.items("disk_r").get("_Total", 0.0)
        disk_w = ctr.items("disk_w").get("_Total", 0.0)
        idle = ctr.items("disk_idle").get("_Total")
        gpu = _nvidia()
        if gpu is None:  # other GPUs: Windows' own GPU counters (like Task Manager)
            eng = ctr.items("gpu_eng")
            use3d = sum(v for k, v in eng.items() if k.endswith("engtype_3D"))
            vram = ctr.items("gpu_mem")
            gpu = {"name": None, "util": round(min(100.0, use3d)), "vram_used": max(vram.values(), default=None)}
        # busiest processes (by name; "chrome#3" counts as chrome)
        procs = {}
        pmem = ctr.items("proc_mem")
        for name, v in ctr.items("proc_cpu").items():
            if name in ("_Total", "Idle"):
                continue
            base = name.split("#")[0]
            e = procs.setdefault(base, [0.0, 0])
            e[0] += v / NCPU
            e[1] += int(pmem.get(name, 0))
        top = sorted(procs.items(), key=lambda kv: -kv[1][0])[:6]
        h = self._hist
        h["cpu"].append(cpu or 0)
        h["mem"].append(round(100 * mem["used"] / mem["total"], 1) if mem["total"] else 0)
        h["gpu"].append(gpu.get("util") or 0)
        for k, v in (("net_rx", net_rx), ("net_tx", net_tx), ("disk_r", disk_r), ("disk_w", disk_w)):
            h[k].append(round(v))
        return {
            "cpu": {"name": self._name, "total": cpu,
                    "threads": threads, "cores": self._cores, "mhz": mhz, "base_mhz": self._base,
                    "clock": round(sum(clocks) / len(clocks)) if clocks else None, "clock_max": max(clocks, default=None)},
            "mem": mem, "counts": counts, "gpu": gpu,
            "disk": {"volumes": self._disks, "read": disk_r, "write": disk_w,
                     "active": round(max(0.0, 100 - idle), 1) if idle is not None else None},
            "net": {"rx": net_rx, "tx": net_tx},
            "procs": [{"name": n, "cpu": round(c, 1), "mem": m} for n, (c, m) in top],
            "battery": _battery(), "uptime_s": _k32.GetTickCount64() // 1000,
            "history": {k: list(v) for k, v in h.items()},
        }


_k32.GetTickCount64.restype = ctypes.c_uint64
MONITOR = Monitor()
