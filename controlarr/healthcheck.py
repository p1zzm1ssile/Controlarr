"""Docker healthcheck: exits 0 if the web page answers on /healthz."""
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("CONTROLARR_WEB_PORT", "8080").strip()
    if port in ("", "0"):
        return 0  # web page turned off, nothing to check
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
