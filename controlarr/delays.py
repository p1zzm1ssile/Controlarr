"""Suggested wait after starting a container, before starting the next one.

The wait belongs to the container that is loading: a heavy app like Plex gets
longer to settle than a light one like Prowlarr. Order of preference:

1. ``controlarr.delay`` set on the container (seconds) - always wins.
2. The container has a Docker healthcheck - wait until it reports healthy
   (capped by CONTROLARR_HEALTH_TIMEOUT), so the wait matches the real load.
3. A suggestion from the table below, matched on the image name.
4. CONTROLARR_START_DELAY for anything not recognised.
"""
from __future__ import annotations

# (substring of the image name, seconds, why)
KNOWN_APPS: list[tuple[str, int, str]] = [
    # Databases first: other apps usually need them, and they replay logs on start.
    ("postgres", 45, "database, replays its log on start"),
    ("mariadb", 45, "database, replays its log on start"),
    ("mysql", 45, "database, replays its log on start"),
    ("mongo", 45, "database, replays its log on start"),
    ("redis", 10, "small in-memory store"),
    # Media servers: scan libraries and build caches, heavy on disk and CPU.
    ("plex", 90, "media server, scans libraries on start"),
    ("jellyfin", 60, "media server, scans libraries on start"),
    ("emby", 60, "media server, scans libraries on start"),
    ("immich", 90, "photo server with machine learning, heavy on start"),
    ("photoprism", 60, "photo indexer, heavy on start"),
    ("nextcloud", 60, "large PHP app with a database"),
    ("homeassistant", 60, "loads many integrations on start"),
    ("home-assistant", 60, "loads many integrations on start"),
    # Download clients: check partial files on disk when they start.
    ("qbittorrent", 30, "download client, checks torrents on disk"),
    ("transmission", 30, "download client, checks torrents on disk"),
    ("deluge", 30, "download client, checks torrents on disk"),
    ("sabnzbd", 25, "download client"),
    ("nzbget", 20, "download client"),
    # *arr apps: .NET apps with a SQLite database, moderate start cost.
    ("sonarr", 20, "*arr app, opens its database and refreshes"),
    ("radarr", 20, "*arr app, opens its database and refreshes"),
    ("lidarr", 20, "*arr app, opens its database and refreshes"),
    ("readarr", 20, "*arr app, opens its database and refreshes"),
    ("whisparr", 20, "*arr app, opens its database and refreshes"),
    ("bazarr", 15, "subtitle app, light"),
    ("prowlarr", 15, "indexer manager, light"),
    ("jackett", 15, "indexer proxy, light"),
    ("overseerr", 15, "request app, light"),
    ("jellyseerr", 15, "request app, light"),
    ("tautulli", 15, "stats app, light"),
    ("gluetun", 20, "VPN, needs time to connect before apps behind it"),
]


def suggest_delay(image: str, default: int) -> tuple[int, str]:
    """Suggested seconds to wait after this image starts, and why."""
    name = (image or "").lower().rsplit("/", 1)[-1].split(":", 1)[0]
    for key, seconds, why in KNOWN_APPS:
        if key in name:
            return seconds, why
    return default, "not a known app, using the default"
