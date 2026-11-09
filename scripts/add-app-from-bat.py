#!/usr/bin/env python3
"""
add-app-from-bat.py — Generate aitrepreneur-docker app config from a .bat file.

Usage:
    python scripts/add-app-from-bat.py <app-name> <path-to-bat> [--dry-run] [--force]
                                       [--comfy-ver vX.Y.Z] [--choose VAR=value ...]

Parses Aitrepreneur-style Windows .bat installers and writes:
    comfyui/apps/<name>/nodes.txt        custom nodes (call :clone / git clone)
    comfyui/apps/<name>/models.txt       model files (call :grab / curl -o)
    comfyui/apps/<name>/constraints.txt  proven pip baseline (tune per app)
    comfyui/apps/<name>/extras.txt       pip packages the .bat installs by name
    comfyui/apps/<name>/workflows/       empty; copy the workflow JSONs here

How the .bat is read:
    - `^` line continuations are joined.
    - `set "VAR=value"` / `set VAR=value` define variables; %VAR% and !VAR!
      are expanded. A variable set to several different values (e.g. a GGUF
      quant menu) is reported as a CHOICE; pass --choose VAR=value to pick one.
    - Model paths are taken as written in the .bat (relative to ComfyUI's
      models/ folder), never guessed from the filename.
    - Anything that cannot be parsed confidently is printed as a WARNING.

Then run scripts/check-app.py and scripts/wire-app.py (see the install-bat
skill in .claude/skills/install-bat/SKILL.md for the full flow).
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Model folders ComfyUI knows about (used to sanity-check .bat paths).
KNOWN_MODEL_DIRS = {
    "checkpoints", "clip", "clip_vision", "controlnet", "diffusers",
    "diffusion_models", "embeddings", "gligen", "hypernetworks", "inpaint",
    "ipadapter", "latent_upscale_models", "loras", "model_patches",
    "photomaker", "sams", "style_models", "text_encoders", "unet",
    "upscale_models", "vae", "vae_approx", "ultralytics", "sam2", "sam3",
    "LLM", "audio_encoders", "onnx", "insightface", "facerestore_models",
    "grounding-dino", "mmdets", "depthanything", "yolo",
}

# Packages never taken from the .bat's pip lines: the image locks them.
LOCKED_PACKAGES = (
    "torch", "torchvision", "torchaudio", "xformers", "triton",
    "sageattention", "numpy", "nvidia-", "cuda-",
)


# ───────────────────────── .bat reading ─────────────────────────

def read_lines(text):
    """Join ^ continuations; return (lineno, line) pairs without comments."""
    out, buf, start = [], "", 0
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf:
            start = i
        if line.endswith("^"):
            buf += line[:-1].rstrip() + " "
            continue
        buf += line.strip() if buf else line.strip()
        if buf and not re.match(r"^(rem\b|::)", buf, re.IGNORECASE):
            out.append((start, buf))
        buf = ""
    return out


SET_RE = re.compile(r'^set\s+(?!/[pa]\b)"?([A-Za-z_][\w]*)=([^"]*)"?\s*$', re.IGNORECASE)


def collect_vars(lines):
    """Map VAR -> list of distinct values in order of appearance."""
    values = {}
    for _, line in lines:
        # `if ... set "X=..."` and `(set X=...)` forms count too
        for m in re.finditer(r'\bset\s+(?!/[pa]\b)"?([A-Za-z_]\w*)=([^"&)]*)"?', line, re.IGNORECASE):
            name, val = m.group(1).upper(), m.group(2).strip()
            values.setdefault(name, [])
            if val not in values[name]:
                values[name].append(val)
    return values


def make_expander(values, chosen):
    def resolve(name):
        name = name.upper()
        if name in chosen:
            return chosen[name]
        vals = values.get(name)
        return vals[0] if vals else None

    def expand(s, depth=0):
        def sub(m):
            v = resolve(m.group(1))
            return m.group(0) if v is None else v
        new = re.sub(r"[%!]([A-Za-z_]\w*)[%!]", sub, s)
        return expand(new, depth + 1) if new != s and depth < 5 else new
    return expand


def unquote(s):
    return s.strip().strip('"').strip()


# ───────────────────────── parsing ─────────────────────────

def parse_nodes(lines, expand, warnings):
    nodes, by_folder = [], {}
    clone_re = re.compile(
        r'(?:call\s+:clone|git\s+clone)\s+((?:-{1,2}[\w-]+(?:[ =]\S+)?\s+)*)("?[^\s"]+"?)(?:\s+("?[^\s"&>]+"?))?',
        re.IGNORECASE)
    for lineno, line in lines:
        for m in clone_re.finditer(line):
            url = unquote(expand(m.group(2)))
            if "%" in url or not url.lower().startswith(("http", "git@")):
                continue  # the :clone helper body (git clone %*) and similar
            folder = m.group(3)
            folder = unquote(expand(folder)) if folder else ""
            if not folder or folder.startswith((">", "2>")) or "%" in folder:
                folder = Path(urlparse(url).path).name
            folder = re.sub(r"\.git$", "", folder.replace("\\", "/").rstrip("/").split("/")[-1])
            ref = None
            bm = re.search(r"(?:-b|--branch)[ =](\S+)", m.group(1) or "")
            if bm:
                ref = unquote(bm.group(1))
            if folder in by_folder:
                continue
            entry = [folder, url, ref]
            by_folder[folder] = entry
            nodes.append(entry)

    # Pinned commits: `git -C <folder> checkout <ref>` or `cd <folder> && git checkout <ref>`
    for lineno, line in lines:
        m = re.search(r'git\s+-C\s+("?[^"\s]+"?)\s+checkout\s+(\S+)', line, re.IGNORECASE)
        if m:
            folder = unquote(expand(m.group(1))).replace("\\", "/").rstrip("/").split("/")[-1]
            if folder in by_folder:
                by_folder[folder][2] = unquote(m.group(2))
            else:
                warnings.append(f"line {lineno}: checkout for unknown node folder '{folder}': {line}")
        elif re.search(r"\bgit\s+checkout\s+\S+", line, re.IGNORECASE):
            warnings.append(f"line {lineno}: git checkout without -C, pin it by hand in nodes.txt: {line}")
    return nodes


def normalize_model_path(path, warnings, lineno):
    p = unquote(path).replace("\\", "/")
    p = re.sub(r"^\./", "", p)
    # Strip a leading ".../models/" (whole segment only: not "diffusion_models/")
    m = list(re.finditer(r"(?:^|/)models/", p, re.IGNORECASE))
    if m:
        p = p[m[-1].end():]
    p = p.strip("/")
    top = p.split("/")[0] if "/" in p else ""
    if not top:
        warnings.append(f"line {lineno}: model '{p}' has no folder; decide where it goes")
    elif top not in KNOWN_MODEL_DIRS:
        warnings.append(f"line {lineno}: unusual model folder '{top}/' for {p}; confirm a node expects it")
    return p


def parse_models(lines, expand, warnings):
    models, seen = [], set()
    grab_re = re.compile(r'call\s+:(?:grab|download|dl)\w*\s+("[^"]+"|\S+)\s+("[^"]+"|\S+)', re.IGNORECASE)
    curl_re = re.compile(r'\bcurl\b.*?\s-o\s+("[^"]+"|\S+)\s+("[^"]+"|\S+)', re.IGNORECASE)
    for lineno, line in lines:
        m = grab_re.search(line) or curl_re.search(line)
        if not m:
            continue
        path, url = unquote(expand(m.group(1))), unquote(expand(m.group(2)))
        if "%~" in path or "%~" in url or "%1" in path:
            continue  # helper routine body
        if not url.lower().startswith("http"):
            continue
        if url.lower().endswith((".7z", ".zip", ".exe", ".msi")) or "github.com/comfyanonymous" in url:
            continue  # ComfyUI portable / tool downloads
        if "%" in url or "!" in url:
            warnings.append(f"line {lineno}: unexpanded variable in URL: {url}")
        rel = normalize_model_path(path, warnings, lineno)
        if "huggingface.co" in url and "/resolve/" in url and "download=" not in url:
            url += ("&" if "?" in url else "?") + "download=true"
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        models.append((rel, url))
    return models


def parse_pip(lines, expand):
    """Packages the .bat pip-installs by name (not -r requirements files)."""
    pkgs = []
    for _, line in lines:
        m = re.search(r"pip(?:\.exe)?\"?\s+install\s+(.+)$", expand(line), re.IGNORECASE)
        if not m or re.search(r"(^|\s)-r\s", m.group(1)):
            continue
        for tok in m.group(1).split():
            tok = unquote(tok)
            if not tok or tok.startswith("-") or tok.startswith(("http", ".", "/")) or "%" in tok:
                continue
            if tok.lower().startswith(LOCKED_PACKAGES) or tok.lower() == "pip":
                continue
            if tok not in pkgs:
                pkgs.append(tok)
    return pkgs


def parse_comfy_version(text, expand):
    m = re.search(r'COMFY_VER\s*=\s*"?([^"\s]+)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"ComfyUI/releases/download/(v[\d.]+)/", expand(text))
    if m:
        return m.group(1)
    return None


# ───────────────────────── output ─────────────────────────

def nodes_txt(nodes, bat_name):
    lines = ["# <folder> <git-url> [git-ref]",
             f"# From {bat_name} (cloned unpinned like the installer unless a ref is given)."]
    lines += [" ".join(x for x in (folder, url, ref) if x) for folder, url, ref in nodes]
    return "\n".join(lines) + "\n"


def models_txt(models, bat_name, chosen):
    lines = ["# <path relative to models/> <url>", f"# From {bat_name}."]
    for k, v in chosen.items():
        lines.append(f"# Choice: {k}={v}")
    lines.append("")
    current = None
    for rel, url in models:
        top = rel.split("/")[0]
        if top != current:
            if current is not None:
                lines.append("")
            lines.append(f"# --- {top} ---")
            current = top
        lines.append(f"{rel} {url}")
    return "\n".join(lines) + "\n"


def constraints_txt(bat_name, comfy_ver):
    # Baseline proven on the qwen app (ComfyUI v0.38, py3.12, torch 2.8).
    return f"""\
