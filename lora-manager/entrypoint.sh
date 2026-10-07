#!/usr/bin/env bash
# Bootstraps the settings dir (settings.json + cache/logs/wildcards live under
# one volume dir) and starts the standalone LoRA Manager server.
set -euo pipefail

LORA_ROOT=/opt/lora-manager
DATA_DIR=/data
SETTINGS_DIR="$DATA_DIR/settings"

mkdir -p "$SETTINGS_DIR"

# Start settings.json from the upstream example if there is none, with folder
# paths pointed at the shared NAS model tree.
if [[ ! -f "$SETTINGS_DIR/settings.json" ]]; then
  cat > "$SETTINGS_DIR/settings.json" <<'EOF'
{
  "civitai_api_key": "",
  "folder_paths": {
    "loras": ["/models/loras"],
    "checkpoints": ["/models/checkpoints"],
    "unet": ["/models/diffusion_models"],
    "embeddings": ["/models/embeddings"]
  }
}
EOF
  # Merge in the example's defaults (language etc.) so we don't have to
  # replicate them here.
  python - "$SETTINGS_DIR/settings.json" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    settings = json.load(f)
try:
    with open("/opt/lora-manager/settings.json.example") as f:
        example = json.load(f)
except (OSError, ValueError):
    example = {}
paths = settings["folder_paths"]
for key, default_paths in example.get("folder_paths", {}).items():
    if key not in paths:
        paths[key] = []  # unknown model type until configured in the UI
settings = {**example, **settings}
settings["folder_paths"] = paths
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
PYEOF
fi

# Keep settings out of the container layer and let the app write anything else
# (sqlite cache, logs, wildcards) under the same volume.
export LORA_MANAGER_SETTINGS_DIR="$SETTINGS_DIR"

exec "$@"
