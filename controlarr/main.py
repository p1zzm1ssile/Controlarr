"""Controlarr: start, stop and restart Docker containers at set times of day."""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import docker
import yaml
from docker.errors import DockerException, NotFound
from requests.exceptions import RequestException

from .delays import suggest_delay
from .web import start_web
from .schedule import (ACTION_ORDER, Rule, desired_states, due_rules,
                       rules_from_config, rules_from_labels)

log = logging.getLogger("controlarr")

# Errors from talking to Docker: the daemon refusing a request, or the socket
# going away (for example while Docker restarts).
DOCKER_ERRORS = (DockerException, RequestException)


def _flag(env, name: str, default: str) -> bool:
    return env.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self, env=os.environ):
        self.config_path = Path(env.get("CONTROLARR_CONFIG", "/config/schedules.yml"))
        self.poll_seconds = int(env.get("CONTROLARR_POLL_SECONDS", "30"))
        self.dry_run = _flag(env, "CONTROLARR_DRY_RUN", "false")
        # On start (and after losing Docker for a while), put containers that
        # have both a start and a stop time into the state they should be in.
        self.catch_up = _flag(env, "CONTROLARR_CATCH_UP", "true")
        # Wait this long after Controlarr starts before catching up, so a
        # server that is still auto-starting its containers has finished.
        self.catch_up_delay = int(env.get("CONTROLARR_CATCH_UP_DELAY", "120"))
        self.stop_timeout = int(env.get("CONTROLARR_STOP_TIMEOUT", "30"))
        # Wait after starting a container that isn't a known app and has no
        # healthcheck or controlarr.delay label.
        self.start_delay = int(env.get("CONTROLARR_START_DELAY", "30"))
        # Longest to wait for a container's healthcheck to report healthy.
        self.health_timeout = int(env.get("CONTROLARR_HEALTH_TIMEOUT", "300"))
        self.tz = ZoneInfo(env.get("TZ", "UTC"))
        self.log_level = env.get("CONTROLARR_LOG_LEVEL", "INFO").upper()
        # Web page for viewing and editing schedules. 0 turns it off.
        self.web_port = int(env.get("CONTROLARR_WEB_PORT", "8080"))
        self.web_password = env.get("CONTROLARR_WEB_PASSWORD", "")


def own_container_id() -> str | None:
    # Inside Docker the hostname defaults to the short container id.
    return socket.gethostname() or None


def collect_rules(client, settings: Settings) -> tuple[list[Rule], list[str]]:
    errors: list[str] = []
    rules: list[Rule] = []
    me = own_container_id()
    # all=True so stopped containers (the ones we need to start) are included.
    for c in client.containers.list(all=True):
        if me and c.id.startswith(me):
            continue
        rules.extend(rules_from_labels(c.name, c.labels or {}, errors))
    if settings.config_path.exists():
        try:
            config = yaml.safe_load(settings.config_path.read_text()) or {}
            rules.extend(rules_from_config(config, errors))
        except yaml.YAMLError as e:
            errors.append(f"{settings.config_path}: {e}")
    return rules, errors


def has_healthcheck(container) -> bool:
    test = (container.attrs.get("Config", {}).get("Healthcheck") or {}).get("Test") or []
    return bool(test) and test[0] != "NONE"


def plan_wait(rule: Rule, container, settings: Settings) -> tuple[str, int, str]:
    """How to wait after this container starts: ("fixed"|"health", seconds, why)."""
    if rule.delay is not None:
        return "fixed", rule.delay, "set by controlarr.delay"
    if container is not None and has_healthcheck(container):
        return "health", settings.health_timeout, "waits for its healthcheck"
    image = container.attrs.get("Config", {}).get("Image", "") if container else ""
    seconds, why = suggest_delay(image, settings.start_delay)
    return "fixed", seconds, f"suggested: {why}"


def wait_after_start(rule: Rule, container, settings: Settings,
                     stop: threading.Event) -> None:
    kind, seconds, why = plan_wait(rule, container, settings)
    if seconds <= 0:
        return
    if kind == "fixed" or settings.dry_run:
        log.info("waiting %ss after %s (%s)", seconds, rule.container, why)
        stop.wait(seconds)
        return
    log.info("waiting for %s to be healthy (up to %ss)", rule.container, seconds)
    deadline = _time.monotonic() + seconds
    while not stop.is_set() and _time.monotonic() < deadline:
        try:
            container.reload()
        except DOCKER_ERRORS as e:
            log.warning("%s: could not check its health (%s), carrying on", rule.container, e)
            return
        health = (container.attrs.get("State", {}).get("Health") or {}).get("Status")
        if health == "healthy":
            log.info("%s is healthy", rule.container)
            return
        if container.status not in ("running", "restarting"):
            log.warning("%s is %s, not waiting for it", rule.container, container.status)
            return
        stop.wait(5)
    log.warning("%s not healthy after %ss, carrying on", rule.container, seconds)


def run_action(client, rule: Rule, settings: Settings):
    """Carry out one rule. Returns the container if it was started or restarted.

    Never raises for Docker problems: they are logged and the rule is skipped."""
    try:
        c = client.containers.get(rule.container)
    except NotFound:
        log.warning("%s: container not found, skipping %s", rule.container, rule.action)
        return None
    except DOCKER_ERRORS as e:
        log.error("%s: could not reach Docker to %s it: %s", rule.container, rule.action, e)
        return None
    status = c.status
    if rule.action == "stop" and status not in ("running", "restarting", "paused"):
        log.info("%s: already %s, nothing to stop", c.name, status)
        return None
    if rule.action == "start" and status == "running":
        log.info("%s: already running, nothing to start", c.name)
        return None
    prefix = "[dry run] would " if settings.dry_run else ""
    when = "now" if rule.source in ("web page", "catch-up") else rule.describe()
    log.info("%s%s %s (%s, from %s)", prefix, rule.action, c.name, when, rule.source)
    if settings.dry_run:
        return c if rule.action != "stop" else None
    try:
        if rule.action == "stop":
            c.stop(timeout=settings.stop_timeout)
        elif rule.action == "start":
            c.start()
        elif rule.action == "restart":
            c.restart(timeout=settings.stop_timeout)
    except DOCKER_ERRORS as e:
        log.error("%s: %s failed: %s", c.name, rule.action, e)
        return None
    return c if rule.action != "stop" else None


