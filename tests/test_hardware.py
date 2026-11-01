"""The hardware Annona is deployed on, tested without owning it.

A DGX Spark and a Ryzen AI Max box are what customers ask about. What can be
checked on any machine is checked here: that each is recognised, that the model
named for it fits its memory, that a model running on the CPU instead of the GPU
is reported, and that the compose file starts on a machine with no NVIDIA card.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from runner.cli_setup import models_on_cpu, recommend_model
from runner.services.host import describe

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ("Linux", "aarch64", "Neoverse", "NVIDIA GB10", False),
            "NVIDIA GB10 (DGX Spark) · CUDA · unified memory",
        ),
        (
            (
                "Linux",
                "x86_64",
                "AMD Ryzen AI Max+ 395 w/ Radeon 8060S",
                "",
                True,
            ),
            "AMD Radeon (Strix Halo) · Vulkan/ROCm · unified memory",
        ),
        (("Linux", "x86_64", "AMD Ryzen 9 7950X", "", True), "AMD Radeon · Vulkan/ROCm"),
        (
            ("Linux", "x86_64", "Intel Xeon", "NVIDIA L40S", False),
            "NVIDIA L40S · CUDA",
        ),
        (("Darwin", "arm64", "Apple M1 Pro", "", False), "Apple GPU · Metal · unified memory"),
        (("Linux", "x86_64", "Intel Xeon", "", False), "CPU only"),
    ],
)
def test_each_machine_is_recognised(args, expected):
    assert describe(*args) == expected


@pytest.mark.parametrize(
    ("memory_gb", "model"),
    [
        (128, "qwen2.5:72b"),
        (96, "qwen2.5:72b"),
        (64, "qwen2.5:32b"),
        (24, "qwen2.5:14b"),
        (16, "qwen2.5:7b"),
        (8, "qwen2.5:3b"),
    ],
)
def test_the_named_model_fits_the_memory(memory_gb, model):
    assert recommend_model(memory_gb) == model


class _Ollama(BaseHTTPRequestHandler):
    loaded: list[dict] = []

    def do_GET(self):  # noqa: N802 - http.server's name
        body = json.dumps({"models": self.loaded}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@pytest.fixture
def ollama():
    server = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_a_model_loaded_on_the_cpu_is_named(ollama):
    """The silent failure on a GPU box: every answer arrives, ten times slower."""
    _Ollama.loaded = [
        {"name": "qwen2.5:72b", "size": 47_000_000_000, "size_vram": 0},
        {"name": "bge-m3", "size": 1_200_000_000, "size_vram": 1_200_000_000},
    ]
    assert models_on_cpu(ollama) == ["qwen2.5:72b"]
    assert models_on_cpu(ollama + "/v1") == ["qwen2.5:72b"]  # an openai-style endpoint too


def test_an_idle_or_absent_runtime_is_not_a_failure(ollama):
    _Ollama.loaded = []
    assert models_on_cpu(ollama) == []
    assert models_on_cpu("http://127.0.0.1:9") == []


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def test_the_base_compose_file_asks_for_no_gpu():
    """An NVIDIA reservation here made `docker compose up` fail on a Mac or an AMD box."""
    assert "deploy" not in _compose("docker-compose.yml")["services"]["ollama"]


def test_the_nvidia_overlay_reserves_the_gpu():
    devices = _compose("docker-compose.nvidia.yml")["services"]["ollama"]["deploy"]["resources"][
        "reservations"
    ]["devices"]
    assert devices[0]["driver"] == "nvidia"


def test_the_amd_overlay_passes_the_kernel_driver_devices():
    ollama = _compose("docker-compose.amd.yml")["services"]["ollama"]
    assert {"/dev/kfd", "/dev/dri"} <= set(ollama["devices"])
    assert "rocm" in ollama["image"]
