# ──────────────────────────────────────────────────────────────────
# vLLM Manager — Mnemosyne Inference Container
#
# Base: CUDA 12.8 devel (forward-compatible with your CUDA 13.2 driver)
# vLLM wheels ship against cu128; your driver 580 runs them fine.
# ──────────────────────────────────────────────────────────────────
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04

# Prevent interactive prompts during apt
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ── System deps ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 \
      python3.11-dev \
      python3-pip \
      python3.11-venv \
      curl \
      git \
      jq \
      && rm -rf /var/lib/apt/lists/*

# Make python3.11 the default
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
 && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
 && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

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
# Last refreshed: 2026-04-27 (commit 2c8b76c5c, Blackwell sm_100).
RUN pip install --no-cache-dir \
      "vllm==0.20.1rc1.dev10+g2c8b76c5c" \
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
     downloader.py download_worker.py ./

# HuggingFace cache lives in a volume (models persist across restarts)
ENV HF_HOME=/hf-cache
ENV TRANSFORMERS_CACHE=/hf-cache

# Manager: inference :8000, admin :8001 (LAN-gated by ADMIN_PASSWORD).
# vLLM inner server: 127.0.0.1:8002 inside container.
EXPOSE 8000 8001

ENTRYPOINT ["python", "vllm_manager.py"]
