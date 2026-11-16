"""Start the Neko web app: the weekly calls, the desk, the forex planner, the
console and the public page, all in one process. No Telegram involved.

Only a password is required:

    NEKO_PASSWORD='a long password' python service/start_web.py

Settings (all optional except the password):
  NEKO_PASSWORD     password for signing in (WEB_OPERATOR_PASSWORD also works)
  NEKO_DATA_DIR     where the app keeps its database and secret key
                    (default: service/data)
  NEKO_SECRET_KEY   key that encrypts stored secrets; created and saved in the
                    data directory on first start if not given
  PORT              port to listen on (default 8000)

Do not run this next to the old Telegram master bot (service/start.py)
unless WEB_MANAGE_AGENTS=0 is set: both would start trading agents.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE / "server"


def prepare_environment() -> list[str]:
    """Map the web app's settings onto the names the older modules read.

    Returns notes worth printing at start-up."""
    notes = []
    data_dir = Path(os.getenv("NEKO_DATA_DIR") or (HERE / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TG_REGISTRY_PATH", str(data_dir / "registry.db"))

    if os.getenv("NEKO_PASSWORD") and not os.getenv("WEB_OPERATOR_PASSWORD"):
        os.environ["WEB_OPERATOR_PASSWORD"] = os.environ["NEKO_PASSWORD"]
    if not os.getenv("WEB_OPERATOR_PASSWORD"):
        notes.append("NEKO_PASSWORD is not set, so nobody can sign in yet.")

    key = os.getenv("NEKO_SECRET_KEY") or os.getenv("TG_VAULT_MASTER_KEY")
    if not key:
        key_file = data_dir / "secret.key"
        if key_file.exists():
            key = key_file.read_text().strip()
        else:
            from cryptography.fernet import Fernet
            key = Fernet.generate_key().decode()
            key_file.touch(mode=0o600)
            key_file.write_text(key + "\n")
            notes.append(f"Created a secret key at {key_file}. Back it up with the database.")
    os.environ["TG_VAULT_MASTER_KEY"] = key
    return notes


def main() -> int:
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("WEB_HOST", "0.0.0.0")
    for note in prepare_environment():
        sys.stderr.write(f"[neko] {note}\n")
    # The agents call back into this API for prices.
    os.environ.setdefault("AI_TRADER_URL", f"http://127.0.0.1:{port}")
    # The web app runs paper bots only; never inherit a live-trading switch.
    os.environ["LIVE_AGENT_EXECUTION"] = "0"
    os.environ.setdefault("API_STDERR_LOG", "true")
    os.chdir(SERVER_DIR)
    sys.path.insert(0, str(SERVER_DIR))
    import uvicorn
    uvicorn.run("main:app", host=host, port=port, proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
