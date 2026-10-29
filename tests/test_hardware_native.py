"""Native input fidelity, driver failure, and cross-platform fixture coverage."""
import json
import plistlib
import sys
from types import SimpleNamespace

import pytest

from ipx import hardware_native as native


def test_cpu_brand_and_cache_units():
    assert native.normalize_brand("Intel(R) Core(TM) i7-12700 CPU @ 2.10GHz") == "Core\u2122 i7-12700"
    assert native.normalize_brand("AMD Ryzen 9 7950X 16-Core Processor") == "Ryzen 9 7950X 16-Core Processor"
    assert native.normalize_brand("Apple M4 Pro") == "M4 Pro"
    assert native.cache_bytes("32 MiB (2 instances)") == 32 * 1024**2
    assert native.cache_bytes("4096 KB") == 4 * 1024**2


def test_windows_64bit_vram_without_smi(monkeypatch):
    monkeypatch.setattr(native, "powershell", lambda _: json.dumps([
        {"Name": "RTX 5090", "AdapterCompatibility": "NVIDIA", "AdapterRAM": 0xffffffff,
         "PNPDeviceID": r"PCI\VEN_10DE&DEV_1234&SUBSYS_00000001&REV_01\0"}]))
    monkeypatch.setattr(native, "windows_registry_memory", lambda: {"VEN_10DE&DEV_1234": 32 * native.GIB})
    monkeypatch.setattr(native, "dxgi_adapters", lambda: [])
    assert native.windows_gpus()[0]["vram"] == 32768


def test_dxgi_capacity_fallback_without_registry(monkeypatch):
    monkeypatch.setattr(native, "powershell", lambda _: json.dumps([
        {"Name": "AMD", "AdapterCompatibility": "AMD", "AdapterRAM": 0xffffffff,
         "PNPDeviceID": r"PCI\VEN_1002&DEV_1234\0"}]))
    monkeypatch.setattr(native, "windows_registry_memory", lambda: {})
    monkeypatch.setattr(native, "dxgi_adapters", lambda: [{"vendorId": 0x1002, "deviceId": 0x1234, "bytes": 24 * native.GIB}])
    assert native.windows_gpus()[0]["vram"] == 24576


def test_linux_amd_total_and_free_vram(tmp_path, monkeypatch):
    device = tmp_path / "card0" / "device"
    device.mkdir(parents=True)
    for name, value in {"vendor": "0x1002", "mem_info_vram_total": str(24 * native.GIB), "mem_info_vram_used": str(5 * native.GIB)}.items():
        (device / name).write_text(value)
    # Connectors are not adapters.
    (tmp_path / "card0-HDMI-A-1").mkdir()
    monkeypatch.setattr(native.shutil, "which", lambda _: None)
    rows = native.linux_gpus(tmp_path)
    assert len(rows) == 1
    assert rows[0]["vendor"] == "AMD" and rows[0]["vram"] == 24576 and rows[0]["memoryFree"] == 19456
    (device / "mem_info_vram_used").unlink()
    assert native.linux_gpus(tmp_path)[0]["memoryFree"] is None


def test_macos_unified_memory_is_not_double_counted(monkeypatch):
    fixture = [{"_items": [{"sppci_model": "Apple M4 Pro", "spdisplays_vendor": "sppci_vendor_Apple"},
                           {"sppci_model": "AMD Radeon Pro", "spdisplays_vendor": "AMD", "spdisplays_vram": "16 GB"}]}]
    monkeypatch.setattr(native, "run", lambda *a, **kw: plistlib.dumps(fixture).decode())
    rows = native.mac_gpus()
    assert rows[0]["vram"] is None
    assert rows[1]["vram"] == 16384


def test_macos_available_memory_uses_native_page_size(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native.psutil, "virtual_memory", lambda: SimpleNamespace(total=32*native.GIB, available=1))
    monkeypatch.setattr(native, "run", lambda args: "16384" if args[0] == "sysctl" else "Pages active: 700000.\nPages inactive: 200000.\nAnonymous pages: 100000.\nPages purgeable: 20000.\nPages wired down: 30000.\nPages occupied by compressor: 40000.")
    assert native.memory_snapshot() == {"total": 32*native.GIB, "available": 32*native.GIB-150000*16384}


def test_linux_memory_accounting_matches_memavailable(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.psutil, "virtual_memory", lambda: SimpleNamespace(total=1, available=1))
    monkeypatch.setattr(native.Path, "read_text", lambda self: "MemTotal: 32768000 kB\nMemAvailable: 16000000 kB\nSlab: 1000 kB\n")
    assert native.memory_snapshot() == {"total": 32768000*1024, "available": 16000000*1024}


def test_vendor_root_matches_exact_desktop_build(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "ollama"
    lib.mkdir(parents=True)
    (lib / "cuda_v12").mkdir()
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setenv("IPX_OLLAMA_VENDOR_ROOT", str(tmp_path))
    assert native.detect_backend() == "cuda"
    (lib / "rocm").mkdir()
    assert native.detect_backend() == "rocm"


def test_nvml_driver_failure_keeps_os_measurements(monkeypatch):
    class Error(Exception): pass
    def fail(): raise Error()
    monkeypatch.setitem(sys.modules, "pynvml", SimpleNamespace(NVMLError=Error, nvmlInit=fail))
    rows = [{"model": "AMD", "vram": 24000}]
    assert native.enrich_nvidia(rows) == rows


def test_nvml_direct_driver_memory_without_smi(monkeypatch):
    closed = []
    fake = SimpleNamespace(NVMLError=RuntimeError, nvmlInit=lambda: None, nvmlShutdown=lambda: closed.append(True),
                           nvmlDeviceGetCount=lambda: 1, nvmlDeviceGetHandleByIndex=lambda i: i,
                           nvmlDeviceGetName=lambda _: "RTX 5090", nvmlDeviceGetPciInfo=lambda _: SimpleNamespace(busId="00000000:01:00.0"),
                           nvmlDeviceGetMemoryInfo=lambda _: SimpleNamespace(total=32*native.GIB, free=25*native.GIB))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    rows = native.enrich_nvidia([])
    assert rows[0]["vram"] == 32768 and rows[0]["memoryFree"] == 25600 and closed


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native DXGI")
def test_native_dxgi_smoke():
    for adapter in native.dxgi_adapters():
        assert isinstance(adapter["bytes"], int) and adapter["bytes"] >= 0
