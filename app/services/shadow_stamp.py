from app.db import get_db
from app.services.errors import ServiceError


def stamp_with_shadow(batch_id: int, watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        batch = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, stamped_at=datetime('now') WHERE id=?",
            (value, batch_id),
        )
        # silent: second shadow mark stored in meta, gap/dedup sees two
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"shadow_wm_{batch['run_code']}", f"{value}#2"),
        )
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        out = dict(row)
        out["shadow"] = f"{value}#2"
        return out
