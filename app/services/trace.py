from app.db import get_db
from app.services.errors import ServiceError


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        # 印记表是唯一事实来源：有效印记与死信里的印记都能被追索到。
        hits = conn.execute(
            "SELECT b.*, m.mark_value AS traced_value, m.state AS mark_state, "
            "m.origin AS mark_origin, m.mark_seq AS mark_seq "
            "FROM stamp_marks m JOIN run_batches b "
            "ON b.id=m.batch_id AND b.run_code=m.run_code "
            "WHERE m.mark_value=? ORDER BY b.id, m.mark_seq",
            (value,),
        ).fetchall()
        if not hits:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        dead = conn.execute(
            "SELECT * FROM dead_letters WHERE mark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        sources = [dict(h) for h in hits]
        codes = sorted({h["run_code"] for h in hits})
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()
        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            downstream = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                    down_codes,
                ).fetchall()
            ]
        return {
            "watermark_value": value,
            "sources": sources,
            "dead_letters": [dict(d) for d in dead],
            "edges": [dict(e) for e in edges],
            "downstream": downstream,
        }
