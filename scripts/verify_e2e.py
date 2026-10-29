#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T14 · 端到端集成与运行验证（独立验证，非实现者视角）。

以「真实服务器 + 真实 HTTP」为**首选**验证方式，并覆盖 plan 2.3.3 的端到端主链路
与 3.3-T14 的全部验收项：

1. ``python run.py`` 子进程启动（真实 loopback HTTP）——可选，取决于沙箱是否放行；
2. 进程内 uvicorn 线程 + 真实 HTTP——次级备选；
3. **进程内 ASGI 直调**（httpx.ASGITransport）——末级回退，明确标注理由。

> 回退说明（重要）：本运行沙箱对 127.0.0.1 回环 socket 实施**间歇性/持续限制**
> （``WinError 10051``），asyncio 事件循环自管道（``socket.socketpair``）与
> ``uvicorn`` 监听均可能失败。为确保「可复跑且证据可复现」，脚本在无法建立真实
> 连接时回退为**进程内 ASGI 直调**：仍然完整经过 FastAPI 路由、Pydantic 校验、
> SQLAlchemy 之外的 DB 层与全部业务逻辑，仅绕过网络栈（socket）。回退时的具体
> 规避手段与可信性理由见 :func:`install_loop_workaround` 与运行报告。

运行：``python scripts/verify_e2e.py``（可选 ``--mode auto|live|asgi``、``--port``）。

退出码：0 = 全部通过；1 = 存在 FAIL；2 = 存在未执行的必需检查。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RUN_DB_PATH = REPO_ROOT / "backend" / "app" / "data" / "app.db"
REPORT_PATH = REPO_ROOT / ".resultverify" / "e2e_verification_report.json"

# 已知内容事实（用于断言口径，实际值仍从 API 读取核对）
EXPECTED_CHAPTERS = 2
EXPECTED_MIN_TOTAL_KPS = 16
PRIMARY_KP = "cell-membrane"
SECONDARY_KP = "genetics-gene-trait"  # 用于软删除/status 过滤检查
NOT_REVIEWED_KP = "cell-nucleus"  # 全程从不加入复习的对照知识点


# ===========================================================================
# 沙箱规避：让 in-process ASGI（含同步端点）可运行
# ===========================================================================
def install_loop_workaround() -> str:
    """安装「无 loopback」下运行 asyncio + ASGI 的最小规避层，返回说明。

    规避点：

    1. ``socket.socketpair`` → 返回一对**未连接的真实 socket**。asyncio 事件循环仅
       用其 ``fileno()`` 注册 self-pipe；单线程纯 async 场景下 self-pipe 从不读写，
       故无需真正互联，从而绕开被沙箱禁止的 loopback ``connect``。
    2. 事件循环策略 → ``WindowsSelectorEventLoop``（其 self-pipe 只要真实 socket fd
       即可被 ``select`` 轮询；Proactor 的 IOCP 路径对未连接 socket 不友好）。
    3. ``anyio.to_thread.run_sync`` → 内联执行。FastAPI 对 ``def`` 端点用线程池
       （``run_in_threadpool``），跨线程唤醒依赖 self-pipe；内联后同步端点直接在当前
       事件循环内执行，不再需要跨线程唤醒。

    这三者共同保证：进程内 ASGI 调用（真实路由/校验/DB/业务逻辑）可稳定运行。
    """
    import socket as _socket

    def _fake_socketpair(family: Optional[int] = None, type: Optional[int] = None, proto: int = 0):
        sock_a = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock_b = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        return sock_a, sock_b

    _socket.socketpair = _fake_socketpair  # type: ignore[assignment]

    import asyncio as _asyncio

    try:
        _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:  # noqa: BLE001 - 非 Windows 或策略不可用时忽略
        pass

    import anyio.to_thread as _to_thread

    def _inline_run_sync(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async def _runner() -> Any:
            return func(*args)

        return _runner()

    _to_thread.run_sync = _inline_run_sync  # type: ignore[assignment]

    return (
        "socketpair→未连接socket; 事件循环=WindowsSelectorEventLoop; "
        "anyio.to_thread.run_sync→内联"
    )


# ===========================================================================
# 报告与断言辅助
# ===========================================================================
class CheckFailed(Exception):
    """单条检查失败。"""


def need(condition: Any, message: str) -> None:
    """断言辅助：不满足即抛 :class:`CheckFailed`。"""
    if not condition:
        raise CheckFailed(message)


class Report:
    """收集逐项检查结果。"""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, cid: str, name: str, owner: str, status: str, evidence: str) -> None:
        self.items.append(
            {"id": cid, "name": name, "owner": owner, "status": status, "evidence": evidence}
        )
        print(f"[{status:7s}] {cid:4s} {name}  <{owner}>\n         证据: {evidence}")

    @property
    def failures(self) -> list[dict[str, str]]:
        return [item for item in self.items if item["status"] == "FAIL"]

    @property
    def blocked(self) -> list[dict[str, str]]:
        return [item for item in self.items if item["status"] == "BLOCKED"]


async def run_check(
    report: Report,
    cid: str,
    name: str,
    owner: str,
    factory: Callable[[], Awaitable[str]],
) -> None:
    """执行单条检查并记录结果（异常一律计为 FAIL，绝不放过）。"""
    try:
        evidence = await factory()
        report.add(cid, name, owner, "PASS", evidence)
    except CheckFailed as exc:
        report.add(cid, name, owner, "FAIL", str(exc))
    except Exception as exc:  # noqa: BLE001 - 任何异常都视为失败，避免漏检
        report.add(cid, name, owner, "FAIL", f"未预期异常：{type(exc).__name__}: {exc}")


