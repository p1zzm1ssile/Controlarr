"""Small built-in web page for seeing and editing schedules.

No extra dependencies: it uses Python's own HTTP server. Schedules set with
labels are shown read-only; schedules added on the page are saved to the
schedules file (CONTROLARR_CONFIG), which Controlarr re-reads on every check.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from docker.errors import NotFound

from .schedule import (ACTIONS, LABEL_PREFIX, rules_from_config,
                       rules_from_labels, validate_entry)

log = logging.getLogger("controlarr.web")

FIELDS = ("stop", "start", "restart", "days", "order", "delay")
INDEX_HTML = Path(__file__).with_name("static").joinpath("index.html")
_config_lock = threading.Lock()


# ---------------------------------------------------------------- config file

def read_config(path: Path) -> dict:
    if not path.exists():
        return {"containers": {}}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("containers", {})
    if data["containers"] is None:
        data["containers"] = {}
    return data


class _QuotedDumper(yaml.SafeDumper):
    pass


# Quote anything with a digit so times like 23:30 are never read back as numbers.
_QuotedDumper.add_representer(
    str, lambda d, v: d.represent_scalar(
        "tag:yaml.org,2002:str", v, style='"' if any(ch.isdigit() for ch in v) else None))


def write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("# Managed by Controlarr. You can edit this file by hand too.\n"
            "# Times are 24-hour HH:MM.\n"
            + yaml.dump(data, Dumper=_QuotedDumper, sort_keys=True,
                        default_flow_style=False, allow_unicode=True))
    # Write to a temporary file then rename, so a half-written file is never read.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".schedules-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def clean_entry(raw: dict) -> dict:
    entry = {}
    for key in FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        entry[key] = int(value) if key in ("order", "delay") and value.isdigit() else value
    return entry


# ---------------------------------------------------------------- state

def _labels_entry(labels: dict) -> dict:
    return {k[len(LABEL_PREFIX):]: v for k, v in labels.items()
            if k.startswith(LABEL_PREFIX) and k[len(LABEL_PREFIX):] in FIELDS}


def build_state(client, settings, own_id) -> dict:
    from .main import plan_wait  # avoid an import cycle

    now = datetime.now(settings.tz)
    errors: list[str] = []
    try:
        config = read_config(settings.config_path)
    except yaml.YAMLError as e:
        config = {"containers": {}}
        errors.append(f"{settings.config_path}: {e}")
    config_entries = config.get("containers") or {}

    rows = []
    seen = set()
    for c in client.containers.list(all=True):
        if own_id and c.id.startswith(own_id):
            continue
        seen.add(c.name)
        labels = c.labels or {}
        rules = rules_from_labels(c.name, labels, errors)
        entry = config_entries.get(c.name)
        if isinstance(entry, dict):
            rules += rules_from_config({"containers": {c.name: entry}}, errors)
        health = (c.attrs.get("State", {}).get("Health") or {}).get("Status")
        rows.append(_row(c.name, c.attrs.get("Config", {}).get("Image", ""), c.status,
                         health, _labels_entry(labels), entry, rules, c, settings,
                         now, plan_wait))

    for name, entry in config_entries.items():
        if name in seen or not isinstance(entry, dict):
            continue
        rules = rules_from_config({"containers": {name: entry}}, errors)
        rows.append(_row(name, "", "missing", None, {}, entry, rules, None,
                         settings, now, plan_wait))

    rows.sort(key=lambda r: (not r["scheduled"], r["order"], r["name"]))
    return {
        "timezone": str(settings.tz),
        "now": now.strftime("%a %d %b %Y, %H:%M"),
        "dry_run": settings.dry_run,
        "config_path": str(settings.config_path),
        "errors": sorted(set(errors)),
        "containers": rows,
    }


def _row(name, image, status, health, label_entry, config_entry, rules,
         container, settings, now, plan_wait) -> dict:
    nxt = None
    for r in rules:
        at = r.next_after(now)
        if at and (nxt is None or at < nxt[0]):
            nxt = (at, r.action)
    order = min((r.order for r in rules), default=100)
    wait = None
    starts = [r for r in rules if r.action != "stop"]
    if starts:
        kind, secs, why = plan_wait(starts[0], container, settings)
        wait = (f"until healthy (max {secs}s)" if kind == "health" else f"{secs}s") + f", {why}"
    return {
        "name": name,
        "image": image,
        "status": status,
        "health": health,
        "scheduled": bool(rules),
        "labels": label_entry or None,
        "config": config_entry or None,
        "schedule": sorted(({"action": r.action, "when": r.describe(),
                             "source": r.source} for r in rules),
                           key=lambda x: (x["when"][:5], x["action"])),
        "order": order,
        "wait": wait,
        "next": f"{nxt[1]} {nxt[0]:%a %H:%M}" if nxt else None,
    }


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "Controlarr"
    app = None  # set by start_web

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _authorised(self) -> bool:
        password = self.app["settings"].web_password
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(given, password):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Controlarr"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _body(self) -> dict | None:
        # Only accept JSON with our own header, so other websites can't post
        # forms to Controlarr from your browser.
        if self.headers.get("X-Controlarr") != "1":
            self._json(403, {"error": "missing X-Controlarr header"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            self._json(400, {"error": "body must be a JSON object"})
            return None

    # --- routes
    def do_GET(self):
        if not self._authorised():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            a = self.app
            try:
                self._json(200, build_state(a["client"], a["settings"], a["own_id"]))
            except Exception as e:  # Docker unreachable, etc.
                log.exception("could not build state")
                self._json(500, {"error": str(e)})
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return
        data = self._body()
        if data is None:
            return
        if self.path == "/api/schedule":
            self._save_schedule(data)
        elif self.path == "/api/action":
            self._action(data)
        else:
            self._json(404, {"error": "not found"})

    def _save_schedule(self, data: dict) -> None:
        name = str(data.get("container") or "").strip()
        if not name:
            self._json(400, {"error": "container is required"})
            return
        settings = self.app["settings"]
        entry = clean_entry(data)
        problems = validate_entry(name, entry)
        if problems:
            self._json(400, {"error": "; ".join(p.split(": ", 1)[1] for p in problems)})
            return
        with _config_lock:
            try:
                config = read_config(settings.config_path)
            except yaml.YAMLError as e:
                self._json(500, {"error": f"schedules file can't be read: {e}"})
                return
            if entry and any(k in entry for k in ACTIONS):
                config["containers"][name] = entry
                msg = f"Saved schedule for {name}"
            else:
                config["containers"].pop(name, None)
                msg = f"Removed schedule for {name}"
            write_config(settings.config_path, config)
        log.info("%s (from web page)", msg)
        self._json(200, {"ok": True, "message": msg})

    def _action(self, data: dict) -> None:
        from .main import run_action
        from .schedule import Rule, ALL_DAYS
        from datetime import time

        name = str(data.get("container") or "")
        action = data.get("action")
        if action not in ACTIONS:
            self._json(400, {"error": "action must be stop, start or restart"})
            return
        a = self.app
        try:
            a["client"].containers.get(name)
        except NotFound:
            self._json(404, {"error": f"no container called {name}"})
            return
        rule = Rule(name, action, time(0, 0), ALL_DAYS, "web page")
        log.info("%s %s now (from web page)", action, name)
        # Run in the background: stopping can take up to the stop timeout.
        threading.Thread(target=run_action, args=(a["client"], rule, a["settings"]),
                         daemon=True).start()
        self._json(202, {"ok": True, "message": f"{action.capitalize()} sent to {name}"})


def start_web(client, settings, own_id) -> ThreadingHTTPServer | None:
    if settings.web_port <= 0:
        return None
    Handler.app = {"client": client, "settings": settings, "own_id": own_id}
    server = ThreadingHTTPServer(("0.0.0.0", settings.web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("web page on port %s%s", settings.web_port,
             "" if settings.web_password else " (no password set)")
    return server
