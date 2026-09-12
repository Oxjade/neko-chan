"""Gates G1-G5: single-master-bot DB migration safety.

Every test operates on a TEMPORARY copy of a legacy-shaped registry.db. Nothing
here touches the real service/tg_bot/registry.db or production.
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry
import db_migrate

# The EXACT legacy shape captured from production (NOT NULL token columns,
# UNIQUE bot_token_hash, later-added trading_mode/priority_watch).
LEGACY_BOTS_DDL = """
CREATE TABLE bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    bot_name TEXT NOT NULL,
    bot_token_enc BLOB NOT NULL,
    bot_token_hash TEXT NOT NULL UNIQUE,
    bot_username TEXT NOT NULL,
    agent_name TEXT NOT NULL UNIQUE,
    agent_id INTEGER,
    platform_token TEXT NOT NULL,
    symbols TEXT NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    interval_sec INTEGER NOT NULL DEFAULT 120,
    risk_profile TEXT NOT NULL,
    risk_caps TEXT NOT NULL,
    is_running INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,
    pid INTEGER,
    last_heartbeat TEXT,
    last_error TEXT,
    scheduled_deletion_at TEXT,
    trader_type TEXT DEFAULT 'scalp',
    chain TEXT DEFAULT 'sui',
    network TEXT DEFAULT 'testnet',
    watchlist TEXT DEFAULT '',
    onboarding_complete INTEGER DEFAULT 0,
    wallet_addr TEXT DEFAULT '',
    created_at TEXT NOT NULL
