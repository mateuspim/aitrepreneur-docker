#!/usr/bin/env bash
# migrate-model-dirs.sh — reorganise the shared NAS model tree so each ComfyUI
# app owns its loras/controlnets/checkpoints/embeddings in a subfolder.
#
# Default is dry-run. Run with --apply to actually move files.
#
# Layout after migration:
#   /mnt/gungnir/comfyui/models/loras/Anima/       ← anima container sees this as loras/
#   /mnt/gungnir/comfyui/models/loras/Krea2/
#   /mnt/gungnir/comfyui/models/loras/Ideogram/
#   /mnt/gungnir/comfyui/models/loras/Ltx/
#   /mnt/gungnir/comfyui/models/loras/Minimax/
#   /mnt/gungnir/comfyui/models/loras/Flux.2 Klein 9B/   (Klein; LoRA Manager name)
#   /mnt/gungnir/comfyui/models/loras/Shared/      ← anything that doesn't match an app
#
# The same pattern is used for controlnet, checkpoints, embeddings.
# Big shared categories (diffusion_models, text_encoders, vae, upscale_models,
# sams, ultralytics) stay flat — every app still sees them.

set -euo pipefail

DRY_RUN=1
MODELS_DIR="${COMFYUI_MODELS_DIR:-/mnt/gungnir/comfyui/models}"

# App names must match the subfolder names used in docker-compose.yml overlays.
APPS=(Anima Krea2 Ideogram Ltx Minimax Klein)

# Per-app filename prefixes (case-insensitive globs, space-separated).
# Underscore variants are included so "anima_p3..." matches as well as "anima-..."
declare -A PREFIXES=(
  [Anima]="anima- anima_ ANIMA- ANIMA_ Anima- Anima_"
  [Krea2]="krea2- krea2_ KREA2- KREA2_ Krea2- Krea2_ krea- krea_ Krea- Krea_"
  [Ideogram]="ideogram- ideogram_ IDEOGRAM- IDEOGRAM_ Ideogram- Ideogram_"
  [Ltx]="ltx- ltx_ LTX- LTX_ Ltx- Ltx_"
  [Minimax]="minimax- minimax_ MINIMAX- MINIMAX_ Minimax- Minimax_"
  [Klein]="klein- klein_ KLEIN- KLEIN_ Klein- Klein_ lenovo_flux_klein nicegirls_flux_klein uncrop_F2K9B KLEIN-DETAILER"
)

CATEGORIES=(loras controlnet checkpoints embeddings)

# loras/ subfolders that follow LoRA Manager's base-model naming instead of
# the app name (must match the loras overlay in docker-compose.yml).
declare -A LORA_DIRS=(
  [Klein]="Flux.2 Klein 9B"
)
# loras/ subfolders owned by LoRA Manager itself; never touched.
LORA_KEEP=(recipes)

# Subfolder name for an app within a category.
app_dir() {
  local cat="$1" app="$2"
  if [[ "$cat" == loras && -n "${LORA_DIRS[$app]:-}" ]]; then
    echo "${LORA_DIRS[$app]}"
  else
    echo "$app"
  fi
}

# Primary model-file extensions that trigger a move (sidecars come with them).
PRIMARY_EXTS=(safetensors ckpt pt pth bin gguf onnx sft)

usage() {
  cat <<EOF
Usage: $(basename "$0") [--apply] [--models-dir <path>]

  --apply          Actually move files (default is dry-run)
  --models-dir     Path to the shared ComfyUI models tree
                   (default: \$COMFYUI_MODELS_DIR or /mnt/gungnir/comfyui/models)
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) DRY_RUN=0; shift ;;
    --models-dir) MODELS_DIR="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1"; usage ;;
  esac
done

if [[ ! -d "$MODELS_DIR" ]]; then
  echo "ERROR: models dir does not exist: $MODELS_DIR"
  exit 1
fi

# Helper: strip known primary extensions from a filename to get the base name.
base_name() {
  local fn="$1"
  local lowered="${fn,,}"
  for ext in "${PRIMARY_EXTS[@]}"; do
    if [[ "$lowered" == *".""$ext" ]]; then
      echo "${fn%.*}"
      return
    fi
  done
  echo "$fn"
}

# Helper: does a base name match any of the space-separated prefixes?
matches_prefix() {
  local base="$1" prefixes="$2"
  for p in $prefixes; do
    case "$base" in
      "$p"*) return 0 ;;
    esac
  done
  return 1
}

