from controlarr.delays import suggest_delay


def test_known_apps():
    assert suggest_delay("lscr.io/linuxserver/plex:latest", 30)[0] == 90
    assert suggest_delay("lscr.io/linuxserver/prowlarr", 30)[0] == 15
    assert suggest_delay("postgres:16", 30)[0] == 45


def test_unknown_uses_default():
    assert suggest_delay("ghcr.io/me/thing:1", 42) == (42, "not a known app, using the default")
