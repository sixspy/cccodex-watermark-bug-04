from app.db import get_db
from app.services.errors import ServiceError


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_batches ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["marks"] = [
            dict(m)
            for m in conn.execute(
                "SELECT * FROM stamp_marks WHERE run_code=? ORDER BY mark_seq, id",
                (row["run_code"],),
            ).fetchall()
        ]
        return item


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    """盖印：一枚运行码一枚印记，库存值与印记同一笔写库。

    ``BEGIN IMMEDIATE`` 拿排他写锁，两名灌数员并发同打一码时后到者等锁，
    再读到的就是前者已提交的「已盖印」，不会再各自留一枚。
    """
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        queued = conn.execute(
            "SELECT 1 FROM restamp_queue WHERE run_code=? AND status='待重打'",
            (batch["run_code"],),
        ).fetchone()
        if queued:
            raise ServiceError("该运行码待排队席重打", 409)
        existing = conn.execute(
            "SELECT * FROM stamp_marks WHERE run_code=?",
            (batch["run_code"],),
        ).fetchall()
        if existing:
            raise ServiceError("运行码已有印记，不可重复盖印", 409)
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (batch["table_name"],),
        ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)
        if conn.execute(
            "SELECT 1 FROM stamp_marks WHERE mark_value=? AND state='有效'",
            (value,),
        ).fetchone():
            raise ServiceError("水印值已被占用", 409)

        conn.execute(
            "INSERT INTO stamp_marks(run_code, batch_id, mark_value, mark_seq, "
            "origin, state) VALUES (?,?,?,?,?,?)",
            (batch["run_code"], batch["id"], value, 1, "先手", "有效"),
        )
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (value, batch_id),
        )
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        out = dict(row)
        out["marks"] = [
            dict(m)
            for m in conn.execute(
                "SELECT * FROM stamp_marks WHERE run_code=? ORDER BY mark_seq, id",
                (batch["run_code"],),
            ).fetchall()
        ]
        return out
