import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from docker.errors import APIError, NotFound
from requests.exceptions import ConnectionError

from controlarr.main import Settings, catch_up, run_action
from controlarr.schedule import ALL_DAYS, Rule, rules_from_labels

TZ = ZoneInfo("Europe/London")


class FakeContainer:
    def __init__(self, name, status, fail=None):
        self.name, self.status, self.fail = name, status, fail
        self.attrs = {"Config": {"Image": "example/thing"}, "State": {}}
        self.calls = []

    def _do(self, what, new_status):
        if self.fail:
            raise self.fail
        self.calls.append(what)
        self.status = new_status

    def start(self):
        self._do("start", "running")

    def stop(self, timeout):
        self._do("stop", "exited")

    def restart(self, timeout):
        self._do("restart", "running")


class FakeClient:
    def __init__(self, *containers, get_error=None):
        self.by_name = {c.name: c for c in containers}
        self.get_error = get_error
        self.containers = self

    def get(self, name):
        if self.get_error:
            raise self.get_error
        if name not in self.by_name:
            raise NotFound(name)
        return self.by_name[name]


def settings(**env):
    base = {"CONTROLARR_START_DELAY": "0", "TZ": "Europe/London"}
    base.update(env)
    return Settings(base)


def rule(name, action):
    return Rule(name, action, datetime.min.time(), ALL_DAYS, "label")


def test_docker_errors_do_not_raise():
    s = settings()
    c = FakeContainer("a", "running", fail=APIError("boom"))
    assert run_action(FakeClient(c), rule("a", "stop"), s) is None
    lost = FakeClient(get_error=ConnectionError("socket gone"))
    assert run_action(lost, rule("a", "start"), s) is None


def test_catch_up_fixes_state():
    s = settings()
    rules = rules_from_labels("sonarr", {"controlarr.stop": "02:00",
                                         "controlarr.start": "07:00"}, [])
    rules += rules_from_labels("radarr", {"controlarr.stop": "02:00",
                                          "controlarr.start": "07:00"}, [])
    rules += rules_from_labels("plex", {"controlarr.stop": "02:00"}, [])
    sonarr = FakeContainer("sonarr", "running")   # should be stopped at 03:00
    radarr = FakeContainer("radarr", "exited")    # already right
    plex = FakeContainer("plex", "running")       # only a stop time: left alone
    client = FakeClient(sonarr, radarr, plex)
    catch_up(client, rules, s, threading.Event(), datetime(2026, 11, 3, 3, 0, tzinfo=TZ))
    assert sonarr.calls == ["stop"] and radarr.calls == [] and plex.calls == []

    catch_up(client, rules, s, threading.Event(), datetime(2026, 11, 3, 8, 0, tzinfo=TZ))
    assert sonarr.calls == ["stop", "start"] and radarr.calls == ["start"]


def test_catch_up_setting():
    assert settings().catch_up is True
    assert settings(CONTROLARR_CATCH_UP="false").catch_up is False
