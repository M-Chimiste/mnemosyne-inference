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
      build-essential \
      cmake \
      libcurl4-openssl-dev \
      && rm -rf /var/lib/apt/lists/*

# ── llama.cpp (llama-server) ───────────────────────────────────────
# Built from a pinned tag against the same CUDA toolkit as vLLM.
# Refresh deliberately after checking llama.cpp release notes (the binary
# CLI flags occasionally change). Last refreshed: 2026-05-07.
#
# Architectures land in llama.cpp on a rolling basis; if a new model
# (qwen35moe, nemotron_h_moe, gpt-oss, etc.) isn't recognized at load time
# the symptom is `unknown model architecture: '<arch>'` from llama-server
# stderr. Bump this tag to a release that contains the arch's PR. Override
# at build time without editing the Dockerfile:
#   docker build --build-arg LLAMA_CPP_TAG=b9100 ...
# Latest tags: see https://github.com/ggerganov/llama.cpp/tags
ARG LLAMA_CPP_TAG=b9060
# Compute capabilities to compile kernels for. Docker builds have no GPU,
# so `-arch=native` falls back to a default arch and the resulting binary
# may not run on the deployment card. The default targets RTX PRO 6000
# Blackwell (sm_120); override to broaden coverage if you redeploy this
# image to a non-Blackwell host. Common values:
#  80  — A100 / Ampere data-center
#  86  — RTX 30-series / A10 / A40
#  89  — RTX 40-series / L4 / L40
#  90  — H100 / H200 (Hopper)
# 100  — Blackwell B100/B200 data-center
# 120  — Blackwell workstation (RTX PRO 6000) / RTX 50-series consumer
# Use a semicolon-separated list to bake in multiple arches:
#   --build-arg CMAKE_CUDA_ARCHITECTURES="89;90;120"
ARG CMAKE_CUDA_ARCHITECTURES="120"
# Link-time fix for `libcuda.so.1`: the CUDA devel image provides `libcuda.so`
# (no .1 SONAME) in a stubs directory for build-time linking. ggml-cuda's
# SONAME demands `libcuda.so.1`, so we symlink it and register the stub dir
# with ldconfig before the build, then unregister + remove it afterward so
# the runtime container falls back to the real driver supplied by the NVIDIA
# Container Toolkit. Stub path is discovered dynamically because CUDA 13
# could move it; we fail loudly if not found.
RUN set -eu \
 && STUB_DIR="$(dirname "$(find /usr/local /usr/lib -name libcuda.so 2>/dev/null | head -n1)")" \
 && [ -n "${STUB_DIR}" ] && [ -f "${STUB_DIR}/libcuda.so" ] \
      || (echo "could not locate libcuda.so stub in CUDA devel image" >&2; exit 1) \
 && echo "Using libcuda stub dir: ${STUB_DIR}" \
 && ln -sf "${STUB_DIR}/libcuda.so" "${STUB_DIR}/libcuda.so.1" \
 && echo "${STUB_DIR}" > /etc/ld.so.conf.d/zz-cuda-stubs.conf \
 && ldconfig \
 && git clone --depth 1 --branch ${LLAMA_CPP_TAG} \
      https://github.com/ggerganov/llama.cpp /tmp/llama.cpp \
 && cmake -S /tmp/llama.cpp -B /tmp/llama.cpp/build \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
      -DCMAKE_EXE_LINKER_FLAGS="-Wl,-rpath-link,${STUB_DIR}" \
      -DCMAKE_SHARED_LINKER_FLAGS="-Wl,-rpath-link,${STUB_DIR}" \
      -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
 && cmake --build /tmp/llama.cpp/build -j --target llama-server \
 && cp /tmp/llama.cpp/build/bin/llama-server /usr/local/bin/ \
 && mkdir -p /usr/local/lib/llama.cpp \
 && cp /tmp/llama.cpp/build/bin/*.so /usr/local/lib/llama.cpp/ \
 && echo /usr/local/lib/llama.cpp > /etc/ld.so.conf.d/llama-cpp.conf \
 && rm -rf /tmp/llama.cpp \
 && rm -f "${STUB_DIR}/libcuda.so.1" \
 && rm -f /etc/ld.so.conf.d/zz-cuda-stubs.conf \
 && ldconfig
ENV LLAMA_SERVER_BIN=/usr/local/bin/llama-server

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
     repo_probe.py vllm_supported_architectures.json ./
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
