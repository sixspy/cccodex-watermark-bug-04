import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout/busy_timeout: wait for the writer lock instead of failing at once.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    """Open a connection as a unit of work.

    ``immediate=True`` takes SQLite's write lock up front (``BEGIN IMMEDIATE``)
    so two concurrent stampers cannot both read "待盖印" and then each write a
    mark. The second writer waits, then re-reads the committed state.
    """
    conn = connect(db_path)
    try:
        if immediate:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 印记是运行码盖印的唯一事实来源；run_batches.watermark_value 与它同源。
CREATE TABLE IF NOT EXISTS stamp_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_code TEXT NOT NULL,
  batch_id INTEGER NOT NULL,
  mark_value TEXT NOT NULL,
  mark_seq INTEGER NOT NULL CHECK (mark_seq IN (1,2)),
  origin TEXT NOT NULL CHECK (origin IN ('先手','后手','重打')),
  state TEXT NOT NULL CHECK (state IN ('有效','冲突待处置')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(run_code, mark_seq)
);

-- 运行码的唯一有效印记由库内约束兜底：任何时刻只允许一枚。
CREATE UNIQUE INDEX IF NOT EXISTS ux_stamp_marks_live
  ON stamp_marks(run_code) WHERE state = '有效';

-- 运行码必须与库存行同源，双影之外的行一律拒绝落库。
CREATE TRIGGER IF NOT EXISTS trg_stamp_marks_same_source_insert
BEFORE INSERT ON stamp_marks
WHEN NOT EXISTS (
  SELECT 1 FROM run_batches b
  WHERE b.id = NEW.batch_id AND b.run_code = NEW.run_code
)
BEGIN
  SELECT RAISE(ABORT, '运行码与库存行不同源');
END;

CREATE TRIGGER IF NOT EXISTS trg_stamp_marks_same_source_update
BEFORE UPDATE OF run_code, batch_id ON stamp_marks
WHEN NOT EXISTS (
  SELECT 1 FROM run_batches b
  WHERE b.id = NEW.batch_id AND b.run_code = NEW.run_code
)
BEGIN
  SELECT RAISE(ABORT, '运行码与库存行不同源');
END;

-- 整笔回滚的行原样落在死信，payload 保留回滚前的完整字段。
CREATE TABLE IF NOT EXISTS dead_letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_code TEXT NOT NULL,
  batch_id INTEGER NOT NULL,
  mark_value TEXT NOT NULL,
  mark_seq INTEGER NOT NULL,
  origin TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dedup_progress (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_code TEXT NOT NULL UNIQUE,
  total_marks INTEGER NOT NULL DEFAULT 0,
  kept_marks INTEGER NOT NULL DEFAULT 0,
  dead_letters INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('停等','已完成','待重打')),
  reason TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS restamp_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_code TEXT NOT NULL,
  batch_id INTEGER NOT NULL,
  target_value TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重打','已重打')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  done_at TEXT
);

CREATE TABLE IF NOT EXISTS merge_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_code TEXT NOT NULL,
  strategy TEXT NOT NULL,
  outcome TEXT NOT NULL,
  kept_mark_value TEXT,
  dead_count INTEGER NOT NULL,
  queue_id INTEGER,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
    # 库存里已经出现的双影：同一运行码两枚印记。
    ("dw_payments_d", "pay-20261115-01", "2026-11-15", "已盖印", "WM-PAY-151-01"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]

# 双影两枚：先手与后手，等待冲突目录处置。
SEED_CONFLICT_MARKS = [
    ("pay-20261115-01", 1, "先手", "WM-PAY-151-01"),
    ("pay-20261115-01", 2, "后手", "WM-PAY-151-02"),
]


def _insert_mark(conn, run_code: str, batch_id: int, value: str, seq: int,
                 origin: str, state: str) -> int:
    cur = conn.execute(
        "INSERT INTO stamp_marks(run_code, batch_id, mark_value, mark_seq, origin, state) "
        "VALUES (?,?,?,?,?,?)",
        (run_code, batch_id, value, seq, origin, state),
    )
    return int(cur.lastrowid)


