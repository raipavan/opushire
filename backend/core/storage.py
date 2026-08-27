"""SQLite-based persistent storage — replaces fragile JSON files."""

from __future__ import annotations

import json
import sqlite3
import threading
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from loguru import logger

_DB_PATH: Optional[Path] = None
_LOCAL = threading.local()
_db_write_lock = threading.RLock()


def _commit_with_retry(conn: sqlite3.Connection, retries: int = 5, delay: float = 1.0) -> None:
    """Commit with retry on 'database is locked' errors."""
    with _db_write_lock:
        import sqlite3 as _sqlite3
        for attempt in range(retries):
            try:
                conn.commit()
                return
            except _sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < retries - 1:
                    logger.warning(f"SQLite locked (attempt {attempt + 1}/{retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise


def _run_db(fn, retries: int = 5, delay: float = 1.0):
    """Run a DB operation (execute + commit) with retry on 'database is locked'."""
    with _db_write_lock:
        import sqlite3 as _sqlite3
        for attempt in range(retries):
            try:
                return fn()
            except _sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < retries - 1:
                    logger.warning(f"SQLite locked in op (attempt {attempt + 1}/{retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    try:
                        _get_conn().rollback()
                    except Exception:
                        pass
                    raise

# Inter-call gap (seconds) between outbound dials.
_GAP_LEGACY_DEFAULT_SEC = 5.0
_GAP_CORE_ROLE_NAMES = frozenset({"data_edge"})
STRICT_CORE_GAP_MIN_SEC = 96.0
STRICT_CORE_GAP_MAX_SEC = 144.0
STRICT_CORE_GAP_SEC = 120.0
_GAP_CORE_PRODUCT_ROLES_SEC = STRICT_CORE_GAP_SEC


def is_strict_gap_core_role(role: Optional[str]) -> bool:
    return (role or "data_edge").strip().lower() in _GAP_CORE_ROLE_NAMES


def default_inter_call_gap_sec(role: Optional[str]) -> float:
    r = (role or "data_edge").strip().lower()
    if r in _GAP_CORE_ROLE_NAMES:
        return float(_GAP_CORE_PRODUCT_ROLES_SEC)
    return float(_GAP_LEGACY_DEFAULT_SEC)


def init_db(data_dir: Optional[Path | str] = None) -> Path:
    """Initialize the SQLite database. Call once at startup."""
    global _DB_PATH
    if isinstance(data_dir, str):
        data_dir = Path(data_dir)
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    _DB_PATH = base / "vernika.db"
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS role_state (
            role TEXT PRIMARY KEY,
            prompt TEXT DEFAULT '',
            rag TEXT DEFAULT '',
            delay_sec REAL DEFAULT 5.0,
            vobiz_config TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT (datetime('now')),
            greeting_text TEXT DEFAULT ''
        );
    """)
    # Migration: add greeting_text if missing
    try:
        conn.execute("ALTER TABLE role_state ADD COLUMN greeting_text TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass # Already exists

    # Per-role campaign Cases. The operator defines one or more named "Cases"
    # (e.g. "April Steel Sheets Push", "Diwali Discount Drive") and **activates
    # exactly one** per role. The bridge appends the active case description
    # to the system prompt so the AI runs today's campaign without editing the
    # base persona prompt.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cases_role ON cases(role);
        CREATE INDEX IF NOT EXISTS idx_cases_active ON cases(role, active);
    """)
    _commit_with_retry(conn)

    # Per-role campaign schedules. The operator uploads leads, then schedules
    # the campaign to start automatically at a future date/time. A small
    # background loop in ``core.worker`` polls this table every 30 s and, when
    # ``run_at <= now`` and ``status='scheduled'``, kicks off the same worker
    # the Start Campaign button does. ``run_at`` is stored as epoch seconds
    # (UTC) so timezone math is trivial both server- and client-side.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT DEFAULT '',
            run_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            started_at REAL,
            error TEXT,
            stop_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_role ON schedules(role);
        CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, run_at);
    """)
    _commit_with_retry(conn)
    # Migration: installs created before stop_at existed need the column added
    # *before* the index that references it can be created. Split the work so
    # CREATE INDEX never runs against a missing column.
    try:
        conn.execute("ALTER TABLE schedules ADD COLUMN stop_at REAL")
    except sqlite3.OperationalError:
        pass  # Already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedules_stop ON schedules(status, stop_at)"
    )
    _commit_with_retry(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inbound_callbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            from_phone TEXT NOT NULL DEFAULT '',
            to_phone TEXT,
            call_uuid TEXT,
            matched_lead_id INTEGER,
            matched_name TEXT,
            matched_company TEXT,
            matched_email TEXT,
            matched_status TEXT,
            campaign_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            dismissed INTEGER NOT NULL DEFAULT 0,
            raw_meta TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending'
        );
    """)
    _commit_with_retry(conn)

    # Migration: add status column if missing before creating indexes that use it
    try:
        conn.execute("ALTER TABLE inbound_callbacks ADD COLUMN status TEXT DEFAULT 'pending'")
        _commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass  # Already exists

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_inbound_callbacks_role
            ON inbound_callbacks(role, dismissed, id DESC);
        CREATE INDEX IF NOT EXISTS idx_inbound_callbacks_status
            ON inbound_callbacks(role, status, id DESC);
    """)
    _commit_with_retry(conn)
    # Migration: add status column if missing
    try:
        conn.execute("ALTER TABLE inbound_callbacks ADD COLUMN status TEXT DEFAULT 'pending'")
        _commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass  # Already exists
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_callbacks_call_uuid
            ON inbound_callbacks(call_uuid)
            WHERE call_uuid IS NOT NULL AND trim(call_uuid) != ''
            """
        )
    except sqlite3.OperationalError:
        pass
    _commit_with_retry(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS manual_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            camp_id TEXT NOT NULL UNIQUE,
            to_phone TEXT NOT NULL DEFAULT '',
            callee_name TEXT NOT NULL DEFAULT '',
            log_id TEXT,
            status TEXT NOT NULL DEFAULT 'dialing',
            started_at TEXT DEFAULT (datetime('now')),
            ended_at TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            duration_sec REAL,
            disposition TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            next_steps TEXT DEFAULT '',
            emotion_label TEXT DEFAULT '',
            emotion_rationale TEXT DEFAULT '',
            emotion_confidence REAL,
            analysis_json TEXT DEFAULT '{}',
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_manual_calls_role_started
            ON manual_calls(role, id DESC);
    """)
    _commit_with_retry(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT DEFAULT 'Unknown',
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            company TEXT DEFAULT '',
            details TEXT DEFAULT '',
            extra TEXT DEFAULT '{}',
            segment TEXT DEFAULT 'rfq',
            status TEXT DEFAULT 'pending',
            analysis TEXT DEFAULT '{}',
            start_time REAL,
            duration_sec REAL DEFAULT 0.0,
            error TEXT,
            _log_id TEXT,
            _call_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'factory',
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            voice TEXT DEFAULT 'Puck',
            knowledge_files TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS agent_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            name TEXT DEFAULT 'Unknown',
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            company TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_leads_role ON leads(role);
        CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(role, status);
        CREATE INDEX IF NOT EXISTS idx_agent_leads_agent ON agent_leads(agent_id);
    """)
    _commit_with_retry(conn)

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_leads_role_status_created ON leads(role, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_inbound_callbacks_created_at ON inbound_callbacks(created_at);
    """)
    _commit_with_retry(conn)

    # ``extra``: JSON blob for CSV columns beyond name/phone/email/company.
    # IMPORTANT: ALTER must run *after* ``CREATE TABLE IF NOT EXISTS leads`` so new
    # installs get the column and older DBs (created before ``extra``) are migrated.
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN extra TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # Already exists

    try:
        conn.execute("ALTER TABLE leads ADD COLUMN duration_sec REAL DEFAULT 0.0")
        _commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass  # Already exists

    try:
        conn.execute("ALTER TABLE leads ADD COLUMN segment TEXT DEFAULT 'rfq'")
        _commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass  # Already exists

    # Migration: add role column to agents
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT 'factory'")
        _commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass

    # Seed default roles if empty
    for role in (
        "data_edge",
    ):
        conn.execute(
            "INSERT OR IGNORE INTO role_state (role) VALUES (?)",
            (role,)
        )
    _commit_with_retry(conn)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
    """)
    _commit_with_retry(conn)

    # WhatsApp message log — tracks every outbound WhatsApp message sent
    # via the auto-send or manual API features.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS whatsapp_message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            phone TEXT NOT NULL,
            role TEXT NOT NULL,
            message_type TEXT DEFAULT 'project_details',
            status TEXT DEFAULT 'Pending',
            provider TEXT DEFAULT '',
            error TEXT DEFAULT '',
            analysis_summary TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wa_log_lead ON whatsapp_message_log(lead_id);
        CREATE INDEX IF NOT EXISTS idx_wa_log_status ON whatsapp_message_log(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_wa_log_role ON whatsapp_message_log(role, created_at DESC);
    """)
    _commit_with_retry(conn)

    # Interested Leads Feedback — centralized feedback tracker
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS interested_leads_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            contact_number TEXT NOT NULL DEFAULT '',
            lead_status TEXT NOT NULL DEFAULT '',
            custom_status TEXT DEFAULT '',
            feedback_notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ilf_name ON interested_leads_feedback(name);
        CREATE INDEX IF NOT EXISTS idx_ilf_contact ON interested_leads_feedback(contact_number);
        CREATE INDEX IF NOT EXISTS idx_ilf_status ON interested_leads_feedback(lead_status);
    """)
    _commit_with_retry(conn)

    # Create role-specific data directories for prompt + RAG files
    for role in ("data_edge",):
        role_dir = base / role
        role_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ Created directory: {role_dir}")
    
    # Initialize Vobiz accounts (if configured in .env)
    from config import settings
    role_vobiz_map = {
        "data_edge": {
            "auth_id": settings.vobiz_data_edge_auth_id,
            "auth_token": settings.vobiz_data_edge_auth_token,
            "from_number": settings.vobiz_data_edge_from_number,
            "public_url": settings.vobiz_public_base_url,
        },
    }
    
    for role, vobiz_creds in role_vobiz_map.items():
        if vobiz_creds["auth_id"] and vobiz_creds["auth_token"] and vobiz_creds["from_number"]:
            vobiz_config = {
                "auth_id": vobiz_creds["auth_id"],
                "auth_token": vobiz_creds["auth_token"],
                "from_number": vobiz_creds["from_number"],
                "public_url": settings.vobiz_public_base_url,
            }
            conn.execute(
                "UPDATE role_state SET vobiz_config = ?, updated_at = datetime('now') WHERE role = ?",
                (json.dumps(vobiz_config), role)
            )
            _commit_with_retry(conn)
            logger.info(f"✅ Initialized role '{role}' with dedicated Vobiz account")
        else:
            logger.info(f"ℹ️  Role '{role}' will use global Vobiz fallback credentials (VOBIZ_AUTH_ID from .env)")

    close_db()
    logger.info(f"Database initialized: {_DB_PATH}")
    return _DB_PATH


