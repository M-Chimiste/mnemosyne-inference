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
# Base: CUDA 13.0 devel. vLLM's release wheels bundle their CUDA runtime;
# the workstation driver reports CUDA 13.0, which can run the cu129 wheels.
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
# PyTorch cu129 first (matches current vLLM release wheel guidance).
RUN pip install --no-cache-dir \
      torch \
      torchvision \
      torchaudio \
      --index-url https://download.pytorch.org/whl/cu129

# vLLM stable release pin. Refresh deliberately after checking upstream
# release notes and regenerating vllm_supported_architectures.json.
# Last refreshed: 2026-05-03 (v0.20.1).
RUN pip install --no-cache-dir \
      "vllm==0.20.1" \
      --extra-index-url https://download.pytorch.org/whl/cu129

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