# Pip constraints for every install step. Torch lines are prepended from the
# Dockerfile ARGs at build time. Reference installer: {bat_name}
# (ComfyUI {comfy_ver}). Windows portable installers add no pins of their
# own; this is the project baseline — tune it if the build fails to resolve.
Pillow>=11.0.0
# rembg/albumentationsx need numpy>=2.3; numba caps it below 2.6.
numpy>=2.3,<2.6

# Every cv2 provider on the same version (whichever installs last wins).
# opencv 4.12 caps numpy<2.3, so stay on 4.13+.
opencv-python-headless==4.13.0.92
opencv-python==4.13.0.92
opencv-contrib-python==4.13.0.92

# Resolver guards: without these pip backtracks into py<3.12-only sdists
# (numba 0.58) or into the old albumentations/albucore pair.
numba==0.67.0
scikit-image==0.26.0
albumentationsx==2.4.13
albucore==0.2.20
# ComfyUI_essentials: 1.2.12 is the last release without the old
# albumentations dependency (conflicts with albumentationsx).
transparent-background==1.2.12
pixeloe==0.1.4
rembg==2.0.84
"""


def extras_txt(pkgs):
    lines = ["# Extra pip packages installed after the custom nodes.",
             "# Taken from explicit `pip install` lines in the .bat (locked stack excluded)."]
    lines += pkgs or ["# (none in the installer)"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("app_name")
    ap.add_argument("bat_file")
    ap.add_argument("--dry-run", action="store_true", help="print the files instead of writing them")
    ap.add_argument("--force", action="store_true", help="overwrite an existing app directory")
    ap.add_argument("--comfy-ver", help="override the detected ComfyUI version")
    ap.add_argument("--choose", action="append", default=[], metavar="VAR=value",
                    help="pick a value for a multi-valued .bat variable")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9]*", args.app_name):
        sys.exit("app name must be lowercase letters/digits (e.g. qwen, wan22)")
    bat = Path(args.bat_file).expanduser()
    if not bat.is_file():
        sys.exit(f"not found: {bat}")

    text = bat.read_text(encoding="utf-8", errors="ignore")
    lines = read_lines(text)
    values = collect_vars(lines)
    chosen = {}
    for c in args.choose:
        k, _, v = c.partition("=")
        chosen[k.upper()] = v
    expand = make_expander(values, chosen)

    warnings = []
    nodes = parse_nodes(lines, expand, warnings)
    models = parse_models(lines, expand, warnings)
    pkgs = parse_pip(lines, expand)
    comfy_ver = args.comfy_ver or parse_comfy_version(text, expand)
    if not comfy_ver:
        comfy_ver = "master"
        warnings.append("no ComfyUI version found in the .bat; defaulting to master")

    choices = {k: v for k, v in values.items() if len(v) > 1 and k not in chosen}
    relevant = [l for _, l in lines if re.search(r":clone|git\s+clone|:grab|curl\b", l, re.IGNORECASE)]
    used = {k for k in choices if any(re.search(rf"[%!]{k}[%!]", l, re.IGNORECASE) for l in relevant)}

    print(f"Parsed {bat.name}")
    print(f"  ComfyUI:      {comfy_ver}")
    print(f"  Custom nodes: {len(nodes)}")
    for folder, url, ref in nodes:
        print(f"    {folder:45} {url}{'  @' + ref if ref else ''}")
    print(f"  Model files:  {len(models)}")
    for rel, _ in models:
        print(f"    {rel}")
    if pkgs:
        print(f"  pip packages: {' '.join(pkgs)}")
    for k in sorted(used):
        print(f"  CHOICE {k}: {values[k]} (using '{values[k][0]}'; override with --choose {k}=...)")
    for w in warnings:
        print(f"  WARNING {w}")

    files = {
        "nodes.txt": nodes_txt(nodes, bat.name),
        "models.txt": models_txt(models, bat.name, chosen),
        "constraints.txt": constraints_txt(bat.name, comfy_ver),
        "extras.txt": extras_txt(pkgs),
    }
    app_dir = Path(__file__).resolve().parent.parent / "comfyui" / "apps" / args.app_name
    if args.dry_run:
        for name, content in files.items():
            print(f"\n──── {app_dir / name}\n{content}", end="")
        return
    if app_dir.exists() and not args.force:
        sys.exit(f"\n{app_dir} already exists (use --force to overwrite)")
    (app_dir / "workflows").mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (app_dir / name).write_text(content)
    print(f"\nWrote {app_dir}/{{{','.join(files)}}} and workflows/")
    print(f"COMFY_VER={comfy_ver}")


if __name__ == "__main__":
    main()
