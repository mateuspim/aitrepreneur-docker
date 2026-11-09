#!/usr/bin/env python3
"""
wire-app.py — Register a ComfyUI app (comfyui/apps/<name>/) in the project.

Usage:
    python scripts/wire-app.py <name> --comfy-ref vX.Y.Z --title "Qwen Image Edit 2.1"
                               [--port N] [--lora-dir "Qwen 2.1"] [--prefixes "qwen- qwen_"]
                               [--dry-run]

Edits (each insertion is anchored on an existing pattern; the script aborts
without writing anything if an anchor is missing or the app already exists):
    docker-compose.yml          service block, inserted before the loramgr service
    .env.example (+ .env)       COMFY_REF_<NAME> and <NAME>_PORT
    Makefile                    COMPOSE profiles, app target, version, delete-input, .PHONY
    README.md                   service table, port vars, make list, workflows note
    scripts/migrate-model-dirs.sh  APPS, PREFIXES and (if --lora-dir) LORA_DIRS

--port defaults to the next free port after the highest *_PORT in .env.example.
--lora-dir is the loras/ subfolder the app mounts as its loras/; use the folder
LoRA Manager files that base model under (e.g. "Flux.2 Klein 9B", "Qwen 2.1").
Defaults to the capitalized app name.
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Edit:
    def __init__(self):
        self.files = {}

    def text(self, rel):
        if rel not in self.files:
            p = ROOT / rel
            old = p.read_text() if p.exists() else None
            self.files[rel] = [old, old]
        return self.files[rel][1]

    def set(self, rel, new):
        self.files[rel][1] = new

    def sub(self, rel, pattern, repl, flags=re.MULTILINE, what=None):
        s = self.text(rel)
        new, n = re.subn(pattern, repl, s, count=1, flags=flags)
        if n != 1:
            sys.exit(f"anchor not found in {rel}: {what or pattern}")
        self.set(rel, new)

    def insert_before(self, rel, anchor, block):
        s = self.text(rel)
        if s.count(anchor) != 1:
            sys.exit(f"anchor not found exactly once in {rel}: {anchor!r}")
        self.set(rel, s.replace(anchor, block + anchor))

    def apply(self, dry_run):
        for rel, (old, new) in self.files.items():
            if old == new or old is None:
                continue
            if dry_run:
                sys.stdout.writelines(difflib.unified_diff(
                    old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
            else:
                (ROOT / rel).write_text(new)
                print(f"updated {rel}")


def next_port(env_text):
    ports = sorted(int(p) for p in re.findall(r"^[A-Z0-9_]+_PORT=(\d+)", env_text, re.MULTILINE))
    p = max(p for p in ports if p < 8600) + 1 if ports else 8188
    while p in ports:
        p += 1
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--comfy-ref", required=True)
    ap.add_argument("--title", required=True, help='README description, e.g. "Qwen Image Edit 2.1"')
    ap.add_argument("--port", type=int)
    ap.add_argument("--lora-dir")
    ap.add_argument("--prefixes", help="extra filename prefixes for migrate-model-dirs.sh")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    name, NAME, Cap = a.name, a.name.upper(), a.name.capitalize()
    if not (ROOT / "comfyui/apps" / name).is_dir():
        sys.exit(f"comfyui/apps/{name}/ does not exist; run add-app-from-bat.py first")
    e = Edit()

    compose = e.text("docker-compose.yml")
    if re.search(rf"^  {re.escape(name)}:$", compose, re.MULTILINE):
        sys.exit(f"service '{name}' already exists in docker-compose.yml")

    env = e.text(".env.example")
    port = a.port or next_port(env)
    if re.search(rf"_PORT={port}$", env, re.MULTILINE):
        sys.exit(f"port {port} is already used in .env.example")
    lora_dir = a.lora_dir or Cap
    models = "${COMFYUI_MODELS_DIR:-/mnt/gungnir/comfyui/models}"

    # ── docker-compose.yml
    lora_comment = (f"      # LoRA Manager categorizes {lora_dir} downloads under this folder.\n"
                    if lora_dir != Cap else "")
    e.insert_before("docker-compose.yml", "  # LoRA Manager, standalone edition", f"""\
  {name}:
    <<: *gpu
    profiles: [{name}]
    build:
      context: ./comfyui
      args:
        APP: {name}
        COMFY_REF: ${{COMFY_REF_{NAME}:-{a.comfy_ref}}}
        CACHEBUST: ${{CACHEBUST:-0}}
    image: comfyui-{name}:local
    container_name: comfyui-{name}
    volumes:
      - {models}:/comfyui/models
{lora_comment}      - {models}/loras/{lora_dir}:/comfyui/models/loras
      - {models}/controlnet/{Cap}:/comfyui/models/controlnet
      - {models}/checkpoints/{Cap}:/comfyui/models/checkpoints
      - {models}/embeddings/{Cap}:/comfyui/models/embeddings
      - ./data/{name}:/data
      - ${{AUX_CACHE_DIR:-./data/{name}/cache}}:/data/cache
    environment:
      NVIDIA_VISIBLE_DEVICES: all
      HF_TOKEN: ${{HF_TOKEN:-}}

