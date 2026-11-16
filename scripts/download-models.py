#!/usr/bin/env python3
"""Download and integrity-check every ComfyUI app's models.txt on the host.

Does on the host what each container's entrypoint does at start, for all apps
at once, so first starts don't stall on downloads.

Paths come from the live compose config (`docker compose config --format json`
on stdin): a models.txt entry like loras/x.safetensors is written to wherever
that app mounts /comfyui/models/loras (e.g. .../loras/Minimax), so files land
exactly where the container will look for them.

Guardrails:
  * Files present and intact are never downloaded again; files shared by
    several apps are handled once.
  * A .part written to in the last BUSY_SECS seconds belongs to a download in
    progress (a container's entrypoint or another run) and is left alone.
    Only one run of this script at a time (lock file).
  * Every present file is checked: its size against the size Hugging Face
    reports, and a .safetensors header that matches the file length. With
    --verify, also its SHA-256 (slow; results cached until the file changes).
    A file that fails a check is deleted and downloaded again.
  * Each new download is checked for size and SHA-256 before it replaces the
    .part, so a bad download never becomes a "present" file.

Usage (see `make download-models`):
  docker compose --profile '*' config --format json \
    | python3 scripts/download-models.py [--dry-run] [--verify] [APP ...]
"""
import argparse
import fcntl
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "comfyui" / "apps"
CONTAINER_MODELS = "/comfyui/models"
BUSY_SECS = 120
LOCK = Path("/run/lock/aitd-download-models.lock")


def manifest(app):
    path = APPS_DIR / app / "models.txt"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and not parts[0].startswith("#"):
            rows.append((parts[0], parts[1].strip()))
    return rows


def mounts(service):
    """Container path -> host path for every bind under /comfyui/models."""
    out = {}
    for v in service.get("volumes", []):
        target = v.get("target", "") if isinstance(v, dict) else str(v).split(":")[1]
        source = v.get("source", "") if isinstance(v, dict) else str(v).split(":")[0]
        if target == CONTAINER_MODELS or target.startswith(CONTAINER_MODELS + "/"):
            out[target] = source
    return out


def host_path(relpath, binds):
    """Resolve models/<relpath> through the longest matching mount."""
    container = f"{CONTAINER_MODELS}/{relpath}"
    best = max((t for t in binds if container == t or container.startswith(t + "/")), key=len, default=None)
    if best is None:
        return None
    return Path(binds[best]) / container[len(best):].lstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def remote_meta(url):
    """(size, sha256) as Hugging Face reports them; None where unknown."""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "aitd-download-models"})
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = opener.open(req, timeout=20)
        headers = resp.headers
    except urllib.error.HTTPError as e:  # 302 to the CDN lands here
        headers = e.headers
    except OSError:
        return None, None
    size = headers.get("x-linked-size")
    sha = (headers.get("x-linked-etag") or "").strip('"')
    if size is None and not headers.get("location"):
        size = headers.get("content-length")
    return (int(size) if size else None), (sha if len(sha) == 64 else None)


def safetensors_problem(path):
    """Cheap local check: the header's tensor offsets must match the file size."""
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n > 100_000_000:
                return "header length is garbage"
            header = json.loads(f.read(n))
    except (OSError, ValueError, struct.error) as e:
        return f"unreadable header ({e.__class__.__name__})"
    end = max((t["data_offsets"][1] for k, t in header.items() if k != "__metadata__"), default=0)
    actual = path.stat().st_size
    if 8 + n + end != actual:
        return f"header expects {8 + n + end} bytes, file has {actual}"
    return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


class VerifyCache:
    """Remembers files whose SHA-256 matched, keyed on size + mtime."""

    def __init__(self, models_root):
        self.path = Path(models_root) / ".aitd-verified.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def ok(self, path, sha):
        st = path.stat()
        return self.data.get(str(path)) == [st.st_size, st.st_mtime_ns, sha]

    def add(self, path, sha):
        st = path.stat()
        self.data[str(path)] = [st.st_size, st.st_mtime_ns, sha]
        try:
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        except OSError:
            pass


def busy(part):
    try:
        return time.time() - part.stat().st_mtime < BUSY_SECS
    except FileNotFoundError:
        return False


