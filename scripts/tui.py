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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static
from rich.text import Text

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("AITD_COMPOSE_DIR", SCRIPT_ROOT)).expanduser().resolve()
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
    states, images = {}, None
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
        built = (image in images) if not data['error'] and image and images is not None else None
        if not data["error"] and not row and image and built is False and svc.get("build"):
            state = "not built"
        if data["error"]:
            state = "unknown"
        path = ROOT / "comfyui" / "apps" / name
        models = manifest(path / "models.txt")
        present = [r[0] for r in models if model_path(r[0], svc, env).is_file()]
        missing = [r[0] for r in models if r[0] not in present]
        logs = []
        if state == "running" and path.is_dir():
            _, out, _ = run(DOCKER + ["compose", "--profile", "*", "logs", "--tail", "100", name])
            logs = [line for line in out.splitlines() if "[DL]" in line][-8:]
            # The entrypoint writes one [DL] line before curl, which can run
            # for hours. Its .part file persists throughout the download.
            partial = any(Path(str(model_path(rel, svc, env)) + ".part").is_file() for rel in missing)
            if partial:
                state = "downloading models"
        port = port_for(name, svc, services, env)
        suffix = "/loras" if name == "loramgr" else ""
        urls = [f"http://{host}:{port}{suffix}" for host in ["localhost", *lan, *ts, *([dns] if dns else [])] if port != "?" and name != "proxy"]
        data["services"][name] = {"state": state, "port": port, "urls": urls, "gpu": svc.get("runtime") == "nvidia" or bool(svc.get("gpus")), "profiles": svc.get("profiles", []), "image": image, "built": built, "ref": env.get("COMFY_REF_" + name.upper().replace("-", "_"), ""), "app": path.is_dir(), "workflows": [p.name for p in sorted((path / "workflows").glob("*")) if p.is_file()], "models_present": present, "models_missing": missing, "nodes": len(manifest(path / "nodes.txt")), "downloads": logs}
    if "proxy" in data["services"]:
        data["proxy"] = f"{data['services']['proxy']['state']} (basic auth configured; not independently verified)"
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


class PasswordPrompt(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self):
        with Vertical(id="dialog"):
            yield Label("Proxy password for the live check (Esc cancels)")
            yield Input(password=True, placeholder="Password", id="password")
            yield Button("Check", id="submit", variant="primary")

    def on_input_submitted(self, event):
        self.dismiss(event.value or None)

    def on_button_pressed(self, event):
        self.dismiss(self.query_one(Input).value or None)

    def action_cancel(self):
        self.dismiss(None)


