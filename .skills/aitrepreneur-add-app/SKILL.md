---
name: aitrepreneur-add-app
description: >
  Use when adding a new ComfyUI app/profile to the aitrepreneur-docker project.
  Triggers: new app, new profile, add comfyui app, add workflow, .bat parser,
  convert installer, new service, docker-compose new app, add anima/flux/wan/etc.
  This skill covers both the scripted (add-app-from-bat.py) and manual methods.
---

# aitrepreneur-add-app Skill

Add a new ComfyUI app to the aitrepreneur-docker project.

## When to Use

**ALWAYS use this skill when:**
- Adding a new ComfyUI app/profile (e.g. flux, wan, cogvideo, etc.)
- Converting an Aitrepreneur .bat installer into a Docker app
- The user says "add X app" or "new profile for X"
- The user references a .bat file or installer script for a new workflow

## Prerequisites

- Project root is at `/home/pym/Projects/aitrepreneur-docker` (or wherever cloned)
- The user has a reference: .bat file, URL, or knows nodes/models needed

## Step 1: Gather Requirements

Ask or determine:
1. **App name** — short, lowercase, no spaces (e.g. `flux`, `wan`, `cogvideo`)
2. **Reference source** — .bat file path, URL, or manual spec
3. **ComfyUI version** — e.g. `v0.21.1`, `v0.33.1`, `master`
4. **Port** — next available (check `.env` for existing: 8188, 8189, 8190, 8191, 8192...)

## Step 2: Parse the .bat (Automated)

If a .bat file is available, run the parser script first:

```bash
cd /home/pym/Projects/aitrepreneur-docker

# Dry run to preview
python scripts/add-app-from-bat.py <name> <path-to-bat> --dry-run

# If it looks good, create the files
python scripts/add-app-from-bat.py <name> <path-to-bat>
```

This creates:
- `comfyui/apps/<name>/nodes.txt`
- `comfyui/apps/<name>/models.txt`
- `comfyui/apps/<name>/constraints.txt`
- `comfyui/apps/<name>/extras.txt`
- `comfyui/apps/<name>/workflows/` (empty)

**Review the output:**
- Check nodes.txt has all git clones from the .bat
- Check models.txt paths are normalized to ComfyUI conventions
- Verify constraints.txt matches the app's torch/transformers needs

## Step 3: Add Workflows

Copy workflow JSON(s) into:
```
comfyui/apps/<name>/workflows/
```

Source them from:
- The user's gitea repo (`ssh gitea`)
- Patreon downloads
- Manual copy

## Step 4: Wire into docker-compose.yml

Add a service block. Copy from an existing app and adapt:

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

## Step 5: Update .env and .env.example

Add lines to both files:

```bash
COMFY_REF_<NAME>=<version>
<NAME>_PORT=<port>
```

Example for "flux":
```bash
COMFY_REF_FLUX=master
FLUX_PORT=8193
```

## Step 6: Update Makefile

Edit `Makefile` — four places:

1. **COMPOSE variable** — add `--profile <name>`:
   ```makefile
   COMPOSE = sudo docker compose --profile krea2 --profile ideogram --profile ltx --profile minimax --profile anima --profile <name>
   ```

2. **App targets pattern rule** — add `<name>`:
   ```makefile
   krea2 ideogram ltx minimax anima <name>: setup ## Start one ComfyUI app (...)
   ```

3. **version loop** — add image commit reference:
   ```makefile
   comfyui-<name>:/opt/app/comfyui-commit.txt
   ```

4. **.PHONY** — add `<name>`:
   ```makefile
   .PHONY: help setup build up krea2 ideogram ltx minimax anima <name> down upgrade version gpu-check
   ```

## Step 7: Update README.md

Add the app to:
1. **Service table** (copy an existing row, change name/port)
2. **Day-to-day commands** list (add `make <name>`)
3. **Data layout** section if the app has unique mounts

## Step 8: Commit

```bash
git add -A
git commit -m "Add <name> ComfyUI app

- <N> custom nodes
- <N> model files
- Workflow JSON from <source>
- Port <port>, ComfyUI <version>"
git push origin master
```

## Step 9: Build and Test

```bash
# Build just the new app
sudo docker compose --profile <name> build <name>

# Or rebuild everything
make build

# Start it
make <name>

# Watch logs
make logs-<name>
```

First start downloads all models. If interrupted, restart — curl resumes `.part` files automatically.

## Reference: Model Path Conventions

| Type | Directory |
|------|-----------|
| Base/checkpoint models | `diffusion_models/` |
| LoRAs | `loras/` |
| ControlNets | `controlnet/` |
| VAE | `vae/` |
| Text encoders / CLIP / T5 / Qwen | `text_encoders/` |
| Upscalers (ESRGAN, etc.) | `upscale_models/` |
| SAM | `sams/` |
| Ultralytics bbox detectors | `ultralytics/bbox/` |
| Ultralytics segm detectors | `ultralytics/segm/` |
| IPAdapter | `ipadapter/` |
| Embeddings | `embeddings/` |
| Inpaint models | `inpaint/` |

## Reference: Existing Apps (for copying)

| App | Port | ComfyUI | Notes |
|-----|------|---------|-------|
| krea2 | 8188 | master | Image gen |
| ideogram | 8189 | master | Typography |
| ltx | 8190 | v0.21.1 | Video |
| minimax | 8191 | v0.33.1 | Video |
| anima | 8192 | v0.21.1 | Anime |
| **next** | **8193** | — | — |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Missing nodes | Check nodes.txt URLs, rebuild |
| Missing models | Check models.txt URLs, restart container |
| CUDA OOM | Stop other apps first |
| Import error | Add missing pip dep to extras.txt, rebuild |
| Wrong ComfyUI version | Update `COMFY_REF_<NAME>` in .env, `make upgrade-<name>` |
| Download resume | `.part` files auto-resume on container restart |