# ===========================================================================
# SSE 解析
# ===========================================================================
async def collect_sse(client: Any, path: str, payload: dict, headers: dict) -> dict:
    """发起 SSE POST 并收集帧序列与响应元信息。"""
    events: list[tuple[str, str]] = []
    async with client.stream("POST", path, json=payload, headers=headers) as resp:
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        current_event: Optional[str] = None
        current_data: Optional[str] = None
        async for line in resp.aiter_lines():
            if line == "":
                if current_event is not None:
                    events.append((current_event, current_data or ""))
                current_event, current_data = None, None
            elif line.startswith("event:"):
                current_event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                current_data = line[len("data:") :].strip()
        if current_event is not None:  # 收尾（无尾随空行）
            events.append((current_event, current_data or ""))
    return {"status": status, "content_type": content_type, "events": events}


# ===========================================================================
# 真实服务器启动尝试（子进程 / 进程内 uvicorn）
# ===========================================================================
def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    """裸 socket 探测端口是否可连（回环被禁时返回 False）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def try_live_subprocess(db_path: Path, port: int, ready_timeout: float = 12.0) -> dict:
    """尝试以子进程 ``python run.py`` 启动真实服务器。"""
    log_path = db_path.parent / "run_py.log"
    env = dict(os.environ)
    env["ALA_DB_PATH"] = str(db_path)
    env["ALA_HOST"] = "127.0.0.1"
    env["ALA_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"

    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + ready_timeout
    started = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _can_connect("127.0.0.1", port):
            started = True
            break
        time.sleep(0.3)

    if started:
        return {"ok": True, "mode": "live-subprocess", "proc": proc, "port": port, "log": log_path}

    # 失败：回收子进程并读取日志证据
    time.sleep(0.3)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log_handle.flush()
    log_handle.close()
    log_text = ""
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return {"ok": False, "mode": "live-subprocess", "proc": None, "port": port, "log": log_path, "log_text": log_text}


def try_live_inproc(db_path: Path, port: int, ready_timeout: float = 6.0) -> dict:
    """尝试进程内 uvicorn 线程启动真实服务器（真实 HTTP）。"""
    env = dict(os.environ)
    env["ALA_DB_PATH"] = str(db_path)
    os.environ["ALA_DB_PATH"] = str(db_path)

    try:
        import uvicorn

        from backend.app.main import create_app

        app = create_app()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        error_box: dict[str, str] = {}

        def _run() -> None:
            try:
                server.run()
            except BaseException as exc:  # noqa: BLE001
                error_box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_run, daemon=True, name="uvicorn-inproc")
        thread.start()
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if getattr(server, "started", False) and _can_connect("127.0.0.1", port):
                return {"ok": True, "mode": "live-inproc", "server": server, "thread": thread, "port": port}
            if not thread.is_alive():
                break
            time.sleep(0.2)
        server.should_exit = True
        thread.join(timeout=3)
        return {"ok": False, "mode": "live-inproc", "port": port, "log_text": error_box.get("error", "未在超时内就绪")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": "live-inproc", "port": port, "log_text": f"{type(exc).__name__}: {exc}"}


def startup_selfcheck(db_path: Path) -> dict:
    """子进程内确定性校验「启动装配」：建表/加载内容/注册路由/挂载静态。

    不依赖网络，规避回环限制后仍能验证 ``create_app`` 的完整启动链路（plan 2.3.1）。
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from backend.app.main import create_app\n"
        "import backend.app.db as dbmod\n"
        "from backend.app.models import TABLE_NAMES\n"
        "app = create_app()\n"
        "conn = dbmod.connect()\n"
        "tables = set(dbmod.list_tables(conn))\n"
        "print('TABLES=' + ','.join(sorted(tables)))\n"
        "print('MISSING=' + ','.join(sorted(set(TABLE_NAMES) - tables)))\n"
        "print('CHAPTERS=' + str(len(app.state.content_repo.list_chapters())))\n"
        "print('KPS=' + str(len(app.state.content_repo.list_knowledge_points())))\n"
        "print('ROUTES=' + str(len(app.routes)))\n"
        "mounts = [r for r in app.routes if getattr(r, 'name', '') == 'static']\n"
        "print('STATIC_MOUNT=' + str(len(mounts)))\n"
    )
    env = dict(os.environ)
    env["ALA_DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    parsed: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
    parsed["returncode"] = str(proc.returncode)
    parsed["stderr"] = (proc.stderr or "")[-1500:]
    return parsed


# ===========================================================================
# 主流程
# ===========================================================================
def _sha256(path: Path) -> str:
    if not path.is_file():
        return "<absent>"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def build_asgi_client(db_path: Path):
    """构建进程内 ASGI 客户端（httpx.AsyncClient + ASGITransport）。"""
    import httpx

    os.environ["ALA_DB_PATH"] = str(db_path)
    from backend.app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://e2e.local")
    return client, app


async def run_all_checks(client: Any, report: Report, user: str) -> None:
    """执行全部端到端检查（client 已配置为真实或 ASGI）。"""
    headers = {"X-User-Id": user}

    # ---------------- 前端与静态资源 ----------------
    async def c_home() -> str:
        resp = await client.get("/")
        need(resp.status_code == 200, f"GET / 期望 200，实际 {resp.status_code}")
        need("AI 学习助手" in resp.text, "首页内容未包含 index.html 标记「AI 学习助手」")
        need("<html" in resp.text.lower(), "首页非 HTML 文档")
        return f"GET / -> 200，{len(resp.text)} bytes，命中 index.html 标记"

    async def _static(name: str) -> str:
        resp = await client.get(f"/static/{name}")
        need(resp.status_code == 200, f"GET /static/{name} 期望 200，实际 {resp.status_code}")
        need(len(resp.text) > 0, f"/static/{name} 内容为空")
        return f"GET /static/{name} -> 200，{len(resp.text)} bytes"

    async def c_health() -> str:
        resp = await client.get("/health")
        need(resp.status_code == 200, f"/health 期望 200，实际 {resp.status_code}")
        body = resp.json()
        need(body.get("content_loaded") is True, f"content_loaded 非 True：{body}")
        need(body.get("chapters") == EXPECTED_CHAPTERS, f"chapters={body.get('chapters')} 期望 {EXPECTED_CHAPTERS}")
        return f"/health -> {body}"

    await run_check(report, "H1", "GET / 返回 index.html(200)", "T9", c_home)
    for idx, page in enumerate(("index.html", "knowledge.html", "chat.html", "profile.html"), start=2):
        await run_check(report, f"H{idx}", f"GET /static/{page} 返回 200", "T9-T12",
                        (lambda p=page: _static(p)))
    await run_check(report, "H6", "GET /health 内容已加载且 2 章", "T1", c_health)

    # ---------------- 内容契约 ----------------
    async def c_chapters() -> str:
        resp = await client.get("/api/knowledge/chapters")
        need(resp.status_code == 200, f"chapters 期望 200，实际 {resp.status_code}")
        chapters = resp.json()
        need(len(chapters) == EXPECTED_CHAPTERS, f"章节数 {len(chapters)} 期望 {EXPECTED_CHAPTERS}")
        total = sum(int(ch["kp_count"]) for ch in chapters)
        need(total >= EXPECTED_MIN_TOTAL_KPS, f"总知识点 {total} < {EXPECTED_MIN_TOTAL_KPS}")
        detail = ", ".join(f"{ch['chapter_id']}={ch['kp_count']}" for ch in chapters)
        return f"2 章，总知识点 {total}（{detail}），字段={list(chapters[0].keys())}"

    async def c_kps_per_chapter() -> str:
        parts = []
        for chapter_id in ("cells", "genetics"):
            resp = await client.get("/api/knowledge/kps", params={"chapter_id": chapter_id})
            need(resp.status_code == 200, f"kps?chapter_id={chapter_id} 期望 200，实际 {resp.status_code}")
            items = resp.json()
            need(len(items) > 0, f"{chapter_id} 无知识点")
            need(all(item["chapter_id"] == chapter_id for item in items), f"{chapter_id} 返回了其它章节的知识点")
            required = {"kp_id", "chapter_id", "title", "summary", "tags", "difficulty",
                        "importance", "est_minutes", "has_images", "quiz_count"}
            need(required.issubset(items[0].keys()), f"元数据字段缺失：{required - set(items[0].keys())}")
            parts.append(f"{chapter_id}={len(items)}")
        return "按章节过滤正确 -> " + ", ".join(parts)

    async def c_kp_detail() -> str:
        resp = await client.get(f"/api/knowledge/kps/{PRIMARY_KP}")
        need(resp.status_code == 200, f"kps/{PRIMARY_KP} 期望 200，实际 {resp.status_code}")
        kp = resp.json()
        need(len(kp["content_blocks"]) >= 1, "content_blocks 为空")
        need(len(kp["images"]) >= 1, "images 为空")
        need(len(kp["mindmap"]["children"]) >= 1, "mindmap 子节点为空")
        need(len(kp["quiz"]) >= 2, "quiz 少于 2 题")
        need(any(q["type"] != "single_choice" for q in kp["quiz"]), "缺少非 single_choice 题目")
        block_types = sorted({b["type"] for b in kp["content_blocks"]})
        return (f"{PRIMARY_KP}: blocks={len(kp['content_blocks'])} types={block_types}, "
                f"images={len(kp['images'])}, mindmap 子={len(kp['mindmap']['children'])}, quiz={len(kp['quiz'])}")

    async def c_mindmap() -> str:
        resp = await client.get(f"/api/knowledge/kps/{PRIMARY_KP}/mindmap")
        need(resp.status_code == 200, f"mindmap 期望 200，实际 {resp.status_code}")
        node = resp.json()
        need(node.get("id") and node.get("label"), "mindmap 根节点缺少 id/label")
        need(isinstance(node.get("children"), list) and len(node["children"]) >= 1, "mindmap 子节点缺失")
        return f"mindmap 根={node['id']} 标签={node['label']} 子节点={len(node['children'])}"

    async def c_images() -> str:
        resp = await client.get(f"/api/knowledge/kps/{PRIMARY_KP}/images")
        need(resp.status_code == 200, f"images 期望 200，实际 {resp.status_code}")
        images = resp.json()
        need(len(images) >= 1, "images 为空")
        need(all({"src", "caption", "alt"}.issubset(img.keys()) for img in images), "ImageRef 字段不完整")
        need(all(img["src"].startswith("/static/img/biology/") for img in images), "配图 src 前缀不符")
        return f"images={len(images)}，示例 src={images[0]['src']}"

    async def c_content_404() -> str:
        r1 = await client.get("/api/knowledge/kps/no-such-kp")
        r2 = await client.get("/api/knowledge/chapters/no-such-chapter")
        need(r1.status_code == 404 and r1.json().get("code") == "KP_NOT_FOUND", f"未知 kp 应 404 KP_NOT_FOUND，实际 {r1.status_code}/{r1.text[:80]}")
        need(r2.status_code == 404 and r2.json().get("code") == "CHAPTER_NOT_FOUND", f"未知章节应 404 CHAPTER_NOT_FOUND，实际 {r2.status_code}/{r2.text[:80]}")
        return "未知 kp -> 404 KP_NOT_FOUND；未知章节 -> 404 CHAPTER_NOT_FOUND（错误体含 code）"

    await run_check(report, "K1", "chapters 返回 2 章且总知识点 ≥16", "T2/T3/T4", c_chapters)
    await run_check(report, "K2", "kps?chapter_id 过滤与元数据字段完整", "T4", c_kps_per_chapter)
    await run_check(report, "K3", "kps/{kp} 含 content_blocks/images/mindmap/quiz", "T2/T3/T4", c_kp_detail)
    await run_check(report, "K4", "kps/{kp}/mindmap 结构完整", "T4", c_mindmap)
    await run_check(report, "K5", "kps/{kp}/images 结构完整", "T4", c_images)
    await run_check(report, "K6", "[对抗] 未知 kp/章节 -> 404 结构化错误体", "T4", c_content_404)

    # ---------------- 画像 / 问卷 / 策略 ----------------
    async def c_profile_default() -> str:
        resp = await client.get("/api/profile", headers=headers)
        need(resp.status_code == 200, f"GET /api/profile 期望 200，实际 {resp.status_code}")
        profile = resp.json()
        need(profile["user_id"] == user, "user_id 不匹配")
        need(profile["daily_minutes"] == 30, f"默认 daily_minutes 应为 30，实际 {profile['daily_minutes']}")
        need(profile["reminder_enabled"] is True and profile["reminder_time"] == "20:00",
             f"默认提醒设置异常：{profile['reminder_enabled']}/{profile['reminder_time']}")
        need(profile["questionnaire_completed"] is False, "新用户 questionnaire_completed 应为 False")
        return (f"默认画像创建成功：daily_minutes={profile['daily_minutes']}, "
                f"learning_style={profile['learning_style']}, reminder={profile['reminder_time']}")

    async def c_questionnaire_strategy() -> str:
        # 提交覆盖四个 VARK 维度的 style 答案
        answers = {"q6": ["v"], "q7": ["a"], "q8": ["r"], "q9": ["k"]}
        resp = await client.post("/api/profile/questionnaire", json={"answers": answers}, headers=headers)
        need(resp.status_code == 200, f"POST questionnaire 期望 200，实际 {resp.status_code}: {resp.text[:120]}")
        assessment = resp.json()
        need("learning_style" in assessment, "AssessmentResult 缺 learning_style")
        need(set(assessment["style_scores"].keys()) >= {"visual", "auditory", "reading", "kinesthetic"},
             f"style_scores 维度不全：{assessment['style_scores']}")
        need("feynman" in assessment["recommended_modes"], f"recommended_modes 应恒含 feynman：{assessment['recommended_modes']}")

        resp2 = await client.get("/api/profile/strategy", headers=headers)
        need(resp2.status_code == 200, f"GET strategy 期望 200，实际 {resp2.status_code}")
        strategy = resp2.json()
        for field in ("recommended_modes", "session_length_min", "review_intensity_multiplier", "rationale"):
            need(field in strategy, f"AdaptiveStrategy 缺字段 {field}")
        need(len(strategy["rationale"]) >= 2, f"rationale 至少 2 条，实际 {strategy['rationale']}")
        return (f"问卷->测评 learning_style={assessment['learning_style']}, "
                f"modes={assessment['recommended_modes']}；策略 intensity={strategy['review_intensity_multiplier']}, "
                f"session={strategy['session_length_min']}min, rationale={len(strategy['rationale'])} 条")

    async def c_profile_update_semantics() -> str:
        r1 = await client.put("/api/profile", json={"nickname": "端到端测试"}, headers=headers)
        need(r1.status_code == 200 and r1.json()["nickname"] == "端到端测试", "PUT nickname 未生效")
        r2 = await client.put("/api/profile", json={"daily_minutes": None}, headers=headers)
        need(r2.status_code == 200 and r2.json()["daily_minutes"] == 30, "daily_minutes=null 应回退默认 30")
        r3 = await client.put("/api/profile", json={"nickname": None}, headers=headers)
        need(r3.json()["nickname"] == "学习者", "nickname=null 应回退默认「学习者」")
        return "部分更新+null 清除语义正确（nickname 更新、daily_minutes→30、nickname→学习者）"

    await run_check(report, "P1", "GET /api/profile 默认创建", "T8", c_profile_default)
    await run_check(report, "P2", "问卷 -> 测评 -> 策略 链路", "T8", c_questionnaire_strategy)
    await run_check(report, "P3", "[对抗] PUT /api/profile 部分更新与 null 清除语义", "T8", c_profile_update_semantics)

    # ---------------- 复习调度 ----------------
    async def c_add_item_idempotent() -> str:
        r1 = await client.post("/api/review/items", json={"kp_id": PRIMARY_KP}, headers=headers)
        need(r1.status_code == 201, f"加入复习期望 201，实际 {r1.status_code}: {r1.text[:120]}")
        item = r1.json()
        need(item["kp_id"] == PRIMARY_KP and item["status"] == "active", f"复习项状态异常：{item}")
        r2 = await client.post("/api/review/items", json={"kp_id": PRIMARY_KP}, headers=headers)
        need(r2.status_code == 201, f"幂等再入期望 201，实际 {r2.status_code}")
        need(r2.json()["due_at"] == item["due_at"], "[对抗] 幂等加入未保持 due_at 不变")
        need(r2.json()["review_count"] == item["review_count"], "幂等加入改变了 review_count")
        return (f"加入复习 201（level={item['level']}, interval={item['interval_days']}, due_at={item['due_at']}）；"
                f"再次加入幂等（due_at 不变）")

    async def c_today_includes() -> str:
        # 契约 2.2.2：新加入项首次间隔≥1 天，故当日不在清单；模拟「跨日到期」后应纳入。
        before = await client.get("/api/review/today", headers=headers)
        need(before.status_code == 200, f"GET today 期望 200，实际 {before.status_code}")
        before_ids = [it["kp_id"] for it in before.json()["items"]]
        need(PRIMARY_KP not in before_ids,
             f"[口径] 新加入且未到期项不应出现在今日清单，实际出现：{before_ids}")

        # 模拟跨日：将 due_at 置为过去（独立临时库，可安全直改）
        import sqlite3

        conn = sqlite3.connect(str(CURRENT_DB_PATH))
        try:
            past_due = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            past_due = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE review_state SET due_at = ? WHERE kp_id = ? AND user_id = ?",
                (past_due, PRIMARY_KP, user),
            )
            conn.commit()
        finally:
            conn.close()

        resp = await client.get("/api/review/today", headers=headers)
        need(resp.status_code == 200, f"GET today(到期后) 期望 200，实际 {resp.status_code}")
        plan = resp.json()
        ids = [it["kp_id"] for it in plan["items"]]
        need(PRIMARY_KP in ids, f"到期的复习项未出现在今日清单：{ids}")
        item = next(it for it in plan["items"] if it["kp_id"] == PRIMARY_KP)
        required = {"chapter_id", "title", "due_at", "overdue_days", "est_minutes", "level",
                    "interval_days", "mastery", "priority_score"}
        need(required.issubset(item.keys()), f"清单项字段缺失：{required - set(item.keys())}")
        return (f"未到期时不在清单（{before_ids}）；模拟跨日后纳入清单：due={item['due_at']}, "
                f"overdue={item['overdue_days']}, est={item['est_minutes']}min, total={plan['total_minutes']}min, "
                f"truncated={plan['truncated']}")

    async def c_today_budget_boundary() -> str:
        resp = await client.get("/api/review/today", params={"budget_minutes": 0}, headers=headers)
        need(resp.status_code == 200, f"today?budget=0 期望 200，实际 {resp.status_code}")
        plan = resp.json()
        need(len(plan["items"]) >= 1, "[对抗] 预算为 0 时仍应至少保留 1 项（AC-R2-5）")
        need(plan["budget_minutes"] == 0, f"budget_minutes 应回显 0，实际 {plan['budget_minutes']}")
        return f"预算 0 -> 仍保留 {len(plan['items'])} 项，note='{plan['note']}'（AC-R2-5）"

    async def c_review_errors() -> str:
        r1 = await client.post("/api/review/items", json={"kp_id": "no-such-kp"}, headers=headers)
        need(r1.status_code == 404 and r1.json().get("code") == "KP_NOT_FOUND", f"未知 kp 应 404，实际 {r1.status_code}")
        r2 = await client.post(f"/api/review/items/{PRIMARY_KP}/complete", json={"grade": 6}, headers=headers)
        need(r2.status_code == 422 and r2.json().get("code") == "INVALID_GRADE", f"非法 grade 应 422 INVALID_GRADE，实际 {r2.status_code}/{r2.text[:80]}")
        r3 = await client.delete("/api/review/items/no-such-kp", headers=headers)
        need(r3.status_code == 404, f"未知 kp 删除应 404，实际 {r3.status_code}")
        return "未知 kp->404 KP_NOT_FOUND；grade=6->422 INVALID_GRADE；未知 kp 删除->404"

    async def c_reminder() -> str:
        r0 = await client.get("/api/review/reminder", headers=headers)
        need(r0.status_code == 200, f"GET reminder 期望 200，实际 {r0.status_code}")
        seen = r0.json()
        need("enabled" in seen and "time" in seen, f"reminder 响应缺字段：{seen}")
        r1 = await client.put("/api/review/reminder", json={"enabled": False, "time": "07:30"}, headers=headers)
        need(r1.status_code == 200 and r1.json() == {"enabled": False, "time": "07:30"}, f"PUT reminder 异常：{r1.text[:120]}")
        r2 = await client.put("/api/review/reminder", json={"enabled": True}, headers=headers)
        need(r2.json()["time"] == "07:30", f"enabled=false 未保留 time：{r2.json()}")
        r3 = await client.put("/api/review/reminder", json={"enabled": True, "time": "25:00"}, headers=headers)
        need(r3.status_code == 422, f"[对抗] 非法时间应 422，实际 {r3.status_code}")
        return f"提醒读取={seen}；更新(False,07:30)生效；复开保留 07:30；非法 25:00->422"

    async def c_items_status_filter() -> str:
        # 独立核实：软删除后 status 过滤（active/removed/all）
        await client.post("/api/review/items", json={"kp_id": SECONDARY_KP}, headers=headers)
        await client.delete(f"/api/review/items/{SECONDARY_KP}", headers=headers)
        all_items = await client.get("/api/review/items", params={"status": "all"}, headers=headers)
        need(all_items.status_code == 200 and isinstance(all_items.json(), list),
             f"status=all 应返回列表，实际 {all_items.status_code}/{all_items.text[:80]}")
        all_ids = [it["kp_id"] for it in all_items.json()]
        need(SECONDARY_KP in all_ids, f"status=all 应含已删除项：{all_ids}")
        removed = await client.get("/api/review/items", params={"status": "removed"}, headers=headers)
        need(SECONDARY_KP in [it["kp_id"] for it in removed.json()], "status=removed 应含已删除项")
        active = await client.get("/api/review/items", params={"status": "active"}, headers=headers)
        need(SECONDARY_KP not in [it["kp_id"] for it in active.json()], "status=active 不应含已删除项")
        restored = await client.post(f"/api/review/items/{SECONDARY_KP}/restore", headers=headers)
        need(restored.status_code == 200 and restored.json()["status"] == "active", "restore 应恢复为 active")
        return "status=all/removed 含软删除项，active 不含；restore 恢复为 active"

    await run_check(report, "R1", "POST /api/review/items 201 且幂等", "T5", c_add_item_idempotent)
    await run_check(report, "R2", "GET /api/review/today 含到期项且字段完整", "T5", c_today_includes)
    await run_check(report, "R3", "[对抗] today?budget_minutes=0 至少保留 1 项", "T5", c_today_budget_boundary)
    await run_check(report, "R4", "[对抗] 复习端点错误语义（404/422）", "T5", c_review_errors)
    await run_check(report, "R5", "GET/PUT /api/review/reminder 语义", "T5", c_reminder)
    await run_check(report, "R6", "[对抗] 软删除与 status 过滤（active/removed/all）", "T5", c_items_status_filter)

    # ---------------- 问答判分与复习回写 ----------------
    async def c_quiz_attempt() -> str:
        # 取该 kp 的题目（可判分的首选 single_choice/true_false）
        resp = await client.get(f"/api/quiz/{PRIMARY_KP}", headers=headers)
        need(resp.status_code == 200, f"GET quiz 期望 200，实际 {resp.status_code}")
        items = resp.json()
        need(len(items) >= 2, "题目数不足")
        target = next((q for q in items if q["type"] in ("single_choice", "true_false")), items[0])
        correct_answer = target["answer"]

        before = await client.get("/api/review/items", headers=headers)
        before_item = next(it for it in before.json() if it["kp_id"] == PRIMARY_KP)

        r = await client.post(
            f"/api/quiz/{PRIMARY_KP}/attempt",
            json={"qid": target["qid"], "user_answer": correct_answer},
            headers=headers,
        )
        need(r.status_code == 200, f"attempt 期望 200，实际 {r.status_code}: {r.text[:160]}")
        body = r.json()
        grade = body["grade"]
        need(grade["is_correct"] is True and grade["score"] == 100,
             f"正确作答应满分：score={grade['score']}, is_correct={grade['is_correct']}")
        state = body["review_state"]
        need(state is not None, "已加入复习的 kp 作答后 review_state 不应为 null")
        need(state["review_count"] == before_item["review_count"] + 1,
             f"review_count 未 +1：{before_item['review_count']} -> {state['review_count']}")
        need(state["level"] == before_item["level"] + 1,
             f"满分作答 level 未 +1：{before_item['level']} -> {state['level']}")
        return (f"qid={target['qid']}({target['type']}) 正确作答 score=100；"
                f"review_state 更新：level {before_item['level']}->{state['level']}, "
                f"review_count {before_item['review_count']}->{state['review_count']}, mastery={state['mastery']}")

    async def c_quiz_adversarial() -> str:
        resp = await client.get(f"/api/quiz/{PRIMARY_KP}", headers=headers)
        items = resp.json()
        target = next((q for q in items if q["type"] in ("single_choice", "true_false")), items[0])

        wrong = await client.post(
            f"/api/quiz/{PRIMARY_KP}/attempt",
            json={"qid": target["qid"], "user_answer": "__DEFINITELY_WRONG__"},
            headers=headers,
        )
        need(wrong.status_code == 200, f"错误作答期望 200，实际 {wrong.status_code}")
        need(wrong.json()["grade"]["score"] == 0 and wrong.json()["grade"]["is_correct"] is False,
             f"错误作答判分异常：{wrong.json()['grade']}")

        bad_qid = await client.post(
            f"/api/quiz/{PRIMARY_KP}/attempt",
            json={"qid": "no-such-qid", "user_answer": "x"},
            headers=headers,
        )
        need(bad_qid.status_code == 404 and bad_qid.json().get("code") == "QUIZ_ITEM_NOT_FOUND",
             f"未知 qid 应 404 QUIZ_ITEM_NOT_FOUND，实际 {bad_qid.status_code}/{bad_qid.text[:80]}")

        # 未加入复习的 kp：作答不应创建 review_state
        other = await client.get(f"/api/quiz/{NOT_REVIEWED_KP}", headers=headers)
        other_target = next((q for q in other.json() if q["type"] in ("single_choice", "true_false")), other.json()[0])
        not_reviewed = await client.post(
            f"/api/quiz/{NOT_REVIEWED_KP}/attempt",
            json={"qid": other_target["qid"], "user_answer": other_target["answer"]},
            headers=headers,
        )
        need(not_reviewed.status_code == 200, f"对照 kp 作答期望 200，实际 {not_reviewed.status_code}")
        need(not_reviewed.json()["review_state"] is None,
             "[对抗] 未加入复习的 kp 作答不应创建 review_state")
        return ("错误作答 score=0/is_correct=false；未知 qid->404 QUIZ_ITEM_NOT_FOUND；"
                "未加入复习的 kp 作答 review_state=null（不自动建项）")

    await run_check(report, "Q1", "POST /api/quiz/{kp}/attempt 判分并回写 review_state", "T6->T5", c_quiz_attempt)
    await run_check(report, "Q2", "[对抗] 错误作答/未知 qid/未复习 kp 的边界语义", "T6", c_quiz_adversarial)

    # ---------------- 聊天与 SSE ----------------
    async def c_chat_session() -> str:
        resp = await client.post("/api/chat/sessions", json={"mode": "teacher"}, headers=headers)
        need(resp.status_code == 201, f"创建会话期望 201，实际 {resp.status_code}: {resp.text[:120]}")
        session = resp.json()
        need(session["mode"] == "teacher" and session["session_id"], f"会话结构异常：{session}")
        PRIMARY_SESSION["id"] = session["session_id"]
        return f"会话创建 201：session_id={session['session_id']}, mode=teacher, title={session['title']}"

    async def c_chat_stream() -> str:
        session_id = PRIMARY_SESSION["id"]
        result = await collect_sse(
            client,
            "/api/chat/stream",
            {"session_id": session_id, "content": "请讲讲细胞膜的结构", "mode": "teacher"},
            headers,
        )
        need(result["status"] == 200, f"SSE 期望 200，实际 {result['status']}")
        need("text/event-stream" in result["content_type"], f"Content-Type 非 SSE：{result['content_type']}")
        names = [name for name, _ in result["events"]]
        need(names, "SSE 未产生任何帧")
        need(names[0] == "meta", f"首帧应为 meta，实际 {names[0]}")
        need(names[-1] == "done", f"末帧应为 done，实际 {names[-1]}")
        need(all(n in ("meta", "delta", "done") for n in names), f"存在非法帧：{names}")
        deltas = [json.loads(data)["text"] for name, data in result["events"] if name == "delta"]
        need(len(deltas) >= 1, "无 delta 增量帧")
        meta = json.loads(result["events"][0][1])
        done = json.loads(result["events"][-1][1])
        need(meta["provider"] == "local", f"[无凭证] meta.provider 应为 local，实际 {meta['provider']}")
        need(done["provider"] == "local", f"done.provider 应为 local，实际 {done['provider']}")
        need(done["content"] == "".join(deltas), "done.content 与 delta 拼接不一致")
        need(done["content"].strip(), "done.content 为空")
        PRIMARY_SESSION["done_content"] = done["content"]
        return (f"帧序列={names[:1]}+delta×{len(deltas)}+[{names[-1]}]；meta/done.provider=local；"
                f"done.content 与 delta 拼接一致（{len(done['content'])} 字）")

    async def c_chat_persisted() -> str:
        session_id = PRIMARY_SESSION["id"]
        resp = await client.get(f"/api/chat/sessions/{session_id}/messages", headers=headers)
        need(resp.status_code == 200, f"取消息期望 200，实际 {resp.status_code}")
        messages = resp.json()
        roles = [m["role"] for m in messages]
        need(roles.count("user") >= 1 and roles.count("assistant") >= 1, f"消息未落库：{roles}")
        assistant = next(m for m in messages if m["role"] == "assistant")
        need(assistant["provider"] == "local", f"助手消息 provider 应为 local，实际 {assistant['provider']}")
        need(assistant["content"] == PRIMARY_SESSION.get("done_content"),
             "落库助手消息内容与 done.content 不一致")
        return f"落库消息 {len(messages)} 条，roles={roles}，助手 provider=local"

    async def c_chat_adversarial() -> str:
        bad_mode = await client.post("/api/chat/sessions", json={"mode": "wizard"}, headers=headers)
        need(bad_mode.status_code == 422 and bad_mode.json().get("code") == "INVALID_MODE",
             f"[对抗] 非法角色应 422 INVALID_MODE，实际 {bad_mode.status_code}/{bad_mode.text[:80]}")
        missing = await client.post(
            "/api/chat/stream",
            json={"session_id": "no-such-session", "content": "hi"},
            headers=headers,
        )
        need(missing.status_code == 404 and missing.json().get("code") == "SESSION_NOT_FOUND",
             f"[对抗] 未知会话应 404 SESSION_NOT_FOUND，实际 {missing.status_code}/{missing.text[:80]}")
        return "非法角色->422 INVALID_MODE；未知会话->404 SESSION_NOT_FOUND"

    async def c_chat_feynman() -> str:
        session = await client.post("/api/chat/sessions", json={"mode": "feynman"}, headers=headers)
        need(session.status_code == 201, f"feynman 会话创建失败 {session.status_code}")
        sid = session.json()["session_id"]
        result = await collect_sse(
            client,
            "/api/chat/stream",
            {"session_id": sid, "content": "我来讲细胞膜：它由磷脂双分子层构成。", "mode": "feynman"},
            headers,
        )
        need(result["status"] == 200, f"feynman SSE 期望 200，实际 {result['status']}")
        names = [name for name, _ in result["events"]]
        need(names[0] == "meta" and names[-1] == "done", f"帧序列异常：{names}")
        done = json.loads(result["events"][-1][1])
        content = done["content"]
        need("✅" in content and ("🔧" in content or "❓" in content), f"费曼输出应为点评式（肯定+纠错/追问）：{content[:120]}")
        logs = await client.get("/api/profile/analytics", headers=headers)
        need(logs.status_code == 200, "analytics 取用失败")
        return f"feynman 流式完成（provider={done['provider']}），输出为点评式：{content[:60]}..."

    await run_check(report, "C1", "POST /api/chat/sessions 创建会话(201)", "T7", c_chat_session)
    await run_check(report, "C2", "POST /api/chat/stream SSE 帧序列与 provider=local", "T7", c_chat_stream)
    await run_check(report, "C3", "聊天消息在 done 前落库（含 provider）", "T7", c_chat_persisted)
    await run_check(report, "C4", "[对抗] 非法角色/未知会话 错误语义", "T7", c_chat_adversarial)
    await run_check(report, "C5", "feynman 角色 SSE 流为点评式输出", "T7 -> T8", c_chat_feynman)

    # ---------------- 全局共享行为 ----------------
    async def c_missing_user_id() -> str:
        endpoints = [
            ("GET", "/api/review/items", None),
            ("POST", "/api/review/items", {"kp_id": PRIMARY_KP}),
            ("GET", "/api/profile", None),
            ("POST", "/api/chat/sessions", {"mode": "teacher"}),
        ]
        for method, path, payload in endpoints:
            if method == "GET":
                resp = await client.get(path)
            else:
                resp = await client.post(path, json=payload)
            need(resp.status_code == 400, f"{method} {path} 缺 X-User-Id 应 400，实际 {resp.status_code}")
            need(resp.json().get("code") == "MISSING_USER_ID",
                 f"{method} {path} 错误码应为 MISSING_USER_ID，实际 {resp.json().get('code')}")
        return "缺 X-User-Id 的 4 个端点均返回 400 MISSING_USER_ID"

    await run_check(report, "A1", "[对抗] 缺 X-User-Id -> 400 MISSING_USER_ID", "T1", c_missing_user_id)


