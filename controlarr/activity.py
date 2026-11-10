"""Recent activity (shown on the web page) and optional alerts.

Alerts go to CONTROLARR_NOTIFY_URL when set:

- a Discord webhook (https://discord.com/api/webhooks/...) gets a message;
- anything else (for example ntfy: https://ntfy.sh/my-topic) gets a plain
  text POST with a Title header.

By default only problems are sent. CONTROLARR_NOTIFY_ALL=true sends every
stop, start and restart too.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections import deque
from datetime import datetime

log = logging.getLogger("controlarr.activity")

OK, SKIPPED, PROBLEM = "ok", "skipped", "problem"


class Activity:
    def __init__(self, size: int = 100):
        self._items: deque[dict] = deque(maxlen=size)
        self._lock = threading.Lock()
        self.notify_url = ""
        self.notify_all = False
        self.tz = None

    def configure(self, settings) -> None:
        self.notify_url = settings.notify_url
        self.notify_all = settings.notify_all
        self.tz = settings.tz

    def record(self, container: str, text: str, level: str = OK,
               source: str = "") -> None:
        item = {"time": datetime.now(self.tz).isoformat(timespec="seconds"),
                "container": container, "text": text, "level": level,
                "source": source}
        with self._lock:
            self._items.appendleft(item)
        if self.notify_url and (level == PROBLEM or (self.notify_all and level == OK)):
            threading.Thread(target=self._send, args=(item,), daemon=True).start()

    def recent(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return list(self._items)[:limit]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _send(self, item: dict) -> None:
        title = "Controlarr problem" if item["level"] == PROBLEM else "Controlarr"
        text = f"{item['container']}: {item['text']}"
        url = self.notify_url
        try:
            if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
                body = json.dumps({"content": f"**{title}**\n{text}"}).encode()
                headers = {"Content-Type": "application/json"}
            else:
                body = text.encode()
                headers = {"Title": title, "Content-Type": "text/plain; charset=utf-8"}
                if item["level"] == PROBLEM:
                    headers["Priority"] = "high"
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # never let an alert break scheduling
            log.warning("could not send alert: %s", e)


ACTIVITY = Activity()