def _get_conn() -> sqlite3.Connection:
    """Thread-local SQLite connection with WAL mode for concurrency."""
    if not hasattr(_LOCAL, "conn") or _LOCAL.conn is None:
        if _DB_PATH is None:
            raise RuntimeError("Database not initialized. Call init_db() first.")
        _LOCAL.conn = sqlite3.connect(
            str(_DB_PATH),
            check_same_thread=False,
            timeout=60.0,
        )
        _LOCAL.conn.execute("PRAGMA journal_mode=WAL")
        _LOCAL.conn.execute("PRAGMA busy_timeout=60000")
        _LOCAL.conn.row_factory = sqlite3.Row
        _LOCAL.conn.execute("PRAGMA foreign_keys = ON")
    return _LOCAL.conn


def close_db() -> None:
    if hasattr(_LOCAL, "conn") and _LOCAL.conn:
        _LOCAL.conn.close()
        _LOCAL.conn = None


# Operator clicked Start → survives process restart until Stop or graceful empty queue.
_CAMPAIGN_WANT_META_PREFIX = "campaign_want_running_v2"


def campaign_want_running_meta_key(role: str) -> str:
    return f"{_CAMPAIGN_WANT_META_PREFIX}:{(role or 'data_edge').strip().lower()}"


async def set_campaign_want_running(role: str, wanted: bool) -> None:
    return await asyncio.to_thread(_set_campaign_want_running_sync, role, wanted)

def _set_campaign_want_running_sync(role: str, wanted: bool) -> None:
    def _do():
        conn = _get_conn()
        k = campaign_want_running_meta_key(role)
        if wanted:
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                (k, "1"),
            )
        else:
            conn.execute("DELETE FROM app_meta WHERE key = ?", (k,))
        _commit_with_retry(conn)
    _run_db(_do)


async def roles_with_campaign_run_wanted() -> list[str]:
    return await asyncio.to_thread(_roles_with_campaign_run_wanted_sync)

def _roles_with_campaign_run_wanted_sync() -> list[str]:
    conn = _get_conn()
    prefix = f"{_CAMPAIGN_WANT_META_PREFIX}:"
    rows = conn.execute(
        """
        SELECT key FROM app_meta
        WHERE key LIKE ?
          AND trim(value) IN ('1', 'true', 'yes')
        """,
        (prefix + "%",),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        key = str(r["key"] or "")
        if key.startswith(prefix):
            out.append(key[len(prefix):])
    return out


# Operator clicked Stop / stop-all — blocks auto-resume on deploy restart until Start.
_CAMPAIGN_PAUSED_META = "campaign_globally_paused_v1"


async def set_campaign_globally_paused(paused: bool) -> None:
    return await asyncio.to_thread(_set_campaign_globally_paused_sync, paused)


def _set_campaign_globally_paused_sync(paused: bool) -> None:
    def _do():
        conn = _get_conn()
        if paused:
            conn.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
                (_CAMPAIGN_PAUSED_META, "1"),
            )
        else:
            conn.execute("DELETE FROM app_meta WHERE key = ?", (_CAMPAIGN_PAUSED_META,))
        _commit_with_retry(conn)
    _run_db(_do)


async def is_campaign_globally_paused() -> bool:
    return await asyncio.to_thread(_is_campaign_globally_paused_sync)


def _is_campaign_globally_paused_sync() -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (_CAMPAIGN_PAUSED_META,),
    ).fetchone()
    return bool(row and str(row["value"] or "").strip().lower() in ("1", "true", "yes"))


# --- Role State ---

async def get_role_state(role: str) -> dict:
    return await asyncio.to_thread(_get_role_state_sync, role)

def _get_role_state_sync(role: str) -> dict:
    role_key = (role or "data_edge").strip().lower()
    fallback_delay = default_inter_call_gap_sec(role_key)
    conn = _get_conn()
    row = conn.execute("SELECT * FROM role_state WHERE role = ?", (role_key,)).fetchone()
    if not row:
        return {
            "role": role_key,
            "prompt": "",
            "rag": "",
            "delay_sec": fallback_delay,
            "vobiz": {},
        }
    ds = row["delay_sec"]
    return {
        "role": row["role"],
        "prompt": row["prompt"] or "",
        "rag": row["rag"] or "",
        "delay_sec": float(fallback_delay if ds is None else ds),
        "vobiz": json.loads(row["vobiz_config"] or "{}"),
        "greeting_text": row["greeting_text"] or "",
    }


async def save_role_state(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None):
    return await asyncio.to_thread(_save_role_state_sync, role, prompt, rag, vobiz_config, delay_sec, greeting_text)

