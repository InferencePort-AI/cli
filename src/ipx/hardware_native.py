"""Native hardware inputs for the desktop rating schema.

CPU and memory use the same OS sources/units as systeminformation 5.33.x.
GPU capacity uses 64-bit DXGI/registry values, DRM sysfs and profiler data;
utilization is never inferred from per-process DXGI budgets or fabricated.
"""
from __future__ import annotations

import ctypes
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import psutil

MIB = 1024**2
GIB = 1024**3


def run(args, timeout=15):
    options = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout, env=dict(os.environ, LC_ALL="C"), **options)
    if proc.returncode and not proc.stdout:
        raise OSError(f"Hardware command failed: {args[0]}")
    return proc.stdout.decode("utf-8", "replace")


def powershell(script):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); " + script])


def values(text):
    return {key.strip().lower(): value.strip() for line in text.splitlines()
            for key, sep, value in [line.partition(":")] if sep}


def normalize_brand(name):
    name = name.split("@", 1)[0].strip()
    for token, symbol in (("(R)", "\u00ae"), ("(TM)", "\u2122"), ("(C)", "\u00a9")):
        name = name.replace(token, symbol)
    name = re.sub(r"CPU+", "", name)
    parts = re.sub(r"\s+", " ", name).strip().split(" ")
    return " ".join(parts[1:])


def cache_bytes(value):
    match = re.match(r"\s*(\d+)", str(value))
    if not match: return 0
    return int(match[1]) * (MIB if "M" in str(value) else 1024 if "K" in str(value) else 1)


def windows_flags():
    # Match the desktop's registry FeatureSet algorithm. It does not expose
    # CPUID leaf 7 (AVX2/AVX512); do not silently change the reference score.
    import winreg
    names = "fpu vme de pse tsc msr pae mce cx8 apic - sep mtrr pge mca cmov pat pse-36 psn clfsh - ds acpi mmx fxsr sse sse2 ss htt tm ia64 pbe".split()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            feature = winreg.QueryValueEx(key, "FeatureSet")[0]
        bits = f"{feature:032b}"
        return " ".join(name for name, bit in zip(names, bits) if name != "-" and bit == "1")
    except OSError:
        return ""


@lru_cache(maxsize=1)
def cpu_snapshot():
    cores, cache, flags, brand = psutil.cpu_count() or 0, 0, "", "unknown"
    if sys.platform == "win32":
        rows = json.loads(powershell("@(Get-CimInstance Win32_Processor | Select-Object Name,L3CacheSize,NumberOfLogicalProcessors) | ConvertTo-Json -Compress"))
        rows = rows if isinstance(rows, list) else [rows]
        if rows:
            brand = normalize_brand(rows[0].get("Name", ""))
            cache = int(rows[0].get("L3CacheSize") or 0) * 1024
            cores = sum(int(row.get("NumberOfLogicalProcessors") or 0) for row in rows) or cores
        flags = windows_flags()
    elif sys.platform == "darwin":
        data = values(run(["sysctl", "machdep.cpu.brand_string", "hw.ncpu", "hw.l3cachesize", "machdep.cpu.features"]))
        brand = normalize_brand(data.get("machdep.cpu.brand_string", "unknown"))
        cores = int(data.get("hw.ncpu", cores))
        cache = cache_bytes(data.get("hw.l3cachesize", 0))
        flags = data.get("machdep.cpu.features", "").lower()
    elif sys.platform.startswith("linux"):
        try:
            data = values(run(["lscpu"]))
        except OSError:
            data = {}
        proc = values(Path("/proc/cpuinfo").read_text())
        name = data.get("bios model name") or data.get("model name") or proc.get("model name", "unknown")
        brand = normalize_brand(name.split("Unknown")[0])
        cache = cache_bytes(data.get("l3 cache", 0))
        flags = data.get("flags") or proc.get("features", "")
    else:
        raise OSError(f"Unsupported hardware platform: {sys.platform}")
    return {"brand": brand, "cores": cores, "cache": {"l3": cache}}, flags


def memory_snapshot():
    mem = psutil.virtual_memory()
    total, available = mem.total, mem.available
    if sys.platform.startswith("linux"):
        data = values(Path("/proc/meminfo").read_text())
        number = lambda key: int(data.get(key, "0").split()[0]) * 1024
        total = number("memtotal") or total
        available = number("memavailable") or sum(number(k) for k in ("memfree", "buffers", "cached", "slab"))
    elif sys.platform == "darwin":
        # systeminformation 5.33.15 (locked by desktop) counts unreclaimable
        # anonymous - purgeable + wired + compressed pages, not Pages active.
        # Apple Silicon uses 16 KiB pages; Intel Macs generally use 4 KiB.
        page_size = int(run(["sysctl", "-n", "vm.pagesize"]).strip())
        pages = values(run(["vm_stat"]))
        count = lambda key: int(pages.get(key, "0").rstrip("."))
        active = (count("anonymous pages") - count("pages purgeable")
                  + count("pages wired down") + count("pages occupied by compressor"))
        available = total - active * page_size
    return {"total": total, "available": available}


