# Annona — the sovereign execution kernel.
#
# Built for the machine it is meant to run on: a DGX Spark is arm64, so this
# image is built and tested for linux/arm64 as well as linux/amd64. An x86-only
# image silently does not run on a GB10, and finding that out during an
# installation at a customer site is the worst possible time.
#
# Two properties are deliberate and worth stating, because they are the ones a
# security reviewer checks first:
#
#   * the daemon runs as an unprivileged user and never touches the GPU. Only
#     the inference server (vLLM, Ollama) does. If the model runtime is
#     compromised it does not hold the data; if the daemon is compromised it
#     does not hold the GPU.
#
#   * nothing here talks to a model. The image ships the kernel and its policy
#     engine; substrates are declared in the policy at runtime, so the same
#     digest runs in an air-gapped rack and in a cloud tenant.
#
# Build:
#   docker buildx build --platform linux/arm64,linux/amd64 -t annona:0.1.0 .
# Run:
#   docker run --rm -p 7070:7070 -v annona-home:/home/annona/.annona annona:0.1.0

# ── Stage 1: build the wheel ──────────────────────────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

# Dependencies first: they change far less often than the source, and this is
# the difference between a 15-second rebuild and a 3-minute one.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY runner ./runner
COPY skills ./skills
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

# ── Stage 2: the runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Annona" \
      org.opencontainers.image.description="The sovereign execution kernel for AI agents" \
      org.opencontainers.image.source="https://github.com/akaion-ai/annona" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Akaion AI Lab"

# curl is the healthcheck. ffmpeg is the only other binary, and it is here
# because an appliance is exactly the box somebody drops a voice memo or a
# recorded call on: without it the audio and video readers report "ffmpeg is not
# installed" on a machine nobody can apt-get into. It decodes untrusted media,
# which is a real surface — mitigated the same way the rest of this image is: an
# unprivileged user, a read-only root filesystem, every capability dropped, and
# no route out of the compose network.
#
# Not installed: tesseract (OCR) and the speech models. Both need language data
# or weights measured in hundreds of megabytes, and `annona formats` reports
# them as missing with the exact install line rather than pretending.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin annona

COPY --from=build /opt/venv /opt/venv
# Skills ship with the image at a fixed path: a source checkout's layout is not
# something a container should have to reproduce.
COPY --from=build /src/skills /opt/annona/skills

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANNONA_HOME=/home/annona/.annona \
    AKAION_HOME=/home/annona/.annona \
    AKAION_BRAIN_DIR=/home/annona/vault \
    # Inside a container, loopback means "reachable by nothing". This is set by
    # the image and never by a default, so a laptop install stays on loopback.
    ANNONA_SKILLS_DIR=/opt/annona/skills \
    ANNONA_BIND=0.0.0.0

# Created here, owned here. A named volume mounted on a path that does not exist
# in the image is created root-owned, and an unprivileged daemon then cannot
# write its own policy — which fails at `annona policy init`, on a customer's
# machine, in front of them.
RUN mkdir -p /home/annona/.annona /home/annona/vault \
 && chown -R annona:annona /home/annona

USER annona
WORKDIR /home/annona

# The policy and the ledger live on a volume: they are the customer's, they
# outlive the container, and they must not be baked into an image that gets
# pushed to a registry.
VOLUME ["/home/annona/.annona", "/home/annona/vault"]

EXPOSE 7070

# Liveness only. Readiness is a policy question — `annona substrates` answers
# whether anything can actually serve a request — and conflating the two would
# make a container restart look like a fix for a placement problem.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7070/health || exit 1

ENTRYPOINT ["annona"]
CMD ["run", "--port", "7070"]