class Output(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title, command, extra_env=None, on_success=None):
        super().__init__()
        self.title_text, self.command = title, command
        self.extra_env = extra_env or {}
        self.on_success = on_success
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
            self.proc = await asyncio.create_subprocess_exec(*self.command, env={**os.environ, **self.extra_env}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            while line := await self.proc.stdout.readline():
                log.write(Text(line.decode(errors="replace").rstrip()))
            code = await self.proc.wait()
            if code and self.command[:2] == ["sudo", "-n"]:
                log.write("If sudo needs a password, exit and run make tui again.")
            if code == 0 and self.on_success:
                self.command, self.on_success = self.on_success, None
                await self.stream()
                return
            log.write(f"\nExited with status {code}")
        except OSError as e:
            log.write(str(e))

    def action_close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
        self.dismiss()


class DetailPane(VerticalScroll):
    can_focus = True


class Dashboard(App):
    TITLE = "AITREPRENEUR"
    SUB_TITLE = "Service dashboard"
    CSS = """
    Screen { background: #0c111b; color: #dbe5f3; }
    Header { background: #111a2a; color: #e6eef9; }
    Footer { background: #111a2a; }
    #overview { height: 6; padding: 0 1; }
    .card { width: 1fr; height: 6; background: #152131; border: round #33475e; padding: 0 1; }
    #status-card, #network-card { margin-left: 1; }
    #notice { height: auto; max-height: 2; padding: 0 2; color: #f5bd75; }
    #body { height: 1fr; padding: 0 1; }
    #sidebar { width: 34%; min-width: 25; height: 1fr; border: round #33475e; background: #111b29; }
    #list-title { height: 2; padding: 0 1; color: #8da6c4; text-style: bold; }
    #services { height: 1fr; background: #111b29; }
    #services > ListItem { height: 3; padding: 0 1; }
    #services > ListItem.--highlight { background: #263b53; }
    #services:focus > ListItem.--highlight { background: #29455f; }
    #detail { width: 1fr; height: 1fr; margin-left: 1; border: round #33475e; background: #111b29; padding: 1 2; overflow-y: auto; }
    #detail:focus { border: round #79c9ff; }
    #detail-content { width: 1fr; height: auto; }
    #dialog { width: 70%; height: auto; max-height: 70%; padding: 1 2; border: thick $accent; background: $surface; align: center middle; }
    Confirm, PasswordPrompt { align: center middle; background: $background 70%; }
    Confirm Horizontal { height: 3; align: center middle; }
    Output { background: $surface; padding: 1; }
    Output Label { height: 1; }
    Output RichLog { height: 1fr; border: solid $primary; }
    """
    BINDINGS = [Binding("s", "start", "Start"), Binding("x", "stop", "Stop"), Binding("r", "restart", "Restart"), Binding("L", "logs", "Logs"), Binding("h", "focus_services", "Services", show=False), Binding("l", "focus_detail", "Details", show=False), Binding("j", "move_down", "Down", show=False), Binding("k", "move_up", "Up", show=False), Binding("b", "build", "Build"), Binding("o", "open_url", "Open"), Binding("c", "copy_url", "Copy"), Binding("u", "check", "Check"), Binding("X", "stop_all", "Stop all", priority=True), Binding("question_mark", "help", "Help"), Binding("q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.data = {"services": {}}
        self.selected = None
        self.refreshing = False
        self.rendered_detail = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="overview"):
            yield Static("Loading GPU…", id="gpu-card", classes="card")
            yield Static("Loading services…", id="status-card", classes="card")
            yield Static("Loading network…", id="network-card", classes="card")
        yield Static("", id="notice")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("SERVICES  ·  ↑↓ to select", id="list-title")
                yield ListView(id="services")
            with DetailPane(id="detail"):
                yield Static("Select a service", id="detail-content")
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

    @staticmethod
    def state_style(state):
        return {"running": "#65d9a3", "downloading models": "#79c9ff", "restarting": "#f5bd75", "not built": "#f5bd75", "stopped": "#8da2b9", "exited": "#8da2b9", "unknown": "#f5bd75"}.get(state, "#f08b8b")

    def render_state(self):
        d = self.data
        gpu = Text("GPU  /  LIVE\n", style="bold #87a9ca")
        gpu.append(d['gpu'], style="#e6eef9")
        self.query_one("#gpu-card", Static).update(gpu)

        services = d['services']
        running = sum(item['state'] in ('running', 'downloading models') for item in services.values())
        downloading = sum(item['state'] == 'downloading models' for item in services.values())
        unknown = sum(item['state'] == 'unknown' for item in services.values())
        status = Text("STACK  /  SERVICES\n", style="bold #87a9ca")
        if unknown:
            status.append(f"{unknown} unknown", style="bold #f5bd75")
            status.append(f"  ·  {len(services)} total")
        else:
            status.append(f"{running} running", style="bold #65d9a3")
            status.append(f"  ·  {len(services) - running} other")
        if downloading:
            status.append(f"\n{downloading} downloading", style="#79c9ff")
        proxy = services.get('proxy')
        if proxy:
            status.append("\nProxy: ")
            status.append(proxy['state'], style=self.state_style(proxy['state']))
            status.append(" · auth configured", style="#8da6c4")
        self.query_one("#status-card", Static).update(status)

        network = Text("NETWORK  /  ACCESS\n", style="bold #87a9ca")
        network.append("LAN  ", style="#8da6c4")
        network.append(", ".join(d['lan']) or "unavailable")
        network.append("\nTAIL  ", style="#8da6c4")
        network.append(", ".join(d['tailscale']) or "unavailable")
        if d['magicdns']:
            network.append("\n" + d['magicdns'], style="#8da6c4")
        self.query_one("#network-card", Static).update(network)
        notice = Text("PROJECT  ", style="bold #8da6c4")
        notice.append(str(ROOT), style="#8da6c4")
        if d['error']:
            notice.append("\n" + d['error'], style="bold #f5bd75")
        self.query_one("#notice", Static).update(notice)
        view = self.query_one(ListView)
        names = list(services)
        if self.selected not in names:
            self.selected = names[0] if names else None
        existing = {item.name: item for item in view.children if isinstance(item, ListItem)}
        # Keep the widgets (and their highlight) across normal refreshes.
        # Rebuild only when the Compose service set actually changes.
        rebuild = list(existing) != names
        if rebuild:
            view.clear()
        for name, item in services.items():
            label = Text()
            label.append("● ", style=self.state_style(item['state']))
            label.append(name, style="bold #e6eef9")
            label.append("\n  " + item['state'].upper(), style=self.state_style(item['state']))
            if name != 'proxy' and item['port'] != '?':
                label.append("  ·  :" + item['port'], style="#8da6c4")
            if rebuild:
                view.append(ListItem(Label(label), name=name))
            else:
                existing[name].query_one(Label).update(label)
        if self.selected:
            if rebuild:
                view.index = names.index(self.selected)
            self.show_detail()

    def on_list_view_highlighted(self, event):
        if event.item:
            self.selected = event.item.name
            self.show_detail()

    def show_detail(self):
        item = self.data['services'].get(self.selected)
        if not item:
            return
        text = Text()
        def section(title):
            text.append("\n" + title.upper() + "\n", style="bold #87a9ca")
        def line(label, value, style="#dbe5f3"):
            text.append(label + "  ", style="#8da6c4")
            text.append(str(value) + "\n", style=style)
        text.append(self.selected.upper() + "\n", style="bold #eaf3ff")
        text.append("● " + item['state'].upper() + "\n", style="bold " + self.state_style(item['state']))
        section("Access")
        if item['urls']:
            for label, url in zip(['LOCAL', *(['LAN'] * len(self.data['lan'])), *(['TAILSCALE'] * len(self.data['tailscale'])), *(['MAGICDNS'] if self.data['magicdns'] else [])], item['urls']):
                line(label, url, "#8ed0ff")
            if 'proxy' in self.data['services']:
                line("LOGIN", "Proxy authentication required")
        else:
            text.append("No app URL for this service\n", style="#8da6c4")
        section("Image")
        built = "unknown" if item['built'] is None else ("available" if item['built'] else "not built")
        line("IMAGE", item['image'] or 'unspecified')
        line("BUILD", built)
        if item['app']:
            line("COMFYUI REF", item['ref'] or 'default')
            section("App resources")
            present, missing = item['models_present'], item['models_missing']
            line("MODELS", f"{len(present)} present  ·  {len(missing)} missing", "#f5bd75" if missing else "#65d9a3")
            line("NODES", item['nodes'])
            line("WORKFLOWS", len(item['workflows']))
            for workflow in item['workflows']:
                text.append("  " + workflow + "\n", style="#dbe5f3")
            if missing:
                section("Missing models")
                for model in missing:
                    text.append("  ○ " + model + "\n", style="#f5bd75")
            if present:
                section("Available models")
                for model in present:
                    text.append("  ✓ " + model + "\n", style="#9db8a9")
        if item['downloads']:
            section("Recent downloads")
            for log in item['downloads']:
                text.append(log + "\n", style="#79c9ff")
        detail = self.query_one("#detail", VerticalScroll)
        signature = (self.selected, text.plain, tuple(text.spans))
        if signature != self.rendered_detail:
            same_service = self.rendered_detail is not None and self.rendered_detail[0] == self.selected
            scroll_y = detail.scroll_y if same_service else 0
            self.query_one("#detail-content", Static).update(text)
            self.call_after_refresh(detail.scroll_to, y=scroll_y, animate=False, force=True)
            self.rendered_detail = signature

    def action_focus_services(self):
        self.query_one("#services", ListView).focus()

    def action_focus_detail(self):
        self.query_one("#detail", DetailPane).focus()

    def action_move_down(self):
        if self.focused is self.query_one("#detail", DetailPane):
            self.query_one("#detail", DetailPane).scroll_down(animate=False)
        else:
            self.query_one("#services", ListView).action_cursor_down()

    def action_move_up(self):
        if self.focused is self.query_one("#detail", DetailPane):
            self.query_one("#detail", DetailPane).scroll_up(animate=False)
        else:
            self.query_one("#services", ListView).action_cursor_up()

    def command(self, *args):
        return DOCKER + ["compose", "--profile", "*", *args]

    def output(self, title, command, extra_env=None, on_success=None):
        self.push_screen(Output(title, command, extra_env, on_success), lambda _: self.refresh_state())

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
                if 'proxy' in self.data['services'] and name != 'proxy':
                    self.output('Start proxy and ' + name, DOCKER + ['compose', 'up', '-d', '--no-deps', 'proxy'], on_success=cmd)
                else:
                    self.output('Start ' + name, cmd)
        warnings = []
        if item['state'] in ('running', 'downloading models', 'restarting'):
            warnings.append(name + ' is already active; starting it will recreate its container and interrupt any work.')
        if item['gpu'] and others:
            warnings.append('Other GPU apps running: ' + ', '.join(others) + '\n' + self.data['gpu'])
        if warnings:
            self.push_screen(Confirm('\n\n'.join(warnings) + '\n\nStart ' + name + '?'), start)
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
        cmd = [sys.executable, str(ROOT / 'scripts/check-app.py'), self.selected, '--models-dir', env_values().get('COMFYUI_MODELS_DIR', '/mnt/gungnir/comfyui/models')]
        if item['state'] in ('running', 'downloading models') and item['urls']:
            cmd += ['--live', item['urls'][0]]
        if '--live' in cmd and 'proxy' in self.data['services']:
            password = os.environ.get('AITD_PASSWORD')
            def check(password):
                if password:
                    self.output('Check ' + self.selected, cmd, {'AITD_PASSWORD': password})
            if password:
                check(password)
            else:
                self.push_screen(PasswordPrompt(), check)
        else:
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
        self.push_screen(Confirm('h/l focus list/details · j/k move or scroll\ns start · x stop · r restart · L logs · b build\no open URL · c copy URL · u check app · X stop all\nq quit · Esc close pane'), lambda _: None)


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