def problem(target, size, sha, verify, cache):
    """None if the present file looks intact, else why not."""
    actual = target.stat().st_size
    if size is not None and actual != size:
        return f"size {actual}, expected {size}"
    if target.suffix == ".safetensors" and (why := safetensors_problem(target)):
        return why
    if verify and sha and not cache.ok(target, sha):
        print(f" [SHA] {target.name} …", flush=True)
        if sha256(target) != sha:
            return "SHA-256 mismatch"
        cache.add(target, sha)
    return None


def download(target, url, size, sha, cache):
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        # Same flags as the entrypoint: .part + -C - resumes interrupted files.
        cmd = ["curl", "-L", "--fail", "-C", "-", "--retry", "5", "--retry-delay", "5",
               "--retry-all-errors", "-o", str(part), url]
        if subprocess.run(cmd).returncode != 0:
            return f"curl failed (partial kept at {part}; rerun to resume)"
        bad = None
        if size is not None and part.stat().st_size != size:
            bad = f"size {part.stat().st_size}, expected {size}"
        elif sha:
            print(" [SHA] checking download …", flush=True)
            if sha256(part) != sha:
                bad = "SHA-256 mismatch"
        if not bad:
            os.replace(part, target)
            if sha:
                cache.add(target, sha)
            return None
        print(f" [BAD] {bad}; " + ("starting over from scratch" if attempt == 1 else "giving up"))
        part.unlink(missing_ok=True)
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("apps", nargs="*", help="apps to download (default: all with a models.txt)")
    ap.add_argument("--dry-run", action="store_true", help="check and report; change nothing")
    ap.add_argument("--verify", action="store_true", help="also SHA-256 every present file (slow, cached)")
    args = ap.parse_args()

    services = json.load(sys.stdin).get("services", {})
    apps = args.apps or sorted(p.name for p in APPS_DIR.iterdir() if (p / "models.txt").is_file())
    unknown = [a for a in apps if a not in services or not (APPS_DIR / a / "models.txt").is_file()]
    if unknown:
        sys.exit(f"unknown app(s): {', '.join(unknown)}")

    if not args.dry_run:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock = open(LOCK, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another download-models run is already going; wait for it to finish")

    # Unique targets; several apps share files (text encoders, VAEs).
    entries, seen = [], set()
    for app in apps:
        binds = mounts(services[app])
        for relpath, url in manifest(app):
            target = host_path(relpath, binds)
            if target is None:
                print(f" [??]  {app}: {relpath} (no /comfyui/models mount)")
            elif target not in seen:
                seen.add(target)
                entries.append((app, relpath, target, url))

    print(f"──── Checking {len(entries)} files for {len(apps)} apps")
    with ThreadPoolExecutor(16) as pool:
        metas = list(pool.map(lambda e: remote_meta(e[3]), entries))
    cache = VerifyCache(mounts(services[apps[0]]).get(CONTAINER_MODELS, entries[0][2].parent))

    todo, ok, skipped = [], 0, 0
    for (app, relpath, target, url), (size, sha) in zip(entries, metas):
        part = target.with_name(target.name + ".part")
        if busy(part):
            print(f" [BUSY] {app:<9} {relpath} (being downloaded elsewhere; skipped)")
            skipped += 1
        elif target.is_file():
            why = problem(target, size, sha, args.verify, cache)
            if why:
                print(f" [BAD] {app:<9} {relpath}: {why}")
                todo.append((app, relpath, target, url, size, sha, True))
            else:
                ok += 1
        else:
            print(f" [NEW] {app:<9} {relpath}" + (" (resumes .part)" if part.exists() else ""))
            todo.append((app, relpath, target, url, size, sha, False))
        if size is None:
            print(f"       note: no size from server for {relpath}; size check skipped")

    print(f"──── {ok} intact, {len(todo)} to (re)download, {skipped} busy elsewhere")
    if args.dry_run or not todo:
        return

    failed = []
    for i, (app, relpath, target, url, size, sha, corrupt) in enumerate(todo, 1):
        print(f"\n──── [{i}/{len(todo)}] {app}: {relpath}\n      → {target}", flush=True)
        part = target.with_name(target.name + ".part")
        if busy(part):  # something started on it since the scan
            print(" [BUSY] being downloaded elsewhere; skipped")
            continue
        if corrupt:
            print(" [DEL] removing the bad file")
            target.unlink(missing_ok=True)
        if why := download(target, url, size, sha, cache):
            failed.append(f"{app}: {relpath}: {why}")

    print(f"\n──── done: {len(todo) - len(failed)} ok, {len(failed)} failed")
    for f in failed:
        print(f"  FAIL {f}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
