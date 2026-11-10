"""Schedule rules: where they come from and when they are due.

Schedules use plain 24-hour times, for example:

    controlarr.stop=02:00
    controlarr.start=07:30
    controlarr.days=mon-fri                 # optional, default every day
    controlarr.stop=mon-fri 23:00, sat-sun 01:00   # per-time days

Several times for one action are separated by commas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Mapping

ACTIONS = ("stop", "start", "restart")
# Order actions run in when several are due for one container in the same tick.
ACTION_ORDER = {"stop": 0, "restart": 1, "start": 2}
LABEL_PREFIX = "controlarr."

DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
ALL_DAYS = frozenset(range(7))
DAY_GROUPS = {
    "daily": ALL_DAYS, "everyday": ALL_DAYS, "every day": ALL_DAYS, "all": ALL_DAYS,
    "weekdays": frozenset(range(5)), "weekends": frozenset({5, 6}),
    "weekend": frozenset({5, 6}),
}
_TIME_RE = re.compile(r"^(?:(?P<days>.*?)\s+)?(?P<h>\d{1,2}):(?P<m>\d{2})$")


class ScheduleError(ValueError):
    pass


def parse_days(text: str) -> frozenset[int]:
    """Parse "mon-fri", "sat,sun", "weekdays", "daily" (case-insensitive)."""
    text = text.strip().lower()
    if not text:
        return ALL_DAYS
    if text in DAY_GROUPS:
        return DAY_GROUPS[text]
    days: set[int] = set()
    for part in re.split(r"[,/]", text):
        part = part.strip()
        if not part:
            continue
        if part in DAY_GROUPS:
            days |= DAY_GROUPS[part]
        elif "-" in part:
            a, b = (p.strip() for p in part.split("-", 1))
            if a not in DAY_NAMES or b not in DAY_NAMES:
                raise ScheduleError(f"unknown day range {part!r}")
            i, j = DAY_NAMES[a], DAY_NAMES[b]
            # Ranges may wrap around the week, e.g. fri-mon.
            while True:
                days.add(i)
                if i == j:
                    break
                i = (i + 1) % 7
        elif part in DAY_NAMES:
            days.add(DAY_NAMES[part])
        else:
            raise ScheduleError(f"unknown day {part!r}")
    return frozenset(days)


def parse_times(text: str, default_days: frozenset[int]) -> list[tuple[time, frozenset[int]]]:
    """Parse "02:00", "02:00, 14:30" or "mon-fri 23:00, sat-sun 01:00"."""
    # Split on commas, but a fragment with no digits ("sat" in "sat,sun 01:00")
    # is part of a day list and is joined to the entry that follows it.
    merged: list[str] = []
    pending: list[str] = []
    for entry in (e.strip() for e in str(text).split(",")):
        if not entry:
            continue
        if re.search(r"\d", entry):
            merged.append(",".join(pending + [entry]))
            pending = []
        else:
            pending.append(entry)
    if pending:
        raise ScheduleError(f"days {','.join(pending)!r} have no time after them")

    result = []
    for entry in merged:
        m = _TIME_RE.match(entry)
        if not m:
            raise ScheduleError(f"{entry!r} is not a 24-hour time like 07:30")
        h, mi = int(m["h"]), int(m["m"])
        if h > 23 or mi > 59:
            raise ScheduleError(f"{entry!r} is not a valid 24-hour time (00:00 to 23:59)")
        days = parse_days(m["days"]) if m["days"] else default_days
        result.append((time(h, mi), days))
    return result


@dataclass(frozen=True)
class Rule:
    container: str
    action: str
    at: time
    days: frozenset[int]
    source: str  # "label" or "config"
    order: int = 100  # lower starts first when several start together
    delay: int | None = None  # seconds to wait after this one starts

    def describe(self) -> str:
        if self.days == ALL_DAYS:
            d = "every day"
        elif self.days == DAY_GROUPS["weekdays"]:
            d = "weekdays"
        elif self.days == DAY_GROUPS["weekends"]:
            d = "weekends"
        else:
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            d = ",".join(names[i] for i in sorted(self.days))
        return f"{self.at:%H:%M} {d}"

    # Times are compared in UTC. Comparing local times directly goes wrong
    # when the clocks change: when they go back, 01:30 happens twice and the
    # rule would fire twice. A time skipped when the clocks go forward (01:30
    # in the UK) fires an hour later instead of being missed.

    def _fires_on(self, day, tz) -> datetime:
        return datetime.combine(day, self.at, tzinfo=tz)

    def next_after(self, now: datetime) -> datetime | None:
        """The next time this rule fires after now."""
        day = now.date()
        for _ in range(8):
            if day.weekday() in self.days:
                at = self._fires_on(day, now.tzinfo)
                if _utc(at) > _utc(now):
                    return at
            day += timedelta(days=1)
        return None

    def last_at_or_before(self, now: datetime) -> datetime | None:
        """The most recent time this rule fired, up to and including now."""
        day = now.date()
        for _ in range(8):
            if day.weekday() in self.days:
                at = self._fires_on(day, now.tzinfo)
                if _utc(at) <= _utc(now):
                    return at
            day -= timedelta(days=1)
        return None

    def is_due(self, since: datetime, now: datetime) -> bool:
        """True if this time falls in the window (since, now]."""
        start, end = _utc(since), _utc(now)
        day = since.date() - timedelta(days=1)
        while day <= now.date():
            if day.weekday() in self.days:
                at = _utc(self._fires_on(day, now.tzinfo))
                if start < at <= end:
                    return True
            day += timedelta(days=1)
        return False


def _utc(d: datetime) -> datetime:
    return d.astimezone(timezone.utc)


def _make_rules(container: str, mapping: Mapping[str, object], source: str,
                errors: list[str]) -> list[Rule]:
    try:
        default_days = parse_days(str(mapping.get("days") or ""))
    except ScheduleError as e:
        errors.append(f"{container}: days: {e}")
        return []
    order = 100
    try:
        if mapping.get("order") not in (None, ""):
            order = int(str(mapping["order"]).strip())
    except (TypeError, ValueError):
        errors.append(f"{container}: order: {mapping.get('order')!r} is not a whole number")
        order = 100
    delay = None
    if mapping.get("delay") not in (None, ""):
        try:
            delay = int(str(mapping["delay"]).strip().rstrip("s"))
            if delay < 0:
                raise ValueError
        except ValueError:
            errors.append(f"{container}: delay: {mapping['delay']!r} must be seconds, like 60")
    rules = []
    for action in ACTIONS:
        if action not in mapping or mapping[action] in (None, ""):
            continue
        value = mapping[action]
        if isinstance(value, int) and not isinstance(value, bool):
            # YAML reads an unquoted 23:30 as the number 1410 (minutes).
            value = f"{value // 60:02d}:{value % 60:02d}"
        try:
            for at, days in parse_times(str(value), default_days):
                rules.append(Rule(container, action, at, days, source, order, delay))
        except ScheduleError as e:
            errors.append(f"{container}: {action}: {e}")
    return rules


def validate_entry(container: str, mapping: Mapping[str, object]) -> list[str]:
    """Check one container's settings; returns a list of problems."""
    errors: list[str] = []
    _make_rules(container, mapping, "config", errors)
    return errors


