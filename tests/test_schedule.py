from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from controlarr.schedule import (ALL_DAYS, ScheduleError, due_rules, parse_days,
                                 parse_times, rules_from_config, rules_from_labels)

TZ = ZoneInfo("Europe/London")


def dt(day, hh, mm, ss=0):
    # 2026-11-02 is a Monday
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=TZ)


@pytest.mark.parametrize("text,expected", [
    ("", ALL_DAYS), ("daily", ALL_DAYS), ("weekdays", {0, 1, 2, 3, 4}),
    ("weekends", {5, 6}), ("mon-fri", {0, 1, 2, 3, 4}), ("Sat,Sun", {5, 6}),
    ("fri-mon", {4, 5, 6, 0}), ("wednesday", {2}),
])
def test_parse_days(text, expected):
    assert parse_days(text) == frozenset(expected)


def test_parse_times_plain_and_multiple():
    assert parse_times("02:00", ALL_DAYS) == [(time(2, 0), ALL_DAYS)]
    assert [t for t, _ in parse_times("7:05, 14:30", ALL_DAYS)] == [time(7, 5), time(14, 30)]


def test_parse_times_with_days():
    got = parse_times("mon-fri 23:00, sat,sun 01:00", ALL_DAYS)
    assert got == [(time(23, 0), frozenset(range(5))), (time(1, 0), frozenset({5, 6}))]


@pytest.mark.parametrize("bad", ["24:00", "7pm", "12:60", "0 2 * * *", "sat"])
def test_parse_times_rejects(bad):
    with pytest.raises(ScheduleError):
        parse_times(bad, ALL_DAYS)


def test_labels_build_rules_and_report_errors():
    errors = []
    rules = rules_from_labels("sonarr", {
        "controlarr.stop": "02:00", "controlarr.start": "7pm",
        "controlarr.days": "weekdays", "other.label": "x"}, errors)
    assert [(r.action, r.at) for r in rules] == [("stop", time(2, 0))]
    assert rules[0].days == frozenset(range(5))
    assert errors == ["sonarr: start: '7pm' is not a 24-hour time like 07:30"]


def test_disabled_label():
    assert rules_from_labels("x", {"controlarr.enable": "false",
                                   "controlarr.stop": "02:00"}, []) == []


def test_due_window():
    [r] = rules_from_labels("sonarr", {"controlarr.stop": "02:00"}, [])
    assert r.is_due(dt(3, 1, 59, 45), dt(3, 2, 0, 15))
    assert not r.is_due(dt(3, 2, 0, 15), dt(3, 2, 0, 45))   # no double fire
    assert not r.is_due(dt(3, 1, 0), dt(3, 1, 59))


def test_due_across_midnight():
    [r] = rules_from_labels("x", {"controlarr.start": "00:00"}, [])
    assert r.is_due(dt(3, 23, 59, 50), dt(4, 0, 0, 20))


def test_days_respected():
    [r] = rules_from_labels("x", {"controlarr.stop": "23:00",
                                  "controlarr.days": "sat-sun"}, [])
    assert not r.is_due(dt(3, 22, 59), dt(3, 23, 1))   # Tuesday
    assert r.is_due(dt(7, 22, 59), dt(7, 23, 1))       # Saturday


def test_due_rules_order_and_dedupe():
    errors = []
    rules = rules_from_labels("a", {"controlarr.start": "03:00",
                                    "controlarr.stop": "03:00"}, errors)
    rules += rules_from_config({"containers": {"a": {"stop": "03:00"}}}, errors)
    got = due_rules(rules, dt(3, 2, 59), dt(3, 3, 0))
    assert [(r.container, r.action) for r in got] == [("a", "stop"), ("a", "start")]
    assert not errors


def test_order_and_delay_labels():
    errors = []
    rules = rules_from_labels("plex", {"controlarr.start": "07:00",
                                       "controlarr.order": "5",
                                       "controlarr.delay": "120"}, errors)
    rules += rules_from_labels("sonarr", {"controlarr.start": "07:00",
                                          "controlarr.stop": "07:00"}, errors)
    rules += rules_from_labels("db", {"controlarr.start": "07:00",
                                      "controlarr.order": "1"}, errors)
    got = due_rules(rules, dt(3, 6, 59), dt(3, 7, 0))
    # stops first, then starts by order (default 100)
    assert [(r.container, r.action) for r in got] == [
        ("sonarr", "stop"), ("db", "start"), ("plex", "start"), ("sonarr", "start")]
    assert got[2].delay == 120 and got[1].delay is None
    assert not errors


def test_bad_order_and_delay_reported():
    errors = []
    rules_from_labels("x", {"controlarr.start": "07:00", "controlarr.order": "first",
                            "controlarr.delay": "soon"}, errors)
    assert len(errors) == 2
