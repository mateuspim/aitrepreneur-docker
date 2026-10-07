# Aitrepreneur RunPod stacks — local Docker (NVIDIA GPU)

Runs the tools from [Aitrepreneur](https://www.youtube.com/@Aitrepreneur)'s
RunPod one-click scripts locally, as reproducible Docker images: pinned
versions, persistent data on the host/NAS, one command to run or upgrade each
app.

All credit for the stack selection, model curation, version pinning research,
and workflows goes to Aitrepreneur — this repo only translates his work into
Docker form. His scripts, workflows, and templates are **paid Patreon content**
and are **not** included here: if you find this useful, please support him at
**<https://www.patreon.com/c/aitrepreneur/home>** — that's also where you get
the original files. See [`docs/reference/README.md`](docs/reference/README.md)
for where to place them if you have access (only the Ideogram workflow JSON is
actually used by the build; the rest is optional reference).

| Service | What it is | Start | UI |
|---|---|---|---|
| `ai-toolkit` | [ostris/ai-toolkit](https://github.com/ostris/ai-toolkit) LoRA training UI | `make up` | <http://localhost:8675> |
| `krea2` | ComfyUI + Krea 2 image generation | `make krea2` | <http://localhost:8188> |
| `ideogram` | ComfyUI + Ideogram 4 (typography/design) | `make ideogram` | <http://localhost:8189> |
| `ltx` | ComfyUI + LTX-2.3 video generation | `make ltx` | <http://localhost:8190> |
| `minimax` | ComfyUI + MiniMax H3 video generation | `make minimax` | <http://localhost:8191> |
| `anima` | ComfyUI + ANIMA 1.0 Ultra Anime | `make anima` | <http://localhost:8192> |
| `klein` | ComfyUI + FLUX.2 Klein 9B (GGUF editing) | `make klein` | <http://localhost:8193> |
| `loramgr` | [LoRA Manager](https://github.com/willmiao/ComfyUI-Lora-Manager) standalone edition (organize/preview/download loras over the shared model tree; no ComfyUI, CPU-only) | `make loramgr` | <http://localhost:8194/loras> |

Ports are the defaults; override them in `.env` (`AI_TOOLKIT_PORT`,
`KREA2_PORT`, `IDEOGRAM_PORT`, `LTX_PORT`, `MINIMAX_PORT`, `ANIMA_PORT`,
`KLEIN_PORT`).

Each app gets its own container because their Python stacks conflict
(different torch/transformers pins). They share the models tree under
`/mnt/gungnir`, so a model downloaded once is available to all.

## Prerequisites

- NVIDIA driver + nvidia-container-toolkit (verify with `make gpu-check`).
  Compose uses the legacy `nvidia` runtime rather than `gpus: all` because the
  latter reads the CDI spec (`/etc/cdi/nvidia.yaml`), which breaks after
  driver updates until regenerated
  (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`).
- Docker with Compose v2; ~20 GB image disk per app.
- Models and caches stored at `/mnt/gungnir` (models land there — LTX alone is ~40 GB).
- One RTX 4080 = run one heavy app at a time (`make stop-krea2` before
  `make ltx`, etc.). The containers themselves can coexist.

## Quick start

```bash
git clone https://github.com/mateuspim/aitrepreneur-docker.git
cd aitrepreneur-docker
make setup          # creates .env — edit it to add your HF_TOKEN
make build          # builds all six images (long first time)
make up             # ai-toolkit at http://localhost:8675
make krea2          # ComfyUI+Krea2 at http://localhost:8188
```

Model files are **not** in the images: each ComfyUI app checks
`comfyui/apps/<app>/models.txt` at container start and downloads only what is
missing from the NAS models tree. First start of an app therefore takes a
while — watch with `make logs-krea2`.

Day to day: `make up` / `make krea2` / `make ideogram` / `make ltx` /
`make minimax` / `make anima` / `make klein`, `make stop-<app>`, `make logs-<app>`, `make shell-<app>`,
`make down`.

## Data layout

Everything stateful lives outside the containers. Rebuilding or upgrading
never touches data, and nothing big can land in a container's writable layer
(all caches are env-redirected onto volumes).

```
./data/<app>/                            # small, local (gitignored)
    output/, input/, user/    ComfyUI apps: results, uploads, UI state
    datasets/, outputs/, db/  ai-toolkit: training data, LoRAs, job db

/mnt/gungnir/comfyui/models/            # big, shared model storage
    diffusion_models/, text_encoders/, vae/, loras/, unet/, ...
                 # shared ComfyUI-layout tree: your existing models plus
                 # whatever the apps download (skip-if-exists)
    hf-cache/    # ai-toolkit's Hugging Face cache
    aitk-cache/  # torch.hub + misc library caches, shared by all apps

/mnt/gungnir/comfyui/ideogram-templates/  # extracted reference templates
```

The `ideogram` container also seeds the bundled Ultra workflow into its UI
(`comfyui/apps/ideogram/workflows/`) and mounts the template images read-only
at `input/templates`; `minimax` seeds its Ultra and Ultra Turbo workflows the
same way (`comfyui/apps/minimax/workflows/`); `anima` seeds its ANIMA Base Ultra
workflow (`comfyui/apps/anima/workflows/`); `klein` seeds its Klein Edit Ultra
workflow (`comfyui/apps/klein/workflows/`). ai-toolkit mounts the shared models tree read-only at
`/comfyui-models` so training configs can reference existing checkpoints.

The `loramgr` service is different: it has no GPU/torch stack at all (the
standalone server is plain aiohttp). It mounts the shared models tree
read-write at `/models` — it must be rw because it writes `.metadata.json`
sidecars and preview images **next to the model files**, and CivitAI
downloads land in-place so every ComfyUI app sees them immediately. Its
`settings.json` (folder paths, CivitAI API key) is created on first run in
`./data/loramgr/` and can be edited from the UI's Settings page. Config in
compose: `loramgr`, defaults in [`.env.example`](.env.example)
(`LORAMGR_PORT`, `LORA_MANAGER_REF`).

## Per-app model isolation

By default every ComfyUI app sees the entire flat `loras/` (and `controlnet/`,
`checkpoints/`, `embeddings/`) folder — so the Krea2 dropdown is cluttered with
Anima-only loras. You can reorganise the NAS tree so each app only sees its own
models plus the big shared categories (diffusion_models, vae, text_encoders,
etc.).

```bash
make setup-model-dirs        # dry-run — review what will be moved
make setup-model-dirs --apply   # actually move files
```

This creates subfolders on the NAS (`loras/Anima/`, `loras/Krea2/`, …,
`controlnet/Anima/`, …) and moves existing files by name prefix. Anything that
doesn't match an app goes into `…/Shared/`. The overlay mounts in
`docker-compose.yml` then replace `/comfyui/models/loras` with
`/comfyui/models/loras/Anima` (etc.) per app, so each container sees a clean,
app-specific library. The LoRA Manager still mounts the full tree read-write,
so it can manage everything and write sidecars in-place.

If you want a lora to appear in **multiple** apps, hardlink it into each app's
folder (same filesystem, no extra disk space):

```bash
cp -l /mnt/gungnir/comfyui/models/loras/Shared/cool-lora.safetensors \
      /mnt/gungnir/comfyui/models/loras/Krea2/
```

## How an app is defined

Each ComfyUI app is a directory under `comfyui/apps/<name>/` — that's the
whole upgrade surface:

| File | Purpose |
|---|---|
| `nodes.txt` | custom nodes baked into the image (`folder url [git-ref]`) |
| `constraints.txt` | pip pins protected during every install |
| `extras.txt` | extra pip packages the workflows need |
| `models.txt` | model files fetched at container start (skip-if-exists) |
| `check.sh` | optional build-time sanity check |
| `workflows/` | workflow JSONs seeded into the UI |

Add a node → append to `nodes.txt`, `make upgrade-<app>`. New model file →
append to `models.txt`, restart the app. ComfyUI-Manager works in the UI for
experiments, but anything it installs lives in the container layer and
disappears on rebuild — promote keepers into `nodes.txt`. A pip blacklist is
seeded into each app's user dir so the Manager can't replace the locked
torch/transformers stack (same protection as the reference scripts).

## Upgrading

```bash
make upgrade-krea2   # rebuild one app from fresh upstream clones
make upgrade         # rebuild everything
make version         # upstream commits baked into each image
```

See [`docs/UPGRADING.md`](docs/UPGRADING.md) for version-pin locations and
the torch/CUDA compatibility notes.

## Deviations from the reference scripts

- **torch cu128 everywhere.** The ideogram/ltx scripts pin torch 2.4.0+cu121,
  which has no RTX 50xx (Blackwell/sm_120) kernels and cannot run on this
  machine's GPU. All ComfyUI apps here use torch 2.8.0+cu128 (the krea2
  script's own pin); xformers is dropped for ltx (cu121-only build) in favor
  of ComfyUI's native PyTorch attention.
- **Install at build, not at boot.** The scripts install apt/pip/nodes on
  every fresh pod; here that's baked into images, and pods'/containers' state
  can't drift — rebuilding is the only way anything changes.
- **Models on shared storage, atomically.** Downloads go to `.part` files and are
  renamed only when complete, so an interrupted download is never mistaken
  for a finished model (the scripts' skip-if-exists check has that flaw).
- **krea2 node fix.** The reference script clones `ComfyUI-Krea2T-Enhancer`
  but forgot to list it in `REQUIRED_NODES`, so its Python requirements were
  never installed. Here every cloned node gets its requirements.
- **minimax from a Windows installer.** The MiniMax H3 reference is a Windows
  `.bat` that unpacks ComfyUI portable v0.33.1 with its bundled Python stack.
  Here the same node set and models run in the shared Linux image (torch
  2.8.0+cu128), with ComfyUI pinned to the matching `v0.33.1` tag.
