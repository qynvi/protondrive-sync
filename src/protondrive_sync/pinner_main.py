"""Standalone entry point for the cache pinner daemon.

Used by systemd service / Windows scheduled task to run the pinner
independently of the TUI.
"""

from __future__ import annotations

import signal
import sys

from .core.config import load_config, ConfigError
from .core.pinner import Pinner


def main() -> None:
    config = load_config()

    def log(msg: str) -> None:
        print(f"[pinner] {msg}", flush=True)

    pinner = Pinner(config, on_status=log)

    # Handle graceful shutdown
    def shutdown(signum: int, frame: object) -> None:
        log("Shutting down ...")
        pinner.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log("Starting cache pinner daemon")
    pinner.start()

    # Block main thread until stopped
    try:
        signal.pause()  # Unix only
    except AttributeError:
        # Windows fallback — signal.pause() not available
        import time
        while pinner.running:
            time.sleep(1)


if __name__ == "__main__":
    main()
