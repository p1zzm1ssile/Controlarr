from datetime import datetime
from zoneinfo import ZoneInfo

from controlarr.activity import Activity, PROBLEM
from controlarr.holds import Holds
from controlarr.schedule import due_rules, rules_from_labels

TZ = ZoneInfo("Europe/London")


def dt(day, hh, mm):
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)  # 2 Nov 2026 is a Monday


def sonarr():
    return rules_from_labels("sonarr", {"controlarr.stop": "02:00",
                                        "controlarr.start": "07:00"}, [])


def test_pause_holds_everything(tmp_path):
    h = Holds(tmp_path / "state.json")
    h.set_paused("sonarr", True)
    due = due_rules(sonarr(), dt(3, 1, 59), dt(3, 2, 0))
    run, held = h.apply(due, dt(3, 2, 0))
    assert run == [] and [why for _, why in held] == ["schedule paused"]
    h.set_paused("sonarr", False)
    run, _ = h.apply(due, dt(3, 2, 0))
    assert len(run) == 1


def test_skip_next_only_once(tmp_path):
    h = Holds(tmp_path / "state.json")
    h.set_skip("sonarr", dt(3, 2, 0))
    stop = due_rules(sonarr(), dt(3, 1, 59), dt(3, 2, 0))
    run, held = h.apply(stop, dt(3, 2, 0))
    assert run == [] and held[0][1] == "skipped once"
    # Still held later (so a catch-up at 03:00 doesn't stop it either)...
    assert h.apply(stop, dt(3, 3, 0))[0] == []
    # ...until the next time comes round, which runs and clears the skip.
    start = due_rules(sonarr(), dt(3, 6, 59), dt(3, 7, 0))
    run, held = h.apply(start, dt(3, 7, 0))
    assert len(run) == 1 and not held
    assert h.snapshot()["skip"] == {}


def test_bad_state_file_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("not json")
    assert Holds(p).snapshot() == {"paused": [], "skip": {}}


def test_stops_go_in_reverse_order():
    rules = []
    for name, order in [("gluetun", 1), ("qbittorrent", 10), ("sonarr", 20)]:
        rules += rules_from_labels(name, {"controlarr.stop": "02:00",
                                          "controlarr.start": "07:00",
                                          "controlarr.order": str(order)}, [])
    stops = due_rules(rules, dt(3, 1, 59), dt(3, 2, 0))
    starts = due_rules(rules, dt(3, 6, 59), dt(3, 7, 0))
    assert [r.container for r in stops] == ["sonarr", "qbittorrent", "gluetun"]
    assert [r.container for r in starts] == ["gluetun", "qbittorrent", "sonarr"]


def test_activity_alerts(monkeypatch):
    sent = []
    a = Activity()
    a.notify_url = "https://ntfy.example/topic"
    monkeypatch.setattr(a, "_send", sent.append)
    monkeypatch.setattr("threading.Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    a.record("sonarr", "stopped")
    a.record("plex", "start failed: boom", PROBLEM)
    assert [i["container"] for i in a.recent()] == ["plex", "sonarr"]
    assert [i["container"] for i in sent] == ["plex"]   # only problems by default
