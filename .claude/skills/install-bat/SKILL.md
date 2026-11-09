---
name: install-bat
description: >
  Install an Aitrepreneur Windows .bat ComfyUI installer as a new Docker app in
  aitrepreneur-docker: parse the .bat, generate comfyui/apps/<name>/, verify
  URLs and workflows, wire compose/Makefile/.env/README, then build, start and
  verify it. Use when the user points at a .bat (often in ~/Downloads/<APP>/),
  says "install this bat", "add the new <model> app", or asks to convert an
  Aitrepreneur installer.
argument-hint: "<path to .bat or its folder> [app-name]"
---

# install-bat

Turn an Aitrepreneur `*_AUTO_INSTALL.bat` into a working ComfyUI app in this
repo. Work from the repo root. The three scripts do the deterministic parts;
your job is the judgment calls between them.

| Script | Does |
|---|---|
| `scripts/add-app-from-bat.py` | .bat → `comfyui/apps/<name>/{nodes,models,constraints,extras}.txt` |
| `scripts/check-app.py` | workflows vs manifests, URL checks, live `/object_info` check |
| `scripts/wire-app.py` | compose service, `.env(.example)`, Makefile, README, migrate script |

## 1. Locate inputs and pick the name

- The folder usually holds the `.bat` plus one or more `*_WORKFLOW.json`.
  Read the whole `.bat` yourself first — the parser is a helper, not an oracle.
- App name: short, lowercase, no separators (`qwen`, `klein`, `wan22`). Ask
  only if the folder name doesn't make it obvious.
- Never copy the `.bat` into the repo: it is paid Patreon content
  (see `docs/reference/README.md`).

## 2. Parse

```bash
python3 scripts/add-app-from-bat.py <name> <bat> --dry-run
```

Read the summary and resolve every flag before writing:

- `CHOICE VAR: [...]` — the .bat has a menu (usually a GGUF quant). Ask the
  user which one, offering VRAM guidance from the .bat's own echo text, then
  rerun with `--choose VAR=value`.
- `WARNING ... has no folder` / `unusual model folder` — decide the folder
  from how the workflow loads it (`UNETLoader` → `diffusion_models/`,
  `ModelPatchLoader` → `model_patches/`, etc.). Edit `models.txt` after writing.
- `pip packages:` — these went to `extras.txt`; drop any that are Windows-only.
- Cross-check counts against the .bat: every `:clone`/`git clone` and every
  `:grab`/`curl -o` should appear. If the .bat uses a pattern the parser
  missed, fix the parser (and keep it passing on previous installers) rather
  than hand-writing around it.

Then write for real (`--force` only if you mean to replace an existing app):

```bash
python3 scripts/add-app-from-bat.py <name> <bat> [--choose ...]
cp <folder>/*.json comfyui/apps/<name>/workflows/
```

Add `comfyui/apps/<name>/workflows/*.json` to `.gitignore` (Patreon content,
same as ideogram/minimax) unless the user says otherwise.

## 3. Check before building

```bash
python3 scripts/check-app.py <name> --urls
```

- `MISS model` — the workflow loads a file the .bat never downloads. Tell the
  user; add it to `models.txt` only if you can find a real URL (often the same
  HF repo). Otherwise it's a model they fetch themselves (LoRA Manager/CivitAI).
- `CHECK node pack` — heuristic; pack ids often differ from repo names. Look at
  which repo provides the node type; the live check in step 6 is authoritative.
- `FAIL` URL — fix or report before building; the container would crash-loop
  on a 404 at start.

## 4. Wire it in

Find the LoRA folder name first: LoRA Manager files LoRAs under its base-model
name, not the app name (existing: `Flux.2 Klein 9B`, `Qwen 2.1`). Look at
`ls /mnt/gungnir/comfyui/models/loras/` and ask the user if unsure. Pass
filename prefixes for the .bat's LoRAs that don't start with the app name.

```bash
python3 scripts/wire-app.py <name> --comfy-ref <COMFY_VER from step 2> \
  --title "<Model name> (<workflow variants>)" --lora-dir "<LoRA Manager folder>" \
  --prefixes "<extra lora prefixes>" --dry-run
```

Review the diff, then run without `--dry-run`. It picks the next free port and registers it with the authenticated proxy.
Validate: `docker compose config -q && bash -n scripts/migrate-model-dirs.sh`.

## 5. Build

`sudo` cannot prompt in this environment; use `pkexec` (desktop password
dialog — tell the user to watch for it) and one call per step so they
authenticate once:

```bash
pkexec sh -c 'cd <repo> && docker compose --profile <name> build <name>'
```

Run it in the background; it takes several minutes. On failure, read the log
around the first `ERROR`. Known resolver traps (the generated
`constraints.txt` already guards these — keep them unless proven wrong):

| Symptom | Cause | Fix |
|---|---|---|
| `albumentationsx ... albucore==X` ResolutionImpossible | old albumentations (via transparent-background ≥1.2.13) vs albumentationsx (controlnet_aux) | `transparent-background==1.2.12`, `albumentationsx`/`albucore` pinned pair |
| numba sdist "only versions <3.12 supported" | pip backtracking; real cause is usually opencv 4.12 capping numpy<2.3 | opencv 4.13.x for all cv2 packages, `numpy>=2.3,<2.6`, pin numba |
| `pip is looking at multiple versions of X` for minutes | an unpinned transitive dep | pin X to the version a clean resolve picks |

Reproduce offline instead of rebuilding blind:
`uv pip compile --python-version 3.12 --python-platform x86_64-manylinux_2_28 -c comfyui/apps/<name>/constraints.txt reqs.txt`
with the failing nodes' `requirements.txt` (minus torch/numpy/opencv/transformers, which the sanitizer strips).

## 6. Start and verify

```bash
pkexec sh -c 'cd <repo> && docker compose --profile <name> up -d --force-recreate <name>'
```

The entrypoint downloads every `models.txt` file before ComfyUI starts, so the
UI is down until then. Docker logs need root; track progress by checking
`<models dir>/<path>` vs `<path>.part` instead. When the port answers (set `AITD_PASSWORD` or enter the proxy password when prompted):

```bash
python3 scripts/check-app.py <name> --live http://localhost:<port>
```

This must report every workflow node type registered. A `MISS node type` means
a node failed to import: check `pkexec docker logs comfyui-<name>` for its
traceback (usually a missing pip dep → `extras.txt` → rebuild).

Ask the user to run one generation from each workflow; you can't judge output.

## 7. Commit

Only after the user confirms it runs. One commit per app: the app dir (minus
gitignored workflows) plus the wired files. Summarize nodes, model count,
port, ComfyUI version, and any constraint fixes in the body. Don't push
unless asked.
