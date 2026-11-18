from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    get_batch,
    list_batch_cards,
    list_batches,
    list_conflicts,
    list_dead_letters,
    list_dedup_progress,
    list_marks,
    list_merge_outcomes,
    list_reports,
    list_restamp_queue,
    list_templates,
    merge_shadow,
    run_gap_report,
    run_restamp,
    stamp_batch,
    trace_downstream,
    verify_dedup,
    verify_gap,
    STRATEGIES,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": [
                "水印模板",
                "批次盖印",
                "下游追索",
                "缺口对账",
                "双影处置",
            ],
            "shadow_strategies": [
                {"key": key, "label": label} for key, label in STRATEGIES.items()
            ],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_response({"error": "批次编号无效"}, 400)
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ValueError:
        return json_response({"ok": False, "error": "批次编号无效"}, 400)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_reports()})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    body = await request.json()
    report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    return json_response({"ok": True, "report": report})


@app.router.get("/api/shadow-strategies")
async def api_shadow_strategies(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "items": [
                {"key": key, "label": label} for key, label in STRATEGIES.items()
            ]
        }
    )


@app.router.get("/api/conflicts")
async def api_conflicts(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_conflicts()})


@app.router.get("/api/marks")
async def api_marks(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    run_code = request.query.get("run_code")
    code = run_code[0] if run_code else None
    return json_response({"items": list_marks(run_code=code)})


@app.router.get("/api/batch-cards")
async def api_batch_cards(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_batch_cards()})


@app.router.get("/api/dedup-progress")
async def api_dedup_progress(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_dedup_progress()})


@app.router.get("/api/dead-letters")
async def api_dead_letters(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_dead_letters()})


@app.router.get("/api/restamp-queue")
async def api_restamp_queue(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_restamp_queue()})


@app.router.get("/api/merge-outcomes")
async def api_merge_outcomes(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    run_code = request.query.get("run_code")
    code = run_code[0] if run_code else None
    return json_response({"items": list_merge_outcomes(run_code=code)})


@app.router.post("/api/conflicts/{run_code}/merge")
async def api_conflict_merge(request: Request):
    try:
        require_user(request)
        run_code = str(request.route_values["run_code"])
        body = await request.json()
        result = merge_shadow(
            run_code,
            str(body.get("strategy", "")),
            str(body.get("target_value", "") or "") or None,
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.post("/api/restamp-queue/{queue_id}/run")
async def api_restamp_run(request: Request):
    try:
        require_user(request)
        queue_id = int(request.route_values["queue_id"])
        result = run_restamp(queue_id)
    except ValueError:
        return json_response({"ok": False, "error": "重打任务编号无效"}, 400)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/verify/gap")
async def api_verify_gap(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(verify_gap())


@app.router.get("/api/verify/dedup")
async def api_verify_dedup(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(verify_dedup())
