#!/usr/bin/env python3
"""
check-app.py — Sanity-check a ComfyUI app before and after building it.

Usage:
    python scripts/check-app.py <app-name> [--urls] [--live http://localhost:PORT]

Offline (always):
    - Lists every custom node pack the workflow JSONs use (by cnr_id/aux_id)
      and flags (CHECK, not fatal) packs with no obvious nodes.txt entry —
      pack ids often differ from repo names; --live is authoritative.
    - Lists every model filename the workflows reference and flags ones that
      are neither in models.txt nor already present in the shared models dir.

--urls:
    HEAD-checks every URL in models.txt and every git URL in nodes.txt.

--live URL:
    Asks the running ComfyUI for /object_info and reports workflow node types
    it does not know (= missing or failed-to-import custom nodes), plus the
    model lists it actually sees for loras/diffusion_models/etc.

Exit status is 1 when something is missing, so the skill can gate on it.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_EXT = re.compile(r"\.(safetensors|gguf|pth|pt|bin|onnx|ckpt|sft)$", re.IGNORECASE)

# Node types implemented in the frontend (never in /object_info).
FRONTEND_TYPES = {
    "Note", "MarkdownNote", "Reroute", "PrimitiveNode", "GetNode", "SetNode",
    "Label (rgthree)", "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)",
    "Fast Bypasser (rgthree)", "Fast Muter (rgthree)", "Bookmark (rgthree)",
    "Node Collector (rgthree)", "Display Any (rgthree)",
}

# cnr_id -> repo-name hints when they differ from the repo folder name.
CNR_ALIASES = {
    "comfyui-pid": ["ComfyUI-AusBoss", "ComfyUI-Fantastic-MiniMaxH3-PromptBuilder"],
    "comfyui-kjnodes": ["ComfyUI-KJNodes"],
    "comfyui_essentials": ["ComfyUI_essentials"],
    "comfyui-impact-pack": ["ComfyUI-Impact-Pack"],
    "comfyui-impact-subpack": ["ComfyUI-Impact-Subpack"],
    "comfyui-videohelpersuite": ["ComfyUI-VideoHelperSuite"],
    "comfyui-easy-use": ["ComfyUI-Easy-Use"],
    "comfyui-gguf": ["ComfyUI-GGUF"],
}


def load_workflow_nodes(path):
    d = json.loads(path.read_text())
    nodes = list(d.get("nodes", []))
    subgraph_ids = set()
    for sg in d.get("definitions", {}).get("subgraphs", []):
        subgraph_ids.add(sg.get("id"))
        nodes += sg.get("nodes", [])
    return nodes, subgraph_ids


def read_manifest(path):
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(line.split())
    return rows


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def head(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "check-app"})
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"ERR {e.__class__.__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app_name")
    ap.add_argument("--urls", action="store_true", help="HEAD-check model and node URLs")
    ap.add_argument("--live", metavar="URL", help="running ComfyUI to check /object_info against")
    ap.add_argument("--models-dir", default=os.environ.get("COMFYUI_MODELS_DIR", "/mnt/gungnir/comfyui/models"))
    args = ap.parse_args()

    app = ROOT / "comfyui" / "apps" / args.app_name
    if not app.is_dir():
        sys.exit(f"no such app: {app}")
    problems = 0

    nodes_rows = read_manifest(app / "nodes.txt")
    model_rows = read_manifest(app / "models.txt")
    node_names = {norm(r[0]) for r in nodes_rows} | {norm(Path(r[1]).name.removesuffix(".git")) for r in nodes_rows}
    model_files = {Path(r[0]).name for r in model_rows}

    wf_files = sorted((app / "workflows").glob("*.json"))
    if not wf_files:
        print("WARNING no workflow JSONs in workflows/")
    all_types = set()
    for wf in wf_files:
        nodes, subgraphs = load_workflow_nodes(wf)
        packs, models = {}, set()
        for n in nodes:
            t = n.get("type")
            if not t or t in subgraphs:
                continue
            all_types.add(t)
            props = n.get("properties") or {}
            pack = props.get("cnr_id") or props.get("aux_id")
            if pack and pack != "comfy-core":
                packs.setdefault(pack, set()).add(t)
            for v in n.get("widgets_values") or []:
                if isinstance(v, str) and MODEL_EXT.search(v) and "\n" not in v and "://" not in v:
                    models.add(v.replace("\\", "/"))
                elif isinstance(v, dict) and isinstance(v.get("lora"), str):
                    models.add(v["lora"].replace("\\", "/"))  # Power Lora Loader rows

        print(f"\n== {wf.name}")
        for pack, types in sorted(packs.items()):
            hints = CNR_ALIASES.get(pack.lower(), []) + [pack.split("/")[-1]]
            ok = any(norm(h) in node_names for h in hints)
            # Heuristic only (cnr_id often differs from the repo name);
            # --live is the authoritative node check.
            mark = "ok  " if ok else "CHECK"
            print(f"  {mark} node pack {pack}: {', '.join(sorted(types))}")
        for m in sorted(models):
            base = Path(m).name
            if base in model_files:
                print(f"  ok   model {m}")
                continue
            hits = list(Path(args.models_dir).rglob(base)) if Path(args.models_dir).is_dir() else []
            if hits:
                print(f"  ok   model {m} (already on disk: {hits[0].relative_to(args.models_dir)})")
            else:
                problems += 1
                print(f"  MISS model {m} (not in models.txt or {args.models_dir})")

    if args.urls:
        print("\n== URLs")
        for r in nodes_rows:
            code = head(r[1].removesuffix(".git"))
            bad = code != 200
            problems += bad
            print(f"  {'FAIL' if bad else 'ok  '} {code} {r[1]}")
        for r in model_rows:
            code = head(r[1])
            bad = code != 200
            problems += bad
            print(f"  {'FAIL' if bad else 'ok  '} {code} {r[0]}")

    if args.live:
        print(f"\n== live {args.live}")
        try:
            with urllib.request.urlopen(args.live.rstrip("/") + "/object_info", timeout=60) as r:
                info = json.load(r)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"  cannot reach {args.live}: {e}")
        missing = sorted(t for t in all_types if t not in info and t not in FRONTEND_TYPES)
        for t in missing:
            print(f"  MISS node type {t}")
        problems += len(missing)
        if not missing:
            print(f"  ok   all {len(all_types)} workflow node types are registered")
        for folder in ("loras", "diffusion_models", "text_encoders", "vae"):
            try:
                with urllib.request.urlopen(f"{args.live.rstrip('/')}/models/{folder}", timeout=30) as r:
                    print(f"  {folder}: {len(json.load(r))} files")
            except Exception:  # noqa: BLE001
                pass

    print(f"\n{'OK' if not problems else f'{problems} problem(s)'}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
