"""Neko-Chan full-deployment supervisor (Telegram-first).

Runs the pieces the product actually needs in one process tree:
  1. Internal API server (uvicorn :8000) — required by live_agent to fetch
     prices, self-register, and read positions. Bound to 127.0.0.1 only.
  2. Master Telegram bot (service/tg_bot/main.py) — the product.

This container exposes NO public web port on purpose; it is NOT a web app.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(HERE, "server")


def _run_api():
    return subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=SERVER_DIR,
        env={**os.environ, "API_STDERR_LOG": "true"},
    )


def _run_bot():
    return subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.path.join(HERE, "tg_bot"),
        env=dict(os.environ),
    )


def main() -> int:
    procs = {
        "api": _run_api(),
        "bot": _run_bot(),
    }
    for name, proc in procs.items():
        sys.stdout.write(f"[start] {name} pid={proc.pid}\n")
        sys.stdout.flush()

    try:
        while True:
            for name, proc in list(procs.items()):
                code = proc.poll()
                if code is not None:
                    sys.stderr.write(f"[start] {name} exited rc={code}; restarting\n")
                    procs[name] = _run_api() if name == "api" else _run_bot()
            time.sleep(5)
    except KeyboardInterrupt:
        for proc in procs.values():
            proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
