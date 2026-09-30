"""Live system monitor for the Controls tab: per-thread CPU load with the
core topology (performance vs efficiency cores), clocks, memory, GPU, disks,
network and the busiest processes.

Everything comes from Windows itself through ctypes (NtQuerySystemInformation,
GetLogicalProcessorInformationEx, PDH performance counters, psapi) and NVML
for NVIDIA GPUs, so it needs no extra packages. Sampling runs only while
someone is looking: 20 times a second for the live values (pushed over a
WebSocket), once a second for the slower ones (busiest apps, PCIe). (CPU temperature has no dependable Windows API without admin
rights or vendor drivers, so it isn't shown; the GPU's is.)"""
import ctypes
import json
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
HISTORY = 60  # seconds of history kept for the graphs
IDLE_STOP = 60.0  # HTTP polling: keep sampling this long after a request


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

    def __init__(self, keys=None):
        self.q = ctypes.c_void_p()
        self.c = {}
        if _pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q)) != 0:
            return
        for k, path in self.PATHS.items():
            if keys and k not in keys:
                continue
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


def _nvidia(pcie=False):
    """NVML readings. PCIe traffic takes ~30 ms to sample, so it's opt-in."""
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
    if pcie:
        g["pcie_rx"], g["pcie_tx"] = val("nvmlDeviceGetPcieThroughput", 1), val("nvmlDeviceGetPcieThroughput", 0)  # KB/s
    return g


# ---- sampler ------------------------------------------------------------------
FAST_S = 0.05   # 20 samples a second: CPU threads, clocks, memory, GPU, disk and network rates
SLOW_S = 1.0    # busiest apps (~8 ms of counters), PCIe (~30 ms), drive space, counts
FAST_KEYS = ("perf", "disk_r", "disk_w", "disk_idle", "net_rx", "net_tx", "gpu_eng", "gpu_mem")
SLOW_KEYS = ("proc_cpu", "proc_mem")
SERIES = ("cpu", "mem", "gpu", "net_rx", "net_tx", "disk_r", "disk_w")


class _IdleCycles:
    """Per-thread load from each processor's idle *cycle* count. The usual
    idle-time counters only move in 15.6 ms clock ticks (and a sleeping core
    catches up in a lump when it wakes), which is noise at 20 samples a
    second; cycle counts are exact."""

    def __init__(self, tsc_hz):
        self.hz = tsc_hz  # the cycle counter's rate: the CPU's nominal clock
        self.buf = (ctypes.c_ulonglong * NCPU)()
        self.prev = self._read()

    def _read(self):
        size = ctypes.c_ulong(ctypes.sizeof(self.buf))
        if not _k32.QueryIdleProcessorCycleTime(ctypes.byref(size), self.buf):
            return None
        return time.perf_counter(), list(self.buf)

    def loads(self):
        cur = self._read()
        if not cur or not self.prev:
            self.prev = cur
            return []
        dt, out = cur[0] - self.prev[0], []
        if not self.hz:  # nominal clock unknown: an idle processor's rate is the best estimate
            self.hz = max((b - a) / dt for a, b in zip(self.prev[1], cur[1])) or 1
        for a, b in zip(self.prev[1], cur[1]):
            out.append(round(min(100.0, max(0.0, 100 * (1 - (b - a) / (self.hz * dt)))), 1))
        self.prev = cur
        return out


