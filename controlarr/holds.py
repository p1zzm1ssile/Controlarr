"""Pause a container's schedule, or skip just its next scheduled time.

Kept in state.json next to the schedules file, so it survives restarts:

    {"paused": ["sonarr"], "skip": {"radarr": "2026-11-11T02:00:00+00:00"}}

A skip holds every scheduled time up to and including the one it names. It
is cleared when the container's next scheduled time after that comes round.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("controlarr.holds")
_lock = threading.Lock()


class Holds:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"paused": [], "skip": {}}
        except (OSError, ValueError) as e:
            log.warning("%s can't be read, ignoring it: %s", self.path, e)
            return {"paused": [], "skip": {}}
        paused = data.get("paused") if isinstance(data.get("paused"), list) else []
        skip = data.get("skip") if isinstance(data.get("skip"), dict) else {}
        return {"paused": [str(p) for p in paused], "skip": {str(k): str(v) for k, v in skip.items()}}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with _lock:
            return self._read()

    def set_paused(self, name: str, paused: bool) -> None:
        with _lock:
            data = self._read()
            names = set(data["paused"])
            names.add(name) if paused else names.discard(name)
            data["paused"] = sorted(names)
            self._write(data)

    def set_skip(self, name: str, until: datetime | None) -> None:
        with _lock:
            data = self._read()
            if until is None:
                data["skip"].pop(name, None)
            else:
                data["skip"][name] = until.astimezone(timezone.utc).isoformat()
            self._write(data)

    def apply(self, rules: list, now: datetime) -> tuple[list, list[tuple[object, str]]]:
        """Split due rules into (to run, [(held rule, why)]), and clear skips
        that are used up because a later scheduled time has come round."""
        snap = self.snapshot()
        run, held_rules = [], []
        for r in rules:
            why = held(snap, r, now)
            if why:
                held_rules.append((r, why))
            else:
                run.append(r)
                if r.container in snap["skip"]:
                    self.set_skip(r.container, None)
                    snap["skip"].pop(r.container, None)
        return run, held_rules


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def held(snapshot: dict, rule, now: datetime) -> str | None:
    """Why this scheduled rule should not run now, or None if it should."""
    if rule.container in snapshot["paused"]:
        return "schedule paused"
    until = _parse(snapshot["skip"].get(rule.container, ""))
    if until is not None:
        fired = rule.last_at_or_before(now)
        if fired is not None and fired <= until:
            return "skipped once"
    return None
