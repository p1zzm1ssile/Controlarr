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
