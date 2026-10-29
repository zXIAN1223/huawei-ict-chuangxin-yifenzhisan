"""FastAPI 依赖与统一错误类型。

- `get_user_id`：读取 `X-User-Id` 请求头，缺失时抛 `ApiError(400, MISSING_USER_ID)`。
- `get_db`：为每个请求提供独立的 SQLite 连接，请求结束后关闭。
- `get_content_repo`：提供进程级 `ContentRepository`。
- `ApiError`：统一错误体的推荐抛出类型，由 `main.py` 的异常处理器转换。
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Header

from backend.app import db as db_module
from backend.app.data.content_loader import ContentRepository, get_repository


class ApiError(Exception):
    """携带统一错误体信息的业务异常。

    错误体形状：`{"code": <string>, "message": <string>, "detail"?: <object>}`。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


def get_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str:
    """读取并返回 `X-User-Id` 请求头；缺失或空白时抛 400 MISSING_USER_ID。"""
    if x_user_id is None or not x_user_id.strip():
        raise ApiError(400, "MISSING_USER_ID", "缺少 X-User-Id 请求头")
    return x_user_id.strip()


def get_db() -> Iterator[sqlite3.Connection]:
    """为每个请求提供独立连接，退出时关闭。"""
    conn = db_module.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_content_repo() -> ContentRepository:
    """返回进程级内容仓库。"""
    return get_repository()


__all__ = ["ApiError", "get_user_id", "get_db", "get_content_repo"]