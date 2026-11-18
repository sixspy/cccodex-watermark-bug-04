"""双影（同一运行码两枚印记）的处置台。

印记是盖印的唯一事实来源：``stamp_marks`` 里一个运行码任一时刻最多一枚
``有效`` 印记（库内部分唯一索引兜底），``run_batches.watermark_value``
始终与该有效印记同值同源。双影出现时差额台与去重同时停等，处置只有两种
落库命运：

* ``keep_first``  —— 留存先手印记，后手行整笔回滚进死信；
* ``rollback_all`` —— 两枚一并整笔回滚进死信，交排队席重打唯一印记。

两种处置都在**同一个写事务**里完成，失败只回一句短话，不留半截状态。
"""

import json

from app.db import get_db
from app.services.errors import ServiceError

STRATEGIES = {
    "keep_first": "留存先手",
    "rollback_all": "整笔回滚·待重打",
}

PENDING_REASON = "双影待处置·死信因待定"


# ---------------------------------------------------------------- 读视图


def _mark_dict(conn, row) -> dict:
    item = dict(row)
    batch = conn.execute(
        "SELECT * FROM run_batches WHERE id=?", (row["batch_id"],)
    ).fetchone()
    item["batch_run_code"] = batch["run_code"] if batch else None
    item["table_name"] = batch["table_name"] if batch else None
    item["biz_date"] = batch["biz_date"] if batch else None
    item["source_ok"] = bool(batch) and batch["run_code"] == row["run_code"]
    return item


def _marks_for(conn, run_code: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stamp_marks WHERE run_code=? ORDER BY mark_seq, id",
        (run_code,),
    ).fetchall()
    return [_mark_dict(conn, r) for r in rows]


def _progress_for(conn, run_code: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM dedup_progress WHERE run_code=?", (run_code,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["dead_letter_total"] = conn.execute(
        "SELECT COUNT(*) AS c FROM dead_letters WHERE run_code=?", (run_code,)
    ).fetchone()["c"]
    return item


def _latest_outcome(conn, run_code: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM merge_outcomes WHERE run_code=? ORDER BY id DESC LIMIT 1",
        (run_code,),
    ).fetchone()
    return dict(row) if row else None