def _save_role_state_sync(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None):
    def _do():
        conn = _get_conn()
        r = (role or "data_edge").strip().lower()
        conn.execute("INSERT OR IGNORE INTO role_state (role) VALUES (?)", (r,))
        updates = []
        params = []
        if prompt is not None:
            updates.append("prompt = ?")
            params.append(prompt)
        if rag is not None:
            updates.append("rag = ?")
            params.append(rag)
        if vobiz_config is not None:
            updates.append("vobiz_config = ?")
            params.append(json.dumps(vobiz_config))
        if delay_sec is not None:
            updates.append("delay_sec = ?")
            params.append(delay_sec)
        if greeting_text is not None:
            updates.append("greeting_text = ?")
            params.append(greeting_text)
        if not updates:
            return
        updates.append("updated_at = datetime('now')")
        params.append(r)
        conn.execute(f"UPDATE role_state SET {', '.join(updates)} WHERE role = ?", params)
        _commit_with_retry(conn)
    _run_db(_do)


# --- Leads ---

async def get_lead(role: str, lead_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_lead_sync, role, lead_id)

def _get_lead_sync(role: str, lead_id: int) -> Optional[dict]:
    """Single campaign lead row keyed by SQLite ``id`` and ``role``."""
    conn = _get_conn()
    r = (role or "data_edge").strip().lower()
    row = conn.execute(
        "SELECT * FROM leads WHERE role = ? AND id = ?",
        (r, int(lead_id)),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def get_leads(
    role: str,
    status: str = None,
    limit: int = 0,
    *,
    order: str = "created",
) -> list[dict]:
    return await asyncio.to_thread(_get_leads_sync, role, status, limit, order)

def _get_leads_sync(
    role: str,
    status: str = None,
    limit: int = 0,
    order: str = "created",
) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM leads WHERE role = ?"
    params = [role]
    if status:
        query += " AND status = ?"
        params.append(status)
    if (order or "created").strip().lower() == "activity":
        query += """
         ORDER BY
             CASE WHEN start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
                  THEN CAST(start_time AS REAL) ELSE 0.0 END DESC,
             updated_at DESC,
             created_at DESC
        """
    else:
        query += " ORDER BY created_at DESC"
    if int(limit) > 0:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


async def count_leads_with_outbound_attempt(role: str) -> int:
    return await asyncio.to_thread(_count_leads_with_outbound_attempt_sync, role)

def _count_leads_with_outbound_attempt_sync(role: str) -> int:
    """How many rows have evidence of at least one dial / bridge session started.

    Mirrors the dashboard ``isCalled`` heuristic without loading every row."""
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM leads WHERE role = ?
          AND (
                (COALESCE(trim(_log_id), '') != '')
             OR (start_time IS NOT NULL AND CAST(start_time AS REAL) > 0)
          )
        """,
        ((role or "data_edge").strip().lower(),),
    ).fetchone()
    return int(row["c"]) if row else 0


async def get_leads_with_outbound_activity(role: str, limit: int = 32000) -> list[dict]:
    return await asyncio.to_thread(_get_leads_with_outbound_activity_sync, role, limit)

def _get_leads_with_outbound_activity_sync(role: str, limit: int = 32000) -> list[dict]:
    """All campaign leads that have been bridged/outbound-dialed (log id or ``start_time``).

    Engagement timeline aggregates use this rather than the small ``chart_sample`` slice so
    activity on older CSV rows still appears alongside ``called_count``.
    """

    role = (role or "data_edge").strip().lower()
    lim = max(1, min(int(limit), 50000))
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM leads WHERE role = ?
          AND (
                (COALESCE(TRIM(COALESCE(_log_id, '')), '') != '')
             OR (start_time IS NOT NULL AND CAST(start_time AS REAL) > 0)
          )
        ORDER BY
             CASE WHEN start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
                  THEN CAST(start_time AS REAL) ELSE 0.0 END DESC,
             updated_at DESC
        LIMIT ?
        """,
        (role, lim),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


async def add_lead(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    return await asyncio.to_thread(_add_lead_sync, role, name, phone, email, company, details)

def _add_lead_sync(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    def _do():
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads (role, name, phone, email, company, details) VALUES (?, ?, ?, ?, ?, ?)",
            (role, name, phone, email, company, details)
        )
        _commit_with_retry(conn)
        return cur.lastrowid
    return _run_db(_do)


async def bulk_add_leads(role: str, leads: list[dict]) -> int:
    return await asyncio.to_thread(_bulk_add_leads_sync, role, leads)

def _bulk_add_leads_sync(role: str, leads: list[dict]) -> int:
    """Insert leads, persisting any **extra** caller fields (anything beyond
    name/phone/email/company/details/status) into the ``extra`` JSON column so
    the AI can reference them on the call.
    """
    def _do():
        conn = _get_conn()
        count = 0
        _RESERVED = {
            "name", "phone", "email", "company", "details",
            "status", "role", "id", "extra", "segment",
        }
        for lead in leads:
            phone = lead.get("phone", "").strip()
            if not phone:
                continue
            raw_extra = lead.get("extra")
            if isinstance(raw_extra, dict):
                extras_dict = {k: v for k, v in raw_extra.items() if v not in (None, "")}
            else:
                extras_dict = {
                    k: v for k, v in lead.items()
                    if k not in _RESERVED and v not in (None, "")
                }
            extras_dict = {str(k): str(v) for k, v in extras_dict.items() if str(v).strip()}
            extra_json = json.dumps(extras_dict, ensure_ascii=False) if extras_dict else "{}"
            conn.execute(
                "INSERT INTO leads (role, name, phone, email, company, details, extra, segment, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    role,
                    lead.get("name", "Unknown"),
                    phone,
                    lead.get("email", ""),
                    lead.get("company", ""),
                    lead.get("details", ""),
                    extra_json,
                    lead.get("segment", "rfq"),
                    "pending",
                )
            )
            count += 1
        _commit_with_retry(conn)
        return count
    return _run_db(_do)


async def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None, duration_sec: float = None):
    return await asyncio.to_thread(_update_lead_status_sync, lead_id, status, error, analysis, duration_sec)

def _update_lead_status_sync(lead_id: int, status: str, error: str = None, analysis: dict = None, duration_sec: float = None):
    old_status = "?"
    def _do():
        nonlocal old_status
        conn = _get_conn()
        try:
            old = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
            old_status = old[0] if old else "?"
        except Exception:
            old_status = "?"
        updates = ["status = ?", "error = ?", "updated_at = datetime('now')"]
        params = [status, error]
        if analysis is not None:
            updates.append("analysis = ?")
            params.append(json.dumps(analysis))
        if duration_sec is not None:
            updates.append("duration_sec = ?")
            params.append(duration_sec)
        params.append(lead_id)
        conn.execute(
            f"UPDATE leads SET {', '.join(updates)} WHERE id = ?",
            tuple(params)
        )
        _commit_with_retry(conn)
    _run_db(_do)
    logger.info("[STATUS] Lead {}: {} → {}", lead_id, old_status, status)


VALID_DISPOSITIONS = {"Interested", "Not Interested", "Answered", "Call Later", "Busy", "Wrong Number"}


async def update_lead_disposition(lead_id: int, disposition: str) -> bool:
    return await asyncio.to_thread(_update_lead_disposition_sync, lead_id, disposition)


def _update_lead_disposition_sync(lead_id: int, disposition: str) -> bool:
    def _do():
        conn = _get_conn()
        row = conn.execute("SELECT analysis FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            return False
        raw = row[0] if row[0] else "{}"
        try:
            aj = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        except (json.JSONDecodeError, TypeError):
            aj = {}
        aj["disposition"] = disposition
        aj["disposition_overridden"] = True
        conn.execute(
            "UPDATE leads SET analysis = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(aj), lead_id),
        )
        _commit_with_retry(conn)
        return True
    result = _run_db(_do)
    if result:
        logger.info("[DISPO] Lead {} disposition overridden → {}", lead_id, disposition)
    return bool(result)


async def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None):
    return await asyncio.to_thread(_update_lead_call_info_sync, lead_id, log_id, call_id, start_time)