def rules_from_labels(container: str, labels: Mapping[str, str],
                      errors: list[str]) -> list[Rule]:
    if labels.get(LABEL_PREFIX + "enable", "true").lower() in ("false", "0", "no"):
        return []
    mapping = {k[len(LABEL_PREFIX):]: v for k, v in labels.items()
               if k.startswith(LABEL_PREFIX)}
    return _make_rules(container, mapping, "label", errors)


def rules_from_config(config: Mapping | None, errors: list[str]) -> list[Rule]:
    rules: list[Rule] = []
    for name, mapping in ((config or {}).get("containers") or {}).items():
        if not isinstance(mapping, Mapping):
            errors.append(f"{name}: config entry must be a mapping")
            continue
        rules.extend(_make_rules(str(name), mapping, "config", errors))
    return rules


def due_rules(rules: Iterable[Rule], since: datetime, now: datetime) -> list[Rule]:
    due = [r for r in rules if r.is_due(since, now)]
    # De-duplicate (same container+action from label and config) and order:
    # all stops first, highest "order" first (the reverse of starting, so apps
    # stop before the VPN or database they rely on), then restarts and starts
    # lowest "order" first.
    def key(r):
        is_stop = r.action == "stop"
        return (not is_stop, -r.order if is_stop else r.order, r.container,
                ACTION_ORDER[r.action])

    seen, out = set(), []
    for r in sorted(due, key=key):
        key = (r.container, r.action)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def desired_states(rules: Iterable[Rule], now: datetime) -> dict[str, tuple[str, Rule]]:
    """For containers with both a start and a stop time, whether each should be
    running right now: {container: ("start" or "stop", the rule that decided)}.

    Containers with only a stop time (or only a start time) are left out, so a
    container you start by hand after its stop time isn't stopped again."""
    latest: dict[str, tuple[datetime, Rule]] = {}
    actions: dict[str, set[str]] = {}
    for r in rules:
        if r.action not in ("start", "stop"):
            continue
        actions.setdefault(r.container, set()).add(r.action)
        at = r.last_at_or_before(now)
        if at is None:
            continue
        best = latest.get(r.container)
        # On a tie (start and stop at the same minute) start wins: stops run
        # first and the start follows, so the container ends up running.
        if (best is None or _utc(at) > _utc(best[0])
                or (_utc(at) == _utc(best[0]) and r.action == "start")):
            latest[r.container] = (at, r)
    return {name: (rule.action, rule) for name, (_, rule) in latest.items()
            if actions[name] == {"start", "stop"}}