def run_batch(client, rules: list[Rule], settings: Settings, stop: threading.Event) -> None:
    """Run due rules in order. Stops go first; starts are staggered one by one."""
    pending_wait = None
    for rule in rules:
        if stop.is_set():
            return
        if rule.action != "stop" and pending_wait:
            wait_after_start(*pending_wait, settings, stop)
            pending_wait = None
            if stop.is_set():
                return
        started = run_action(client, rule, settings)
        if started is not None:
            pending_wait = (rule, started)


def catch_up(client, rules: list[Rule], settings: Settings,
             stop: threading.Event, now: datetime) -> None:
    """Put containers into the state their schedule says they should be in now.

    Covers times missed while Controlarr (or the server) was off, and Unraid
    starting every container when the array starts."""
    batch = []
    for name, (action, rule) in sorted(desired_states(rules, now).items()):
        try:
            status = client.containers.get(name).status
        except NotFound:
            continue
        except DOCKER_ERRORS as e:
            log.error("catch-up: could not check %s: %s", name, e)
            continue
        running = status in ("running", "restarting", "paused")
        if (action == "stop") == running:
            log.info("catch-up: %s should be %s since %s, so %s it", name,
                     "stopped" if action == "stop" else "running",
                     rule.describe()[:5], action)
            batch.append(Rule(name, action, rule.at, rule.days, "catch-up",
                              rule.order, rule.delay))
    batch.sort(key=lambda r: (r.action != "stop", r.order, r.container))
    run_batch(client, batch, settings, stop)


def print_plan(client, settings: Settings) -> None:
    """Show each container's schedule, start order and wait, for tuning."""
    rules, errors = collect_rules(client, settings)
    starts = sorted((r for r in rules if r.action != "stop"),
                    key=lambda r: (r.order, r.container, ACTION_ORDER[r.action]))
    stops = [r for r in rules if r.action == "stop"]
    print(f"Timezone {settings.tz}\n")
    if stops:
        print("Stops")
        for r in sorted(stops, key=lambda r: (r.at, r.container)):
            print(f"  {r.container:<24} {r.describe()}")
        print()
    if starts:
        print("Starts and restarts, in load order (lower order goes first)")
        print(f"  {'order':>5}  {'container':<24} {'action':<8} {'when':<26} wait after")
        for r in starts:
            try:
                c = client.containers.get(r.container)
            except NotFound:
                c = None
            kind, secs, why = plan_wait(r, c, settings)
            wait = f"until healthy (max {secs}s)" if kind == "health" else f"{secs}s"
            print(f"  {r.order:>5}  {r.container:<24} {r.action:<8} {r.describe():<26} {wait}, {why}")
        print("\nOnly containers due at the same time are staggered. Change the order "
              "with controlarr.order and the wait with controlarr.delay.")
    if errors:
        print("\nProblems")
        for e in errors:
            print(f"  {e}")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    settings = Settings()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = docker.from_env()
    if argv[:1] == ["plan"]:
        print_plan(client, settings)
        return

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    start_web(client, settings, own_container_id())

    last_errors: set[str] = set()
    last = datetime.now(settings.tz)
    try:
        rules, _ = collect_rules(client, settings)
    except Exception as e:  # Docker daemon unreachable etc.
        log.error("could not read schedules: %s", e)
        rules = []
    log.info("controlarr started (tz=%s, dry_run=%s, catch_up=%s), %d schedule(s) found",
             settings.tz, settings.dry_run, settings.catch_up, len(rules))
    for r in rules:
        log.info("  %s: %s at %s (%s)", r.container, r.action, r.describe(), r.source)
    catch_up_at = None
    if settings.catch_up:
        catch_up_at = _time.monotonic() + settings.catch_up_delay
        log.info("catching up missed times in %ss", settings.catch_up_delay)

    lost_docker = False
    while not stop.wait(settings.poll_seconds):
        now = datetime.now(settings.tz)
        try:
            rules, errors = collect_rules(client, settings)
        except Exception as e:  # Docker daemon unreachable etc.
            if not lost_docker:
                log.error("could not read schedules: %s", e)
            lost_docker = True
            continue
        for err in set(errors) - last_errors:
            log.warning(err)
        last_errors = set(errors)
        if lost_docker:
            log.info("Docker is back")
            lost_docker = False
            if settings.catch_up:
                # Rather than replaying every time missed while Docker was
                # away, put containers straight into the state they should be in.
                catch_up_at = _time.monotonic()
        try:
            if catch_up_at is not None and _time.monotonic() >= catch_up_at:
                catch_up_at = None
                catch_up(client, rules, settings, stop, now)
            # Anything that comes due while a staggered batch is running is
            # picked up on the next check, as the window starts at this check.
            run_batch(client, due_rules(rules, last, now), settings, stop)
        except Exception:  # never let one bad check stop the scheduler
            log.exception("something went wrong running schedules")
        last = now
    log.info("controlarr stopping")


if __name__ == "__main__":
    main()
