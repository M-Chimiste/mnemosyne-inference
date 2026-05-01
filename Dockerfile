# ──────────────────────────────────────────────────────────────────
# UI build stage
# ──────────────────────────────────────────────────────────────────
FROM node:22-alpine AS ui-builder

WORKDIR /ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# ──────────────────────────────────────────────────────────────────
# vLLM Manager — Mnemosyne Inference Container
#
# Base: CUDA 13.0 devel (matches current vLLM nightlies for Blackwell).
# The workstation driver reports CUDA 13.0, so the runtime and wheel agree.
# ──────────────────────────────────────────────────────────────────
FROM nvidia/cuda:13.0.2-devel-ubuntu24.04

# Prevent interactive prompts during apt
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ── System deps ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-dev \
      python3-pip \
      python3-venv \
      curl \
      git \
      jq \
      && rm -rf /var/lib/apt/lists/*

# Keep Python packages outside Ubuntu's externally managed system environment.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# ── Python deps ────────────────────────────────────────────────────
# PyTorch cu128 first (vLLM will use this rather than pulling its own)
RUN pip install --no-cache-dir \
      torch \
      torchvision \
      torchaudio \
      --index-url https://download.pytorch.org/whl/cu128

# vLLM nightly — has Blackwell sm_100 kernels.
# Pinned so rebuilds are reproducible. To refresh, either:
#   curl -s https://wheels.vllm.ai/nightly/vllm/ \
#     | grep -oE 'vllm-[0-9.]+(rc[0-9]+)?\.dev[0-9]+\+g[0-9a-f]+' | sort -u
# or run on a CUDA host:
#   pip index versions vllm --pre --index-url https://wheels.vllm.ai/nightly
# Last refreshed: 2026-04-30 (commit 3ca6ca210, Blackwell sm_100).
RUN pip install --no-cache-dir \
      "vllm==0.20.1rc1.dev105+g3ca6ca210" \
      --extra-index-url https://wheels.vllm.ai/nightly

# Manager API deps
RUN pip install --no-cache-dir \
      fastapi \
      "uvicorn[standard]" \
      httpx \
      huggingface_hub \
      pydantic \
      pyyaml

# ── App ────────────────────────────────────────────────────────────
WORKDIR /app
COPY vllm_manager.py config.py catalog.py profiles.py runtime.py \
     downloader.py download_worker.py hf_search.py logsetup.py \
     vllm_supported_architectures.json ./
COPY scripts/ ./scripts/
COPY --from=ui-builder /ui/dist /app/static

# HuggingFace cache lives in a volume (models persist across restarts)
ENV HF_HOME=/hf-cache
ENV TRANSFORMERS_CACHE=/hf-cache

# Phase 5 — sensible defaults for huggingface_hub HTTP timeouts on
# hf_hub_download (covers /manager/hf/search per-row config.json fetches).
# These are read by huggingface_hub itself; list_models / model_info use
# the underlying requests session's default timeout.
ENV HF_HUB_ETAG_TIMEOUT=10
ENV HF_HUB_DOWNLOAD_TIMEOUT=30

# Manager: inference :8000, admin :8001 (LAN-gated by ADMIN_PASSWORD).
# vLLM inner server: 127.0.0.1:8002 inside container.
EXPOSE 8000 8001

ENTRYPOINT ["python", "vllm_manager.py"]
