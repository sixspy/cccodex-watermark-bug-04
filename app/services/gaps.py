from app.db import get_db


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _count(conn, sql: str, args: tuple = ()) -> int:
    return conn.execute(sql, args).fetchone()["c"]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    """差额对账：应盖印 vs 已盖印，两边都只从库存行算。

    双影不参与计数（印记表里一个运行码至多一枚有效印记）；有双影待处置
    时对账本身照常产出，但 ``stopped`` 标出停在哪些运行码上。
    """
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path, immediate=True) as conn:
        expected = _count(
            conn,
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status!='已作废'",
            (day,),
        )
        stamped = _count(
            conn,
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status='已盖印'",
            (day,),
        )
        gap = expected - stamped

        shadow_codes = [
            r["run_code"]
            for r in conn.execute(
                "SELECT run_code FROM stamp_marks GROUP BY run_code HAVING COUNT(*) > 1 "
                "ORDER BY run_code"
            ).fetchall()
        ]
        orphan = _count(
            conn,
            "SELECT COUNT(*) AS c FROM stamp_marks m LEFT JOIN run_batches b "
            "ON b.id=m.batch_id AND b.run_code=m.run_code WHERE b.id IS NULL",
        )
        verdict = "对账一致"
        if shadow_codes:
            verdict = "双影停等"
        elif orphan:
            verdict = "同源校验失败"
        elif gap > 0:
            verdict = "存在缺口"
        elif gap < 0:
            verdict = "超额盖印"

        summary = (
            f"截至 {day}：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}"
            f"（{verdict}）"
        )
        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, gap_count, summary) "
            "VALUES (?,?,?,?,?)",
            (day, expected, stamped, gap, summary),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        report = dict(row)
        report.update(
            {
                "verdict": verdict,
                "blocked_run_codes": shadow_codes,
                "stopped": bool(shadow_codes) or bool(orphan),
            }
        )
        return report


def verify_gap(db_path: str | None = None) -> dict:
    """差额校验：两边计数都只认库存行，印记表只做同源比对。"""
    with get_db(db_path) as conn:
        checks = []

        expected = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE status!='已作废'"
        ).fetchone()["c"]
        stamped = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE status='已盖印'"
        ).fetchone()["c"]
        live = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_marks WHERE state='有效'"
        ).fetchone()["c"]
        checks.append(
            {
                "name": "已盖印行与有效印记一一对应",
                "ok": stamped == live,
                "detail": f"已盖印 {stamped} 行，有效印记 {live} 枚"
                if stamped != live
                else f"已盖印 {stamped} 行对应 {live} 枚有效印记",
            }
        )

        no_mark = conn.execute(
            "SELECT run_code FROM run_batches b WHERE b.status='已盖印' "
            "AND NOT EXISTS (SELECT 1 FROM stamp_marks m "
            "WHERE m.run_code=b.run_code AND m.state='有效')"
        ).fetchall()
        checks.append(
            {
                "name": "已盖印行都有有效印记",
                "ok": not no_mark,
                "detail": "缺印记：" + "、".join(r["run_code"] for r in no_mark)
                if no_mark
                else "无缺印记的已盖印行",
            }
        )

        extra = conn.execute(
            "SELECT m.run_code FROM stamp_marks m WHERE m.state='有效' "
            "AND NOT EXISTS (SELECT 1 FROM run_batches b "
            "WHERE b.run_code=m.run_code AND b.status='已盖印')"
        ).fetchall()
        checks.append(
            {
                "name": "有效印记不越出已盖印行",
                "ok": not extra,
                "detail": "越界印记：" + "、".join(r["run_code"] for r in extra)
                if extra
                else "无越界印记",
            }
        )

        shadow = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_marks GROUP BY run_code HAVING c > 1"
        ).fetchall()
        checks.append(
            {
                "name": "差额不被双影污染",
                "ok": not shadow,
                "detail": f"{len(shadow)} 个运行码仍有两枚印记"
                if shadow
                else "计数里没有第二枚印记",
            }
        )

        unsettled = conn.execute(
            "SELECT run_code FROM dedup_progress WHERE status IN ('停等','待重打') "
            "UNION SELECT run_code FROM restamp_queue WHERE status='待重打' "
            "ORDER BY run_code"
        ).fetchall()
        checks.append(
            {
                "name": "无停等运行码",
                "ok": not unsettled,
                "detail": "差额台停等：" + "、".join(r["run_code"] for r in unsettled)
                if unsettled
                else "无停等中的运行码",
            }
        )

        gap = expected - stamped
        return {
            "ok": all(c["ok"] for c in checks),
            "expected_count": expected,
            "stamped_count": stamped,
            "gap_count": gap,
            "checks": checks,
        }