def _adopt_legacy_shadows(conn) -> None:
    """把旧版 meta 里的隐形第二枚印记搬进印记表，并标成双影待处置。

    只搬运、不改计数；真正删除发生在冲突处置的同一笔写库里。
    """
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'shadow_wm_%'"
    ).fetchall()
    for row in rows:
        run_code = row["key"][len("shadow_wm_"):]
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?", (run_code,)
        ).fetchone()
        if not batch:
            conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))
            continue
        existing = conn.execute(
            "SELECT * FROM stamp_marks WHERE run_code=? ORDER BY mark_seq",
            (run_code,),
        ).fetchall()
        if len(existing) >= 2:
            conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))
            continue
        if existing:
            primary = existing[0]
            if primary["mark_seq"] != 1:
                # 只剩序 2 的残行：搬回序 1，让双影行序稳定。
                conn.execute(
                    "DELETE FROM stamp_marks WHERE id=?", (primary["id"],)
                )
                _insert_mark(
                    conn,
                    run_code,
                    batch["id"],
                    primary["mark_value"],
                    1,
                    primary["origin"],
                    "冲突待处置",
                )
                primary = conn.execute(
                    "SELECT * FROM stamp_marks WHERE run_code=? AND mark_seq=1",
                    (run_code,),
                ).fetchone()
        else:
            value = batch["watermark_value"] or row["value"].split("#", 1)[0]
            _insert_mark(
                conn, run_code, batch["id"], value, 1, "先手", "冲突待处置"
            )
            primary = conn.execute(
                "SELECT * FROM stamp_marks WHERE run_code=? AND mark_seq=1",
                (run_code,),
            ).fetchone()
        conn.execute(
            "UPDATE stamp_marks SET state='冲突待处置' WHERE id=?",
            (primary["id"],),
        )
        _insert_mark(
            conn,
            run_code,
            batch["id"],
            row["value"],
            2,
            "后手",
            "冲突待处置",
        )
        conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))


def _backfill_marks(conn) -> None:
    """给已盖印但还没有印记行的库存行补一枚先手印记。"""
    rows = conn.execute(
        "SELECT b.* FROM run_batches b "
        "WHERE b.status='已盖印' AND b.watermark_value IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM stamp_marks m WHERE m.run_code=b.run_code)"
    ).fetchall()
    for batch in rows:
        _insert_mark(
            conn,
            batch["run_code"],
            batch["id"],
            batch["watermark_value"],
            1,
            "先手",
            "有效",
        )


def _ensure_conflict_progress(conn) -> None:
    """双影没有处置进度时补一条停等记录，供去重页展示。"""
    rows = conn.execute(
        "SELECT run_code, COUNT(*) AS c FROM stamp_marks "
        "WHERE state='冲突待处置' GROUP BY run_code"
    ).fetchall()
    for row in rows:
        if row["c"] < 2:
            continue
        existing = conn.execute(
            "SELECT id FROM dedup_progress WHERE run_code=?", (row["run_code"],)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO dedup_progress(run_code, total_marks, kept_marks, dead_letters, "
            "status, reason) VALUES (?,?,?,?,?,?)",
            (
                row["run_code"],
                row["c"],
                0,
                0,
                "停等",
                "双影待处置·死信因待定",
            ),
        )


def migrate_marks(conn) -> None:
    _adopt_legacy_shadows(conn)
    _backfill_marks(conn)
    _ensure_conflict_progress(conn)


def init_db(db_path: str | None = None) -> None:
    # executescript 自带 COMMIT，故 DDL 之后另行显式拿写锁再播种/迁移。
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        seeded = conn.execute(
            "SELECT value FROM meta WHERE key='seeded'"
        ).fetchone()
        if not seeded:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
            for run_code, seq, origin, value in SEED_CONFLICT_MARKS:
                batch = conn.execute(
                    "SELECT * FROM run_batches WHERE run_code=?", (run_code,)
                ).fetchone()
                _insert_mark(
                    conn, run_code, batch["id"], value, seq, origin, "冲突待处置"
                )
            conn.execute(
                "INSERT INTO dedup_progress(run_code, total_marks, kept_marks, dead_letters, "
                "status, reason) VALUES (?,?,?,?,?,?)",
                (
                    "pay-20261115-01",
                    2,
                    0,
                    0,
                    "停等",
                    "双影待处置·死信因待定",
                ),
            )
            migrate_marks(conn)
        else:
            migrate_marks(conn)
