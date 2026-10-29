"""pytest 共享夹具（契约 T13，plan 3.3）。

核心约束（AC-R8-3）：**测试使用独立临时数据库，绝不触碰运行库**
``backend/app/data/app.db``。

实现要点：
1. 在本模块被导入时（早于任何 ``backend.app.*`` 模块被导入）即把环境变量
   ``ALA_DB_PATH`` 指向会话级临时目录，从而保证 ``backend.app.main`` 的模块级
   ``create_app()`` 不会初始化/写入运行库；
2. ``temp_db_path`` 夹具为**每个测试**创建独立临时库（``tempfile.mkdtemp``）并
   指向 ``ALA_DB_PATH``，测试结束清理；
3. ``client`` 夹具返回一个**进程内路由直调客户端** ``DirectClient``。

.. note::
    **为何不使用 FastAPI ``TestClient``？**
    本仓库运行环境的 pytest 进程被网络沙箱限制，禁止一切 ``127.0.0.1`` 环回套接字
    （``socket.socketpair`` 回退为 TCP 环回并被阻断），因此**无法创建任何 asyncio
    事件循环**（``TestClient`` 依赖 anyio 线程 portal）。这属于 plan 2.1.1 / O4 明确
    允许的回退场景：改用「直接调用路由函数」。``DirectClient`` 会读取真实路由表，
    复现依赖注入（用户身份 / 数据库连接 / 内容仓库）、请求体与查询参数校验，以及
    统一错误体（``ApiError`` / ``HTTPException`` / 422），从而仍然端到端地验证契约，
    但不依赖套接字与事件循环。
"""

from __future__ import annotations

import inspect
import json as _json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

# ---------------------------------------------------------------------------
# 步骤 0：保证仓库根在 sys.path 中，使 `import backend.app...` 可用。
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 运行库路径（仅用于断言未被污染，绝不写入）。
RUN_DB_PATH = REPO_ROOT / "backend" / "app" / "data" / "app.db"

# ---------------------------------------------------------------------------
# 步骤 1（关键）：在任何 backend 模块导入前，把库路径指向临时目录。
# ---------------------------------------------------------------------------
_SESSION_DB_DIR = tempfile.mkdtemp(prefix="ala_test_session_")
os.environ["ALA_DB_PATH"] = str(Path(_SESSION_DB_DIR) / "session_default.db")

import pytest  # noqa: E402  （必须在设置环境变量之后导入）


# ---------------------------------------------------------------------------
# 进程内路由直调客户端
# ---------------------------------------------------------------------------


class Response:
    """极简 HTTP 响应对象（与 TestClient 的常用接口兼容）。"""

    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return _json.loads(self.text)


def _extract_bounds(field: Any) -> tuple[Any, Any]:
    """从 FastAPI ``Query`` 字段中提取 ``ge`` / ``le`` 约束（pydantic v2 存于 ``metadata``）。"""
    ge = getattr(field, "ge", None)
    le = getattr(field, "le", None)
    for meta in getattr(field, "metadata", None) or []:
        if ge is None and hasattr(meta, "ge"):
            ge = meta.ge
        if le is None and hasattr(meta, "le"):
            le = meta.le
    return ge, le


class _QueryValidationError(Exception):
    """查询参数校验失败（用于复现 FastAPI 的 422）。"""

    def __init__(self, location: str, message: str, error_type: str = "value_error") -> None:
        self.location = location
        self.message = message
        self.error_type = error_type
        super().__init__(message)


def _coerce(annotation: Any, raw: Any) -> Any:
    """把字符串原始值按注解类型转换（支持 Optional / Union / bool / int / float）。"""
    if raw is None:
        return None
    if annotation is inspect.Parameter.empty or annotation is Any:
        return raw
    origin = get_origin(annotation)
    if origin is Literal:
        text = str(raw)
        allowed = get_args(annotation)
        if text not in allowed:
            raise ValueError(f"{text!r} 不在允许取值 {allowed} 中")
        return text
    if origin is None and isinstance(annotation, type):
        return _coerce_scalar(annotation, raw)
    if origin in (dict, list, tuple, set):
        return raw
    # Union / Optional / Literal
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    for arg in args:
        if arg is Any:
            return raw
        if get_origin(arg) is Literal:
            text = str(raw)
            allowed = get_args(arg)
            if text not in allowed:
                raise ValueError(f"{text!r} 不在允许取值 {allowed} 中")
            return text
        try:
            return _coerce_scalar(arg, raw)
        except (TypeError, ValueError):
            continue
    raise ValueError(f"无法将 {raw!r} 转换为 {annotation!r}")


