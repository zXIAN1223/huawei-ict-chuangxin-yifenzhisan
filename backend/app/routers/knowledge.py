"""知识点查询 API（契约 C1-KNOWLEDGE@1 的 REST 部分）。

本模块提供 6 个只读端点，统一前缀 ``/api/knowledge``：

===========================  =====================================================
方法 / 路径                   语义
===========================  =====================================================
GET /chapters                章节元数据列表（ChapterMeta[]）
GET /chapters/{chapter_id}   章节详情；``?full=true`` 时含完整知识点
GET /kps                     知识点元数据列表（按章节/标签/难度过滤）
GET /kps/{kp_id}             完整知识点（KnowledgePoint）
GET /kps/{kp_id}/mindmap     知识点思维导图节点树（MindmapNode）
GET /kps/{kp_id}/images      知识点配图列表（ImageRef[]）
===========================  =====================================================

错误响应统一为 ``{"code": <str>, "message": <str>, "detail"?: <obj>}``：
未知道章节返回 ``404 CHAPTER_NOT_FOUND``，未知道知识点返回 ``404 KP_NOT_FOUND``。

依赖说明（与 T1 的组装约定）：

* ``ContentRepository`` 由 T1 提供，位于契约路径
  ``backend/app/data/content_loader.py``（顶层 ``backend.app.content_loader``
  作为兼容回退）。
* 应用组装时（``main.create_app``）会将其注册到 ``app.state.content_repo``；
  本模块依赖 :func:`get_repository` 优先从 ``app.state`` 读取，并回退到 T1 的
  ``deps.get_content_repo``。若均不可用（环境错误而非契约错误），返回
  ``503 CONTENT_UNAVAILABLE``。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

# 内容仓库：契约路径为 `backend/app/data/content_loader.py`；保留顶层路径回退，
# 以兼容 T1 组装侧的导入路径。
try:  # pragma: no cover - 依赖 T1 交付布局
    from backend.app.data.content_loader import ContentRepository
except ModuleNotFoundError:  # pragma: no cover
    from backend.app.content_loader import ContentRepository  # type: ignore[no-redef]

from backend.app.models import (
    Chapter,
    ChapterMeta,
    ImageRef,
    KnowledgePoint,
    KnowledgePointMeta,
    MindmapNode,
)

# T1 通过 `deps.get_content_repo` 提供进程级仓库；若该模块导入失败则仅用 app.state。
try:  # pragma: no cover - 依赖 T1 交付布局
    from backend.app.deps import get_content_repo as _deps_get_content_repo
except Exception:  # noqa: BLE001 - 兼容导入：任何错误都不应阻止本模块加载
    _deps_get_content_repo = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# ``app.state`` 上承载内容库的属性名候选，按优先级依次探测。
_REPO_STATE_ATTRS: tuple[str, ...] = (
    "content_repo",
    "content_repository",
    "repository",
    "content",
    "repo",
)


# --------------------------------------------------------------------------- #
# 依赖注入
# --------------------------------------------------------------------------- #
def get_repository(request: Request) -> ContentRepository:
    """获取已加载并校验的 :class:`ContentRepository`。

    优先读取 ``app.state`` 上注册的仓库（``main.create_app`` 的约定），
    其次回退到 T1 的 ``deps.get_content_repo`` 进程级单例。两者均不可用时
    返回 503（环境错误，非契约错误）。
    """
    state = getattr(request.app, "state", None)
    if state is not None:
        for attr in _REPO_STATE_ATTRS:
            repo = getattr(state, attr, None)
            if repo is not None:
                return repo

    if _deps_get_content_repo is not None:
        try:
            repo = _deps_get_content_repo()
            if repo is not None:
                return repo
        except Exception:  # noqa: BLE001 - 回退失败不应掩盖原始 503 语义
            logger.exception("通过 deps.get_content_repo 获取内容库失败")

    logger.error(
        "内容库不可用：app.state 未注册（已尝试属性：%s）且 deps 回退失败",
        ", ".join(_REPO_STATE_ATTRS),
    )
    raise HTTPException(
        status_code=503,
        detail={"code": "CONTENT_UNAVAILABLE", "message": "内容库尚未加载"},
    )


# --------------------------------------------------------------------------- #
# 内部辅助函数
# --------------------------------------------------------------------------- #
def _error_response(
    status_code: int, code: str, message: str, detail: Any | None = None
) -> JSONResponse:
    """构造符合契约的统一错误体 ``{"code","message","detail"?}``。"""
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        payload["detail"] = detail
    return JSONResponse(status_code=status_code, content=payload)


def _chapter_not_found(chapter_id: str) -> JSONResponse:
    return _error_response(
        404, "CHAPTER_NOT_FOUND", f"章节不存在：{chapter_id}", {"chapter_id": chapter_id}
    )


def _kp_not_found(kp_id: str) -> JSONResponse:
    return _error_response(
        404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id}
    )


def _parse_tags(raw_tags: list[str] | None) -> list[str] | None:
    """解析 ``tags`` 查询参数。

    同时支持重复参数（``?tags=a&tags=b``）与逗号分隔（``?tags=a,b``），
    结果按首次出现顺序去重；无有效标签时返回 ``None``（表示不做标签过滤）。
    标签匹配语义由 :class:`ContentRepository` 决定（当前实现为「任一命中」）。
    """
    if not raw_tags:
        return None
    parsed: list[str] = []
    for raw in raw_tags:
        for token in str(raw).split(","):
            token = token.strip()
            if token and token not in parsed:
                parsed.append(token)
    return parsed or None


def _get_field(obj: Any, name: str) -> Any:
    """兼容 Pydantic 模型与普通 dict 的字段读取。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _to_plain(obj: Any) -> Any:
    """将 Pydantic 模型转换为可 JSON 序列化的普通 dict（兼容 pydantic v1/v2）。"""
    if isinstance(obj, dict):
        return obj
    for method_name in ("model_dump", "dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            return method()
    return obj


def _meta_from_kp(kp: Any) -> dict[str, Any]:
    """在仓库缺失元数据索引时，从完整知识点兜底构建元数据。"""
    return {
        "kp_id": _get_field(kp, "kp_id"),
        "chapter_id": _get_field(kp, "chapter_id"),
        "title": _get_field(kp, "title"),
        "summary": _get_field(kp, "summary"),
        "tags": _get_field(kp, "tags") or [],
        "difficulty": _get_field(kp, "difficulty"),
        "importance": _get_field(kp, "importance"),
        "est_minutes": _get_field(kp, "est_minutes"),
        "has_images": bool(_get_field(kp, "images")),
        "quiz_count": len(_get_field(kp, "quiz") or []),
    }


def _chapter_meta_payload(chapter: Any, repo: ContentRepository) -> dict[str, Any]:
    """将章节收敛为「章节结构 + 知识点元数据」视图（对应 ``?full=false``）。"""
    data = _to_plain(chapter)
    if not isinstance(data, dict):
        # 极端兜底：无法收敛时返回空知识点列表，避免 500。
        return {"knowledge_points": []}

    metas: list[Any] = []
    for kp in data.get("knowledge_points") or []:
        kp_id = _get_field(kp, "kp_id")
        meta = repo.get_knowledge_point_meta(kp_id) if kp_id else None
        metas.append(_to_plain(meta) if meta is not None else _meta_from_kp(kp))

    result = dict(data)
    result["knowledge_points"] = metas
    return result


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get(
    "/chapters",
    response_model=list[ChapterMeta],
    summary="章节元数据列表",
    description="返回全部章节的元数据（不含知识点正文）。",
)
def list_chapters(
    repo: ContentRepository = Depends(get_repository),
) -> list[ChapterMeta]:
    return repo.list_chapters()


@router.get(
    "/chapters/{chapter_id}",
    summary="章节详情",
    responses={
        200: {
            "description": (
                "章节详情。默认（full=false）knowledge_points 为知识点元数据；"
                "full=true 时为完整知识点对象（含 content_blocks/images/mindmap/quiz）。"
            )
        },
        404: {"description": "章节不存在（CHAPTER_NOT_FOUND）"},
    },
)
def get_chapter(
    chapter_id: str,
    full: bool = Query(False, description="为 true 时返回包含完整知识点的章节"),
    repo: ContentRepository = Depends(get_repository),
) -> Any:
    chapter = repo.get_chapter(chapter_id)
    if chapter is None:
        return _chapter_not_found(chapter_id)
    if full:
        return chapter
    return _chapter_meta_payload(chapter, repo)


@router.get(
    "/kps",
    response_model=list[KnowledgePointMeta],
    summary="知识点元数据列表",
    description="按章节、标签、难度区间组合过滤，返回知识点元数据（不含正文）。",
)
def list_knowledge_points(
    chapter_id: str | None = Query(None, description="按所属章节过滤"),
    tags: list[str] | None = Query(
        None, description="标签过滤，支持重复参数或逗号分隔"
    ),
    difficulty_min: int | None = Query(None, ge=1, le=5, description="难度下限（含）"),
    difficulty_max: int | None = Query(None, ge=1, le=5, description="难度上限（含）"),
    repo: ContentRepository = Depends(get_repository),
) -> list[KnowledgePointMeta]:
    return repo.list_knowledge_points(
        chapter_id=chapter_id,
        tags=_parse_tags(tags),
        difficulty_min=difficulty_min,
        difficulty_max=difficulty_max,
    )


@router.get(
    "/kps/{kp_id}",
    response_model=KnowledgePoint,
    summary="完整知识点",
    responses={404: {"description": "知识点不存在（KP_NOT_FOUND）"}},
)
def get_knowledge_point(
    kp_id: str,
    repo: ContentRepository = Depends(get_repository),
) -> Any:
    kp = repo.get_knowledge_point(kp_id)
    if kp is None:
        return _kp_not_found(kp_id)
    return kp


@router.get(
    "/kps/{kp_id}/mindmap",
    response_model=MindmapNode,
    summary="知识点思维导图",
    responses={404: {"description": "知识点不存在（KP_NOT_FOUND）"}},
)
def get_knowledge_point_mindmap(
    kp_id: str,
    repo: ContentRepository = Depends(get_repository),
) -> Any:
    kp = repo.get_knowledge_point(kp_id)
    if kp is None:
        return _kp_not_found(kp_id)
    return _get_field(kp, "mindmap")


@router.get(
    "/kps/{kp_id}/images",
    response_model=list[ImageRef],
    summary="知识点配图列表",
    responses={404: {"description": "知识点不存在（KP_NOT_FOUND）"}},
)
def get_knowledge_point_images(
    kp_id: str,
    repo: ContentRepository = Depends(get_repository),
) -> Any:
    kp = repo.get_knowledge_point(kp_id)
    if kp is None:
        return _kp_not_found(kp_id)
    images = _get_field(kp, "images")
    return images if images is not None else []