# Helper: move a primary model file + its sidecars.
# $3 is the full basename of the primary file (e.g. "foo.safetensors").
move_bundle() {
  local src_dir="$1" dst_dir="$2" primary_fn="$3"
  # Derive the base name used for sidecars (strip primary extension).
  local base="$(base_name "$primary_fn")"

  # Move the primary file itself.
  local pf="$src_dir/$primary_fn"
  if [[ -e "$pf" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  would move  $pf  →  $dst_dir/"
    else
      mkdir -p "$dst_dir"
      mv -n "$pf" "$dst_dir/"
      echo "  moved       $pf  →  $dst_dir/"
    fi
  fi

  # Move known sidecars.
  for ext in .metadata.json .jpeg .jpg .png .webp .part .txt .info .civitai.info; do
    local f="$src_dir/$base$ext"
    if [[ -e "$f" ]]; then
      if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  would move  $f  →  $dst_dir/"
      else
        mkdir -p "$dst_dir"
        mv -n "$f" "$dst_dir/"
        echo "  moved       $f  →  $dst_dir/"
      fi
    fi
  done
}

# Ensure every app + Shared subdir exists (create on --apply only)
ensure_dirs() {
  for cat in "${CATEGORIES[@]}"; do
    for app in "${APPS[@]}" Shared; do
      local d="$MODELS_DIR/$cat/$(app_dir "$cat" "$app")"
      if [[ "$DRY_RUN" -eq 0 ]]; then
        mkdir -p "$d"
      fi
    done
  done
}

migrate_category() {
  local cat="$1"
  local cat_dir="$MODELS_DIR/$cat"
  if [[ ! -d "$cat_dir" ]]; then
    echo "Skipping missing category: $cat_dir"
    return
  fi

  echo ""
  echo "== $cat =="

  # Build the find predicate for primary extensions
  local find_pred=""
  local first=1
  for ext in "${PRIMARY_EXTS[@]}"; do
    if [[ "$first" -eq 1 ]]; then
      find_pred="-iname '*.${ext}'"
      first=0
    else
      find_pred="$find_pred -o -iname '*.${ext}'"
    fi
  done

  # Process only primary model files at the category root.
  eval "find \"$cat_dir\" -maxdepth 1 -type f \( $find_pred \) -print0" | \
    while IFS= read -r -d '' file; do
      local basename
      basename="$(basename "$file")"
      local base
      base="$(base_name "$basename")"

      local matched=""
      for app in "${APPS[@]}"; do
        if matches_prefix "$base" "${PREFIXES[$app]}"; then
          matched="$app"
          break
        fi
      done

      if [[ -z "$matched" ]]; then
        matched="Shared"
      else
        matched="$(app_dir "$cat" "$matched")"
      fi

      move_bundle "$cat_dir" "$cat_dir/$matched" "$basename"
    done

  # Move any remaining loose directories that aren't app names or Shared.
  find "$cat_dir" -maxdepth 1 -mindepth 1 -type d -print0 | while IFS= read -r -d '' dir; do
    local dname
    dname="$(basename "$dir")"
    local known=0
    for app in "${APPS[@]}"; do
      [[ "$dname" == "$app" || "$dname" == "$(app_dir "$cat" "$app")" ]] && known=1
    done
    if [[ "$cat" == loras ]]; then
      for keep in "${LORA_KEEP[@]}"; do
        [[ "$dname" == "$keep" ]] && known=1
      done
    fi
    [[ "$dname" == "Shared" ]] && known=1
    if [[ "$known" -eq 0 ]]; then
      if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  would move  $dir  →  $cat_dir/Shared/"
      else
        mkdir -p "$cat_dir/Shared"
        mv -n "$dir" "$cat_dir/Shared/"
        echo "  moved       $dir  →  $cat_dir/Shared/"
      fi
    fi
  done
}

# ── main ─────────────────────────────────────────────────────────────────────

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY-RUN mode — no files will be moved. Pass --apply to execute."
else
  echo "LIVE mode — moving files."
fi

ensure_dirs

for cat in "${CATEGORIES[@]}"; do
  migrate_category "$cat"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  echo "Dry-run complete. Review the plan above, then run:"
  echo "  $(basename "$0") --apply"
else
  echo ""
  echo "Migration complete. Restart the containers so ComfyUI rescans:"
  echo "  make down && make krea2   (etc.)"
  echo "Also restart the LoRA Manager so its DB rescans the new paths:"
  echo "  make stop-loramgr && make loramgr"
fi
