"""What weight training this machine can actually do: a factual probe, no installation, no download.

`python -m auditlab.research.training probe-hardware` prints CPU, memory, GPUs, CUDA visibility and the
training packages, then a verdict with concrete blockers. LoRA SFT of the instruction-tuned open models we
would compare (7B class) needs a CUDA GPU with roughly 16 GB+ of memory even with 4-bit loading; a CPU or
a shared-memory integrated GPU is not a supported training device for the local_lora backend.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGES = ("torch", "transformers", "peft", "trl", "datasets", "vllm")
MIN_GPU_MEMORY_GB = 16.0


def _memory_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_avail", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_avail", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
            status = Status()
            status.length = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.total / 1024 ** 3, 1)
        with open("/proc/meminfo", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 ** 2, 1)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _display_adapters() -> list[str]:
    """Names of display adapters as the OS reports them (informational; not proof of a training device)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                                 capture_output=True, text=True, timeout=20).stdout
            return [line.strip() for line in out.splitlines() if line.strip()]
        if shutil.which("lspci"):
            out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=20).stdout
            return [line.split(": ", 1)[-1] for line in out.splitlines() if "VGA" in line or "3D controller" in line]
    except (OSError, subprocess.SubprocessError):
        return []
    return []


def _nvidia() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.splitlines():
        if "," in line:
            name, memory = line.rsplit(",", 1)
            try:
                gpus.append({"name": name.strip(), "memory_gb": round(float(memory) / 1024, 1)})
            except ValueError:
                continue
    return gpus


REPOSITORY = Path(__file__).resolve().parents[3]


def _installed(name: str) -> bool:
    """An installed distribution, not a same-named folder of this repository (e.g. AuditLab's own datasets/)."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False
    locations = [spec.origin] if spec.origin else list(spec.submodule_search_locations or [])
    return not any(loc and Path(loc).resolve().is_relative_to(REPOSITORY) for loc in locations)


def probe() -> dict:
    packages = {name: _installed(name) for name in PACKAGES}
    cuda = None
    if packages["torch"]:
        try:
            import torch
            cuda = {"available": bool(torch.cuda.is_available()), "devices": torch.cuda.device_count()}
        except Exception as error:  # noqa: BLE001 - a broken install is itself a finding
            cuda = {"available": False, "error": type(error).__name__}
    nvidia = _nvidia()
    blockers = []
    if not nvidia:
        blockers.append("no NVIDIA GPU visible (nvidia-smi absent or reports none): the local_lora backend needs CUDA")
    elif max(g["memory_gb"] for g in nvidia) < MIN_GPU_MEMORY_GB:
        blockers.append(f"largest GPU has under {MIN_GPU_MEMORY_GB:.0f} GB memory")
    missing = [name for name in ("torch", "transformers", "peft", "trl", "datasets") if not packages[name]]
    if missing:
        blockers.append("training packages not installed: " + ", ".join(missing) +
                        " (docker/train-gpu/requirements.in; pinning and lock belong to the integrator)")
    if not packages["vllm"]:
        blockers.append("vllm not installed: needed to serve the trained adapter for re-evaluation")
    if packages["torch"] and not (cuda or {}).get("available"):
        blockers.append("torch reports no CUDA device")
    return {"schema_version": "1.0", "kind": "hardware_probe", "platform": platform.platform(),
            "python": platform.python_version(), "cpu": platform.processor() or platform.machine(),
            "cpu_count": os.cpu_count(), "memory_gb": _memory_gb(), "display_adapters": _display_adapters(),
            "nvidia_gpus": nvidia, "packages": packages, "cuda": cuda,
            "weight_training_possible": not blockers, "blockers": blockers,
            "note": "Facts of this machine only; nothing was installed or downloaded."}


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
