# LoRA Manager standalone edition (willmiao/ComfyUI-Lora-Manager) — a
# lightweight, CPU-only web UI for organizing the shared model tree. No
# ComfyUI and no torch stack: the standalone server is plain aiohttp, so this
# image is a few hundred MB instead of the multi-GB ComfyUI ones.
#
# The shared COMFYUI_MODELS_DIR is mounted rw at /models: the manager writes
# sidecar .metadata.json files and preview images next to the real lora files
# on the NAS (that is the point of it), and CivitAI downloads land in-place so
# every ComfyUI instance sees them immediately.

ARG LORA_MANAGER_REF=main

FROM python:3.12-slim-bookworm

ARG LORA_MANAGER_REF

RUN apt-get update && apt-get install --no-install-recommends -y \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# CACHEBUST forces a re-clone when tracking a moving ref like main.
ARG CACHEBUST=0
RUN git clone https://github.com/willmiao/ComfyUI-Lora-Manager.git /opt/lora-manager \
    && git -C /opt/lora-manager checkout "${LORA_MANAGER_REF}" \
    && git -C /opt/lora-manager rev-parse HEAD > /opt/lora-manager-commit.txt \
    && pip install --no-cache-dir --prefer-binary -r /opt/lora-manager/requirements.txt

WORKDIR /opt/lora-manager

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8188

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "standalone.py", "--host", "0.0.0.0", "--port", "8188"]