def _coerce_scalar(scalar: Any, raw: Any) -> Any:
    if scalar is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if scalar is int:
        return int(raw)
    if scalar is float:
        return float(raw)
    if scalar is str:
        return str(raw)
    raise ValueError(f"不支持的标量类型：{scalar!r}")


class _RequestStub:
    """供依赖函数读取 ``request.app`` 的最小请求对象。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.state = getattr(app, "state", None)


def _collect_api_routes(routes: Any, collected: list[Any] | None = None) -> list[Any]:
    """递归提取 FastAPI 的 ``APIRoute``。

    新版 FastAPI（0.162）用 ``_IncludedRouter`` 包裹被 ``include_router`` 的子路由，
    其路由位于 ``original_router.routes``；此处兼容多层嵌套与普通 ``.routes``。
    """
    from fastapi.routing import APIRoute

    if collected is None:
        collected = []
    for route in routes:
        if isinstance(route, APIRoute):
            collected.append(route)
            continue
        sub = getattr(route, "routes", None)
        if sub:
            _collect_api_routes(sub, collected)
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            _collect_api_routes(getattr(original, "routes", []) or [], collected)
    return collected


class DirectClient:
    """读取 FastAPI 路由表并直接调用端点函数的进程内客户端。"""

    def __init__(self, app: Any) -> None:
        self.app = app

        self._routes: list[tuple[Any, re.Pattern[str]]] = []
        for route in _collect_api_routes(app.routes):
            pattern = self._compile(route.path)
            self._routes.append((route, pattern))

    @staticmethod
    def _compile(path: str) -> re.Pattern[str]:
        """把 ``/a/{b}/c`` 路径模板编译为带命名组的正则。"""
        escaped = re.escape(path)
        escaped = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", escaped)
        return re.compile(f"^{escaped}$")

    def _find_route(self, method: str, path: str):
        for route, pattern in self._routes:
            if method.upper() not in route.methods:
                continue
            match = pattern.match(path)
            if match is not None:
                return route, match.groupdict()
        return None, None

    # -- 请求入口 -----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        route, path_values = self._find_route(method, path)
        if route is None:
            body = _json.dumps(
                {"code": "NOT_FOUND", "message": "路由不存在"}, ensure_ascii=False
            ).encode("utf-8")
            return Response(404, {"content-type": "application/json"}, body)

        connections: list[sqlite3.Connection] = []
        try:
            kwargs = self._build_kwargs(route, path_values, params or {}, json_body, headers or {}, connections)
            result = self._call_endpoint(route, kwargs)
            return self._build_response(route, result)
        except _QueryValidationError as exc:
            return self._error_response(
                422,
                "VALIDATION_ERROR",
                "请求参数校验失败",
                [{"loc": ["query", exc.location], "msg": exc.message, "type": exc.error_type}],
            )
        except Exception as exc:  # noqa: BLE001 - 复现 main.py 的统一异常处理
            return self._handle_unexpected(exc)
        finally:
            for conn in connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("GET", path, params=params, headers=headers)

    def post(self, path: str, json: Any = None, params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("POST", path, params=params, json_body=json, headers=headers)

    def put(self, path: str, json: Any = None, params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("PUT", path, params=params, json_body=json, headers=headers)

    def delete(self, path: str, params: dict | None = None, headers: dict | None = None) -> Response:
        return self.request("DELETE", path, params=params, headers=headers)

    # -- 依赖注入复现 -------------------------------------------------------

    def _build_kwargs(
        self,
        route: Any,
        path_values: dict[str, str],
        params: dict[str, Any],
        json_body: Any,
        headers: dict[str, str],
        connections: list[sqlite3.Connection],
    ) -> dict[str, Any]:
        from fastapi import params as fastapi_params
        from pydantic import BaseModel, ValidationError

        from backend.app import db as db_module
        from backend.app.data.content_loader import get_repository
        from backend.app.deps import ApiError, get_content_repo, get_db, get_user_id

        endpoint = route.endpoint
        # 解析字符串注解（模块使用 `from __future__ import annotations`）。
        signature = inspect.signature(endpoint, eval_str=True)
        kwargs: dict[str, Any] = {}

        for name, parameter in signature.parameters.items():
            annotation = parameter.annotation
            default = parameter.default

            # 1) 路径参数
            if name in path_values:
                kwargs[name] = path_values[name]
                continue

            # 2) 依赖注入（按依赖函数映射）
            if isinstance(default, fastapi_params.Depends):
                dependency = default.dependency
                if dependency is get_user_id:
                    raw_uid = headers.get("X-User-Id")
                    if raw_uid is None or not str(raw_uid).strip():
                        raise ApiError(400, "MISSING_USER_ID", "缺少 X-User-Id 请求头")
                    kwargs[name] = str(raw_uid).strip()
                elif dependency is get_db:
                    conn = db_module.connect()
                    connections.append(conn)
                    kwargs[name] = conn
                elif dependency is get_content_repo:
                    repo = getattr(getattr(self.app, "state", None), "content_repo", None)
                    kwargs[name] = repo if repo is not None else get_repository()
                else:
                    # 其它依赖（如 knowledge.py 自带的 request 版 get_repository）
                    kwargs[name] = self._call_dependency(dependency)
                continue

            # 3) 请求体（Pydantic 模型）
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                data = json_body if json_body is not None else {}
                try:
                    kwargs[name] = annotation.model_validate(data)
                except ValidationError as exc:
                    raise _QueryValidationError(
                        name, "请求体校验失败", "validation_error"
                    ) from exc
                continue

            # 4) 查询参数（FastAPI Query(...)）
            if isinstance(default, fastapi_params.Query):
                raw = params.get(name)
                if raw is None:
                    if default.default is None or default.default is inspect.Parameter.empty:
                        kwargs[name] = default.default
                    else:
                        kwargs[name] = default.default
                    continue
                try:
                    value = _coerce(annotation, raw)
                except (TypeError, ValueError) as exc:
                    raise _QueryValidationError(name, str(exc), "type_error") from exc
                ge, le = _extract_bounds(default)
                if ge is not None and value is not None and value < ge:
                    raise _QueryValidationError(
                        name, f"输入应大于或等于 {ge}", "greater_than_equal"
                    )
                if le is not None and value is not None and value > le:
                    raise _QueryValidationError(
                        name, f"输入应小于或等于 {le}", "less_than_equal"
                    )
                kwargs[name] = value
                continue

            # 5) 普通默认值参数
            raw = params.get(name, default)
            try:
                kwargs[name] = _coerce(annotation, raw) if raw is not None else raw
            except (TypeError, ValueError):
                kwargs[name] = raw

        return kwargs

    def _call_dependency(self, dependency: Any) -> Any:
        """调用未显式映射的依赖函数（自动注入 ``request`` 桩）。"""
        signature = inspect.signature(dependency)
        if "request" in signature.parameters:
            return dependency(request=_RequestStub(self.app))
        return dependency()

    # -- 端点调用（支持无事件循环驱动协程 / 异步生成器） --------------------

    def _call_endpoint(self, route: Any, kwargs: dict[str, Any]) -> Any:
        endpoint = route.endpoint
        if inspect.iscoroutinefunction(endpoint):
            return self._run_coroutine(endpoint(**kwargs))
        return endpoint(**kwargs)

    @staticmethod
    def _run_coroutine(coroutine: Any) -> Any:
        """手动驱动协程（不依赖事件循环）。

        本仓库被测的异步端点（如 ``quiz.submit_attempt``）在测试路径上不 await
        任何需要事件循环的对象（无 sleep / 线程池 / Future），故可用 ``send(None)``
        协议直接驱动至完成。
        """
        try:
            coroutine.send(None)
        except StopIteration as stop:
            return stop.value
        raise RuntimeError("协程未在无事件循环下完成（存在需要事件循环的 await）")

    # -- 响应装配 -----------------------------------------------------------

    def _build_response(self, route: Any, result: Any) -> Response:
        from pydantic import BaseModel
        from starlette.responses import Response as StarletteResponse, StreamingResponse

        if isinstance(result, StreamingResponse):
            text = "".join(self._collect_async_iter(result.body_iterator))
            headers = _headers_to_dict(result.headers)
            if "content-type" not in headers:
                headers["content-type"] = str(result.media_type)
            return Response(result.status_code, headers, text.encode("utf-8"))

        if isinstance(result, StarletteResponse):
            return Response(result.status_code, _headers_to_dict(result.headers), bytes(result.body))

        status_code = getattr(route, "status_code", 200) or 200

        def _dump(value: Any) -> Any:
            if isinstance(value, BaseModel):
                return value.model_dump(mode="json")
            return value

        if isinstance(result, BaseModel):
            payload: Any = result.model_dump(mode="json")
        elif isinstance(result, list):
            payload = [_dump(item) for item in result]
        else:
            payload = _dump(result)

        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return Response(status_code, {"content-type": "application/json"}, body)

    @staticmethod
    def _collect_async_iter(async_iterable: Any) -> list[str]:
        """手动驱动异步迭代器，收集全部字符串分片（无需事件循环）。

        注意：在无事件循环下手动 ``send(None)`` 驱动 ``async_generator_asend`` 时，
        每个已产出的分片会作为 ``StopIteration.value`` 抛出，而真正结束时抛出
        ``StopAsyncIteration``。
        """
        chunks: list[str] = []
        iterator = async_iterable.__aiter__()
        while True:
            try:
                next_coro = iterator.__anext__()
            except StopAsyncIteration:
                break
            try:
                chunk = next_coro.send(None)
            except StopIteration as stop:
                chunk = stop.value
            except StopAsyncIteration:
                break
            if chunk is None:
                break
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode("utf-8"))
            else:
                chunks.append(str(chunk))
        return chunks

    # -- 错误装配 -----------------------------------------------------------

    def _handle_unexpected(self, exc: Exception) -> Response:
        from fastapi import HTTPException
        from pydantic import ValidationError
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from backend.app.deps import ApiError

        if isinstance(exc, ApiError):
            return self._error_response(exc.status_code, exc.code, exc.message, exc.detail)
        if isinstance(exc, (HTTPException, StarletteHTTPException)):
            detail = exc.detail
            if isinstance(detail, dict) and "code" in detail:
                return self._error_response(
                    exc.status_code,
                    str(detail.get("code")),
                    str(detail.get("message", detail.get("code", "错误"))),
                    detail.get("detail"),
                )
            message = detail if isinstance(detail, str) else "请求处理失败"
            return self._error_response(exc.status_code, "HTTP_ERROR", message)
        if isinstance(exc, ValidationError):
            return self._error_response(
                422, "VALIDATION_ERROR", "请求参数校验失败", exc.errors()
            )
        raise exc

    @staticmethod
    def _error_response(status_code: int, code: str, message: str, detail: Any = None) -> Response:
        body: dict[str, Any] = {"code": code, "message": message}
        if detail is not None:
            body["detail"] = detail
        return Response(
            status_code,
            {"content-type": "application/json"},
            _json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )


def _headers_to_dict(headers: Any) -> dict[str, str]:
    """把 Starlette ``Headers`` 转为小写键的普通 dict。"""
    result: dict[str, str] = {}
    for key, value in headers.items():
        result[str(key).lower()] = str(value)
    return result


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db_path(monkeypatch):
    """每测试独立的临时 SQLite 库文件，并把 ``ALA_DB_PATH`` 指向它。

    :returns: 该临时库的绝对路径（``pathlib.Path``）。
    """
    from backend.app.db import init_db

    work_dir = Path(tempfile.mkdtemp(prefix="ala_test_"))
    db_file = work_dir / "ala_test.db"
    monkeypatch.setenv("ALA_DB_PATH", str(db_file))
    init_db(db_file)
    try:
        yield db_file
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@pytest.fixture()
def runtime_db_path() -> Path:
    """返回运行库路径（仅用于断言未被测试写入）。"""
    return RUN_DB_PATH


@pytest.fixture()
def app(temp_db_path):
    """基于当前 ``ALA_DB_PATH`` 组装并返回 FastAPI 应用实例。"""
    from backend.app.main import create_app

    return create_app()


@pytest.fixture()
def client(app) -> DirectClient:
    """返回指向临时库的进程内路由直调客户端。"""
    return DirectClient(app)


@pytest.fixture()
def user_id() -> str:
    """样例用户 id（对应前端 ``localStorage['ala_user_id']`` 语义）。"""
    return "test-user-0001"


@pytest.fixture()
def auth_headers(user_id: str) -> dict[str, str]:
    """携带 ``X-User-Id`` 的请求头。"""
    return {"X-User-Id": user_id}


@pytest.fixture()
def repo():
    """返回进程级内容仓库（真实章节内容，只读）。"""
    from backend.app.data.content_loader import get_repository

    get_repository().load_all()
    return get_repository()