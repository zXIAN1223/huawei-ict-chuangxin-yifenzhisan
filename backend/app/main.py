"""应用组装：FastAPI 实例、静态资源、首页、路由容错注册与统一异常处理。

启动顺序（plan 2.3.1）：create_app() -> init_db() -> ContentRepository.load_all()
-> 注册路由 -> 挂载静态 -> 交由 run.py/uvicorn 启动。
"""

from __future__ import annotations

import importlib
import logging
from typing import Iterable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.config import get_settings
from backend.app.data.content_loader import get_repository
from backend.app.db import init_db
from backend.app.deps import ApiError

logger = logging.getLogger(__name__)

#: 待注册的业务路由（模块缺失时容错跳过，支持渐进式开发）
ROUTER_MODULES: tuple[tuple[str, str], ...] = (
    ("knowledge", "backend.app.routers.knowledge"),
    ("review", "backend.app.routers.review"),
    ("quiz", "backend.app.routers.quiz"),
    ("chat", "backend.app.routers.chat"),
    ("profile", "backend.app.routers.profile"),
    ("asr", "backend.app.routers.asr"),
)

PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI 辅助学习助手</title>
  <style>
    body { font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
           margin: 0; padding: 3rem 1.5rem; background: #f6f8fb; color: #1f2a37; }
    .card { max-width: 640px; margin: 0 auto; background: #fff; border-radius: 12px;
            padding: 2rem; box-shadow: 0 6px 24px rgba(15, 23, 42, .08); }
    h1 { margin: 0 0 .75rem; font-size: 1.5rem; }
    p { line-height: 1.7; color: #475569; }
    code { background: #eef2f7; padding: .15rem .4rem; border-radius: 4px; }
    ul { line-height: 1.9; }
  </style>
</head>
<body>
  <div class="card">
    <h1>AI 辅助学习助手</h1>
    <p>后端已就绪，但尚未找到前端页面 <code>frontend/index.html</code>。</p>
    <p>可用入口：</p>
    <ul>
      <li>API 文档：<a href="/docs">/docs</a></li>
      <li>健康检查：<a href="/health">/health</a></li>
    </ul>
  </div>
</body>
</html>
"""


def _error_response(
    status_code: int,
    code: str,
    message: str,
    detail: object | None = None,
) -> JSONResponse:
    """构造统一错误体 `{"code", "message", "detail"?}`。"""
    body: dict[str, object] = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def _register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        # 若下游任务通过 HTTPException(detail={"code": ...}) 抛错，保持其语义
        if isinstance(detail, dict) and "code" in detail:
            return _error_response(
                exc.status_code,
                str(detail.get("code")),
                str(detail.get("message", detail.get("code", "错误"))),
                detail.get("detail"),
            )
        message = detail if isinstance(detail, str) else "请求处理失败"
        return _error_response(exc.status_code, "HTTP_ERROR", message)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "请求参数校验失败",
            exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获的服务器异常：%s", exc)
        return _error_response(500, "INTERNAL_ERROR", "服务器内部错误")


def _safe_include(app: FastAPI, name: str, module_path: str) -> None:
    """容错注册路由模块。

    模块缺失/导入失败/未暴露 `router` 时记录 warning 并跳过，不阻塞启动。
    """
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        logger.warning("路由模块 `%s` 尚未就绪，已跳过：%s", module_path, exc)
        return
    except Exception as exc:  # noqa: BLE001 - 容错：任何导入错误都不应阻塞启动
        logger.warning("导入路由模块 `%s` 失败，已跳过：%s", module_path, exc)
        return

    router = getattr(module, "router", None)
    if router is None:
        logger.warning("路由模块 `%s` 未暴露 `router`，已跳过", module_path)
        return

    app.include_router(router)
    logger.info("已注册路由：%s", name)


def create_app() -> FastAPI:
    """构建并返回 FastAPI 应用实例。"""
    settings = get_settings()
    app = FastAPI(
        title="AI 辅助学习助手",
        description="高中生物知识点学习、记忆曲线复习、主动回忆与 AI 讨论",
        version="1.0.0",
    )

    _register_exception_handlers(app)

    # 1) 初始化数据库（建表，幂等）
    init_db(settings.db_path)
    logger.info("数据库已初始化：%s", settings.db_path)

    # 2) 加载并校验内容库（缺文件仅 warning；文件非法则 fail-fast）
    repository = get_repository(settings.data_dir)
    repository.load_all()
    app.state.content_repo = repository

    # 3) 静态资源：/static/** -> frontend/
    settings.frontend_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/static",
        StaticFiles(directory=str(settings.frontend_dir)),
        name="static",
    )

    # 4) 首页：有 index.html 则返回，否则返回可读占位（不 500）
    @app.get("/", include_in_schema=False, response_model=None)
    async def index():
        index_file = settings.frontend_dir / "index.html"
        if index_file.is_file():
            return FileResponse(str(index_file))
        return HTMLResponse(PLACEHOLDER_HTML)

    @app.get("/health", tags=["meta"], response_model=None)
    async def health():
        return {
            "status": "ok",
            "content_loaded": repository.loaded,
            "chapters": len(repository.list_chapters()),
        }

    # 5) 注册业务路由（容错）
    for name, module_path in ROUTER_MODULES:
        _safe_include(app, name, module_path)

    logger.info("应用初始化完成")
    return app


#: 模块级实例：供 uvicorn（`backend.app.main:app`）与 import 检查使用
app = create_app()


__all__ = ["app", "create_app"]