import sqlite3
import threading

import pytest

CONFLICT_CODE = "pay-20261115-01"


async def _json(resp):
    return await resp.json()


@pytest.mark.asyncio
async def test_conflict_surfaces_agree(authed):
    conflicts = (await _json(await authed.get("/api/conflicts")))["items"]
    assert [c["run_code"] for c in conflicts] == [CONFLICT_CODE]
    conflict = conflicts[0]
    assert [m["origin"] for m in conflict["marks"]] == ["先手", "后手"]
    assert conflict["marks"][0]["mark_value"] == "WM-PAY-151-01"
    assert conflict["marks"][1]["mark_value"] == "WM-PAY-151-02"
    assert conflict["stopped"] is True
    assert conflict["source_ok"] is True

    marks = (await _json(await authed.get("/api/marks")))["items"]
    assert len([m for m in marks if m["run_code"] == CONFLICT_CODE]) == 2

    cards = (await _json(await authed.get("/api/batch-cards")))["items"]
    card = next(c for c in cards if c["run_code"] == CONFLICT_CODE)
    assert card["conflict"] is True
    assert card["mark_count"] == 2

    dedup = (await _json(await authed.get("/api/dedup-progress")))["items"]
    progress = next(d for d in dedup if d["run_code"] == CONFLICT_CODE)
    assert progress["status"] == "停等"
    assert "死信因" in progress["reason"] or "待处置" in progress["reason"]

    gap = (await _json(await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )))["report"]
    assert gap["stopped"] is True
    assert gap["blocked_run_codes"] == [CONFLICT_CODE]
    # 双影不参与计数：应盖 5 行、已盖 2 行，差额 3。
    assert gap["expected_count"] == 5
    assert gap["stamped_count"] == 2
    assert gap["gap_count"] == 3


@pytest.mark.asyncio
async def test_keep_first_keeps_first_and_dead_letters_second(authed):
    r = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge", json={"strategy": "keep_first"}
    )
    assert r.status == 200
    body = await r.json()
    assert body["outcome"]["outcome"] == "留存先手"
    assert body["kept_mark"] == "WM-PAY-151-01"
    assert [m["mark_value"] for m in body["marks_left"]] == ["WM-PAY-151-01"]
    assert body["marks_left"][0]["state"] == "有效"

    dead = (await _json(await authed.get("/api/dead-letters")))["items"]
    assert len(dead) == 1
    assert dead[0]["mark_value"] == "WM-PAY-151-02"
    assert dead[0]["origin"] == "后手"
    assert "整笔回滚" in dead[0]["reason"]

    cards = (await _json(await authed.get("/api/batch-cards")))["items"]
    card = next(c for c in cards if c["run_code"] == CONFLICT_CODE)
    assert card["conflict"] is False
    assert card["mark_count"] == 1
    assert card["watermark_value"] == "WM-PAY-151-01"
    assert card["status"] == "已盖印"

    progress = next(
        d
        for d in (await _json(await authed.get("/api/dedup-progress")))["items"]
        if d["run_code"] == CONFLICT_CODE
    )
    assert progress["status"] == "已完成"
    assert progress["kept_marks"] == 1
    assert progress["dead_letters"] == 1
    assert progress["dead_letter_total"] == 1

    verify = await _json(await authed.get("/api/verify/dedup"))
    assert verify["ok"] is True

    vgap = await _json(await authed.get("/api/verify/gap"))
    assert vgap["ok"] is True
    assert vgap["stamped_count"] == 2
    assert vgap["gap_count"] == 3

    gap = (await _json(await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )))["report"]
    assert gap["stopped"] is False
    assert gap["stamped_count"] == 2
    assert gap["gap_count"] == 3