def list_marks(run_code: str | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if run_code:
            return _marks_for(conn, run_code)
        rows = conn.execute(
            "SELECT * FROM stamp_marks ORDER BY run_code, mark_seq, id"
        ).fetchall()
        return [_mark_dict(conn, r) for r in rows]


def list_conflicts(db_path: str | None = None) -> list[dict]:
    """冲突目录：待处置的双影在前，已处置的仍留档以便亮出保留芯片。"""
    with get_db(db_path) as conn:
        pending_codes = [
            r["run_code"]
            for r in conn.execute(
                "SELECT run_code FROM stamp_marks "
                "GROUP BY run_code HAVING COUNT(*) > 1 "
                "OR SUM(CASE WHEN state='冲突待处置' THEN 1 ELSE 0 END) > 0 "
                "ORDER BY run_code"
            ).fetchall()
        ]
        resolved_codes = [
            r["run_code"]
            for r in conn.execute(
                "SELECT DISTINCT run_code FROM merge_outcomes "
                "WHERE run_code NOT IN ("
                " SELECT run_code FROM stamp_marks GROUP BY run_code "
                " HAVING COUNT(*) > 1"
                ") ORDER BY run_code"
            ).fetchall()
        ]
        out = []
        for code, pending in [(c, True) for c in pending_codes] + [
            (c, False) for c in resolved_codes if c not in pending_codes
        ]:
            batch = conn.execute(
                "SELECT * FROM run_batches WHERE run_code=?", (code,)
            ).fetchone()
            marks = _marks_for(conn, code)
            live = [m for m in marks if m["state"] == "有效"]
            out.append(
                {
                    "run_code": code,
                    "batch_id": batch["id"] if batch else None,
                    "table_name": batch["table_name"] if batch else None,
                    "biz_date": batch["biz_date"] if batch else None,
                    "stock_value": batch["watermark_value"] if batch else None,
                    "status": batch["status"] if batch else None,
                    "mark_count": len(marks),
                    "live_mark": live[0]["mark_value"] if live else None,
                    "kept_chip": live[0]["mark_value"] if live else None,
                    "source_ok": all(m["source_ok"] for m in marks)
                    and all(m["batch_run_code"] == code for m in marks),
                    "marks": marks,
                    "progress": _progress_for(conn, code),
                    "outcome": _latest_outcome(conn, code),
                    "pending": pending,
                    "stopped": pending,
                }
            )
        return out


def list_batch_cards(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        batches = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        out = []
        for batch in batches:
            marks = _marks_for(conn, batch["run_code"])
            live = [m for m in marks if m["state"] == "有效"]
            progress = _progress_for(conn, batch["run_code"])
            conflict = len(marks) > 1 or any(
                m["state"] == "冲突待处置" for m in marks
            )
            out.append(
                {
                    **dict(batch),
                    "mark_count": len(marks),
                    "live_mark": live[0]["mark_value"] if live else None,
                    "conflict": conflict,
                    "dedup_status": progress["status"] if progress else None,
                    "dedup_reason": progress["reason"] if progress else None,
                }
            )
        return out


def list_dedup_progress(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM dedup_progress ORDER BY id"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["dead_letter_total"] = conn.execute(
                "SELECT COUNT(*) AS c FROM dead_letters WHERE run_code=?",
                (row["run_code"],),
            ).fetchone()["c"]
            item["marks"] = _marks_for(conn, row["run_code"])
            item["outcome"] = _latest_outcome(conn, row["run_code"])
            out.append(item)
        return out


def list_dead_letters(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM dead_letters ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_restamp_queue(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM restamp_queue ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_merge_outcomes(run_code: str | None = None, db_path: str | None = None) -> list[dict]:
    """编排任务按运行码查询的合并结局。"""
    with get_db(db_path) as conn:
        if run_code:
            rows = conn.execute(
                "SELECT * FROM merge_outcomes WHERE run_code=? ORDER BY id DESC",
                (run_code,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM merge_outcomes ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 处置


def _set_progress(conn, run_code: str, total: int, kept: int, dead: int,
                  status: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO dedup_progress(run_code, total_marks, kept_marks, dead_letters, "
        "status, reason, updated_at) VALUES (?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(run_code) DO UPDATE SET total_marks=excluded.total_marks, "
        "kept_marks=excluded.kept_marks, dead_letters=excluded.dead_letters, "
        "status=excluded.status, reason=excluded.reason, "
        "updated_at=datetime('now')",
        (run_code, total, kept, dead, status, reason),
    )


def _dead_letter(conn, mark: dict, batch: dict, reason: str) -> None:
    payload = {
        "mark": {k: mark[k] for k in (
            "id", "run_code", "batch_id", "mark_value", "mark_seq",
            "origin", "state", "created_at",
        )},
        "batch": dict(batch),
        "reason": reason,
    }
    conn.execute(
        "INSERT INTO dead_letters(run_code, batch_id, mark_value, mark_seq, "
        "origin, reason, payload) VALUES (?,?,?,?,?,?,?)",
        (
            mark["run_code"],
            mark["batch_id"],
            mark["mark_value"],
            mark["mark_seq"],
            mark["origin"],
            reason,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def _default_target(run_code: str) -> str:
    return f"WM-{run_code.upper()}-R1"


def merge_shadow(
    run_code: str,
    strategy: str,
    target_value: str | None = None,
    db_path: str | None = None,
) -> dict:
    code = (run_code or "").strip()
    if not code:
        raise ServiceError("运行码不能为空")
    label = STRATEGIES.get(strategy)
    if not label:
        raise ServiceError("处置方案无效")
    target = (target_value or "").strip() or _default_target(code)

    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE run_code=?", (code,)
        ).fetchone()
        if not batch:
            raise ServiceError("运行码不存在", 404)
        marks = _marks_for(conn, code)
        if len(marks) != 2:
            raise ServiceError("无双影可处置", 409)
        if any(m["state"] != "冲突待处置" for m in marks):
            raise ServiceError("双影已处置", 409)
        if any(
            m["batch_id"] != batch["id"] or m["batch_run_code"] != code
            for m in marks
        ):
            raise ServiceError("运行码与库存行不同源", 409)
        if marks[0]["mark_seq"] != 1 or marks[1]["mark_seq"] != 2:
            raise ServiceError("双影行序异常", 409)

        first, second = marks
        if strategy == "keep_first":
            reason = "双影·后手整笔回滚（留存先手）"
            _dead_letter(conn, second, batch, reason)
            conn.execute("DELETE FROM stamp_marks WHERE id=?", (second["id"],))
            conn.execute(
                "UPDATE stamp_marks SET state='有效' WHERE id=?", (first["id"],)
            )
            conn.execute(
                "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                "stamped_at=COALESCE(stamped_at, datetime('now')) WHERE id=?",
                (first["mark_value"], batch["id"]),
            )
            _set_progress(
                conn, code, 2, 1, 1, "已完成",
                "留存先手印记，后手行整笔回滚入死信",
            )
            outcome = "留存先手"
            status = "已完成"
            kept = first["mark_value"]
            queue_id = None
        else:
            if target in {first["mark_value"], second["mark_value"]}:
                raise ServiceError("重打水印值需与原印记不同")
            duplicate = conn.execute(
                "SELECT 1 FROM stamp_marks WHERE mark_value=? AND state='有效'",
                (target,),
            ).fetchone()
            if duplicate:
                raise ServiceError("重打水印值已被占用", 409)
            reason = "双影·两枚整笔回滚（排队重打）"
            _dead_letter(conn, first, batch, reason)
            _dead_letter(conn, second, batch, reason)
            conn.execute(
                "DELETE FROM stamp_marks WHERE run_code=?", (code,)
            )
            conn.execute(
                "UPDATE run_batches SET status='待盖印', watermark_value=NULL, "
                "stamped_at=NULL WHERE id=?",
                (batch["id"],),
            )
            cur = conn.execute(
                "INSERT INTO restamp_queue(run_code, batch_id, target_value, status) "
                "VALUES (?,?,?,?)",
                (code, batch["id"], target, "待重打"),
            )
            queue_id = int(cur.lastrowid)
            _set_progress(
                conn, code, 2, 0, 2, "待重打",
                "两枚整笔回滚入死信，等待排队席重打唯一印记",
            )
            outcome = "整笔回滚·待重打"
            status = "待重打"
            kept = None

        cur = conn.execute(
            "INSERT INTO merge_outcomes(run_code, strategy, outcome, kept_mark_value, "
            "dead_count, queue_id, status) VALUES (?,?,?,?,?,?,?)",
            (code, strategy, outcome, kept, 2 if strategy == "rollback_all" else 1,
             queue_id, status),
        )
        outcome_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT * FROM merge_outcomes WHERE id=?", (outcome_id,)
        ).fetchone()
        return {
            "outcome": dict(row),
            "queue_id": queue_id,
            "kept_mark": kept,
            "marks_left": _marks_for(conn, code),
        }


def run_restamp(queue_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path, immediate=True) as conn:
        queue = conn.execute(
            "SELECT * FROM restamp_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not queue:
            raise ServiceError("重打任务不存在", 404)
        if queue["status"] != "待重打":
            raise ServiceError("重打任务已完成", 409)
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (queue["batch_id"],)
        ).fetchone()
        if not batch or batch["run_code"] != queue["run_code"]:
            raise ServiceError("运行码与库存行不同源", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("批次状态不可重打", 409)
        if conn.execute(
            "SELECT 1 FROM stamp_marks WHERE run_code=? LIMIT 1",
            (queue["run_code"],),
        ).fetchone():
            raise ServiceError("印记未清，不可重打", 409)
        target = (queue["target_value"] or "").strip()
        if not target:
            raise ServiceError("重打水印值不能为空")
        if conn.execute(
            "SELECT 1 FROM stamp_marks WHERE mark_value=? AND state='有效'",
            (target,),
        ).fetchone():
            raise ServiceError("重打水印值已被占用", 409)
        if conn.execute(
            "SELECT 1 FROM dead_letters WHERE run_code=? AND mark_value=?",
            (queue["run_code"], target),
        ).fetchone():
            raise ServiceError("重打水印值需与原印记不同")

        conn.execute(
            "INSERT INTO stamp_marks(run_code, batch_id, mark_value, mark_seq, "
            "origin, state) VALUES (?,?,?,?,?,?)",
            (queue["run_code"], batch["id"], target, 1, "重打", "有效"),
        )
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (target, batch["id"]),
        )
        conn.execute(
            "UPDATE restamp_queue SET status='已重打', done_at=datetime('now') "
            "WHERE id=?",
            (queue_id,),
        )
        _set_progress(
            conn, queue["run_code"], 2, 1, 2, "已完成",
            "排队席重打唯一印记完成",
        )
        conn.execute(
            "UPDATE merge_outcomes SET status='已完成（重打）', kept_mark_value=? "
            "WHERE run_code=? AND queue_id=?",
            (target, queue["run_code"], queue_id),
        )
        row = conn.execute(
            "SELECT * FROM merge_outcomes WHERE queue_id=? ORDER BY id DESC LIMIT 1",
            (queue_id,),
        ).fetchone()
        return {
            "batch": dict(
                conn.execute(
                    "SELECT * FROM run_batches WHERE id=?", (batch["id"],)
                ).fetchone()
            ),
            "outcome": dict(row) if row else None,
            "marks_left": _marks_for(conn, queue["run_code"]),
        }


# ---------------------------------------------------------------- 校验


def verify_dedup(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        checks = []

        multi = conn.execute(
            "SELECT run_code, COUNT(*) AS c FROM stamp_marks "
            "GROUP BY run_code HAVING c > 1"
        ).fetchall()
        checks.append(
            {
                "name": "双影清零",
                "ok": not multi,
                "detail": "残留双影：" + "、".join(r["run_code"] for r in multi)
                if multi
                else "每个运行码至多一枚印记",
            }
        )

        live_multi = conn.execute(
            "SELECT run_code, COUNT(*) AS c FROM stamp_marks "
            "WHERE state='有效' GROUP BY run_code HAVING c > 1"
        ).fetchall()
        checks.append(
            {
                "name": "单一有效印记",
                "ok": not live_multi,
                "detail": "有效印记重复：" + "、".join(
                    r["run_code"] for r in live_multi
                )
                if live_multi
                else "每个运行码至多一枚有效印记",
            }
        )

        orphan = conn.execute(
            "SELECT m.id FROM stamp_marks m LEFT JOIN run_batches b "
            "ON b.id=m.batch_id AND b.run_code=m.run_code WHERE b.id IS NULL"
        ).fetchall()
        checks.append(
            {
                "name": "运行码与库存行同源",
                "ok": not orphan,
                "detail": f"{len(orphan)} 枚印记对不上库存行"
                if orphan
                else "全部印记的批次行与运行码一致",
            }
        )

        mismatch = conn.execute(
            "SELECT b.id FROM run_batches b JOIN stamp_marks m "
            "ON m.run_code=b.run_code AND m.state='有效' "
            "WHERE b.watermark_value IS NOT m.mark_value"
        ).fetchall()
        checks.append(
            {
                "name": "库存值与有效印记同值",
                "ok": not mismatch,
                "detail": f"{len(mismatch)} 个批次库存值与有效印记不同"
                if mismatch
                else "库存值均取自唯一有效印记",
            }
        )

        shadow_meta = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE 'shadow_wm_%'"
        ).fetchone()["c"]
        checks.append(
            {
                "name": "无隐形双影",
                "ok": shadow_meta == 0,
                "detail": f"meta 仍藏 {shadow_meta} 枚隐形印记"
                if shadow_meta
                else "meta 不再存放印记",
            }
        )

        progress_bad = conn.execute(
            "SELECT p.run_code FROM dedup_progress p WHERE p.status IN ('已完成','待重打') "
            "AND p.dead_letters != (SELECT COUNT(*) FROM dead_letters d "
            "WHERE d.run_code=p.run_code)"
        ).fetchall()
        checks.append(
            {
                "name": "死信计数与死信行一致",
                "ok": not progress_bad,
                "detail": "计数不符：" + "、".join(r["run_code"] for r in progress_bad)
                if progress_bad
                else "去重进度与死信行逐码对齐",
            }
        )

        # 只改计数不改删除逻辑会在这里露馅：宣称已处置却仍有待处置印记。
        fake_done = conn.execute(
            "SELECT p.run_code FROM dedup_progress p WHERE p.status IN ('已完成','待重打') "
            "AND EXISTS (SELECT 1 FROM stamp_marks m WHERE m.run_code=p.run_code "
            "AND m.state='冲突待处置')"
        ).fetchall()
        checks.append(
            {
                "name": "已处置即无待处置印记",
                "ok": not fake_done,
                "detail": "只改计数未删行：" + "、".join(
                    r["run_code"] for r in fake_done
                )
                if fake_done
                else "处置过的运行码不再残留待处置印记",
            }
        )

        stopped = conn.execute(
            "SELECT run_code FROM dedup_progress WHERE status='停等' ORDER BY id"
        ).fetchall()
        checks.append(
            {
                "name": "双影停等清零",
                "ok": not stopped,
                "detail": "停等中：" + "、".join(r["run_code"] for r in stopped)
                if stopped
                else "无停等中的运行码",
            }
        )

        waiting = conn.execute(
            "SELECT run_code FROM restamp_queue WHERE status='待重打' ORDER BY id"
        ).fetchall()
        checks.append(
            {
                "name": "排队席已清空",
                "ok": not waiting,
                "detail": "待重打：" + "、".join(r["run_code"] for r in waiting)
                if waiting
                else "无待重打的运行码",
            }
        )

        return {"ok": all(c["ok"] for c in checks), "checks": checks}