def _update_lead_call_info_sync(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None):
    def _do():
        conn = _get_conn()
        updates = []
        params = []
        if log_id is not None:
            updates.append("_log_id = ?")
            params.append(log_id)
        if call_id is not None:
            updates.append("_call_id = ?")
            params.append(call_id)
        if start_time is not None:
            updates.append("start_time = ?")
            params.append(start_time)
        updates.append("updated_at = datetime('now')")
        params.append(lead_id)
        conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
        _commit_with_retry(conn)
    _run_db(_do)


async def promote_due_scheduled_callbacks(now_epoch: float | None = None) -> int:
    return await asyncio.to_thread(_promote_due_scheduled_callbacks_sync, now_epoch)

def _promote_due_scheduled_callbacks_sync(now_epoch: float | None = None) -> int:
    """Move leads whose defer-until epoch has passed from ``callback_scheduled`` → ``pending``."""
    def _do():
        t = float(now_epoch if now_epoch is not None else time.time())
        conn = _get_conn()
        cur = conn.execute(
            """
            UPDATE leads SET status = 'pending',
                   updated_at = datetime('now')
             WHERE status = 'callback_scheduled'
               AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > 0
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) <= ?
            """,
            (t,),
        )
        _commit_with_retry(conn)
        return int(cur.rowcount or 0)
    n = _run_db(_do)
    if n > 0:
        logger.info(f"Promoted {n} callback_scheduled lead(s) → pending (due recall)")
    return n


async def role_has_future_callback_scheduled(role: str, now_epoch: float) -> bool:
    return await asyncio.to_thread(_role_has_future_callback_scheduled_sync, role, now_epoch)

def _role_has_future_callback_scheduled_sync(role: str, now_epoch: float) -> bool:
    """True if ``role`` has at least one lead waiting for a future transcript-requested recall."""

    from core.state import normalize_console_role as _norm

    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM leads
        WHERE role = ?
          AND status = 'callback_scheduled'
          AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
          AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > ?
        LIMIT 1
        """,
        (rid, float(now_epoch)),
    ).fetchone()
    return row is not None


async def reset_leads(role: str):
    return await asyncio.to_thread(_reset_leads_sync, role)

def _reset_leads_sync(role: str):
    def _do():
        conn = _get_conn()
        conn.execute("UPDATE leads SET status = 'pending', error = NULL, updated_at = datetime('now') WHERE role = ?", (role,))
        _commit_with_retry(conn)
    _run_db(_do)


async def wipe_leads(role: str):
    return await asyncio.to_thread(_wipe_leads_sync, role)

def _wipe_leads_sync(role: str):
    def _do():
        conn = _get_conn()
        conn.execute("DELETE FROM leads WHERE role = ?", (role,))
        _commit_with_retry(conn)
    _run_db(_do)


async def get_lead_counts(role: str) -> dict:
    return await asyncio.to_thread(_get_lead_counts_sync, role)

def _get_lead_counts_sync(role: str) -> dict:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM leads WHERE role = ? GROUP BY status",
        (role,)
    ).fetchall()
    counts = {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0}
    for row in rows:
        status = row["status"]
        count = row["count"]
        counts[status] = count if status in counts else count
        counts["total"] += count
    return counts


async def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    return await asyncio.to_thread(_export_leads_csv_sync, role, status_filter)

def _export_leads_csv_sync(role: str, status_filter: str = "all") -> list[dict]:
    conn = _get_conn()
    query = "SELECT name, phone, email, company, status, start_time, error FROM leads WHERE role = ?"
    params = [role]
    if status_filter != "all":
        filter_map = {
            "responded": "completed",
            "not_responded": "IN ('failed', 'pending', 'dialing')",
            "not_interested": "not_interested",
        }
        status_val = filter_map.get(status_filter, status_filter)
        if "IN" in status_val:
            query += f" AND status {status_val}"
        else:
            query += " AND status = ?"
            params.append(status_val)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


async def find_lead_by_phone(role: str, raw_phone: str) -> Optional[dict]:
    return await asyncio.to_thread(_find_lead_by_phone_sync, role, raw_phone)

def _find_lead_by_phone_sync(role: str, raw_phone: str) -> Optional[dict]:
    """Match a lead row by normalized or last-10-digit phone for any campaign role."""
    from core.utils import _norm_phone_str

    role = (role or "data_edge").strip().lower()
    norm = _norm_phone_str(raw_phone or "")
    conn = _get_conn()
    if norm:
        row = conn.execute(
            "SELECT * FROM leads WHERE role = ? AND phone = ? ORDER BY updated_at DESC LIMIT 1",
            (role, norm),
        ).fetchone()
        if row:
            return _row_to_dict(row)
    digits = "".join(c for c in str(raw_phone or "") if c.isdigit())
    if len(digits) < 10:
        return None
    tail = digits[-10:]
    if len(tail) == 10:
        row = conn.execute(
            "SELECT * FROM leads WHERE role = ? AND phone LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (role, f"%{tail}"),
        ).fetchone()
        if row:
            return _row_to_dict(row)
    return None


async def find_lead_by_phone_for_inbound(role: str, raw_phone: str) -> Optional[dict]:
    return await asyncio.to_thread(_find_lead_by_phone_for_inbound_sync, role, raw_phone)

def _find_lead_by_phone_for_inbound_sync(role: str, raw_phone: str) -> Optional[dict]:
    """Best-effort match of an inbound CLI to a lead row for ``role``."""
    from core.utils import _norm_phone_str

    role = (role or "").strip().lower()
    if role not in ("data_edge",):
        return None
    return _find_lead_by_phone_sync(role, raw_phone)


async def record_inbound_callback(
    role: str,
    from_phone: str,
    *,
    to_phone: Optional[str] = None,
    call_uuid: Optional[str] = None,
    campaign_active: bool = False,
    raw_start: Optional[dict] = None,
) -> Optional[int]:
    return await asyncio.to_thread(_record_inbound_callback_sync, role, from_phone, to_phone, call_uuid, campaign_active, raw_start)

def _record_inbound_callback_sync(
    role: str,
    from_phone: str,
    *,
    to_phone: Optional[str] = None,
    call_uuid: Optional[str] = None,
    campaign_active: bool = False,
    raw_start: Optional[dict] = None,
) -> Optional[int]:
    """Persist one inbound leg (deduped by ``call_uuid`` when present)."""
    from core.state import normalize_console_role
    from core.utils import _norm_phone_str

    role = normalize_console_role(role)
    from_norm = _norm_phone_str(from_phone or "")
    display_from = from_norm or (from_phone or "").strip() or "unknown"
    to_stored: Optional[str] = None
    if to_phone:
        to_stored = _norm_phone_str(to_phone) or (to_phone or "").strip() or None

    conn = _get_conn()
    cu = (call_uuid or "").strip()
    if cu:
        ex = conn.execute(
            "SELECT id FROM inbound_callbacks WHERE call_uuid = ?",
            (cu,),
        ).fetchone()
        if ex:
            return int(ex["id"])

    match = _find_lead_by_phone_for_inbound_sync(role, from_phone or display_from)
    try:
        raw_json = json.dumps(raw_start or {}, ensure_ascii=False, default=str)[:16000]
    except Exception:
        raw_json = "{}"

    cur = conn.execute(
        """
        INSERT INTO inbound_callbacks (
            role, from_phone, to_phone, call_uuid,
            matched_lead_id, matched_name, matched_company, matched_email, matched_status,
            campaign_active, raw_meta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            role,
            display_from,
            to_stored,
            cu or None,
            match["id"] if match else None,
            (match or {}).get("name"),
            (match or {}).get("company"),
            (match or {}).get("email"),
            (match or {}).get("status"),
            1 if campaign_active else 0,
            raw_json,
        ),
    )
    _commit_with_retry(conn)
    return int(cur.lastrowid)