@pytest.mark.asyncio
async def test_rollback_all_dead_letters_both_and_queues_restamp(authed):
    r = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge",
        json={"strategy": "rollback_all", "target_value": "WM-PAY-151-R1"},
    )
    assert r.status == 200
    body = await r.json()
    assert body["outcome"]["outcome"] == "整笔回滚·待重打"
    assert body["kept_mark"] is None
    assert body["marks_left"] == []
    queue_id = body["queue_id"]
    assert queue_id

    dead = (await _json(await authed.get("/api/dead-letters")))["items"]
    assert sorted(d["origin"] for d in dead) == ["先手", "后手"]
    assert {d["mark_value"] for d in dead} == {"WM-PAY-151-01", "WM-PAY-151-02"}

    cards = (await _json(await authed.get("/api/batch-cards")))["items"]
    card = next(c for c in cards if c["run_code"] == CONFLICT_CODE)
    assert card["status"] == "待盖印"
    assert card["watermark_value"] is None

    progress = next(
        d
        for d in (await _json(await authed.get("/api/dedup-progress")))["items"]
        if d["run_code"] == CONFLICT_CODE
    )
    assert progress["status"] == "待重打"
    assert progress["dead_letters"] == 2

    verify = await _json(await authed.get("/api/verify/dedup"))
    assert verify["ok"] is False
    assert "排队席已清空" in {c["name"] for c in verify["checks"] if not c["ok"]}

    vgap = await _json(await authed.get("/api/verify/gap"))
    assert vgap["ok"] is False

    rr = await authed.post(f"/api/restamp-queue/{queue_id}/run", json={})
    assert rr.status == 200
    rb = await rr.json()
    assert rb["batch"]["status"] == "已盖印"
    assert rb["batch"]["watermark_value"] == "WM-PAY-151-R1"
    assert [m["mark_value"] for m in rb["marks_left"]] == ["WM-PAY-151-R1"]
    assert rb["marks_left"][0]["origin"] == "重打"

    progress = next(
        d
        for d in (await _json(await authed.get("/api/dedup-progress")))["items"]
        if d["run_code"] == CONFLICT_CODE
    )
    assert progress["status"] == "已完成"
    assert progress["kept_marks"] == 1
    assert progress["dead_letters"] == 2
    assert (await _json(await authed.get("/api/verify/dedup")))["ok"] is True

    queue = (await _json(await authed.get("/api/restamp-queue")))["items"]
    assert queue[0]["status"] == "已重打"

    outcomes = (await _json(await authed.get("/api/merge-outcomes")))["items"]
    assert outcomes[0]["run_code"] == CONFLICT_CODE
    assert outcomes[0]["status"] == "已完成（重打）"
    assert outcomes[0]["kept_mark_value"] == "WM-PAY-151-R1"

    # 编排任务按运行码查合并结局。
    by_code = (
        await _json(
            await authed.get(
                "/api/merge-outcomes", query={"run_code": CONFLICT_CODE}
            )
        )
    )["items"]
    assert len(by_code) == 1
    assert by_code[0]["outcome"] == "整笔回滚·待重打"
    other = (
        await _json(
            await authed.get("/api/merge-outcomes", query={"run_code": "nope"})
        )
    )["items"]
    assert other == []

    vgap = await _json(await authed.get("/api/verify/gap"))
    assert vgap["ok"] is True
    assert vgap["stamped_count"] == 2

    gap = (await _json(await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )))["report"]
    assert gap["stopped"] is False
    assert gap["stamped_count"] == 2
    assert gap["gap_count"] == 3


@pytest.mark.asyncio
async def test_merge_twice_rejected_with_short_message(authed):
    first = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge", json={"strategy": "keep_first"}
    )
    assert first.status == 200
    again = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge", json={"strategy": "keep_first"}
    )
    assert again.status == 409
    body = await again.json()
    assert body["ok"] is False
    assert body["error"] == "无双影可处置"

    bad = await authed.post(
        "/api/conflicts/not-here/merge", json={"strategy": "keep_first"}
    )
    assert bad.status == 404
    assert (await bad.json())["error"] == "运行码不存在"


@pytest.mark.asyncio
async def test_manual_stamp_cannot_bypass_restamp_queue(authed):
    batches = (await _json(await authed.get("/api/batches")))["items"]
    card = next(c for c in batches if c["run_code"] == CONFLICT_CODE)
    r = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge",
        json={"strategy": "rollback_all", "target_value": "WM-PAY-151-R1"},
    )
    assert r.status == 200

    # 排队席未重打前，普通盖印入口不能抢跑。
    bypass = await authed.post(
        f"/api/batches/{card['id']}/stamp", json={"watermark_value": "WM-SNEAK"}
    )
    assert bypass.status == 409
    assert (await bypass.json())["error"] == "该运行码待排队席重打"

    marks = (await _json(await authed.get("/api/marks")))["items"]
    assert [m for m in marks if m["run_code"] == CONFLICT_CODE] == []


@pytest.mark.asyncio
async def test_restamp_target_must_differ(authed):
    r = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge",
        json={"strategy": "rollback_all", "target_value": "WM-PAY-151-01"},
    )
    assert r.status == 400
    assert (await r.json())["error"] == "重打水印值需与原印记不同"

    r = await authed.post(
        f"/api/conflicts/{CONFLICT_CODE}/merge",
        json={"strategy": "rollback_all", "target_value": "WM-PAY-151-R9"},
    )
    assert r.status == 200
    queue_id = (await r.json())["queue_id"]
    again = await authed.post(f"/api/restamp-queue/{queue_id}/run", json={})
    assert again.status == 200
    done = await authed.post(f"/api/restamp-queue/{queue_id}/run", json={})
    assert done.status == 409
    assert (await done.json())["error"] == "重打任务已完成"