, trading_mode TEXT DEFAULT 'paper', priority_watch TEXT DEFAULT '')
"""

# (id, tg_id, bot_name, agent_name, platform_token, wallet_addr)
LEGACY_ROWS = [
    (1, 6698272364, "Nekoadmin", "Nekoadmin_admin", "ptok-admin", "0xaaa1"),
    (3, 7488318868, "Whale", "Whale_8868", "ptok-whale", "0xccc3"),
    (4, 7488318868, "SMT", "SMT_8868", "ptok-smt", "0xddd4"),
]


def _make_legacy_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE users (tg_id INTEGER PRIMARY KEY, tg_username TEXT, "
        "status TEXT DEFAULT 'onboarding', accepted_disclaimer INTEGER DEFAULT 0, "
        "is_admin INTEGER DEFAULT 0, created_at TEXT NOT NULL);"
        "CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, "
        "provider TEXT, base_url TEXT, model TEXT, encrypted_key BLOB, key_hash TEXT UNIQUE, "
        "validated_at TEXT, last_used_at TEXT, revoked_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, "
        "kind TEXT, ref_id TEXT, payload TEXT, sent_at TEXT, UNIQUE(tg_id,kind,ref_id));")
    con.executescript(LEGACY_BOTS_DDL)
    con.executescript("CREATE UNIQUE INDEX bots_tok ON bots(bot_token_hash);")
    for (i, tg, name, ag, ptok, wa) in LEGACY_ROWS:
        con.execute(
            "INSERT INTO bots (id, tg_id, bot_name, bot_token_enc, bot_token_hash, "
            "bot_username, agent_name, platform_token, symbols, leverage, interval_sec, "
            "risk_profile, risk_caps, wallet_addr, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, tg, name, b"encblob", f"hash-{i}", f"user{i}", ag, ptok,
             '{"perps":1}', 2.0, 120, "balanced", "{}", wa, "2026-09-01T00:00:00+00:00"))
    # AUTOINCREMENT high-water marker (as SQLite would leave it after id=4).
    con.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES ('bots', 4)")
    con.commit()
    con.close()


def _fresh_db(path: str) -> Registry:
    return Registry(path, KeyVault())


def _info(path: str):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute("PRAGMA table_info(bots)").fetchall()
    info = [(r["name"], r["type"], int(r["notnull"] or 0), r["dflt_value"], int(r["pk"] or 0))
            for r in rows]
    uniq = {}
    for ix in con.execute("PRAGMA index_list(bots)").fetchall():
        if ix["unique"]:
            cols = [c["name"] for c in con.execute(f"PRAGMA index_info('{ix['name']}')").fetchall()]
            uniq[tuple(cols)] = True
    con.close()
    return info, uniq


# ---------------------------------------------------------------------------- G1
def test_schema_parity():
    """G1: a fresh-install bots table and a migrated legacy bots table have an
    IDENTICAL shape: token cols nullable, bot_token_hash still UNIQUE,
    agent_name/platform_token still NOT NULL."""
    d = tempfile.mkdtemp()
    fresh = os.path.join(d, "fresh.db")
    r = _fresh_db(fresh); r.close()

    legacy = os.path.join(d, "legacy.db")
    _make_legacy_db(legacy)
    r2 = _fresh_db(legacy); r2.close()  # migration runs on connect

    fi, funiq = _info(fresh)
    li, luniq = _info(legacy)

    # the two token columns became nullable in BOTH (fresh == migrated)
    for col in ("bot_token_enc", "bot_token_hash"):
        f_row = next(x for x in fi if x[0] == col)
        l_row = next(x for x in li if x[0] == col)
        assert f_row[2] == 0, f"{col} notnull should be 0 (fresh)"
        assert l_row[2] == 0, f"{col} notnull should be 0 (migrated)"
    # bot_username stays NOT NULL but gains a DEFAULT '' (never @None; create_bot
    # synthesizes a handle). Same in BOTH.
    fu = next(x for x in fi if x[0] == "bot_username")
    lu = next(x for x in li if x[0] == "bot_username")
    assert fu[2] == 1 and lu[2] == 1 and fu[3] == "''" and lu[3] == "''"
    # still-protected columns remain NOT NULL in BOTH
    for col in ("tg_id", "agent_name", "platform_token", "created_at"):
        assert next(x for x in fi if x[0] == col)[2] == 1
        assert next(x for x in li if x[0] == col)[2] == 1
    # full shape identical (fresh == migrated) - the anti-drift property
    assert fi == li, "fresh and migrated bots shapes differ"
    # bot_token_hash UNIQUE and agent_name UNIQUE preserved in BOTH
    for uniq in (funiq, luniq):
        assert ("agent_name",) in uniq
        assert ("bot_token_hash",) in uniq


# ---------------------------------------------------------------------------- G2
def test_migration_idempotent():
    """G2: migrating twice is a guarded no-op - second run reports noop, does
    not rebuild, does not error, and leaves no bots_new table behind."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "legacy.db")
    _make_legacy_db(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    first = db_migrate.migrate(con)
    assert first["action"] == "migrated"
    assert first["bots"] == 3

    second = db_migrate.migrate(con)
    assert second["action"] == "noop"
    assert second["to"] == db_migrate.TARGET_VERSION

    # no leftover staging table; row count stable
    names = [r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "bots_new" not in names
    assert con.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 3
    # re-opening via Registry after an already-migrated DB never re-migrates
    con.close()
    r = Registry(path, KeyVault())
    assert len(r.all_bots()) == 3
    r.close()


# ---------------------------------------------------------------------------- G3
def test_id_sequence_preserved():
    """G3: ids, agent_name and platform_token survive verbatim; sqlite_sequence
    is restored to MAX(id) so the next bot gets MAX+1 (no collision/restart)."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "legacy.db")
    _make_legacy_db(path)
    r = Registry(path, KeyVault())  # migrates on connect

    bots = {b["id"]: b for b in r.all_bots()}
    assert set(bots) == {1, 3, 4}, "ids were renumbered"
    for (i, tg, name, ag, ptok, wa) in LEGACY_ROWS:
        assert bots[i]["agent_name"] == ag
        assert bots[i]["platform_token"] == ptok
        assert bots[i]["bot_name"] == name

    con = sqlite3.connect(path)
    seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name='bots'").fetchone()[0]
    con.close()
    assert int(seq) == 4, "AUTOINCREMENT high-water not restored"

    # next created bot must be id 5, not restart at 1 (which would collide)
    nb = r.create_bot(999, "NewBot", None, None, "NewBotAgent", "ptok-new",
                      {"perps": 1}, 2.0, 120, "balanced")
    assert nb["id"] == 5, f"expected next id 5, got {nb['id']}"
    r.close()


# ---------------------------------------------------------------------------- G4
def test_tokenless_create():
    """G4: a bot with no Telegram token is created with no IntegrityError;
    bot_token() returns None and no duplicate-token path triggers."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "fresh.db")
    r = _fresh_db(path)
    b = r.create_bot(7000, "Solo", None, None, "SoloAgent", "ptok-solo",
                     {"perps": 1}, 2.0, 120, "balanced")
    assert r.bot_token(b["id"]) is None
    assert b["bot_username"] and b["bot_username"] != "None"
    # two token-less bots coexist (multiple NULLs allowed in UNIQUE hash)
    b2 = r.create_bot(7001, "Solo2", None, None, "Solo2Agent", "ptok-solo2",
                      {"perps": 1}, 2.0, 120, "balanced")
    assert r.bot_token(b2["id"]) is None
    assert b["id"] != b2["id"]
    # legacy duplicate-token guard still enforced when a token IS supplied
    r.create_bot(8000, "Tok", "123:abc", "tokbot", "TokAgent", "pt", {"perps": 1}, 2.0, 120, "balanced")
    with pytest.raises(ValueError):
        r.create_bot(8001, "TokDup", "123:abc", "tokbot2", "TokDupAgent", "pt", {"perps": 1}, 2.0, 120, "balanced")
    r.close()


# ---------------------------------------------------------------------------- G5
def test_ledger_linkage_intact():
    """G5: exec-ledger rows keyed on bot_id still resolve to the same bots after
    migration (no orphaned wallets / orders / paper positions)."""
    d = tempfile.mkdtemp()
    reg_path = os.path.join(d, "legacy.db")
    led_path = os.path.join(d, "exec_ledger.db")
    _make_legacy_db(reg_path)

    # a stand-in exec_ledger.db referencing bots.id exactly like the real schema
    led = sqlite3.connect(led_path)
    led.executescript(
        "CREATE TABLE exec_wallets (id INTEGER PRIMARY KEY, bot_id INTEGER, chain TEXT, address TEXT);"
        "CREATE TABLE exec_orders (id INTEGER PRIMARY KEY, bot_id INTEGER, symbol TEXT, qty REAL);")
    led.executemany("INSERT INTO exec_wallets (bot_id, chain, address) VALUES (?,?,?)",
                    [(1, "sui", "0xaaa1"), (3, "sui", "0xccc3"), (4, "sui", "0xddd4")])
    led.executemany("INSERT INTO exec_orders (bot_id, symbol, qty) VALUES (?,?,?)",
                    [(1, "BTC", 0.1), (3, "ETH", 2.0), (3, "SUI", 50.0), (4, "SOL", 3.0)])
    led.commit()

    r = Registry(reg_path, KeyVault())  # migrates here
    bots = {b["id"]: b for b in r.all_bots()}

    orphans = 0
    for (bot_id, addr) in led.execute("SELECT bot_id, address FROM exec_wallets"):
        assert bot_id in bots, f"wallet bot_id {bot_id} orphaned by migration"
        assert bots[bot_id]["wallet_addr"] == addr, "wallet_addr desynced"
    for (bot_id,) in led.execute("SELECT bot_id FROM exec_orders"):
        if bot_id not in bots:
            orphans += 1
    assert orphans == 0, "orders orphaned: bots.id changed during migration"
    led.close()
    r.close()


# ---------------------------------------------------------------------------- G6
def test_no_per_user_poller():
    """G6: a token-less bot is registered as a ROUTED application (send identity
    = master token) and NEVER spawns a per-token polling thread. The master is
    the sole poller, so the 409 / one-poller-per-token class of bug is gone."""
    import threading
    from userbot import UserBotController

    d = tempfile.mkdtemp()
    r = Registry(os.path.join(d, "fresh.db"), KeyVault())
    b1 = r.create_bot(700, "Tokless", None, None, "ToklessAgent", "pt1",
                      {"perps": 1}, 2.0, 120, "balanced")
    r.update_bot(b1["id"], is_running=1)

    ctrl = UserBotController(r, platform=object(), vault=KeyVault(),
                             agent_pool=None, gateway=None)
    ctrl._master_token = "111:master-fake-token"  # send identity for routed bots

    before = {t.name for t in threading.enumerate()}
    assert ctrl.start_bot(b1["id"]) is True      # routed, not False
    after = {t.name for t in threading.enumerate()}

    assert b1["id"] in ctrl._apps, "routed application was not registered"
    assert ctrl._route_managed.get(b1["id"]) is True
    assert not any(n.startswith("userbot-") for n in (after - before)), \
        "a per-user poller thread was spawned for a token-less bot"
    # no per-bot token to poll with: bot_token() is None by design
    assert r.bot_token(b1["id"]) is None
    r.close()


def test_routes_to_owner():
    """G6 (owner isolation): route() must forward an update only to the caller's
    OWN bot. Single-bot user auto-routes; a multi-bot user routes only to a bot
    they explicitly selected AND own; a foreign/absent tg_id resolves to None."""
    from userbot import UserBotController

    d = tempfile.mkdtemp()
    r = Registry(os.path.join(d, "fresh.db"), KeyVault())
    a = r.create_bot(700, "A", None, None, "AgentA", "pta", {"perps": 1}, 2.0, 120, "balanced")
    single = UserBotController(r, platform=object())
    single._master_token = "111:master-fake-token"
    # single bot for 700 -> always that one
    assert single.route_target(700) == a["id"]
    # unknown user -> None
    assert single.route_target(999) is None

    # second bot for 700 -> now multi-bot
    b = r.create_bot(700, "B", None, None, "AgentB", "ptb", {"perps": 1}, 2.0, 120, "balanced")
    assert single.route_target(700) is None                 # multi, none active -> picker
    assert single.route_target(700, a["id"]) == a["id"]     # explicit + owned -> route
    assert single.route_target(700, b["id"]) == b["id"]
    # active id that the caller does NOT own must be ignored (route to picker)
    other = r.create_bot(701, "C", None, None, "AgentC", "ptc", {"perps": 1}, 2.0, 120, "balanced")
    assert single.route_target(700, other["id"]) is None
    r.close()


# ---------------------------------------------------------------------------- G7
def test_notifier_routes_master():
    """G7: a token-less bot's push goes out via the MASTER token to the owner's
    chat (chat_id == tg_id). A legacy bot that still has its own token is
    unchanged (still uses that token)."""
    import notifier as N
    from notifier import Notifier

    d = tempfile.mkdtemp()
    r = Registry(os.path.join(d, "reg.db"), KeyVault())
    n = Notifier(r)
    n._token = lambda bt: bt or "MASTERTOKEN"  # deterministic (no cfg dependency)

    owner = 6698272364
    calls = []

    def fake_post(url, json=None, **kw):
        calls.append((url, json))
        class _R:
            status_code = 200
            def json(self): return {"ok": True, "result": {"message_id": 123}}
        return _R()

    orig_post, orig_del = N.requests.post, N._schedule_delete
    N.requests.post = fake_post
    N._schedule_delete = lambda *a, **k: None  # don't spawn delete threads
    try:
        # token-less -> master token
        assert n.notify(1, owner, None, owner, "fill", "r1", "hi") is True
        # legacy bot still has its own token -> used verbatim
        assert n.notify(2, owner, "OWNBOT:tok", owner, "fill", "r2", "hi") is True
    finally:
        N.requests.post = orig_post
        N._schedule_delete = orig_del

    (url_tk, payload_tk), (url_own, payload_own) = calls
    assert "/botMASTERTOKEN/sendMessage" in url_tk
    assert payload_tk["chat_id"] == owner
    assert "/botOWNBOT:tok/sendMessage" in url_own
    assert payload_own["chat_id"] == owner
    r.close()

