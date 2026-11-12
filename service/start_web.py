"""Web deployment: the API, the public page and the operator console in one
process. Paper-trading agents are spawned by the console.

Do not run this next to the Telegram master bot (service/start.py) unless
WEB_MANAGE_AGENTS=0 is set: each has its own agent pool and two pools would
start two agents per bot.

    WEB_OPERATOR_PASSWORD=...  TG_VAULT_MASTER_KEY=...  python service/start_web.py
"""

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent / "server"


def main() -> int:
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("WEB_HOST", "0.0.0.0")
    # The agents call back into this API for prices.
    os.environ.setdefault("AI_TRADER_URL", f"http://127.0.0.1:{port}")
    # The web console runs paper bots only; never inherit a live-trading switch.
    os.environ["LIVE_AGENT_EXECUTION"] = "0"
    os.environ.setdefault("API_STDERR_LOG", "true")
    for name in ("WEB_OPERATOR_PASSWORD", "TG_VAULT_MASTER_KEY"):
        if not os.getenv(name):
            sys.stderr.write(f"[start_web] {name} is not set; the console will ask for it.\n")
    os.chdir(SERVER_DIR)
    sys.path.insert(0, str(SERVER_DIR))
    import uvicorn
    uvicorn.run("main:app", host=host, port=port, proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
