#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=8.2,<9", "PyYAML>=6.0.2"]
# ///
"""Local dashboard for the Compose services. Use --snapshot for a noninteractive view."""
import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, RichLog, Static
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
DOCKER = shlex.split(os.environ.get("AITD_DOCKER", "sudo -n docker"))


def run(cmd, timeout=12):
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def env_values():
    values = {}
    for file in (ROOT / ".env.example", ROOT / ".env"):
        if file.exists():
            for line in file.read_text().splitlines():
                m = re.match(r"\s*(?:export\s+)?([\w]+)\s*=\s*(.*?)\s*$", line)
                if m and not line.lstrip().startswith("#"):
                    values[m[1]] = m[2].strip("\"'")
    values.update(os.environ)
    return values


def expand(value, env):
    def replacement(m):
        name, default = m.group(1), m.group(2)
        return env.get(name) or default or ""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replacement, value)


def compose_config(env):
    code, out, err = run(DOCKER + ["compose", "--profile", "*", "config", "--format", "json"])
    if not code:
        try:
            return json.loads(out).get("services", {}), None
        except json.JSONDecodeError:
            err = "invalid Compose JSON"
    # Compose can be read without Docker; this also preserves useful offline details.
    try:
        raw = expand((ROOT / "docker-compose.yml").read_text(), env)
        services = yaml.safe_load(raw).get("services", {})
    except (OSError, yaml.YAMLError) as e:
        return {}, f"Compose config unavailable: {e}"
    if "sudo" in DOCKER[0] and ("password" in err.lower() or "terminal" in err.lower() or "sudo" in err.lower()):
        error = "docker unavailable: sudo needs a password — run via make tui"
    else:
        error = "docker unavailable: " + (err.strip().splitlines()[-1] if err.strip() else "Compose config failed")
    return services, error


