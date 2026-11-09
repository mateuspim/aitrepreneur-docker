---
name: aitrepreneur-add-app
description: >
  Use when adding a new ComfyUI app/profile to the aitrepreneur-docker project,
  e.g. converting an Aitrepreneur .bat installer into a Docker app.
---

# aitrepreneur-add-app

Superseded by the `install-bat` skill: follow
[`.claude/skills/install-bat/SKILL.md`](../../.claude/skills/install-bat/SKILL.md).

Short version, from the repo root:

```bash
python3 scripts/add-app-from-bat.py <name> <bat> --dry-run   # resolve CHOICE/WARNING lines
python3 scripts/add-app-from-bat.py <name> <bat>
cp <bat folder>/*.json comfyui/apps/<name>/workflows/          # gitignore them (Patreon content)
python3 scripts/check-app.py <name> --urls
python3 scripts/wire-app.py <name> --comfy-ref <ver> --title "<desc>" --lora-dir "<LoRA Manager folder>"
docker compose --profile <name> build <name> && make <name>
python3 scripts/check-app.py <name> --live http://localhost:<port>
```