def windows_registry_memory():
    import winreg
    result = {}
    root = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                        device = winreg.QueryValueEx(key, "MatchingDeviceId")[0].upper().removeprefix("PCI\\")
                        size = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")[0]
                        if isinstance(size, bytes): size = int.from_bytes(size, "little")
                        if isinstance(size, int) and size > 0: result[device] = size
                except OSError:
                    continue
    except OSError:
        pass
    return result


def dxgi_adapters():
    """64-bit physical capacity fallback; no dependency on nvidia-smi.

    DXGI QueryVideoMemoryInfo is a process budget, NOT device free VRAM, so
    deliberately do not label Budget-CurrentUsage as free device memory.
    """
    import uuid
    from ctypes import wintypes as w
    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]
    class Desc(ctypes.Structure):
        _fields_ = [("name", w.WCHAR * 128), ("vendor", w.UINT), ("device", w.UINT),
                    ("subsystem", w.UINT), ("revision", w.UINT), ("dedicated", ctypes.c_size_t),
                    ("system", ctypes.c_size_t), ("shared", ctypes.c_size_t), ("luid_low", w.DWORD), ("luid_high", w.LONG)]
    def method(ptr, index, restype, *args):
        table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(table[index])
    factory = ctypes.c_void_p()
    iid = GUID((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("7b7166ec-21c7-44ae-b21a-c9ae321ae369").bytes_le))
    create = ctypes.WinDLL("dxgi.dll").CreateDXGIFactory1
    create.argtypes, create.restype = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], w.LONG
    if create(ctypes.byref(iid), ctypes.byref(factory)) < 0: return []
    result = []
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            if method(factory, 7, w.LONG, w.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, index, ctypes.byref(adapter)) < 0: break
            try:
                desc = Desc()
                if method(adapter, 8, w.LONG, ctypes.POINTER(Desc))(adapter, ctypes.byref(desc)) >= 0:
                    result.append({"model": desc.name, "vendorId": desc.vendor, "deviceId": desc.device, "bytes": desc.dedicated})
            finally:
                method(adapter, 2, w.ULONG)(adapter)
            index += 1
    finally:
        method(factory, 2, w.ULONG)(factory)
    return result


def windows_gpus():
    rows = json.loads(powershell("@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,AdapterRAM,PNPDeviceID) | ConvertTo-Json -Compress"))
    rows = rows if isinstance(rows, list) else [rows]
    registry = windows_registry_memory()
    try: adapters = dxgi_adapters()
    except OSError: adapters = []
    result = []
    for row in rows:
        if not row: continue
        pnp = (row.get("PNPDeviceID") or "").upper()
        matched = [(key, value) for key, value in registry.items() if all(token in pnp for token in key.split("&"))]
        size = max(matched, key=lambda pair: len(pair[0]))[1] if matched else None
        if size is None:
            match = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", pnp)
            native = next((a for a in adapters if match and a["vendorId"] == int(match[1], 16) and a["deviceId"] == int(match[2], 16)), None)
            size = native["bytes"] if native else row.get("AdapterRAM")
        result.append({"model": row.get("Name", ""), "vendor": row.get("AdapterCompatibility", ""),
                       "vram": size / MIB if size and size > 0 else None, "busAddress": pnp})
    return result