async def insert_incoming_call(role: str, camp_id: str, from_phone: str, caller_name: str = "") -> int:
    """Insert a row into ``incoming_calls`` for an inbound call leg."""

    def _sync() -> int:
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO incoming_calls (role, camp_id, from_phone, caller_name)
            VALUES (?, ?, ?, ?)
            """,
            ((role or "").strip().lower(), camp_id or "", (from_phone or "").strip(), (caller_name or "").strip()),
        )
        _commit_with_retry(conn)
        return int(cur.lastrowid)

    return await asyncio.to_thread(_sync)


async def list_inbound_callbacks(
    role: str, limit: int = 100, include_dismissed: bool = False
) -> list[dict]:
    return await asyncio.to_thread(_list_inbound_callbacks_sync, role, limit, include_dismissed)

def _list_inbound_callbacks_sync(
    role: str, limit: int = 100, include_dismissed: bool = False
) -> list[dict]:
    from core.state import normalize_console_role

    role = normalize_console_role(role)
    conn = _get_conn()
    q = "SELECT * FROM inbound_callbacks WHERE role = ?"
    params: list[Any] = [role]
    if not include_dismissed:
        q += " AND dismissed = 0"
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    return [_row_to_dict(r) for r in rows]


async def dismiss_inbound_callback(row_id: int, role: str) -> bool:
    return await asyncio.to_thread(_dismiss_inbound_callback_sync, row_id, role)

def _dismiss_inbound_callback_sync(row_id: int, role: str) -> bool:
    from core.state import normalize_console_role

    role = normalize_console_role(role)
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE inbound_callbacks SET dismissed = 1 WHERE id = ? AND role = ?",
        (row_id, role),
    )
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def count_open_inbounds_for_role(role: str) -> int:
    return await asyncio.to_thread(_count_open_inbounds_for_role_sync, role)

def _count_open_inbounds_for_role_sync(role: str) -> int:
    from core.state import normalize_console_role

    role = normalize_console_role(role)
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM inbound_callbacks WHERE role = ? AND dismissed = 0",
        (role,),
    ).fetchone()
    return int(row["c"] if row else 0)


async def inbound_counts_on_calendar_dates(role: str, iso_dates: list[str]) -> dict[str, int]:
    return await asyncio.to_thread(_inbound_counts_on_calendar_dates_sync, role, iso_dates)

def _inbound_counts_on_calendar_dates_sync(role: str, iso_dates: list[str]) -> dict[str, int]:
    """Return ``{YYYY-MM-DD: count}`` inbound rows stored on those calendar dates (SQLite ``DATE``)."""

    from core.state import normalize_console_role

    role = normalize_console_role(role)
    if not iso_dates:
        return {}
    placeholders = ",".join("?" * len(iso_dates))
    conn = _get_conn()
    rows = conn.execute(
        f"""
        SELECT date(created_at) AS d, COUNT(*) AS c
        FROM inbound_callbacks
        WHERE role = ? AND dismissed = 0 AND date(created_at) IN ({placeholders})
        GROUP BY date(created_at)
        """,
        (role, *tuple(iso_dates)),
    ).fetchall()
    return {str(r["d"]): int(r["c"]) for r in rows if r["d"] is not None}


async def get_pending_callbacks(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_get_pending_callbacks_sync, role, limit)

def _get_pending_callbacks_sync(role: str, limit: int = 100) -> list[dict]:
    """Get callbacks that need to be called back (status='pending')."""
    from core.state import normalize_console_role

    role = normalize_console_role(role)
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM inbound_callbacks
        WHERE role = ? AND status = 'pending' AND dismissed = 0
        ORDER BY id ASC
        LIMIT ?
        """,
        (role, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def mark_callback_processed(row_id: int, role: str) -> bool:
    return await asyncio.to_thread(_mark_callback_processed_sync, row_id, role)

def _mark_callback_processed_sync(row_id: int, role: str) -> bool:
    """Mark a callback as processed so it won't be called again."""
    def _do():
        from core.state import normalize_console_role
        r = normalize_console_role(role)
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE inbound_callbacks SET status = 'processed' WHERE id = ? AND role = ?",
            (row_id, r),
        )
        _commit_with_retry(conn)
        return cur.rowcount > 0
    return _run_db(_do)


async def mark_callback_calling(row_id: int, role: str) -> bool:
    return await asyncio.to_thread(_mark_callback_calling_sync, row_id, role)

def _mark_callback_calling_sync(row_id: int, role: str) -> bool:
    """Mark a callback as currently being called (in progress)."""
    def _do():
        from core.state import normalize_console_role
        r = normalize_console_role(role)
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE inbound_callbacks SET status = 'calling' WHERE id = ? AND role = ?",
            (row_id, r),
        )
        _commit_with_retry(conn)
        return cur.rowcount > 0
    return _run_db(_do)


# --- Sandbox Agents ---

