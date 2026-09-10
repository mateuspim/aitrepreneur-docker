# Skill: Adding a New ComfyUI App to aitrepreneur-docker

## When to Use

Use this workflow when you want to add a new ComfyUI app/profile to the aitrepreneur-docker project based on an Aitrepreneur .bat installer or any other reference.

## Prerequisites

- You have a reference installer (.bat file) or know what nodes/models the app needs
- You know the app name (e.g. `flux`, `wan`, `cogvideo`, etc.)
- You know which ComfyUI version it needs (or can detect it from the .bat)

## Quick Method: Use the Script

```bash
# Parse a .bat file and auto-generate the app config
python scripts/add-app-from-bat.py <app-name> <path-to-bat-file>

# Example (dry run first to see what it would do)
python scripts/add-app-from-bat.py anima /path/to/ANIMA_BASE_ULTRA-MODELS-NODES_INSTALL.bat --dry-run

# Actually create the files
python scripts/add-app-from-bat.py anima /path/to/ANIMA_BASE_ULTRA-MODELS-NODES_INSTALL.bat
```

This creates:
- `comfyui/apps/<name>/nodes.txt`
- `comfyui/apps/<name>/models.txt`
- `comfyui/apps/<name>/constraints.txt`
- `comfyui/apps/<name>/extras.txt`
- `comfyui/apps/<name>/workflows/` (empty — you add JSONs)

## Manual Method: Step by Step

### Step 1: Create the app directory

```bash
mkdir -p comfyui/apps/<name>/workflows
```

### Step 2: nodes.txt

List custom nodes, one per line:

```
# <folder> <git-url> [git-ref]
ComfyUI-Manager https://github.com/ltdrdata/ComfyUI-Manager.git
ComfyUI-Impact-Pack https://github.com/ltdrdata/ComfyUI-Impact-Pack
rgthree-comfy https://github.com/rgthree/rgthree-comfy.git
```

**Where to find them:**
- In the .bat file, look for `git clone` commands
- Copy the folder name and URL

### Step 3: models.txt

List model downloads, one per line:

```
# <path relative to models/> <url>
diffusion_models/my-model.safetensors https://huggingface.co/.../resolve/main/...?download=true
loras/my-lora.safetensors https://huggingface.co/.../resolve/main/...?download=true
controlnet/my-controlnet.safetensors https://huggingface.co/.../resolve/main/...?download=true
```

**Where to find them:**
- In the .bat file, look for `call :grab` commands
- The first arg is the path, the second is the URL
- Normalize paths to ComfyUI conventions (see table below)

**Model path conventions:**

| Type | Directory |
|------|-----------|
| Base/checkpoint models | `diffusion_models/` |
| LoRAs | `loras/` |
| ControlNets | `controlnet/` |
| VAE | `vae/` |
| Text encoders/CLIP/T5 | `text_encoders/` |
| Upscalers (ESRGAN, etc.) | `upscale_models/` |
| SAM | `sams/` |
| Ultralytics detectors | `ultralytics/bbox/` or `ultralytics/segm/` |
| IPAdapter | `ipadapter/` |
| Embeddings | `embeddings/` |
| Inpaint models | `inpaint/` |

**URL rules:**
- HuggingFace raw file URLs should end with `?download=true`
- Example: `https://huggingface.co/Aitrepreneur/FLX/resolve/main/model.safetensors?download=true`

### Step 4: constraints.txt

Pip pins to protect the Python stack. Start with this template and adjust:

```
# Pins from reference installer (torch lines are prepended from
# the Dockerfile ARGs at build time).
transformers==4.51.3
tokenizers>=0.21,<0.22
timm==1.0.15
opencv-python-headless==4.12.0.88
Pillow>=11.0.0
numpy>=1.26,<3
```

**Where to find them:**
- Check the .bat for any pip install pins
- Look at the reference script's torch/transformers versions
- If the app needs a specific ComfyUI version, pin it in docker-compose.yml instead

### Step 5: extras.txt