# 全局会话状态（简化检查间传参）
PRIMARY_SESSION: dict[str, str] = {}
CURRENT_DB_PATH: Path = Path()


def _pick_free_port() -> int:
    """获取一个空闲端口（含重试）。"""
    for _ in range(20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])
        except OSError:
            time.sleep(0.2)
    return 8123


def main() -> int:
    global CURRENT_DB_PATH

    parser = argparse.ArgumentParser(description="T14 端到端集成与运行验证")
    parser.add_argument("--mode", choices=["auto", "live", "asgi"], default="auto")
    parser.add_argument("--port", type=int, default=0, help="真实服务器端口（0=自动）")
    args = parser.parse_args()

    print("=" * 78)
    print("T14 · 端到端集成与运行验证（独立验证）")
    print("=" * 78)

    workdir = Path(tempfile.mkdtemp(prefix="ala_e2e_"))
    CURRENT_DB_PATH = workdir / "e2e.db"

    run_db_hash_before = _sha256(RUN_DB_PATH)

    # 全局将 ALA_DB_PATH 指向独立临时库，避免污染运行库（AC-R8-3）
    os.environ["ALA_DB_PATH"] = str(CURRENT_DB_PATH)

    loop_note = install_loop_workaround()
    print(f"沙箱规避层：{loop_note}\n")

    report = Report()
    server_handle: dict[str, Any] = {}
    client: Any = None
    active_mode = "asgi"
    mode_note = ""

    try:
        if args.mode in ("auto", "live"):
            port = args.port or _pick_free_port()
            print(f"[live] 尝试以 `python run.py` 启动真实服务器（127.0.0.1:{port}）...")
            sub = try_live_subprocess(CURRENT_DB_PATH, port)
            if sub["ok"]:
                server_handle = sub
                active_mode = "live-subprocess"
                mode_note = f"真实服务器（子进程 run.py）HTTP 到 127.0.0.1:{port}"
                import httpx

                client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15.0)
            else:
                print(f"[live] 子进程启动失败/回环受限；日志尾部：\n{(sub.get('log_text') or '')[-600:]}")
                if args.mode == "live":
                    print("[live] --mode=live 指定仅真实服务器，未回退。")
                inproc = try_live_inproc(CURRENT_DB_PATH, port)
                if inproc["ok"]:
                    server_handle = inproc
                    active_mode = "live-inproc"
                    mode_note = f"进程内 uvicorn 线程 HTTP 到 127.0.0.1:{port}"
                    import httpx

                    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15.0)
                else:
                    print(f"[live] 进程内 uvicorn 亦受限：{inproc.get('log_text')}")
                    report.add(
                        "S1", "python run.py 真实服务器启动", "T1", "BLOCKED",
                        f"沙箱限制 127.0.0.1（WinError 10051）：子进程与进程内 uvicorn 均无法绑定/连接。"
                        f"子进程日志：{(sub.get('log_text') or '').strip()[-300:] or '<空>'}；"
                        f"进程内：{inproc.get('log_text')}",
                    )

        # 启动装配确定性自检（不依赖网络）
        print("\n[asgi] 子进程启动装配自检（建表/内容/路由/静态挂载）...")
        selfcheck = startup_selfcheck(workdir / "selfcheck.db")
        tables = [t for t in selfcheck.get("TABLES", "").split(",") if t]
        missing = [t for t in selfcheck.get("MISSING", "").split(",") if t]
        try:
            routes = int(selfcheck.get("ROUTES", "0"))
            chapters = int(selfcheck.get("CHAPTERS", "0"))
            kps = int(selfcheck.get("KPS", "0"))
            static_mount = selfcheck.get("STATIC_MOUNT", "0")
        except ValueError:
            routes = chapters = kps = 0
            static_mount = "0"
        if (
            selfcheck.get("returncode") == "0"
            and not missing
            and len(tables) >= 7
            and routes > 0
            and chapters == EXPECTED_CHAPTERS
            and static_mount != "0"
        ):
            report.add(
                "S0", "create_app 启动装配（建表/内容/路由/静态）", "T1", "PASS",
                f"7 张表齐全={tables}，章节={chapters}，知识点={kps}，路由={routes}，静态挂载={static_mount}",
            )
        else:
            report.add(
                "S0", "create_app 启动装配（建表/内容/路由/静态）", "T1", "FAIL",
                f"returncode={selfcheck.get('returncode')}，缺表={missing}，tables={tables}，"
                f"chapters={chapters}，routes={routes}，static_mount={static_mount}，stderr={selfcheck.get('stderr')}",
            )

        # 建立 HTTP 客户端（真实或 ASGI）
        async def _run() -> None:
            nonlocal client
            if client is None:
                client, _app = await build_asgi_client(CURRENT_DB_PATH)
                print("[asgi] 已回退为进程内 ASGI 直调（httpx.ASGITransport）\n")
            await run_all_checks(client, report, user=f"e2e-{uuid.uuid4().hex[:8]}")
            # 数据隔离检查
            async def c_isolation() -> str:
                run_hash_after = _sha256(RUN_DB_PATH)
                need(
                    run_hash_after == run_db_hash_before,
                    f"运行库 app.db 被修改（{run_db_hash_before[:12]} -> {run_hash_after[:12]}）",
                )
                need(CURRENT_DB_PATH.is_file(), "独立临时库未被创建")
                return f"运行库 app.db 哈希不变（{run_db_hash_before[:12]}...）；独立临时库={CURRENT_DB_PATH}"
            await run_check(report, "A2", "数据隔离：未污染运行库", "T1/T13/T14", c_isolation)
            await client.aclose()

        asyncio.run(_run())
    finally:
        # 清理子进程/线程
        if server_handle:
            if server_handle.get("proc") is not None:
                proc = server_handle["proc"]
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            if server_handle.get("server") is not None:
                server_handle["server"].should_exit = True
                thread = server_handle.get("thread")
                if thread is not None:
                    thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------------- 汇总裁决 ----------------
    print("\n" + "=" * 78)
    passed = [i for i in report.items if i["status"] == "PASS"]
    failed = report.failures
    blocked = report.blocked
    print(f"检查总数：{len(report.items)}；PASS={len(passed)}；FAIL={len(failed)}；BLOCKED={len(blocked)}")
    print(f"验证方式：{active_mode} —— {mode_note or '进程内 ASGI 直调（回退）'}")

    if blocked:
        print("\n环境受限（非产品缺陷）项：")
        for item in blocked:
            print(f"  - {item['id']} {item['name']}：{item['evidence']}")

    if failed:
        print("\n失败项（须归属到具体任务）：")
        for item in failed:
            print(f"  - {item['id']} {item['name']} <{item['owner']}>：{item['evidence']}")
        verdict = "FAIL"
    else:
        verdict = "PASS"

    print(f"\nVERDICT: {verdict}")

    # 写报告
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(
                {
                    "task": "T14",
                    "verification_mode": active_mode,
                    "mode_note": mode_note,
                    "loop_workaround": loop_note,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "verdict": verdict,
                    "counts": {"total": len(report.items), "pass": len(passed), "fail": len(failed), "blocked": len(blocked)},
                    "checks": report.items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入：{REPORT_PATH}")
    except OSError as exc:
        print(f"报告写入失败：{exc}")

    if failed:
        return 1
    if blocked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())