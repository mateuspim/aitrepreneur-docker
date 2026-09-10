#!/usr/bin/env python3
"""
add-app-from-bat.py — Generate aitrepreneur-docker app config from a .bat file.

Usage:
    python scripts/add-app-from-bat.py <app-name> <path-to-bat-file> [options]

Example:
    python scripts/add-app-from-bat.py anima /path/to/ANIMA_BASE_ULTRA-MODELS-NODES_INSTALL.bat

This parses Aitrepreneur-style Windows .bat installers and generates:
    - comfyui/apps/<name>/nodes.txt
    - comfyui/apps/<name>/models.txt
    - comfyui/apps/<name>/constraints.txt
    - comfyui/apps/<name>/extras.txt

You still need to manually:
    1. Add the service to docker-compose.yml
    2. Add Makefile targets
    3. Add ports to .env and .env.example
    4. Add workflows to comfyui/apps/<name>/workflows/
    5. Commit and build
"""

import argparse
import re
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def parse_git_clones(content):
    """Extract git clone URLs from .bat content."""
    nodes = []
    # Pattern: git clone <url> [folder]
    for match in re.finditer(r'git\s+clone\s+(\S+)(?:\s+(\S+))?', content, re.IGNORECASE):
        url = match.group(1).strip('"')
        folder = match.group(2)
        if folder:
            folder = folder.strip('"').strip('%').strip()
        else:
            # Extract folder from URL
            folder = Path(urlparse(url).path).stem
        nodes.append((folder, url))
    return nodes


def parse_model_downloads(content):
    """Extract curl model downloads from .bat content."""
    models = []
    # Pattern: call :grab <path> <url>
    for match in re.finditer(
        r'call\s+:grab\s+([^\^\n]+?)\s+\^?\s*\n?\s*"([^"]+)"',
        content, re.IGNORECASE | re.MULTILINE
    ):
        path = match.group(1).strip().strip('"').strip('%').strip()
        url = match.group(2).strip()
        # Clean up path (remove HF var references, etc.)
        path = re.sub(r'%\w+%[/\\]', '', path)
        path = path.strip('\\/')
        if path and url:
            models.append((path, url))

    # Also try simpler pattern: call :grab "path" "url"
    for match in re.finditer(
        r'call\s+:grab\s+"([^"]+)"\s+"([^"]+)"',
        content, re.IGNORECASE
    ):
        path = match.group(1).strip()
        url = match.group(2).strip()
        models.append((path, url))

    # Pattern with ^ continuation lines
    for match in re.finditer(
        r'call\s+:grab\s+([^\n]+?)\^\s*\n\s*"([^"]+)"',
        content, re.IGNORECASE | re.MULTILINE
    ):
        path = match.group(1).strip().strip('"').strip('%').strip()
        url = match.group(2).strip()
        path = re.sub(r'%\w+%[/\\]', '', path)
        path = path.strip('\\/')
        if path and url:
            models.append((path, url))

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for path, url in models:
        key = (path.lower(), url.lower())
        if key not in seen:
            seen.add(key)
            unique.append((path, url))
    return unique


def parse_comfy_version(content):
    """Extract ComfyUI version pin from .bat content."""
    # Pattern: set "COMFY_VER=v0.21.1"
    match = re.search(r'COMFY_VER\s*=\s*"?([^"\s]+)"?', content, re.IGNORECASE)
    if match:
        return match.group(1)
    # Pattern: ComfyUI v0.33.1
    match = re.search(r'ComfyUI\s+(v[\d.]+)', content, re.IGNORECASE)
    if match:
        return match.group(1)
    return "master"  # default