class Monitor:
    """Samples while someone watches: WebSocket viewers get every sample
    pushed (serve), the HTTP command gets the latest one (get)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs = set()
        self._asked = 0.0      # last HTTP request
        self._running = False
        self._snap = None
        self._slow = {}
        self._hist = {k: deque(maxlen=int(HISTORY / FAST_S)) for k in SERIES}
        self._cores = topology()
        self._base = _cpu_reg("~MHz")
        self._name = " ".join((_cpu_reg("ProcessorNameString") or "").replace("(R)", "").replace("(TM)", "").split()) or None

    # ---- viewers -----------------------------------------------------------
    def _watched(self):
        return bool(self._subs) or time.monotonic() - self._asked < IDLE_STOP

    def _ensure(self):  # under _lock
        if not self._running:
            self._running = True
            threading.Thread(target=self._fast_loop, daemon=True, name="sysmon-fast").start()
            threading.Thread(target=self._slow_loop, daemon=True, name="sysmon-slow").start()

    def static(self):
        return {"name": self._name, "cores": self._cores, "base_mhz": self._base}

    def get(self) -> dict:
        with self._lock:
            self._asked = time.monotonic()
            self._ensure()
        for _ in range(40):
            if self._snap is not None:
                break
            time.sleep(0.05)
        snap = self._snap
        if not snap:
            return {"warming": True}
        return {**snap, "cpu": {**snap["cpu"], **self.static()}, **self._slow, "history": self._history()}

    def serve(self, ws):
        """A live viewer: static facts and the history first, then every sample."""
        from wsock import Outbox
        out = Outbox(ws, 1 << 20)
        with self._lock:
            self._ensure()
            hist = self._history()
        out.send_json({"t": "hello", "every_s": FAST_S, "history": hist, "slow": self._slow, "cpu": self.static()})
        with self._lock:
            self._subs.add(out)
        try:
            for _ in ws.recv_json():  # heartbeats; the socket closing ends it
                pass
        finally:
            with self._lock:
                self._subs.discard(out)
            out.close()

    def _history(self):
        return {k: list(v) for k, v in self._hist.items()}

    # ---- sampling ----------------------------------------------------------
    def _fast_loop(self):
        ctr, idle = Counters(FAST_KEYS), _IdleCycles(self._base * 1e6 if self._base else None)
        seq, slow_seen = 0, None
        try:
            nxt = time.perf_counter()
            while True:
                nxt += FAST_S
                time.sleep(max(0.0, nxt - time.perf_counter()))
                if time.perf_counter() - nxt > 0.5:
                    nxt = time.perf_counter()  # fell behind (the PC slept): don't burst
                with self._lock:
                    if not self._watched():
                        self._running = False
                        return
                    subs = list(self._subs)
                ctr.collect()
                snap = self._sample(ctr, idle.loads())
                seq += 1
                snap["seq"] = seq
                self._snap = snap
                if not subs:
                    continue
                msg = {"t": "s", **snap}
                if self._slow is not slow_seen:  # slow-lane data only when it changed
                    slow_seen = self._slow
                    msg["slow"] = self._slow
                data = json.dumps(msg, separators=(",", ":"))
                for o in subs:
                    o.send(data)
        finally:
            ctr.close()

    def _slow_loop(self):
        ctr = Counters(SLOW_KEYS)
        disks, disks_at = [], 0.0
        try:
            while self._running:
                ctr.collect()
                procs = {}
                pmem = ctr.items("proc_mem")
                for name, v in ctr.items("proc_cpu").items():
                    if name in ("_Total", "Idle"):
                        continue
                    e = procs.setdefault(name.split("#")[0], [0.0, 0])  # "chrome#3" counts as chrome
                    e[0] += v / NCPU
                    e[1] += int(pmem.get(name, 0))
                if time.monotonic() - disks_at > 10:
                    disks, disks_at = _disks(), time.monotonic()
                g = _nvidia(pcie=True) or {}
                _, counts = _memory()
                self._slow = {  # replaced whole, so the fast lane notices by identity
                    "procs": [{"name": n, "cpu": round(c, 1), "mem": m}
                              for n, (c, m) in sorted(procs.items(), key=lambda kv: -kv[1][0])[:6]] if procs else [],
                    "volumes": disks, "counts": counts, "battery": _battery(),
                    "uptime_s": _k32.GetTickCount64() // 1000,
                    "pcie": {"rx": g["pcie_rx"], "tx": g["pcie_tx"]} if g.get("pcie_rx") is not None else None,
                }
                time.sleep(SLOW_S)
        finally:
            ctr.close()

    def _sample(self, ctr, threads: list) -> dict:
        cpu = round(sum(threads) / len(threads), 1) if threads else None
        perf = ctr.items("perf")  # effective clock = nominal clock x "% Processor Performance"
        mhz = [round(self._base * perf[f"0,{i}"] / 100) if self._base and f"0,{i}" in perf else None
               for i in range(NCPU)]
        clocks = [m for m in mhz if m]
        mem, _ = _memory()
        net_rx, net_tx = sum(ctr.items("net_rx").values()), sum(ctr.items("net_tx").values())
        disk_r, disk_w = ctr.items("disk_r").get("_Total", 0.0), ctr.items("disk_w").get("_Total", 0.0)
        idle = ctr.items("disk_idle").get("_Total")
        gpu = _nvidia()
        if gpu is None:  # other GPUs: Windows' own GPU counters (like Task Manager)
            eng, vram = ctr.items("gpu_eng"), ctr.items("gpu_mem")
            gpu = {"name": None, "util": round(min(100.0, sum(v for k, v in eng.items() if k.endswith("engtype_3D")))),
                   "vram_used": max(vram.values(), default=None)}
        h = self._hist
        h["cpu"].append(cpu or 0)
        h["mem"].append(round(100 * mem["used"] / mem["total"], 1) if mem["total"] else 0)
        h["gpu"].append(gpu.get("util") or 0)
        for k, v in (("net_rx", net_rx), ("net_tx", net_tx), ("disk_r", disk_r), ("disk_w", disk_w)):
            h[k].append(round(v))
        return {
            "cpu": {"total": cpu, "threads": threads, "mhz": mhz,
                    "clock": round(sum(clocks) / len(clocks)) if clocks else None},
            "mem": mem, "gpu": gpu, "net": {"rx": round(net_rx), "tx": round(net_tx)},
            "disk": {"read": round(disk_r), "write": round(disk_w),
                     "active": round(max(0.0, 100 - idle), 1) if idle is not None else None},
        }


_k32.GetTickCount64.restype = ctypes.c_uint64
MONITOR = Monitor()