async def create_agent(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    return await asyncio.to_thread(_create_agent_sync, name, prompt, voice, role)

def _create_agent_sync(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    import uuid
    agent_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agents (id, role, name, prompt, voice) VALUES (?, ?, ?, ?, ?)",
        (agent_id, role, name, prompt, voice)
    )
    _commit_with_retry(conn)
    return agent_id


async def get_agent(agent_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_agent_sync, agent_id)

def _get_agent_sync(agent_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    result = _row_to_dict(row)
    result["knowledge_files"] = json.loads(result.get("knowledge_files", "[]"))
    return result


async def list_agents(role: Optional[str] = None) -> list[dict]:
    return await asyncio.to_thread(_list_agents_sync, role)

def _list_agents_sync(role: Optional[str] = None) -> list[dict]:
    conn = _get_conn()
    if role:
        rows = conn.execute("SELECT * FROM agents WHERE role = ? ORDER BY created_at DESC", (role,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        r = _row_to_dict(row)
        r["knowledge_files"] = json.loads(r.get("knowledge_files", "[]"))
        result.append(r)
    return result


async def update_agent(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    return await asyncio.to_thread(_update_agent_sync, agent_id, name, prompt, voice)

def _update_agent_sync(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    conn = _get_conn()
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if voice is not None:
        updates.append("voice = ?")
        params.append(voice)
    if not updates:
        return
    updates.append("updated_at = datetime('now')")
    params.append(agent_id)
    conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
    _commit_with_retry(conn)


async def delete_agent(agent_id: str) -> bool:
    return await asyncio.to_thread(_delete_agent_sync, agent_id)

def _delete_agent_sync(agent_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def add_agent_knowledge_file(agent_id: str, file_id: str, filename: str, extracted_text: str):
    return await asyncio.to_thread(_add_agent_knowledge_file_sync, agent_id, file_id, filename, extracted_text)

def _add_agent_knowledge_file_sync(agent_id: str, file_id: str, filename: str, extracted_text: str):
    conn = _get_conn()
    row = conn.execute("SELECT knowledge_files FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return
    files = json.loads(row["knowledge_files"] or "[]")
    files.append({
        "file_id": file_id,
        "filename": filename,
        "extracted_text": extracted_text,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    conn.execute(
        "UPDATE agents SET knowledge_files = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(files), agent_id)
    )
    _commit_with_retry(conn)


async def add_agent_lead(agent_id: str, lead: dict) -> str:
    return await asyncio.to_thread(_add_agent_lead_sync, agent_id, lead)

def _add_agent_lead_sync(agent_id: str, lead: dict) -> str:
    import uuid
    lead_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agent_leads (agent_id, lead_id, name, phone, email, company) VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, lead_id, lead.get("name", "Unknown"), lead.get("phone", ""), lead.get("email", ""), lead.get("company", ""))
    )
    _commit_with_retry(conn)
    return lead_id


async def get_agent_leads(agent_id: str) -> list[dict]:
    return await asyncio.to_thread(_get_agent_leads_sync, agent_id)

def _get_agent_leads_sync(agent_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agent_leads WHERE agent_id = ? ORDER BY created_at DESC",
        (agent_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Campaign Cases ---

async def list_cases(role: str) -> list[dict]:
    return await asyncio.to_thread(_list_cases_sync, role)

def _list_cases_sync(role: str) -> list[dict]:
    """All cases for a role, newest first. Each row is a plain dict."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = ? ORDER BY active DESC, created_at DESC",
        (role,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = _row_to_dict(r)
        d["active"] = bool(d.get("active"))
        out.append(d)
    return out


from typing import Optional, Union, List

async def get_active_case(role: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_active_case_sync, role)

def _get_active_case_sync(role: str) -> Optional[dict]:
    """Return the (single) active case for a role, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = ? AND active = 1 LIMIT 1",
        (role,),
    ).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    d["active"] = True
    return d


async def add_case(role: str, name: str, description: str = "") -> int:
    return await asyncio.to_thread(_add_case_sync, role, name, description)

def _add_case_sync(role: str, name: str, description: str = "") -> int:
    """Insert a new case (inactive by default). Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO cases (role, name, description, active) VALUES (?, ?, ?, 0)",
        (role, name.strip(), description or ""),
    )
    _commit_with_retry(conn)
    return int(cur.lastrowid)


async def update_case(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    return await asyncio.to_thread(_update_case_sync, case_id, name, description)

def _update_case_sync(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    """Update a case's name and/or description. Returns True if a row changed."""
    conn = _get_conn()
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip())
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if not sets:
        return False
    sets.append("updated_at = datetime('now')")
    params.append(case_id)
    cur = conn.execute(
        f"UPDATE cases SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def delete_case(case_id: int) -> bool:
    return await asyncio.to_thread(_delete_case_sync, case_id)

def _delete_case_sync(case_id: int) -> bool:
    """Delete a case. Returns True if a row was removed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def set_active_case(role: str, case_id: Optional[int]) -> bool:
    return await asyncio.to_thread(_set_active_case_sync, role, case_id)

def _set_active_case_sync(role: str, case_id: Optional[int]) -> bool:
    """Activate exactly one case for ``role`` (or none if ``case_id`` is None).

    Always deactivates any currently-active case for that role first so the
    invariant "at most one active case per role" cannot be violated.
    """
    conn = _get_conn()
    conn.execute(
        "UPDATE cases SET active = 0, updated_at = datetime('now') "
        "WHERE role = ? AND active = 1",
        (role,),
    )
    if case_id is None:
        _commit_with_retry(conn)
        return True
    cur = conn.execute(
        "UPDATE cases SET active = 1, updated_at = datetime('now') "
        "WHERE id = ? AND role = ?",
        (case_id, role),
    )
    _commit_with_retry(conn)
    return cur.rowcount > 0


# --- Campaign Schedules ---

# Allowed status transitions:
#   scheduled -> running | cancelled | failed
#   running   -> completed | failed
# Anything else is a bug; we still let the row update but the API/UI never
# surfaces those transitions.
_SCHEDULE_VALID_STATUSES = {
    "scheduled", "running", "completed", "failed", "cancelled",
}


async def add_schedule(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    return await asyncio.to_thread(_add_schedule_sync, role, run_at, name, stop_at)

def _add_schedule_sync(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    """Schedule a campaign run for ``role`` at epoch ``run_at`` (UTC seconds).

    If ``stop_at`` (also epoch-UTC) is given, the worker auto-stops the campaign
    at that moment — useful for "run from 9 AM to 5 PM only" windows.

    Returns the new schedule id. ``name`` is an optional human label
    (e.g. "Friday morning blast").
    """
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO schedules (role, name, run_at, stop_at, status) "
        "VALUES (?, ?, ?, ?, 'scheduled')",
        (
            role,
            (name or "").strip(),
            float(run_at),
            float(stop_at) if stop_at is not None else None,
        ),
    )
    _commit_with_retry(conn)
    return int(cur.lastrowid)


# Column list re-used across SELECTs so adding fields stays a one-line change.
_SCHEDULE_COLS = (
    "id, role, name, run_at, stop_at, status, "
    "created_at, updated_at, started_at, error"
)


async def list_schedules(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_schedules_sync, role, limit)

def _list_schedules_sync(role: str, limit: int = 100) -> list[dict]:
    """All schedules for ``role``, soonest first (active/scheduled on top)."""
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE role = ? "
        "ORDER BY CASE status "
        "    WHEN 'running'   THEN 0 "
        "    WHEN 'scheduled' THEN 1 "
        "    ELSE 2 END, "
        "run_at ASC LIMIT ?",
        (role, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_schedule(schedule_id: int) -> dict | None:
    return await asyncio.to_thread(_get_schedule_sync, schedule_id)

def _get_schedule_sync(schedule_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def cancel_schedule(schedule_id: int) -> bool:
    return await asyncio.to_thread(_cancel_schedule_sync, schedule_id)

def _cancel_schedule_sync(schedule_id: int) -> bool:
    """Mark a *scheduled* (not-yet-started) run as cancelled. Returns True on success."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE schedules SET status = 'cancelled', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'scheduled'",
        (schedule_id,),
    )
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def mark_schedule_status(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    return await asyncio.to_thread(_mark_schedule_status_sync, schedule_id, status, error, started_at)

def _mark_schedule_status_sync(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    """Update a schedule's lifecycle status. Returns True if a row changed."""
    if status not in _SCHEDULE_VALID_STATUSES:
        return False
    def _do():
        conn = _get_conn()
        sets = ["status = ?", "updated_at = datetime('now')"]
        params: list = [status]
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if started_at is not None:
            sets.append("started_at = ?")
            params.append(float(started_at))
        params.append(schedule_id)
        cur = conn.execute(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        _commit_with_retry(conn)
        return cur.rowcount > 0
    return _run_db(_do)


async def due_schedules(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    return await asyncio.to_thread(_due_schedules_sync, now_epoch, lookahead_sec)

def _due_schedules_sync(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    """All schedules that are eligible to fire at ``now_epoch``.

    ``lookahead_sec`` is for callers that want to peek slightly in the future
    (e.g. to warn the user). The worker always passes 0.
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'scheduled' AND run_at <= ? ORDER BY run_at ASC",
        (float(now_epoch) + float(lookahead_sec),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def expired_running_schedules(now_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_expired_running_schedules_sync, now_epoch)

def _expired_running_schedules_sync(now_epoch: float) -> list[dict]:
    """All ``running`` schedules whose ``stop_at`` has passed.

    Used by the scheduler loop to enforce the auto-stop window even after a
    server restart (which would have orphaned the inline stop watcher).
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'running' AND stop_at IS NOT NULL AND stop_at <= ? "
        "ORDER BY stop_at ASC",
        (float(now_epoch),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Manual calls (console "Make a Call") ---


async def insert_manual_call(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    return await asyncio.to_thread(_insert_manual_call_sync, role, camp_id, to_phone, callee_name)

def _insert_manual_call_sync(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO manual_calls (role, camp_id, to_phone, callee_name, status)
        VALUES (?, ?, ?, ?, 'dialing')
        """,
        (role, camp_id, to_phone or "", callee_name or ""),
    )
    _commit_with_retry(conn)
    return int(cur.lastrowid)


async def mark_manual_call_failed(camp_id: str, message: str = "") -> None:
    return await asyncio.to_thread(_mark_manual_call_failed_sync, camp_id, message)

def _mark_manual_call_failed_sync(camp_id: str, message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE manual_calls SET status = 'failed', error = ?, updated_at = datetime('now')
        WHERE camp_id = ? AND status NOT IN ('completed', 'in_progress')
        """,
        ((message or "")[:2000], camp_id),
    )
    _commit_with_retry(conn)


async def mark_manual_call_in_progress(camp_id: str) -> None:
    return await asyncio.to_thread(_mark_manual_call_in_progress_sync, camp_id)


def _mark_manual_call_in_progress_sync(camp_id: str) -> None:
    def _do():
        conn = _get_conn()
        conn.execute(
            """
            UPDATE manual_calls SET status = 'in_progress', updated_at = datetime('now')
            WHERE camp_id = ? AND status = 'dialing'
            """,
            (camp_id,),
        )
        _commit_with_retry(conn)
    _run_db(_do)


async def manual_call_row_by_camp_id(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_manual_call_row_by_camp_id_sync, camp_id)

def _manual_call_row_by_camp_id_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE camp_id = ?", (camp_id,)).fetchone()
    return dict(row) if row else None


async def lead_row_by_call_id(call_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_lead_row_by_call_id_sync, call_id)

def _lead_row_by_call_id_sync(call_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE _call_id = ?", (call_id,)).fetchone()
    return _row_to_dict(row) if row else None


async def manual_call_exists_for_camp(camp_id: str) -> bool:
    return await asyncio.to_thread(_manual_call_exists_for_camp_sync, camp_id)

def _manual_call_exists_for_camp_sync(camp_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM manual_calls WHERE camp_id = ?", (camp_id,)).fetchone()
    return row is not None


async def finalize_manual_call_record(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    return await asyncio.to_thread(_finalize_manual_call_record_sync, camp_id, log_id, duration_sec, analysis)

def _finalize_manual_call_record_sync(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    def _do():
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, status FROM manual_calls WHERE camp_id = ?",
            (camp_id,),
        ).fetchone()
        if not row or (row["status"] or "") == "completed":
            return
        aj = json.dumps(analysis, ensure_ascii=False)
        conf = analysis.get("emotion_confidence")
        try:
            conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
        except (TypeError, ValueError):
            conf_f = None
        conn.execute(
            """
            UPDATE manual_calls SET
                log_id = ?,
                status = 'completed',
                ended_at = datetime('now'),
                duration_sec = ?,
                disposition = ?,
                summary = ?,
                next_steps = ?,
                emotion_label = ?,
                emotion_rationale = ?,
                emotion_confidence = ?,
                analysis_json = ?,
                updated_at = datetime('now')
            WHERE camp_id = ?
            """,
            (
            log_id or "",
            duration_sec,
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            camp_id,
        ),
        )
        _commit_with_retry(conn)
    _run_db(_do)


async def update_manual_call_analysis_by_id(call_id: int, analysis: dict[str, Any]) -> bool:
    return await asyncio.to_thread(_update_manual_call_analysis_by_id_sync, call_id, analysis)

def _update_manual_call_analysis_by_id_sync(call_id: int, analysis: dict[str, Any]) -> bool:
    """Rewrite analyzer fields on a manual_calls row (e.g. Re-analyze button)."""
    def _do():
        conn = _get_conn()
        row = conn.execute("SELECT id FROM manual_calls WHERE id = ?", (int(call_id),)).fetchone()
        if not row:
            return False
        aj = json.dumps(analysis, ensure_ascii=False)
        conf = analysis.get("emotion_confidence")
        try:
            conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
        except (TypeError, ValueError):
            conf_f = None
        conn.execute(
            """
            UPDATE manual_calls SET
                status = ?,
                disposition = ?,
                summary = ?,
                next_steps = ?,
                emotion_label = ?,
                emotion_rationale = ?,
                emotion_confidence = ?,
                analysis_json = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                str(analysis.get("disposition") or ""),
                str(analysis.get("summary") or ""),
                str(analysis.get("next_steps") or ""),
                str(analysis.get("emotion_label") or ""),
                str(analysis.get("emotion_rationale") or ""),
                conf_f,
                aj,
                int(call_id),
            ),
        )
        _commit_with_retry(conn)
        return True
    return _run_db(_do)

async def get_manual_call_by_id(call_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_manual_call_by_id_sync, call_id)


def _get_manual_call_by_id_sync(call_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE id = ?", (int(call_id),)).fetchone()
    return dict(row) if row else None


async def update_manual_call_disposition(call_id: int, disposition: str) -> bool:
    """Set the disposition column for a manual call (e.g. Interested / Not Interested)."""
    return await asyncio.to_thread(_update_manual_call_disposition_sync, call_id, disposition)


def _update_manual_call_disposition_sync(call_id: int, disposition: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT id FROM manual_calls WHERE id = ?", (int(call_id),)).fetchone()
    if not row:
        return False
    conn.execute(
        """
        UPDATE manual_calls SET
            disposition = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (str(disposition).strip(), int(call_id)),
    )
    _commit_with_retry(conn)
    return True


async def list_recent_manual_calls(role: str, limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(_list_recent_manual_calls_sync, role, limit)

def _list_recent_manual_calls_sync(role: str, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 50))
    rows = conn.execute(
        """
        SELECT * FROM manual_calls WHERE role = ?
        ORDER BY id DESC LIMIT ?
        """,
        (role, lim),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Helpers ---

def _row_to_dict(row: sqlite3.Row) -> dict:
    out = {key: row[key] for key in row.keys()}
    # Decode the leads.extra JSON blob so callers get a normal dict and can
    # use it as ``lead["extra"]["rfq_subject"]`` without re-parsing.
    raw = out.get("extra")
    if raw is not None and isinstance(raw, str):
        if raw.strip():
            try:
                parsed = json.loads(raw)
                out["extra"] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                out["extra"] = {}
        else:
            out["extra"] = {}
    return out


def migrate_from_json(data_dir: Path = None) -> dict:
    """One-time migration from JSON files to SQLite. Returns migration summary."""
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    migrated = {"roles": 0, "leads": 0, "agents": 0}

    # Migrate role states
    for role in (
        "data_edge",
    ):
        json_path = base / role / "state.json"
        if json_path.exists():
            try:
                with open(json_path) as f:
                    data = json.load(f)
                _save_role_state_sync(
                    role,
                    prompt=data.get("prompt", ""),
                    rag=data.get("rag", ""),
                    vobiz_config=data.get("vobiz", {}),
                    delay_sec=data.get("delay_sec", default_inter_call_gap_sec(role)),
                )
                migrated["roles"] += 1
            except Exception as e:
                logger.warning(f"Failed to migrate role state for {role}: {e}")

    # Migrate sandbox agents
    agents_json = Path(__file__).resolve().parent.parent / "sandbox" / "agents.json"
    if agents_json.exists():
        try:
            with open(agents_json) as f:
                agents = json.load(f)
            for agent in agents:
                agent_id = _create_agent_sync(
                    name=agent.get("name", "Unnamed"),
                    prompt=agent.get("prompt", ""),
                    voice=agent.get("voice", "Puck"),
                )
                for kf in agent.get("knowledge_files", []):
                    _add_agent_knowledge_file_sync(
                        agent_id,
                        kf.get("file_id", "unknown"),
                        kf.get("filename", "unknown"),
                        kf.get("extracted_text", ""),
                    )
                for lead in agent.get("leads", []):
                    _add_agent_lead_sync(agent_id, lead)
                migrated["agents"] += 1
        except Exception as e:
            logger.warning(f"Failed to migrate agents: {e}")

    logger.info(f"Migration complete: {migrated}")
    return migrated


async def reschedule_leads_by_outcome(role: str, from_time: float | None, to_time: float | None, categories: list[str], reschedule_time: float) -> int:
    return await asyncio.to_thread(_reschedule_leads_by_outcome_sync, role, from_time, to_time, categories, reschedule_time)


def _reschedule_leads_by_outcome_sync(role: str, from_time: float | None, to_time: float | None, categories: list[str], reschedule_time: float) -> int:
    conn = _get_conn()
    
    # 1. Fetch called leads for this role
    query = "SELECT * FROM leads WHERE role = ? AND (start_time IS NOT NULL OR _log_id IS NOT NULL)"
    params = [role]
    if from_time is not None:
        query += " AND start_time >= ?"
        params.append(from_time)
    if to_time is not None:
        query += " AND start_time <= ?"
        params.append(to_time)
        
    cursor = conn.execute(query, tuple(params))
    rows = cursor.fetchall()
    
    # 2. Filter matching leads based on categories
    target_ids = []
    
    from core.campaign_payload import enrich_lead_for_console
    
    for row in rows:
        lead = dict(row)
        enriched = enrich_lead_for_console(lead)
        dispo = enriched.get("disposition") or ""
        status = (enriched.get("status") or "").lower()
        error = enriched.get("error")
        duration = float(enriched.get("duration_sec") or 0.0)
        
        is_failed_val = status == 'failed' or status == 'error' or status == 'no answer' or bool(error)
        is_interested_val = dispo == 'Interested'
        is_not_interested_val = dispo == 'Not Interested'
        is_cut_middle_val = (status == 'completed') and (duration < 15) and (not is_interested_val) and (not is_not_interested_val)
        
        matched = False
        if "failed" in categories and is_failed_val:
            matched = True
        if "interested" in categories and is_interested_val:
            matched = True
        if "not_interested" in categories and is_not_interested_val:
            matched = True
        if "cut_middle" in categories and is_cut_middle_val:
            matched = True
            
        if matched:
            target_ids.append(lead["id"])
            
    if not target_ids:
        return 0
        
    # 3. Update leads to callback_scheduled
    updated_count = 0
    for lid in target_ids:
        cur = conn.execute("SELECT analysis FROM leads WHERE id = ?", (lid,))
        row = cur.fetchone()
        analysis_dict = {}
        if row and row[0]:
            try:
                analysis_dict = json.loads(row[0])
            except Exception:
                pass
                
        analysis_dict["callback_reminder_epoch"] = reschedule_time
        analysis_dict["disposition"] = "Callback Scheduled"
        analysis_dict["summary"] = "Rescheduled by operator."
        
        conn.execute(
            """
            UPDATE leads
               SET status = 'callback_scheduled',
                   error = NULL,
                   _log_id = NULL,
                   _call_id = NULL,
                   start_time = NULL,
                   analysis = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (json.dumps(analysis_dict), lid)
        )
        updated_count += 1
        
    _commit_with_retry(conn)
    return updated_count


# ---------------------------------------------------------------------------
# Interested Leads Feedback CRUD
# ---------------------------------------------------------------------------

async def add_feedback(name: str, contact_number: str, lead_status: str, custom_status: str = "", feedback_notes: str = "") -> int:
    return await asyncio.to_thread(_add_feedback_sync, name, contact_number, lead_status, custom_status, feedback_notes)

def _add_feedback_sync(name: str, contact_number: str, lead_status: str, custom_status: str = "", feedback_notes: str = "") -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO interested_leads_feedback (name, contact_number, lead_status, custom_status, feedback_notes) "
        "VALUES (?, ?, ?, ?, ?)",
        ((name or "").strip(), (contact_number or "").strip(), (lead_status or "").strip(), (custom_status or "").strip(), (feedback_notes or "").strip()),
    )
    _commit_with_retry(conn)
    return int(cur.lastrowid)


async def update_feedback(row_id: int, name: str = None, contact_number: str = None, lead_status: str = None, custom_status: str = None, feedback_notes: str = None) -> bool:
    return await asyncio.to_thread(_update_feedback_sync, row_id, name, contact_number, lead_status, custom_status, feedback_notes)

def _update_feedback_sync(row_id: int, name: str = None, contact_number: str = None, lead_status: str = None, custom_status: str = None, feedback_notes: str = None) -> bool:
    conn = _get_conn()
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip())
    if contact_number is not None:
        sets.append("contact_number = ?")
        params.append(contact_number.strip())
    if lead_status is not None:
        sets.append("lead_status = ?")
        params.append(lead_status.strip())
    if custom_status is not None:
        sets.append("custom_status = ?")
        params.append(custom_status.strip())
    if feedback_notes is not None:
        sets.append("feedback_notes = ?")
        params.append(feedback_notes.strip())
    if not sets:
        return False
    sets.append("updated_at = datetime('now')")
    params.append(row_id)
    cur = conn.execute(
        f"UPDATE interested_leads_feedback SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def delete_feedback(row_id: int) -> bool:
    return await asyncio.to_thread(_delete_feedback_sync, row_id)

def _delete_feedback_sync(row_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM interested_leads_feedback WHERE id = ?", (row_id,))
    _commit_with_retry(conn)
    return cur.rowcount > 0


async def get_feedback(row_id: int) -> dict | None:
    return await asyncio.to_thread(_get_feedback_sync, row_id)

def _get_feedback_sync(row_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM interested_leads_feedback WHERE id = ?", (row_id,)).fetchone()
    return _row_to_dict(row) if row else None


async def list_feedback(
    search: str = "",
    status_filter: str = "",
    sort_by: str = "created_at",
    sort_dir: str = "DESC",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    return await asyncio.to_thread(_list_feedback_sync, search, status_filter, sort_by, sort_dir, page, page_size)

def _list_feedback_sync(
    search: str = "",
    status_filter: str = "",
    sort_by: str = "created_at",
    sort_dir: str = "DESC",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    conn = _get_conn()
    base_q = "FROM interested_leads_feedback WHERE 1=1"
    params: list = []

    search = (search or "").strip()
    if search:
        base_q += " AND (name LIKE ? OR contact_number LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])

    status_filter = (status_filter or "").strip()
    if status_filter:
        base_q += " AND lead_status = ?"
        params.append(status_filter)

    # Validate sort column
    allowed_sorts = {"id", "name", "contact_number", "lead_status", "created_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "created_at"
    sort_dir = "ASC" if (sort_dir or "").upper() == "ASC" else "DESC"

    count_row = conn.execute(f"SELECT COUNT(*) AS c {base_q}", params).fetchone()
    total = int(count_row["c"]) if count_row else 0

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    offset = (page - 1) * page_size
    total_pages = max(1, -(-total // page_size))  # ceil division

    rows = conn.execute(
        f"SELECT * {base_q} ORDER BY {sort_by} {sort_dir} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    items = [_row_to_dict(r) for r in rows]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


async def export_all_feedback() -> list[dict]:
    return await asyncio.to_thread(_export_all_feedback_sync)

def _export_all_feedback_sync() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT name, contact_number, lead_status, custom_status, feedback_notes, created_at, updated_at "
        "FROM interested_leads_feedback ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# WhatsApp message log CRUD
# ---------------------------------------------------------------------------

async def log_whatsapp_message(
    lead_id: int,
    phone: str,
    role: str,
    message_type: str = "project_details",
    status: str = "Pending",
    provider: str = "",
    error: str = "",
    analysis_summary: str = "",
) -> int:
    """Insert a new row into whatsapp_message_log. Returns the row id."""
    def _insert():
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO whatsapp_message_log
                (lead_id, phone, role, message_type, status, provider, error, analysis_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, phone, role, message_type, status, provider, error, analysis_summary[:500]),
        )
        _commit_with_retry(conn)
        return int(cur.lastrowid)
    return await asyncio.to_thread(_insert)


async def update_whatsapp_message_status(
    message_id: int,
    status: str,
    error: str = "",
) -> None:
    """Update the status (and optional error) of a whatsapp_message_log row."""
    def _update():
        conn = _get_conn()
        conn.execute(
            "UPDATE whatsapp_message_log SET status = ?, error = ? WHERE id = ?",
            (status, error[:500] if error else "", message_id),
        )
        _commit_with_retry(conn)
    return await asyncio.to_thread(_update)


async def get_whatsapp_logs_for_lead(lead_id: int) -> list[dict]:
    """Return all WhatsApp message log entries for a given lead, newest first."""
    def _query():
        conn = _get_conn()
        cur = conn.execute(
            "SELECT * FROM whatsapp_message_log WHERE lead_id = ? ORDER BY id DESC",
            (lead_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return await asyncio.to_thread(_query)


async def get_whatsapp_logs_by_role(
    role: str,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return WhatsApp message log entries for a role, optionally filtered by status."""
    def _query():
        conn = _get_conn()
        if status:
            cur = conn.execute(
                "SELECT * FROM whatsapp_message_log WHERE role = ? AND status = ? ORDER BY id DESC LIMIT ?",
                (role, status, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM whatsapp_message_log WHERE role = ? ORDER BY id DESC LIMIT ?",
                (role, limit),
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return await asyncio.to_thread(_query)