def manifest(path):
    try:
        return [line.split() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        return []


def port_for(name, svc, services, env):
    var = name.upper().replace("-", "_") + "_PORT"
    configured = env.get(var)
    def published(port):
        if isinstance(port, dict):
            return str(port.get("published", ""))
        parts = str(port).split("/")[0].split(":")
        return parts[-2] if len(parts) >= 2 else ""
    ports = [published(p) for p in svc.get("ports", [])]
    if "proxy" in services and name != "proxy":
        # Port variable is the stable interface shared by app and proxy.
        return configured or "?"
    return next((p for p in ports if p), configured or "?")


def model_path(rel, service, env):
    root = Path(env.get("COMFYUI_MODELS_DIR", "/mnt/gungnir/comfyui/models")).expanduser()
    target = "/comfyui/models/" + rel
    best = None
    for volume in service.get("volumes", []):
        if isinstance(volume, dict):
            source, dest = volume.get("source", ""), volume.get("target", "")
        else:
            parts = str(volume).split(":")
            source, dest = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        if source and dest and (target == dest or target.startswith(dest.rstrip("/") + "/")):
            if best is None or len(dest) > len(best[1]):
                best = (source, dest)
    if best:
        return Path(best[0]).expanduser() / target[len(best[1]):].lstrip("/")
    return root / rel


def network():
    _, out, _ = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    lan = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and not re.match(r"^(docker|br-|veth|tailscale)", parts[1]):
            lan.append(parts[3].split("/")[0])
    _, out, _ = run(["tailscale", "status", "--json"])
    try:
        ts = json.loads(out).get("Self", {})
        ips = [ip for ip in ts.get("TailscaleIPs", []) if ":" not in ip]
        dns = ts.get("DNSName", "").rstrip(".")
    except (ValueError, AttributeError):
        ips, dns = [], ""
    return lan, ips, dns


def gpu():
    code, out, _ = run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
    if code or not out.strip():
        return "GPU unavailable"
    return " | ".join(f"{p[0]}: {p[1]}/{p[2]} MiB, {p[3]}%" for line in out.splitlines() if len(p := [s.strip() for s in line.split(",")]) >= 4)


def collect():
    env = env_values()
    services, error = compose_config(env)
    lan, ts, dns = network()
    data = {"gpu": gpu(), "lan": lan, "tailscale": ts, "magicdns": dns, "error": error, "proxy": "configured (authentication depends on proxy configuration)" if "proxy" in services else "not configured", "services": {}}
    states, images = {}, set()
    if not error:
        code, out, err = run(DOCKER + ["compose", "--profile", "*", "ps", "-a", "--format", "json"])
        if code:
            data["error"] = "docker unavailable: " + (err.strip() or "cannot read container state")
        else:
            try:
                rows = json.loads(out) if out.lstrip().startswith("[") else [json.loads(line) for line in out.splitlines() if line.strip()]
                states = {row.get("Service"): row for row in rows}
            except ValueError:
                data["error"] = "docker unavailable: invalid container state"
        code, out, _ = run(DOCKER + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
        if not code:
            images = set(out.splitlines())
    for name, svc in services.items():
        row = states.get(name, {})
        state = row.get("State", "stopped").lower()
        image = svc.get("image", "")
        built = (image in images) if not data['error'] and image else None
        if not error and not row and image and not built and svc.get("build"):
            state = "not built"
        if error:
            state = "unknown"
        logs = []
        if state == "running":
            _, out, _ = run(DOCKER + ["compose", "--profile", "*", "logs", "--since", "30s", "--tail", "100", name])
            logs = [line for line in out.splitlines() if "[DL]" in line][-8:]
            if logs:
                state = "downloading models"
        path = ROOT / "comfyui" / "apps" / name
        models = manifest(path / "models.txt")
        present = [r[0] for r in models if model_path(r[0], svc, env).is_file()]
        missing = [r[0] for r in models if r[0] not in present]
        port = port_for(name, svc, services, env)
        suffix = "/loras" if name == "loramgr" else ""
        urls = [f"http://{host}:{port}{suffix}" for host in ["localhost", *lan, *ts, *([dns] if dns else [])] if port != "?"]
        data["services"][name] = {"state": state, "port": port, "urls": urls, "gpu": svc.get("runtime") == "nvidia" or bool(svc.get("gpus")), "profiles": svc.get("profiles", []), "image": image, "built": built, "ref": env.get("COMFY_REF_" + name.upper().replace("-", "_"), ""), "app": path.is_dir(), "workflows": [p.name for p in sorted((path / "workflows").glob("*")) if p.is_file()], "models_present": present, "models_missing": missing, "nodes": len(manifest(path / "nodes.txt")), "downloads": logs}
    return data


class Confirm(ModalScreen):
    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical(id="dialog"):
            yield Label(self.message)
            with Horizontal():
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Confirm", id="confirm", variant="warning")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm")

    def key_escape(self):
        self.dismiss(False)


class Output(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title, command):
        super().__init__()
        self.title_text, self.command = title, command
        self.proc = None

    def compose(self):
        yield Label(self.title_text + " — Esc closes")
        yield RichLog(id="output", wrap=True, markup=False)

    def on_mount(self):
        self.run_worker(self.stream(), exclusive=True)

    async def stream(self):
        log = self.query_one(RichLog)
        log.write("$ " + shlex.join(self.command))
        try:
            self.proc = await asyncio.create_subprocess_exec(*self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            while line := await self.proc.stdout.readline():
                log.write(Text(line.decode(errors="replace").rstrip()))
            code = await self.proc.wait()
            if code and self.command[:2] == ["sudo", "-n"]:
                log.write("If sudo needs a password, exit and run make tui again.")
            log.write(f"\nExited with status {code}")
        except OSError as e:
            log.write(str(e))

    def action_close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
        self.dismiss()


class Dashboard(App):
    CSS = """
    #summary { height: auto; max-height: 6; padding: 0 1; }
    #body { height: 1fr; }
    #services { width: 33%; border: solid $primary; }
    #detail { width: 67%; border: solid $primary; padding: 1; overflow-y: auto; }
    #dialog { width: 70%; height: auto; max-height: 70%; padding: 1 2; border: thick $accent; background: $surface; align: center middle; }
    Confirm { align: center middle; background: $background 70%; }
    Confirm Horizontal { height: 3; align: center middle; }
    Output { background: $surface; padding: 1; }
    Output Label { height: 1; }
    Output RichLog { height: 1fr; border: solid $primary; }
    """
    BINDINGS = [Binding("s", "start", "Start"), Binding("x", "stop", "Stop"), Binding("r", "restart", "Restart"), Binding("l", "logs", "Logs"), Binding("b", "build", "Build"), Binding("o", "open_url", "Open"), Binding("c", "copy_url", "Copy"), Binding("u", "check", "Check"), Binding("X", "stop_all", "Stop all", priority=True), Binding("question_mark", "help", "Help"), Binding("q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.data = {"services": {}}
        self.selected = None
        self.refreshing = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="summary")
        with Horizontal(id="body"):
            yield ListView(id="services")
            yield Static("Select a service", id="detail")
        yield Footer()

    def on_mount(self):
        self.refresh_state()
        self.set_interval(5, self.refresh_state)
        if DOCKER[:2] == ["sudo", "-n"]:
            self.set_interval(60, self.keep_sudo)

    def keep_sudo(self):
        async def keep():
            code, _, _ = await asyncio.to_thread(run, ["sudo", "-n", "-v"])
            if code:
                self.notify("sudo expired; restart with make tui", severity="error")
        self.run_worker(keep())

    def refresh_state(self):
        if self.refreshing:
            return
        self.refreshing = True
        async def refresh():
            try:
                self.data = await asyncio.to_thread(collect)
                self.render_state()
            finally:
                self.refreshing = False
        self.run_worker(refresh())

    def render_state(self):
        d = self.data
        summary = f"{d['gpu']}\nLAN: {', '.join(d['lan']) or 'unavailable'} | Tailscale: {', '.join(d['tailscale']) or 'unavailable'} | MagicDNS: {d['magicdns'] or 'unavailable'}\nProxy: {d['proxy']}"
        if d['error']:
            summary += "\n" + d['error']
        self.query_one("#summary", Static).update(Text(summary))
        view = self.query_one(ListView)
        names = list(d["services"])
        if self.selected not in names:
            self.selected = names[0] if names else None
        view.clear()
        for name, item in d["services"].items():
            view.append(ListItem(Label(Text(f"{name}  {item['state']}  :{item['port']}")), name=name))
        if self.selected:
            view.index = names.index(self.selected)
            self.show_detail()

    def on_list_view_highlighted(self, event):
        if event.item:
            self.selected = event.item.name
            self.show_detail()

    def show_detail(self):
        item = self.data["services"].get(self.selected)
        if not item:
            return
        built = "unknown" if item['built'] is None else ("yes" if item['built'] else "no")
        lines = [self.selected, f"State: {item['state']}", f"Image: {item['image'] or 'unspecified'} (built: {built})", "URLs:", *["  " + u for u in item['urls']]]
        if item['app']:
            lines += [f"ComfyUI ref: {item['ref'] or 'default'}", "Workflows: " + (", ".join(item['workflows']) or "none"), f"Models: {len(item['models_present'])} present, {len(item['models_missing'])} missing", *["  present: " + p for p in item['models_present']], *["  missing: " + p for p in item['models_missing']], f"Custom nodes: {item['nodes']}"]
        if item['downloads']:
            lines += ["Recent downloads:", *item['downloads']]
        self.query_one("#detail", Static).update(Text("\n".join(lines)))

    def command(self, *args):
        return DOCKER + ["compose", "--profile", "*", *args]

    def output(self, title, command):
        self.push_screen(Output(title, command), lambda _: self.refresh_state())

    def action_start(self):
        name = self.selected
        item = self.data['services'].get(name)
        if not item:
            return
        others = [n for n, s in self.data['services'].items() if n != name and s['gpu'] and s['state'] in ('running', 'downloading models', 'restarting')]
        def start(yes=True):
            if yes:
                profile = item['profiles'][0] if item['profiles'] else None
                cmd = DOCKER + ['compose'] + (['--profile', profile] if profile else []) + ['up', '-d', '--force-recreate', name]
                self.output('Start ' + name, cmd)
        if item['gpu'] and others:
            self.push_screen(Confirm('GPU apps running: ' + ', '.join(others) + '\n' + self.data['gpu'] + '\nStart ' + name + '?'), start)
        else:
            start()

    def action_stop(self):
        if self.selected:
            self.output('Stop ' + self.selected, self.command('stop', self.selected))

    def action_restart(self):
        if self.selected:
            self.output('Restart ' + self.selected, self.command('restart', self.selected))

    def action_build(self):
        if self.selected:
            self.output('Build ' + self.selected, self.command('build', self.selected))

    def action_logs(self):
        if self.selected:
            self.output('Logs ' + self.selected, self.command('logs', '-f', '--tail', '100', self.selected))

    def action_stop_all(self):
        self.push_screen(Confirm('Stop all Compose services?'), lambda yes: self.output('Stop all', self.command('stop')) if yes else None)

    def action_check(self):
        item = self.data['services'].get(self.selected)
        if not item or not item['app']:
            self.notify('Checks are available for ComfyUI apps', severity='warning')
            return
        cmd = [sys.executable, str(ROOT / 'scripts/check-app.py'), self.selected]
        if item['state'] in ('running', 'downloading models') and item['urls']:
            cmd += ['--live', item['urls'][0]]
        self.output('Check ' + self.selected, cmd)

    def url_action(self, program):
        item = self.data['services'].get(self.selected)
        if not item or not item['urls']:
            self.notify('No URL available', severity='warning')
            return
        async def invoke():
            cmd = [program] + ([item['urls'][0]] if program == 'xdg-open' else [])
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if program == 'wl-copy' else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.communicate(item['urls'][0].encode() if program == 'wl-copy' else None)
                self.notify(('Copied URL' if program == 'wl-copy' else 'Opened URL') if proc.returncode == 0 else f'{program} failed', severity='information' if proc.returncode == 0 else 'error')
            except OSError as e:
                self.notify(str(e), severity='error')
        self.run_worker(invoke())

    def action_open_url(self): self.url_action('xdg-open')
    def action_copy_url(self): self.url_action('wl-copy')
    def action_help(self):
        self.push_screen(Confirm('s start · x stop · r restart · l logs · b build\no open URL · c copy URL · u check app · X stop all\nq quit · Esc close pane'), lambda _: None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', '--once', action='store_true', help='print the computed state as JSON')
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(collect(), indent=2))
    else:
        Dashboard().run()


if __name__ == '__main__':
    main()
