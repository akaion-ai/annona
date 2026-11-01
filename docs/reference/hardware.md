# Hardware

Annona is one Python process and one container image, built for `linux/arm64`
and `linux/amd64`. What changes from one machine to the next is where the *local
model* runs and how big it can be. This page is the setup for the machines
people ask about, and what has been checked on each.

## What has been checked, and how

| Machine | Image | Tests | Run on the hardware |
| --- | --- | --- | --- |
| Apple Silicon (M-series) | arm64 builds in CI | suite on macOS CI | **yes**: qwen2.5:7b, rizzo-pii, the full perimeter |
| NVIDIA DGX Spark (GB10, arm64) | arm64 builds in CI | suite on Linux CI | **not yet**: no unit available |
| AMD Ryzen AI Max (Strix Halo, x86-64) | amd64 builds in CI, acceptance tests run in it | suite on Linux CI | **not yet**: no unit available |
| x86-64 + NVIDIA card | amd64, as above | suite on Linux CI | **not yet** |

"Not yet" is the honest state, not a hedge. The first hour on either box is
`make up`, `annona doctor`, then `make verify` — the acceptance run plants a
canary in a client file and checks that nothing which should stay inside went
out. Until that has run on the machine, the machine is not "supported", it is
"expected to work".

## Which model fits

`annona setup` and `annona doctor` read the machine and name the largest model
of the tested family (qwen2.5) that leaves room to work: a quantised model and
its context should use about 60% of what the GPU can address.

| Memory the GPU can use | Model |
| --- | --- |
| 96 GB or more | `qwen2.5:72b` |
| 40 GB or more | `qwen2.5:32b` |
| 20 GB or more | `qwen2.5:14b` |
| 10 GB or more | `qwen2.5:7b` |
| less | `qwen2.5:3b` |

On Apple Silicon, the DGX Spark and Ryzen AI Max the GPU addresses system
memory, so this is the machine's RAM. On a discrete NVIDIA card it is the card's.

```
annona doctor
  ✓ machine   NVIDIA Grace … · NVIDIA GB10 (DGX Spark) · CUDA · unified memory · 128 GB · fits qwen2.5:72b
```

## NVIDIA DGX Spark

Grace Blackwell (GB10): arm64 CPU and a Blackwell GPU on 128 GB of unified
memory, running DGX OS (Ubuntu). DGX OS ships the NVIDIA driver and the
Container Toolkit, so the GPU reaches containers without extra setup.

```bash
git clone https://github.com/akaion-ai/annona && cd annona
make up                    # detects nvidia-smi → docker-compose.nvidia.yml
docker compose exec annona annona doctor
docker compose exec ollama ollama pull qwen2.5:72b
docker compose exec annona annona setup --yes --endpoint http://ollama:11434 --model qwen2.5:72b
make verify
```

Without Docker, `deploy/annona.service` is the systemd unit, and Ollama's own
installer detects arm64 and CUDA.

**The failure to look for** is a model that answers from the CPU: a container
started without the NVIDIA overlay, or a runtime that did not find CUDA. Every
answer still comes back, many times slower, and nothing errors. `annona doctor`
reports it as `gpu local-gpu: … loaded on the CPU, not the GPU`, read from what
Ollama says it holds in GPU memory.

## AMD Ryzen AI Max (Strix Halo)

Ryzen AI Max+ 395 and siblings: x86-64 cores and a Radeon iGPU (gfx1151) on up
to 128 GB of unified memory — the YO2 395 is one of these.

```bash
make up                    # detects /dev/kfd → docker-compose.amd.yml
docker compose exec annona annona doctor
```

Two backends reach this GPU, and which one is faster depends on the model and
the driver release; reports from people running it disagree. Start with the
default and let `doctor` decide:

| Backend | How | Notes |
| --- | --- | --- |
| ROCm | default: `ollama/ollama:rocm` | needs a ROCm release that knows gfx1151 |
| Vulkan | `OLLAMA_AMD_IMAGE=ollama/ollama:latest OLLAMA_VULKAN=1 make up` | needs Mesa's RADV driver on the host; often the simpler path on this chip |

**Memory the GPU may use.** On Linux the iGPU sees what the firmware sets aside
for it plus what the kernel lets it map. If `doctor` reports far less than the
machine has, raise the GPU memory in the firmware (UMA size) or the kernel's
GTT limit, as your distribution documents, before pulling a 72b.

## Apple Silicon

Native, not in a container: Docker on macOS has no Metal, so a model in a
container runs on the CPU. Install Ollama for macOS and Annona with `pip`, or
use the desktop app.

## Any other machine

`docker compose up -d` (no overlay) runs everywhere, the model on the CPU. It is
the right first step on unfamiliar hardware: prove the perimeter, then add the
overlay that gives the model its GPU.