def test_same_source_trigger_blocks_foreign_run_code(tmp_path):
    from app.db import init_db, get_db

    db = str(tmp_path / "src.db")
    init_db(db)
    with pytest.raises(sqlite3.IntegrityError):
        with get_db(db) as conn:
            conn.execute(
                "INSERT INTO stamp_marks(run_code, batch_id, mark_value, mark_seq, "
                "origin, state) VALUES (?,?,?,?,?,?)",
                ("pay-20261115-01", 1, "WM-FOREIGN", 3, "先手", "冲突待处置"),
            )


def test_double_shadow_requires_real_delete_not_just_count(tmp_path):
    """只改计数不删行必须被去重校验抓出来。"""
    from app.db import init_db, get_db
    from app.services import merge_shadow, verify_dedup

    db = str(tmp_path / "fake.db")
    init_db(db)
    with get_db(db) as conn:
        # 伪造一次「只改计数」的处置：把停等改成已完成，但两枚印记原样留着。
        conn.execute(
            "UPDATE dedup_progress SET status='已完成', kept_marks=1, dead_letters=1 "
            "WHERE run_code='pay-20261115-01'"
        )
        conn.execute(
            "INSERT INTO dead_letters(run_code, batch_id, mark_value, mark_seq, "
            "origin, reason, payload) VALUES (?,?,?,?,?,?,?)",
            ("pay-20261115-01", 5, "WM-PAY-151-02", 2, "后手", "假处置", "{}"),
        )

    result = verify_dedup(db)
    assert result["ok"] is False
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "双影清零" in failed
    assert "已处置即无待处置印记" in failed

    # 真处置之后同一校验通过（另起一库，避免假死信行污染计数）。
    clean = str(tmp_path / "clean.db")
    init_db(clean)
    merge_shadow(CONFLICT_CODE, "keep_first", db_path=clean)
    assert verify_dedup(clean)["ok"] is True

    # 原库里补做真处置：印记行被真正删掉，假死信行仍在，故计数仍不齐。
    merge_shadow(CONFLICT_CODE, "keep_first", db_path=db)
    result = verify_dedup(db)
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "双影清零" not in failed
    assert "已处置即无待处置印记" not in failed
    assert "死信计数与死信行一致" in failed


def test_concurrent_stamp_single_winner(tmp_path):
    """两名灌数员并发同打一码：缺排他锁会双写各自留一枚。"""
    from app.db import init_db, get_db
    from app.services import stamp_batch
    from app.services.errors import ServiceError

    db = str(tmp_path / "race.db")
    init_db(db)
    with get_db(db) as conn:
        bid = conn.execute(
            "SELECT id FROM run_batches WHERE run_code='ord-20261114-01'"
        ).fetchone()["id"]

    barrier = threading.Barrier(2)
    results = []

    def worker(value):
        barrier.wait()
        try:
            stamp_batch(bid, value, db)
            results.append(("ok", value))
        except ServiceError as exc:
            results.append(("err", exc.message))
        except sqlite3.Error as exc:
            results.append(("db", str(exc)))

    threads = [
        threading.Thread(target=worker, args=("WM-RACE-A",)),
        threading.Thread(target=worker, args=("WM-RACE-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r[0] for r in results].count("ok") == 1
    assert [r for r in results if r[0] == "err"][0][1] == "批次已盖印"

    with get_db(db) as conn:
        marks = conn.execute(
            "SELECT * FROM stamp_marks WHERE run_code='ord-20261114-01'"
        ).fetchall()
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE run_code='ord-20261114-01'"
        ).fetchone()
    assert len(marks) == 1
    assert batch["status"] == "已盖印"
    assert batch["watermark_value"] == marks[0]["mark_value"]


@pytest.mark.asyncio
async def test_stamp_and_stock_same_source(authed):
    batches = (await _json(await authed.get("/api/batches")))["items"]
    target = next(
        b for b in batches if b["run_code"] == "ord-20261114-01"
    )
    r = await authed.post(
        f"/api/batches/{target['id']}/stamp", json={"watermark_value": "WM-ORD-141-01"}
    )
    body = await r.json()
    assert body["batch"]["watermark_value"] == "WM-ORD-141-01"
    assert len(body["batch"]["marks"]) == 1
    assert body["batch"]["marks"][0]["mark_value"] == "WM-ORD-141-01"
    assert body["batch"]["marks"][0]["state"] == "有效"

    dup = await authed.post(
        f"/api/batches/{target['id']}/stamp", json={"watermark_value": "WM-ORD-141-02"}
    )
    assert dup.status == 409
    assert (await dup.json())["error"] == "批次已盖印"
