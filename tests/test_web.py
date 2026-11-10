import urllib.error
import urllib.request

from controlarr.main import Settings
from controlarr.web import start_web


class EmptyClient:
    class containers:
        @staticmethod
        def list(all=True):
            return []


def test_healthz_needs_no_password(tmp_path):
    s = Settings({"CONTROLARR_WEB_PORT": "18484", "CONTROLARR_WEB_PASSWORD": "secret",
                  "CONTROLARR_CONFIG": str(tmp_path / "schedules.yml")})
    server = start_web(EmptyClient(), s, None)
    try:
        base = "http://127.0.0.1:18484"
        assert urllib.request.urlopen(base + "/healthz?x=1").read() == b"ok"
        try:
            urllib.request.urlopen(base + "/api/state")
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        server.shutdown()
        server.server_close()


class OneContainer:
    class C:
        name, status, id = "sonarr", "running", "abc123"
        labels = {"controlarr.stop": "02:00", "controlarr.start": "07:00"}
        attrs = {"Config": {"Image": "lscr.io/linuxserver/sonarr"}, "State": {}}

    class containers:
        @staticmethod
        def list(all=True):
            return [OneContainer.C]

        @staticmethod
        def get(name):
            from docker.errors import NotFound
            if name != "sonarr":
                raise NotFound(name)
            return OneContainer.C


def test_pause_and_skip_from_page(tmp_path):
    from controlarr.web import _hold_impl, build_state
    s = Settings({"CONTROLARR_CONFIG": str(tmp_path / "schedules.yml")})
    app = {"client": OneContainer, "settings": s, "own_id": None}

    code, body = _hold_impl(app, {"container": "sonarr", "hold": "skip"})
    assert code == 200 and "Skipping sonarr's" in body["message"]
    row = build_state(OneContainer, s, None)["containers"][0]
    assert row["skipping"] and not row["paused"]

    _hold_impl(app, {"container": "sonarr", "hold": "pause"})
    row = build_state(OneContainer, s, None)["containers"][0]
    assert row["paused"] and row["next"] is None

    assert _hold_impl(app, {"container": "nope", "hold": "skip"})[0] == 400
    assert _hold_impl(app, {"container": "sonarr", "hold": "melt"})[0] == 400