def linux_gpus(root=Path("/sys/class/drm")):
    result = []
    names, pci_gpus = {}, {}
    if shutil.which("lspci"):
        for line in run(["lspci", "-Dmm"]).splitlines():
            # quoted PCI names, keyed by full domain:bus:device.function.
            import shlex
            fields = shlex.split(line)
            if len(fields) >= 4:
                names[fields[0]] = (fields[2], fields[3])
                if re.search(r"VGA|3D controller|Display controller", fields[1], re.I):
                    pci_gpus[fields[0]] = (fields[2], fields[3])
    for card in sorted(root.glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name): continue
        device = card / "device"
        if not device.exists(): continue
        address = device.resolve().name
        def number(name):
            try: return int((device / name).read_text().strip(), 0)
            except (OSError, ValueError): return None
        vendor_id = number("vendor")
        vendor, model = names.get(address, ({0x1002: "AMD", 0x10de: "NVIDIA", 0x8086: "Intel"}.get(vendor_id, ""), ""))
        total, used = number("mem_info_vram_total"), number("mem_info_vram_used")
        # Some AMD devices expose total/used through the amdgpu DRM sysfs ABI.
        # Those are adapter-wide byte counts, including other applications.
        result.append({"model": model, "vendor": vendor, "busAddress": address,
                       "vram": total / MIB if total and total > 0 else None,
                       "memoryFree": max(0, total - used) / MIB if total is not None and used is not None else None})
    known = {row["busAddress"] for row in result}
    for address, (vendor, model) in pci_gpus.items():
        if address not in known:
            result.append({"model": model, "vendor": vendor, "busAddress": address, "vram": None, "memoryFree": None})
    return result


def mac_gpus():
    data = plistlib.loads(run(["system_profiler", "-xml", "-detailLevel", "full", "SPDisplaysDataType"], timeout=30).encode())
    result = []
    for block in data:
        for row in block.get("_items", []):
            def size(key):
                text = row.get(key, "")
                match = re.match(r"(\d+)", text)
                return int(match[1]) * (1024 if "GB" in text else 1) if match else None
            memory = size("spdisplays_vram") or size("spdisplays_vram_shared")
            # On unified-memory Apple Silicon, absent VRAM stays null. RAM is
            # already included in mem.total; counting it again would be false.
            result.append({"model": row.get("sppci_model", ""), "vendor": row.get("spdisplays_vendor", ""), "vram": memory})
    return result


def enrich_nvidia(controllers):
    """NVML queries the driver directly, including when nvidia-smi is absent."""
    try:
        import pynvml
    except ImportError:
        return controllers
    initialized = False
    try:
        pynvml.nvmlInit()
        initialized = True
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            name = pynvml.nvmlDeviceGetName(handle)
            name = name.decode() if isinstance(name, bytes) else name
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            pci = pynvml.nvmlDeviceGetPciInfo(handle)
            address = pci.busId.decode() if isinstance(pci.busId, bytes) else pci.busId
            device_id = getattr(pci, "pciDeviceId", None)
            pci_token = f"VEN_{device_id & 0xffff:04X}&DEV_{device_id >> 16:04X}" if device_id else None
            row = next((c for c in controllers if c.get("busAddress", "").lower().endswith(address.lower()[4:]) or c.get("model") == name
                        or pci_token and pci_token in c.get("busAddress", "")), None)
            if row is None:
                row = {"model": name, "vendor": "NVIDIA", "busAddress": address}
                controllers.append(row)
            row.update(vram=memory.total / MIB, memoryTotal=memory.total / MIB, memoryFree=memory.free / MIB)
    except pynvml.NVMLError:
        pass  # preserve successful OS measurements when an optional driver fails
    finally:
        if initialized:
            try: pynvml.nvmlShutdown()
            except pynvml.NVMLError: pass
    return controllers


def detect_backend():
    if sys.platform == "darwin": return "metal"
    # Explicit vendor root allows matching the desktop's exact bundled build.
    # Otherwise inspect the installation that actually serves this CLI host.
    configured = os.environ.get("IPX_OLLAMA_VENDOR_ROOT")
    binary = shutil.which("ollama")
    roots = [Path(configured) / "lib" / "ollama"] if configured else []
    if binary and not configured:
        directory = Path(binary).resolve().parent
        roots += [directory / "lib" / "ollama", directory / "lib"]
    if not configured:
        roots += [Path("/usr/lib/ollama"), Path("/usr/local/lib/ollama")]
    for root in roots:
        if root.is_dir():
            names = [p.name.lower() for p in root.iterdir()]
            if any("rocm" in name for name in names): return "rocm"
            if any("cuda" in name for name in names): return "cuda"
            return "cpu"
    return "unknown"


def inspect_hardware():
    cpu, flags = cpu_snapshot()
    failed = False
    try:
        controllers = windows_gpus() if sys.platform == "win32" else mac_gpus() if sys.platform == "darwin" else linux_gpus()
    except (OSError, ValueError, subprocess.SubprocessError):
        controllers, failed = [], True
    # Optional acceleration probes fail independently of OS enumeration.
    controllers = enrich_nvidia(controllers)
    return {"cpu": cpu, "flags": flags, "mem": memory_snapshot(), "controllers": controllers,
            "backend": detect_backend(), "gpuDetectionFailed": failed}
