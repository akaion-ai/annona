"""The machine under the perimeter, sampled when someone asks for the numbers.

No sampler thread: /metrics and the Monitor view are the only readers, and each
read refreshes the gauges first. CPU is the share used since the previous read,
which is what a scraper polling every few seconds wants.

GPU utilisation is not here yet (NVML on NVIDIA, ioreg on Apple Silicon — see
docs/design/observability.md, O3). What Ollama holds on the GPU is: /api/ps.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Iterable
from functools import cache
from pathlib import Path

import httpx
import psutil

from runner.audit.metrics import METRICS
from runner.kernel.errors import PolicyError
from runner.policy.loader import load_policy
from runner.services.enforcement import policy_path

__all__ = ["describe", "machine", "model_memory_gb", "sample"]


def _ollama_endpoints() -> list[str]:
    """The Ollama substrates the policy declares — the ones worth asking."""
    path = policy_path()
    try:
        policy = load_policy(path) if path.exists() else None
    except PolicyError:
        return []
    return (
        [s.endpoint for s in policy.substrates if s.kind == "ollama" and s.endpoint]
        if policy
        else []
    )


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _linux_cpu_name() -> str:
    """``platform.processor()`` is empty on Linux; /proc/cpuinfo is not."""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith(("model name", "hardware")):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _amd_gpu_present() -> bool:
    """An AMD GPU on Linux: PCI vendor 0x1002 under /sys/class/drm."""
    try:
        return any(
            p.read_text().strip() == "0x1002"
            for p in Path("/sys/class/drm").glob("card*/device/vendor")
        )
    except OSError:
        return False


def describe(os_: str, arch: str, chip: str, nvidia_gpu: str = "", amd_gpu: bool = False) -> str:
    """Where a local model runs on this machine, in one line.

    Pure, so the hardware Annona is sold on — a DGX Spark, a Ryzen AI Max box —
    is tested without owning one. Unified memory is named because it decides
    which model fits: on those three the GPU's memory *is* the machine's.
    """
    if os_ == "Darwin":
        if arch == "arm64":
            return "Apple GPU · Metal · unified memory"
        return "CPU (Intel Mac, no Metal offload)"
    if nvidia_gpu:
        if "GB10" in nvidia_gpu:
            return "NVIDIA GB10 (DGX Spark) · CUDA · unified memory"
        # nvidia-smi already says "NVIDIA L40S"; prefixing it made "NVIDIA NVIDIA".
        name = nvidia_gpu if nvidia_gpu.startswith("NVIDIA") else f"NVIDIA {nvidia_gpu}"
        return f"{name} · CUDA"
    if amd_gpu:
        if "Ryzen AI Max" in chip:
            return "AMD Radeon (Strix Halo) · Vulkan/ROCm · unified memory"
        return "AMD Radeon · Vulkan/ROCm"
    return "CPU only"


def _nvidia_gpu() -> str:
    if not shutil.which("nvidia-smi"):
        return ""
    names = _run("nvidia-smi", "--query-gpu=name", "--format=csv,noheader").splitlines()
    return names[0].strip() if names else ""


def model_memory_gb() -> float:
    """Memory a local model can use: the card's on a discrete NVIDIA GPU, else RAM.

    On Apple Silicon, GB10 and Strix Halo the GPU addresses system memory, so
    RAM is the right number; a discrete card is limited by its own.
    """
    gpu = _nvidia_gpu()
    if gpu and "GB10" not in gpu:
        mib = _run("nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits")
        try:
            return float(mib.splitlines()[0]) / 1024
        except (ValueError, IndexError):
            pass
    return psutil.virtual_memory().total / 2**30


@cache
def machine() -> dict[str, str]:
    """What the machine is and where a local model runs. It does not change, so once."""
    os_, arch = platform.system(), platform.machine().lower()
    chip = platform.processor() or arch
    if os_ == "Darwin":
        chip = _run("sysctl", "-n", "machdep.cpu.brand_string") or chip
    elif os_ == "Linux":
        chip = _linux_cpu_name() or chip
    accelerator = describe(
        os_, arch, chip, nvidia_gpu=_nvidia_gpu(), amd_gpu=os_ == "Linux" and _amd_gpu_present()
    )
    return {"os": os_, "arch": arch, "chip": chip, "accelerator": accelerator}


def sample(ollama_endpoints: Iterable[str] | None = None) -> None:
    """Refresh the host and Ollama gauges in the process registry."""
    if ollama_endpoints is None:
        ollama_endpoints = _ollama_endpoints()
    METRICS.set("annona_host_cpu_ratio", psutil.cpu_percent(interval=None) / 100)
    memory = psutil.virtual_memory()
    METRICS.set("annona_host_memory_bytes", memory.total - memory.available, kind="used")
    METRICS.set("annona_host_memory_bytes", memory.total, kind="total")
    METRICS.set("annona_process_resident_bytes", psutil.Process().memory_info().rss)
    METRICS.set("annona_host_info", 1, **machine())

    # A model Ollama evicted must disappear, not linger at its last size.
    METRICS.clear("annona_ollama_loaded_bytes")
    for endpoint in dict.fromkeys(e.rstrip("/").removesuffix("/v1") for e in ollama_endpoints):
        try:
            models = httpx.get(f"{endpoint}/api/ps", timeout=1.0).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            continue  # Ollama down: substrate_up already says so.
        for m in models:
            name = str(m.get("name", ""))
            METRICS.set(
                "annona_ollama_loaded_bytes", float(m.get("size", 0)), model=name, kind="total"
            )
            METRICS.set(
                "annona_ollama_loaded_bytes", float(m.get("size_vram", 0)), model=name, kind="gpu"
            )