Extra pip packages the workflow needs beyond ComfyUI's requirements.txt:

```
transformers==4.51.3
timm==1.0.15
opencv-python-headless==4.12.0.88
safetensors>=0.4.3
huggingface_hub>=0.25.2,<1.0
ultralytics
diffusers
einops
piexif
librosa
```

**Where to find them:**
- Check the custom nodes' requirements.txt files
- Look at what the .bat installs beyond basic nodes
- Common extras: `ultralytics`, `diffusers`, `accelerate`, `timm`, `librosa`

### Step 6: workflows/

Copy the workflow JSON(s) into:

```
comfyui/apps/<name>/workflows/
```

The entrypoint auto-seeds these into the UI at container start.

### Step 7: docker-compose.yml

Add a service block (copy from an existing app and change names):

```yaml
  <name>:
    <<: *gpu
    profiles: [<name>]
    build:
      context: ./comfyui
      args:
        APP: <name>
        COMFY_REF: ${COMFY_REF_<NAME>:-<version>}
        CACHEBUST: ${CACHEBUST:-0}
    image: comfyui-<name>:local
    container_name: comfyui-<name>
    ports:
      - "${<NAME>_PORT:-<port>}:8188"
    volumes:
      - ${COMFYUI_MODELS_DIR:-/mnt/gungnir/comfyui/models}:/comfyui/models
      - ./data/<name>:/data
      - ${AUX_CACHE_DIR:-./data/<name>/cache}:/data/cache
    environment:
      NVIDIA_VISIBLE_DEVICES: all
      HF_TOKEN: ${HF_TOKEN:-}
```

**Pick a port** that doesn't conflict:
- ai-toolkit: 8675
- krea2: 8188
- ideogram: 8189
- ltx: 8190
- minimax: 8191
- anima: 8192
- **Next available: 8193**

### Step 8: .env and .env.example

Add the port and ComfyUI version:

```bash
COMFY_REF_<NAME>=<version>
<NAME>_PORT=<port>
```

### Step 9: Makefile

Update these lines:

1. **COMPOSE variable** — add the profile:
   ```makefile
   COMPOSE = sudo docker compose --profile krea2 --profile ideogram --profile ltx --profile minimax --profile anima --profile <name>
   ```

2. **App targets** — add to the pattern rule:
   ```makefile
   krea2 ideogram ltx minimax anima <name>: setup ## Start one ComfyUI app
   ```

3. **version target** — add to the loop:
   ```makefile
   comfyui-<name>:/opt/app/comfyui-commit.txt
   ```

4. **.PHONY** — add the app name:
   ```makefile
   .PHONY: help setup build up krea2 ideogram ltx minimax anima <name> down upgrade version gpu-check
   ```

### Step 10: README.md

Add the app to the service table and day-to-day commands.

### Step 11: Build and test

```bash
# Build just the new app
make build

# Or build only the new app:
sudo docker compose --profile <name> build <name>

# Start it
make <name>

# Watch logs
make logs-<name>
```

First start downloads all models. Watch with `make logs-<name>`.

## Tips

- **Model deduplication:** If models overlap with other apps (same URL), they share the same file on disk — no double download
- **Resume downloads:** If a download is interrupted, curl resumes `.part` files automatically
- **GPU memory:** Only run one heavy app at a time on an RTX 4080
- **Clean restart:** `make <name>` now uses `--force-recreate` so every start is fresh
- **Constraints matter:** Pinning the wrong transformers/torch version breaks the app. Copy constraints from a working similar app
- **ultralytics:** If the app uses YOLO models, add `ultralytics` to extras.txt

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Container exits immediately | Check `make logs-<name>` for missing models or node errors |
| CUDA out of memory | Stop other ComfyUI apps first |
| Model download fails | Check URL, HF token, internet. Curl resumes on restart |
| Node import error | Missing pip dep → add to extras.txt, rebuild |
| Wrong ComfyUI version | Update `COMFY_REF_<NAME>` in .env, `make upgrade-<name>` |