""")

    e.insert_before("docker-compose.yml", "    volumes:\n      - ./proxy/Caddyfile", f'      - "${{{NAME}_PORT:-{port}}}:{port}"\n')
    e.set("proxy/Caddyfile", e.text("proxy/Caddyfile") + f""":{port} {{
    import auth
    reverse_proxy {name}:8188
    handle_errors {{
        respond "{name} is not running — start it with make {name} or the TUI" {{err.status_code}}
    }}
}}

""")

    # ── .env.example and .env (last COMFY_REF_* line; last app *_PORT line)
    for rel in (".env.example", ".env"):
        if not (ROOT / rel).exists():
            continue
        s = e.text(rel)
        if f"{NAME}_PORT=" in s:
            continue
        refs = list(re.finditer(r"^COMFY_REF_\w+=.*$", s, re.MULTILINE))
        ports = [m for m in re.finditer(r"^(\w+)_PORT=.*$", s, re.MULTILINE)
                 if m.group(1) not in ("AI_TOOLKIT", "LORAMGR")]
        if not refs or not ports:
            sys.exit(f"anchor not found in {rel}: COMFY_REF_* / *_PORT lines")
        r, p = refs[-1], ports[-1]
        s = s[:p.end()] + f"\n{NAME}_PORT={port}" + s[p.end():]
        s = s[:r.end()] + f"\nCOMFY_REF_{NAME}={a.comfy_ref}" + s[r.end():]
        e.set(rel, s)

    # ── Makefile
    mk = "Makefile"
    e.sub(mk, r"^(COMPOSE = .*?)( --profile loramgr)$", rf"\1 --profile {name}\2", what="COMPOSE line")
    e.sub(mk, r"^(# anima, .*?)(, loramgr)", rf"\1, {name}\2", what="APP targets comment")
    e.sub(mk, r"^([a-z0-9 ]+): proxy ## Start one ComfyUI app \((.*)\)$",
          rf"\1 {name}: proxy ## Start one ComfyUI app (\2, {name} :{port})", what="app target")
    e.sub(mk, r"( lora-manager:/opt/lora-manager-commit\.txt; do)",
          rf" comfyui-{name}:/opt/app/comfyui-commit.txt\1", what="version loop")
    e.sub(mk, r"^(\tfor input_dir in .*?); do", rf"\1 data/{name}/input; do", what="delete-input loop")
    e.sub(mk, r"^(\.PHONY: .*?)( loramgr )", rf"\1 {name}\2", what=".PHONY")

    e.sub(mk, r"qwen\) echo 8195;;", rf"qwen) echo 8195;; {name}) echo {port};;", what="proxy port lookup")

    # ── README.md
    rd = "README.md"
    e.sub(rd, r"^(\| `loramgr` \|)",
          f"| `{name}` | ComfyUI + {a.title} | `make {name}` | <http://localhost:{port}> |\n\\1",
          what="service table loramgr row")
    e.sub(rd, r"(`KLEIN_PORT`[^)]*?)\)\.", rf"\1, `{NAME}_PORT`).", what="port variable list")
    e.sub(rd, r"(`make klein`(?: / `make \w+`)*),", rf"\1 / `make {name}`,", what="make list")
    e.sub(rd, r"(workflows? \(`comfyui/apps/\w+/workflows/`\))\.(?![\s\S]*comfyui/apps/\w+/workflows/)",
          rf"\1; `{name}` seeds its {a.title}\nworkflows (`comfyui/apps/{name}/workflows/`).",
          what="last workflows note")

    # ── scripts/migrate-model-dirs.sh
    mg = "scripts/migrate-model-dirs.sh"
    e.sub(mg, r"^APPS=\((.*)\)$", rf"APPS=(\1 {Cap})", what="APPS array")
    prefixes = f"{name}- {name}_ {NAME}- {NAME}_ {Cap}- {Cap}_" + (f" {a.prefixes}" if a.prefixes else "")
    e.sub(mg, r"^(declare -A PREFIXES=\(\n(?:  \[.*\n)*)(\))", rf'\1  [{Cap}]="{prefixes}"\n\2',
          what="PREFIXES map")
    label = f"{lora_dir}/".ljust(18) + (f" ({Cap}; LoRA Manager name)" if lora_dir != Cap else "")
    e.sub(mg, r"^(#   \S+/loras/)Shared/", rf"\g<1>{label.rstrip()}\n\g<1>Shared/", what="layout comment")
    if lora_dir != Cap:
        e.sub(mg, r"^(declare -A LORA_DIRS=\(\n(?:  \[.*\n)*)(\))", rf'\1  [{Cap}]="{lora_dir}"\n\2',
              what="LORA_DIRS map")

    e.apply(a.dry_run)
    if not a.dry_run:
        print(f"\nwired {name}: port {port}, ComfyUI {a.comfy_ref}, loras/{lora_dir}")


if __name__ == "__main__":
    main()
