"""SQLite 存储层。单文件数据库，挂在容器卷上。"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "./data"), "longhu.db")
_lock = threading.Lock()
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    token         TEXT    NOT NULL,
    user_agent    TEXT,
    api_key       TEXT,
    dx_token      TEXT,
    dx_source     TEXT NOT NULL DEFAULT '5' CHECK (dx_source IN ('1','2','3','5')),
    bu_code       TEXT DEFAULT 'C20400',
    channel       TEXT DEFAULT 'C2',
    growth_value  INTEGER DEFAULT 0,
    level_name    TEXT DEFAULT '',
    balance       REAL DEFAULT 0,
    nickname      TEXT DEFAULT '',
    expire_at     TEXT,
    last_run_at   TEXT,
    last_status   TEXT DEFAULT '',
    last_message  TEXT DEFAULT '',
    app_last_status TEXT DEFAULT '',
    app_last_message TEXT DEFAULT '',
    app_last_run_at TEXT,
    enabled       INTEGER DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER,
    account_name TEXT,
    ts          TEXT,
    action      TEXT,
    status      TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_acc ON logs(account_id, id DESC);
"""

DEFAULT_SETTINGS = {
    "checkin_time": "08:00",
    "checkin_jitter": "60",
    "last_run_date": "",
    "retry_at": "",
    "retry_count": "0",
    "webhook_url": "",
    "notification_provider": "webhook",
    "pushplus_token": "",
    "auto_refresh": "1",
    # APP 专享入口怎么处理：auto（默认）/ manual（只查状态、未签提醒）/ off。
    # 前提是账号里配了真实采集的设备标识，并保留实际来源，否则跳过提交。
    "app_checkin_mode": "auto",
}


def now_iso() -> str:
    return datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")


def business_date() -> str:
    """签到业务按北京时间换日，不依赖宿主机时区。"""
    return datetime.now(BUSINESS_TZ).date().isoformat()


def run_date(value: str | None) -> str:
    """将已存执行时间转换为业务日期；旧的无时区时间按北京时间处理。"""
    try:
        parsed = datetime.fromisoformat(value or "")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BUSINESS_TZ)
        return parsed.astimezone(BUSINESS_TZ).date().isoformat()
    except (ValueError, TypeError):
        return ""


@contextmanager
def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )


# 建表之后新增的列：老库用 ALTER TABLE 补，新库由 SCHEMA 直接带上
MIGRATIONS = {
    "accounts": {
        "dx_token": "ALTER TABLE accounts ADD COLUMN dx_token TEXT",
        "dx_source": "ALTER TABLE accounts ADD COLUMN dx_source TEXT NOT NULL DEFAULT '5' "
                     "CHECK (dx_source IN ('1','2','3','5'))",
        "app_last_status": "ALTER TABLE accounts ADD COLUMN app_last_status TEXT DEFAULT ''",
        "app_last_message": "ALTER TABLE accounts ADD COLUMN app_last_message TEXT DEFAULT ''",
        "app_last_run_at": "ALTER TABLE accounts ADD COLUMN app_last_run_at TEXT",
    },
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(ddl)


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def list_accounts(only_enabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM accounts"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql)]


def get_account(account_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None


def _account_with_token(conn: sqlite3.Connection, token: str,
                        exclude_id: int | None = None) -> dict | None:
    query = "SELECT * FROM accounts WHERE token = ?"
    values = [token]
    if exclude_id is not None:
        query += " AND id <> ?"
        values.append(exclude_id)
    row = conn.execute(query + " ORDER BY id LIMIT 1", values).fetchone()
    return dict(row) if row else None


def account_with_token(token: str, exclude_id: int | None = None) -> dict | None:
    """查找相同登录凭据的记录，不在错误或日志中回显凭据。"""
    with connect() as conn:
        return _account_with_token(conn, token, exclude_id)


def create_account(data: dict) -> int:
    ts = now_iso()
    with connect() as conn:
        # 检查和写入属于同一写事务；并发导入不能各自通过检查后重复创建。
        conn.execute("BEGIN IMMEDIATE")
        if _account_with_token(conn, data["token"]):
            raise ValueError("该账号已经存在，请更新原账号凭据")
        cur = conn.execute(
            """INSERT INTO accounts
               (name, token, user_agent, api_key, dx_token, dx_source, bu_code, channel,
                growth_value, level_name, balance, nickname,
                expire_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["name"], data["token"], data.get("user_agent"), data.get("api_key"),
                data.get("dx_token"),
                data.get("dx_source") or "5",
                data.get("bu_code", "C20400"), data.get("channel", "C2"),
                data.get("growth_value", 0), data.get("level_name", ""),
                data.get("balance", 0), data.get("nickname", ""),
                data.get("expire_at"), ts, ts,
            ),
        )
        return cur.lastrowid


def execution_reset_fields() -> dict:
    return {"last_status": "", "last_message": "", "last_run_at": None,
            "app_last_status": "", "app_last_message": "", "app_last_run_at": None}


def update_account(account_id: int, **fields):
    if not fields:
        return
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if "token" in fields and _account_with_token(conn, fields["token"], account_id):
            raise ValueError("该账号已经存在，请更新原账号凭据")
        if "dx_token" in fields or "dx_source" in fields:
            previous = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if previous and any((fields[key] or None) != (previous[key] or None)
                                for key in ("dx_token", "dx_source") if key in fields):
                # 同一标识重复保存不解除当日验证保护；更换真实设备后允许重新核验。
                fields.update({key: value for key, value in execution_reset_fields().items()
                               if key in previous.keys()})
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE accounts SET {sets} WHERE id = ?",
            (*fields.values(), account_id),
        )


def delete_account(account_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM logs WHERE account_id = ?", (account_id,))


def dx_usage() -> dict[str, list[dict]]:
    """按设备指纹聚合账号，用于检查是否有多账号共用同一指纹。

    共用标识时先暂停后加入账号，避免把来源不明的设备配置用于提交。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, enabled, dx_token FROM accounts "
            "WHERE dx_token IS NOT NULL AND TRIM(dx_token) <> '' ORDER BY id"
        ).fetchall()
    usage: dict[str, list[dict]] = {}
    for row in rows:
        usage.setdefault(row["dx_token"].strip(), []).append(dict(row))
    return usage


def accounts_with_dx(dx_token: str, exclude_id: int | None = None) -> list[dict]:
    """返回使用了指定指纹的其他账号（按 id 升序）。"""
    token = (dx_token or "").strip()
    if not token:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, enabled FROM accounts "
            "WHERE dx_token IS NOT NULL AND TRIM(dx_token) = ? ORDER BY id",
            (token,),
        ).fetchall()
    return [dict(row) for row in rows if row["id"] != exclude_id]


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------

def add_log(account_id: int | None, account_name: str, action: str,
            status: str, message: str):
    with connect() as conn:
        conn.execute(
            "INSERT INTO logs(account_id, account_name, ts, action, status, message) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, account_name, now_iso(), action, status, message),
        )


def list_logs(account_id: int | None = None, limit: int = 50) -> list[dict]:
    with connect() as conn:
        if account_id:
            rows = conn.execute(
                "SELECT * FROM logs WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, limit),
            )
        else:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_settings() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = {row["key"]: row["value"] for row in rows}
    for key, value in DEFAULT_SETTINGS.items():
        data.setdefault(key, value)
    return data


def save_settings(values: dict):
    with connect() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
