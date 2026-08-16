"""Keep the Telegram bot process alive after unexpected exits.

The Telegram library handles update-level failures itself, but a process can
still exit because of a fatal exception, native media encoder failure, or
runtime issue. This small supervisor restarts it with exponential backoff.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

LOGGER = logging.getLogger("telegram-bot-supervisor")
MAX_RESTART_DELAY_SECONDS = 60

stop_requested = False
child: subprocess.Popen[bytes] | None = None


def request_stop(signum: int, _frame) -> None:
    global stop_requested
    stop_requested = True
    LOGGER.info("Received signal %s; stopping bot supervisor", signum)
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def run() -> None:
    global child

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    restart_delay = 1
    while not stop_requested:
        LOGGER.info("Starting Telegram bot process")
        try:
            child = subprocess.Popen(
                [sys.executable, "main.py"],
                start_new_session=True,
            )
            return_code = child.wait()
        except Exception:
            LOGGER.exception("Could not start or monitor Telegram bot process")
            return_code = 1
        finally:
            child = None

        if stop_requested:
            break

        LOGGER.error(
            "Telegram bot exited with code %s; restarting in %s seconds",
            return_code,
            restart_delay,
        )
        time.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, MAX_RESTART_DELAY_SECONDS)

    LOGGER.info("Telegram bot supervisor stopped")


if __name__ == "__main__":
    run()