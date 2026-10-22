"""Hardware rating algorithms ported from desktop sysinfo.ts.

Inspection is isolated from scoring so cross-platform measurements can be
validated against identical desktop systeminformation snapshots.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

import psutil

GPU_TFLOPS_DB = {'rtx 5090': 105.0, 'rtx 5080': 56.0, 'rtx 5070 ti': 44.0, 'rtx 5070': 36.0, 'rtx 4090': 82.6, 'rtx 4080 super': 55.3, 'rtx 4080': 52.1, 'rtx 4070 ti super': 44.1, 'rtx 4070 ti': 40.1, 'rtx 4070 super': 35.5, 'rtx 4070': 29.1, 'rtx 4060 ti': 22.1, 'rtx 4060': 15.1, 'rtx 3090 ti': 40.0, 'rtx 3090': 35.6, 'rtx 3080 ti': 34.1, 'rtx 3080': 29.8, 'rtx 3070 ti': 21.8, 'rtx 3070': 20.3, 'rtx 3060 ti': 16.2, 'rtx 3060': 12.7, 'rtx 2080 ti': 13.4, 'rtx 2080 super': 11.2, 'rtx 2080': 10.1, 'rtx 2070 super': 9.1, 'rtx 2070': 7.5, 'rtx 2060 super': 7.2, 'rtx 2060': 6.5, 'gtx 1660 ti': 5.5, 'gtx 1660 super': 5.2, 'gtx 1660': 5.0, 'gtx 1080 ti': 11.3, 'gtx 1080': 8.9, 'gtx 1070 ti': 8.1, 'gtx 1070': 6.5, 'gtx 1060': 4.4, 'rx 7900 xtx': 61.4, 'rx 7900 xt': 51.5, 'rx 7900 gre': 38.5, 'rx 7800 xt': 37.2, 'rx 7700 xt': 34.6, 'rx 7600': 22.1, 'rx 6950 xt': 23.4, 'rx 6900 xt': 23.0, 'rx 6800 xt': 20.7, 'rx 6800': 16.8, 'rx 6750 xt': 15.5, 'rx 6700 xt': 13.3, 'rx 6600 xt': 10.4, 'rx 6600': 8.9}  # populated below from the audited reference table


def _run(args):
    options = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    return subprocess.check_output(args, timeout=3, text=True, stderr=subprocess.DEVNULL, **options)


@lru_cache(maxsize=1)
def cpu_snapshot():
    import cpuinfo
    data = cpuinfo.get_cpu_info()
    cache = data.get("l3_cache_size", 0)
    return {"brand": data.get("brand_raw", "Unknown"), "cores": psutil.cpu_count() or 0,
            "cache": {"l3": cache if isinstance(cache, (int, float)) else 0}}, " ".join(data.get("flags", []))


def inspect_hardware():
    cpu, flags = cpu_snapshot()
    memory = psutil.virtual_memory()
    controllers, detection_error = [], False
    try:
        if sys.platform == "win32":
            raw = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,AdapterRAM | ConvertTo-Json -Compress"])
            rows = json.loads(raw)
            for row in rows if isinstance(rows, list) else [rows]:
                controllers.append({"model": row.get("Name", ""), "vendor": row.get("AdapterCompatibility", ""),
                                    "vram": (row.get("AdapterRAM") or 0) / 1024**2})
        elif sys.platform == "darwin":
            rows = json.loads(_run(["system_profiler", "SPDisplaysDataType", "-json"]))
            for row in rows.get("SPDisplaysDataType", []):
                controllers.append({"model": row.get("sppci_model", ""), "vendor": row.get("spdisplays_vendor", "")})
        elif shutil.which("lspci"):
            for line in _run(["lspci"]).splitlines():
                if re.search(r"VGA|3D controller|Display controller", line, re.I):
                    controllers.append({"model": line.split(": ", 1)[-1], "vendor": ""})
        if shutil.which("nvidia-smi"):
            rows = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"])
            nvidia = []
            for row in rows.splitlines():
                name, total, free = row.rsplit(",", 2)
                nvidia.append({"model": name.strip(), "vendor": "NVIDIA", "vram": float(total), "memoryFree": float(free)})
            controllers = [c for c in controllers if classify_vendor(c.get("vendor", ""), c["model"]) != "nvidia"] + nvidia
    except (OSError, subprocess.SubprocessError, ValueError):
        detection_error = True
    backend = "metal" if sys.platform == "darwin" else "unknown"
    # Inspect the actual standalone Ollama installation instead of inventing a
    # backend based merely on the GPU vendor.
    binary = shutil.which("ollama")
    if binary and backend != "metal":
        for root in (Path(binary).parent / "lib" / "ollama", Path(binary).parent / "lib", Path("/usr/lib/ollama")):
            if root.is_dir():
                entries = [p.name.lower() for p in root.iterdir()]
                backend = "rocm" if any("rocm" in p for p in entries) else "cuda" if any("cuda" in p for p in entries) else "cpu"
                break
    return {"cpu": cpu, "flags": flags, "mem": {"total": memory.total, "available": memory.available},
            "controllers": controllers, "backend": backend, "gpuDetectionFailed": detection_error}


def classify_vendor(vendor, model):
    text = f"{vendor} {model}".lower()
    if not text.strip(): return "none"
    for pattern, name in ((r"nvidia|geforce|quadro|rtx|gtx|tesla", "nvidia"),
                          (r"amd|radeon|advanced micro devices|instinct|ati ", "amd"),
                          (r"apple", "apple"), (r"intel|iris|uhd graphics|arc", "intel"),
                          (r"qualcomm|adreno", "qualcomm")):
        if re.search(pattern, text): return name
    return "other"


def format_gb(value):
    if not math.isfinite(value) or value <= 0:
        return "unknown"
    places = 0 if value >= 100 else 1
    rounded = Decimal.from_float(float(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{rounded:.{places}f}"


def parse_size(raw):
    lower = raw.lower()
    if lower.endswith("b"): lower = lower.replace("b", "", 1)
    elif lower.endswith("m"):
        return parse_size(lower.replace("m", "", 1)) / 1000
    elif lower.startswith("e"): lower = lower.replace("e", "", 1)
    match = re.match(r"^[\s]*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))", lower, re.I)
    return float(match[1]) if match else math.nan


def performance(params, score, tflops, vram, estimate):
    if vram > 0 and estimate <= vram * .95:
        stars = 4 if tflops is None else 5 if tflops >= 40 else 4 if tflops >= 25 else 3 if tflops >= 12 else 2
    else:
        if vram > 0 and tflops is not None:
            score -= 2 if tflops < 5 else 1 if tflops < 10 else 0
        stars = (5 if params <= 1 else
                 (5 if score >= 5 else 4 if score >= 3 else 3) if params <= 3 else
                 (4 if score >= 7 else 3 if score >= 5 else 2) if params <= 8 else
                 (3 if score >= 8 else 2 if score >= 6 else 1) if params <= 14 else
                 (2 if score >= 10 else 1) if params <= 34 else 1)
    tier, label = {5: ("excellent", "Excellent"), 4: ("good", "Good"), 3: ("fair", "Fair"),
                   2: ("poor", "Slow"), 1: ("critical", "Very Slow")}[stars]
    return {"tier": tier, "stars": stars, "label": label}


def speed(params, score):
    if params <= 1: return "run smoothly on this system."
    if params <= 3: return "run well on this system." if score >= 3 else "run fine, though not instantly."
    if params <= 8: return "run with reasonable performance." if score >= 5 else "run, but response times may be slow on this CPU."
    if params <= 14: return "run with decent performance." if score >= 6 else "be slow (over 30s per response) on this CPU."
    if params <= 34: return "run, but expect slower generations." if score >= 8 else "be too slow for comfortable interactive use on this CPU."
    return "run, but expect slow generations on this CPU."


def get_hardware_rating(raw, snapshot=None):
    params = parse_size(raw)
    snap = snapshot if snapshot is not None else inspect_hardware()
    cpu, mem, flags = snap.get("cpu") or {}, snap.get("mem") or {}, snap.get("flags") or ""
    ram = (mem.get("total") or 0) / 1024**3
    avail = (mem.get("available") or mem.get("total") or 0) / 1024**3
    avx2, avx512 = "avx2" in flags, "avx512" in flags
    score = avx2 * 2 + avx512 * 2 + (cpu.get("cores", 0) >= 8) + (ram >= 16) + ((cpu.get("cache") or {}).get("l3") or 0) / 10
    gpus = []
    for c in snap.get("controllers", []):
        model, vendor = (c.get("model") or "").strip(), (c.get("vendor") or "").strip()
        if re.search(r"microsoft basic|virtual|citrix|vmware|parallels|parsec|remote|oray|meta", f"{vendor} {model}", re.I): continue
        kind = classify_vendor(vendor, model)
        if kind == "none": continue
        memory = c.get("vram", c.get("memoryTotal"))
        vram = memory / 1024 if memory and memory > 0 else None
        free = c.get("memoryFree")
        gpus.append({"vendor": kind, "model": model, "label": vendor or model, "vram": vram,
                     "free": free / 1024 if free and free > 0 else vram,
                     "dedicated": kind == "nvidia" or (vram is not None and vram >= 3)})
    backend = snap.get("backend", "unknown")
    kind = {"cuda": "nvidia", "rocm": "amd", "metal": "apple"}.get(backend)
    gpu = next((g for g in gpus if g["vendor"] == kind), None) if kind else None
    if backend == "metal" and not gpu and gpus: gpu = gpus[0]
    describe = lambda g: g["model"] or g["label"] or g["vendor"]
    mismatch = ""
    if backend == "cuda" and not any(g["vendor"] == "nvidia" for g in gpus):
        discrete = [g for g in gpus if g["vendor"] not in ("intel", "qualcomm")]
        mismatch = (f"This Ollama build is compiled for NVIDIA (CUDA), but the detected GPU ({describe(discrete[0])}) is not CUDA-capable, so it will fall back to CPU." if discrete else
                    "This Ollama build is compiled for NVIDIA (CUDA), but no NVIDIA GPU was found, so it will run on CPU.")
    elif backend == "rocm" and not any(g["vendor"] == "amd" for g in gpus):
        mismatch = ("This Ollama build is compiled for AMD (ROCm), but the detected NVIDIA GPU is not ROCm-compatible, so it will fall back to CPU." if any(g["vendor"] == "nvidia" for g in gpus) else
                    "This Ollama build is compiled for AMD (ROCm), but no AMD GPU was found, so it will run on CPU.")
    elif backend == "cpu" and any(g["dedicated"] for g in gpus):
        mismatch = "This Ollama build ships without GPU acceleration, so the detected GPU will not be used and inference will run on CPU."
    model = re.sub(r"\s+", " ", gpu["model"].lower()).strip() if gpu else ""
    tflops = next((v for k, v in GPU_TFLOPS_DB.items() if k in model), None)
    vram, free = (gpu["vram"], gpu["free"]) if gpu else (None, None)
    base = {"modelSizeRaw": raw, "modelSizeB": params if math.isfinite(params) else None,
            "cpu": cpu.get("brand", "Unknown"), "cores": cpu.get("cores", 0), "ramGB": format_gb(ram), "ramAvailGB": format_gb(avail),
            "avx2": avx2, "avx512": avx512, "gpu": describe(gpu) if gpu else None,
            "gpuVendor": gpu["vendor"] if gpu else None, "vramGB": vram, "vramFreeGB": free,
            "gpuTflops": tflops, "acceleration": backend, "gpuDetectionError": snap.get("gpuDetectionFailed", False), "performance": None}
    if not math.isfinite(params) or params <= 0:
        base["warning"] = f"Could not estimate the requirements for {raw}. Check the model's size before downloading."
        return base
    est = params * .6 + min(2, max(.5, params * .25))
    dedicated = (free or vram or 0) if gpu and gpu["dedicated"] else 0
    base["performance"] = performance(params, score, tflops, dedicated, est)
    note = f" (~{tflops:g} TFLOPS)" if tflops else ""
    if params > 90 or est > ram * .95 + dedicated:
        capacity = f"{format_gb(dedicated)} GB VRAM + {format_gb(ram)} GB RAM" if dedicated else f"{format_gb(ram)} GB RAM"
        warning = f"{raw} needs roughly {format_gb(est)} GB of memory, but this PC only has {capacity}. It will not fit. Try a smaller model."
    elif dedicated > 0 and est <= dedicated * .95:
        warning = f"{raw} should fit entirely in your GPU (needs ~{format_gb(est)} GB; ~{format_gb(dedicated)} GB VRAM){note}. It should run fast."
    elif est > avail + dedicated:
        warning = f"{raw} needs roughly {format_gb(est)} GB, but only {format_gb(avail + dedicated)} GB is free right now. It may load very slowly or fail. Close other apps or use a smaller model."
    elif dedicated > 0:
        warning = f"{raw} should fit by offloading layers between your GPU (~{format_gb(dedicated)} GB VRAM){note} and system RAM (needs ~{format_gb(est)} GB). Expect good performance, a little slower than running fully on the GPU."
    elif gpu:
        warning = f"{raw} should fit in system memory (needs ~{format_gb(est)} GB; {format_gb(avail)} GB free) and will use GPU acceleration ({describe(gpu)}) for faster inference."
    else:
        warning = f"{raw} should fit in system memory (needs ~{format_gb(est)} GB; {format_gb(avail)} GB free). It will run on CPU and should {speed(params, score)}"
    base["warning"] = warning + (" " + mismatch if mismatch else "")
    return base