def parse_hf_base_url(content):
    """Extract HF base URL from .bat content."""
    match = re.search(r'set\s+"HF\s*=\s*([^"]+)"', content, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r'set\s+HF\s*=\s*([^\s]+)', content, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def guess_model_category(path):
    """Map a model path to the correct ComfyUI models/ subdirectory."""
    path_lower = path.lower()
    if 'upscale' in path_lower or 'esrgan' in path_lower or 'remacri' in path_lower:
        return 'upscale_models'
    if 'controlnet' in path_lower or 'lllite' in path_lower or 'canny' in path_lower or 'depth' in path_lower or 'pose' in path_lower or 'lineart' in path_lower or 'inpaint' in path_lower:
        return 'controlnet'
    if 'lora' in path_lower:
        return 'loras'
    if any(x in path_lower for x in ['diffusion', 'checkpoint', 'unet', 'model']):
        return 'diffusion_models'
    if 'vae' in path_lower:
        return 'vae'
    if 'text_encoder' in path_lower or 'clip' in path_lower or 't5' in path_lower or 'qwen' in path_lower:
        return 'text_encoders'
    if 'sam' in path_lower:
        return 'sams'
    if 'ultralytics' in path_lower or 'yolo' in path_lower or 'bbox' in path_lower or 'segm' in path_lower:
        # Keep the ultralytics/bbox or ultralytics/segm structure
        if 'bbox' in path_lower:
            return 'ultralytics/bbox'
        if 'segm' in path_lower:
            return 'ultralytics/segm'
        return 'ultralytics/bbox'
    if 'inpaint' in path_lower:
        return 'inpaint'
    if 'ipadapter' in path_lower:
        return 'ipadapter'
    if 'embed' in path_lower:
        return 'embeddings'
    return 'diffusion_models'  # fallback


def normalize_model_path(path, hf_base=None):
    """Convert Windows-style paths and HF URLs to ComfyUI model paths."""
    # Remove HF variable references
    path = re.sub(r'%HF%[/\\]', '', path)
    path = re.sub(r'%YOLO11%[/\\]', '', path)
    path = path.strip('\\/').replace('\\', '/')

    # If path already has a models/ prefix, use relative part
    if 'models/' in path.lower():
        parts = path.lower().split('models/')
        if len(parts) > 1:
            return 'models/' + path.split('models/')[1]

    # Otherwise, guess the category from filename
    basename = os.path.basename(path)
    category = guess_model_category(basename)
    return f"{category}/{basename}"


def generate_nodes_txt(nodes):
    """Generate nodes.txt content."""
    lines = ["# <folder> <git-url> [git-ref]", ""]
    for folder, url in nodes:
        lines.append(f"{folder} {url}")
    return '\n'.join(lines) + '\n'


def generate_models_txt(models, hf_base=None):
    """Generate models.txt content."""
    lines = ["# <path relative to models/> <url>", ""]
    if hf_base:
        lines.append(f"# HF source: {hf_base}")
        lines.append("")

    for path, url in models:
        rel_path = normalize_model_path(path, hf_base)
        # Make sure URL has ?download=true if it's a huggingface raw file
        if 'huggingface.co' in url and 'download=true' not in url and 'resolve' in url:
            if '?' not in url:
                url += '?download=true'
            elif 'download=' not in url:
                url += '&download=true'
        lines.append(f"{rel_path} {url}")
    return '\n'.join(lines) + '\n'


def generate_constraints_txt(comfy_ver="master"):
    """Generate a starter constraints.txt."""
    lines = [
        f"# Pins from reference installer (torch lines are prepended from",
        f"# the Dockerfile ARGs at build time). ComfyUI pinned to {comfy_ver}.",
        "transformers==4.51.3",
        "tokenizers>=0.21,<0.22",
        "timm==1.0.15",
        "opencv-python-headless==4.12.0.88",
        "Pillow>=11.0.0",
        "numpy>=1.26,<3",
        "",
    ]
    return '\n'.join(lines)


def generate_extras_txt():
    """Generate a starter extras.txt."""
    lines = [
        "# App-specific extra packages (workflow dependencies).",
        "# Add whatever the nodes/workflows need beyond what's in requirements.txt.",
        "# Common ones for ComfyUI apps:",
        "# transformers==4.51.3",
        "# timm==1.0.15",
        "# opencv-python-headless==4.12.0.88",
        "# safetensors>=0.4.3",
        "# huggingface_hub>=0.25.2,<1.0",
        "# accelerate>=0.34.0",
        "# ultralytics",
        "# diffusers",
        "# einops",
        "# piexif",
        "# librosa",
        "",
    ]
    return '\n'.join(lines)


def create_app_directory(app_name, nodes, models, comfy_ver, hf_base, dry_run=False):
    """Create the app directory structure."""
    app_dir = Path(f"comfyui/apps/{app_name}")
    workflows_dir = app_dir / "workflows"

    if dry_run:
        print(f"[DRY RUN] Would create: {app_dir}/")
        print(f"[DRY RUN] Would create: {workflows_dir}/")
    else:
        app_dir.mkdir(parents=True, exist_ok=True)
        workflows_dir.mkdir(parents=True, exist_ok=True)
        print(f"Created: {app_dir}/")
        print(f"Created: {workflows_dir}/")

    files = {
        "nodes.txt": generate_nodes_txt(nodes),
        "models.txt": generate_models_txt(models, hf_base),
        "constraints.txt": generate_constraints_txt(comfy_ver),
        "extras.txt": generate_extras_txt(),
    }

    for filename, content in files.items():
        filepath = app_dir / filename
        if dry_run:
            print(f"[DRY RUN] Would write: {filepath}")
            print(content)
            print("-" * 40)
        else:
            filepath.write_text(content)
            print(f"Wrote: {filepath}")


def print_next_steps(app_name, comfy_ver):
    """Print what the user still needs to do manually."""
    print("\n" + "=" * 60)
    print("NEXT STEPS — Complete these manually:")
    print("=" * 60)
    print(f"""
1. docker-compose.yml — Add service block:

   {app_name}:
     <<: *gpu
     profiles: [{app_name}]
     build:
       context: ./comfyui
       args:
         APP: {app_name}
         COMFY_REF: ${'{'}COMFY_REF_{app_name.upper()}{'}':-{comfy_ver}}
         CACHEBUST: ${'{'}CACHEBUST:-0{'}'}
     image: comfyui-{app_name}:local
     container_name: comfyui-{app_name}
     ports:
       - "${'{'}_{app_name.upper()}_PORT:-<pick-port>{'}'}:8188"
     volumes:
       - ${'{'}COMFYUI_MODELS_DIR:-/mnt/gungnir/comfyui/models{'}'}:/comfyui/models
       - ./data/{app_name}:/data
       - ${'{'}AUX_CACHE_DIR:-./data/{app_name}/cache{'}'}:/data/cache
     environment:
       NVIDIA_VISIBLE_DEVICES: all
       HF_TOKEN: ${'{'}HF_TOKEN:-{'}'}

2. .env / .env.example — Add port:
   {app_name.upper()}_PORT=<pick-port>

3. Makefile — Add '{app_name}' to:
   - COMPOSE profiles list
   - The app targets line: krea2 ideogram ltx minimax {app_name}
   - .PHONY line
   - version loop

4. Add workflow JSONs to:
   comfyui/apps/{app_name}/workflows/

5. Tune constraints.txt and extras.txt based on what the app actually needs.

6. Build and test:
   make build    # or: sudo docker compose --profile {app_name} build {app_name}
   make {app_name}
""")


def main():
    parser = argparse.ArgumentParser(
        description="Generate aitrepreneur-docker app config from a .bat file"
    )
    parser.add_argument("app_name", help="Name of the new app (e.g. anima, flux, etc.)")
    parser.add_argument("bat_file", help="Path to the .bat installer file")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing files")
    parser.add_argument("--comfy-ver", default=None, help="Override ComfyUI version (auto-detected if not set)")
    args = parser.parse_args()

    bat_path = Path(args.bat_file)
    if not bat_path.exists():
        print(f"Error: File not found: {bat_path}", file=sys.stderr)
        sys.exit(1)

    content = bat_path.read_text(encoding='utf-8', errors='ignore')

    print(f"Parsing: {bat_path}")
    print(f"App name: {args.app_name}")
    print()

    nodes = parse_git_clones(content)
    models = parse_model_downloads(content)
    comfy_ver = args.comfy_ver or parse_comfy_version(content)
    hf_base = parse_hf_base_url(content)

    print(f"Found {len(nodes)} custom nodes:")
    for folder, url in nodes:
        print(f"  - {folder}: {url}")
    print()

    print(f"Found {len(models)} model files:")
    for path, url in models:
        print(f"  - {path}")
    print()

    print(f"ComfyUI version: {comfy_ver}")
    if hf_base:
        print(f"HF base URL: {hf_base}")
    print()

    create_app_directory(args.app_name, nodes, models, comfy_ver, hf_base, dry_run=args.dry_run)

    if not args.dry_run:
        print_next_steps(args.app_name, comfy_ver)


if __name__ == "__main__":
    main()
