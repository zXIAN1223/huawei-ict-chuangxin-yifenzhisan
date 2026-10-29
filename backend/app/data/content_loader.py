"""章节内容加载与校验（C1-KNOWLEDGE@1）。

契约要点：
- `load_all()`：扫描内容目录中的章节文件（`biology_*.json`）。
  - 目录中**缺章节文件** → 记录 warning 并跳过（不抛错）；
  - 文件存在但不符合 C1 schema → 抛 `ContentSchemaError`（fail-fast）。
- 校验规则：kp 必填字段、difficulty/importance ∈ 1..5、est_minutes ≥ 1、
  images ≥ 1、mindmap 至少根 + 1 子节点、quiz ≥ 2 且至少 1 题非 single_choice、
  qid 唯一、mindmap 节点 id 唯一、kp_id 两章间全局唯一。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from backend.app.config import get_settings
from backend.app.models import (
    Chapter,
    ChapterMeta,
    KnowledgePoint,
    KnowledgePointMeta,
    MindmapNode,
)

logger = logging.getLogger(__name__)

#: 章节文件命名模式（与 plan 2.1.2 一致）
CHAPTER_FILE_GLOB = "biology_*.json"


class ContentSchemaError(Exception):
    """内容 JSON 不符合 schema 时抛出，携带 `file` 与 `message`。"""

    def __init__(self, file: str | Path | None = None, message: str = "") -> None:
        self.file = str(file) if file is not None else None
        self.message = message
        text = f"[{self.file}] {message}" if self.file else message
        super().__init__(text)


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------


def _iter_mindmap_nodes(node: MindmapNode):
    """深度优先遍历思维导图节点。"""
    yield node
    for child in node.children:
        yield from _iter_mindmap_nodes(child)


def _validate_chapter(data: object, source: Path) -> Chapter:
    """使用 C1 模型校验章节数据，并追加语义校验；失败抛 ContentSchemaError。"""
    if not isinstance(data, dict):
        raise ContentSchemaError(source, "章节文件的顶层必须是 JSON 对象")

    # 1) 结构校验（pydantic：必填字段、枚举、范围、min_length 等）
    try:
        chapter = Chapter.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        msg = first.get("msg", "schema 校验失败")
        raise ContentSchemaError(source, f"字段 `{loc}` 校验失败：{msg}") from exc

    if not chapter.chapter_id.strip():
        raise ContentSchemaError(source, "chapter_id 不能为空")
    if not chapter.knowledge_points:
        raise ContentSchemaError(source, "knowledge_points 至少需要包含 1 个知识点")

    # 2) 语义校验
    seen_kp_ids: set[str] = set()
    for kp in chapter.knowledge_points:
        if kp.kp_id in seen_kp_ids:
            raise ContentSchemaError(source, f"kp_id `{kp.kp_id}` 在章节内重复")
        seen_kp_ids.add(kp.kp_id)
        _validate_kp_semantics(kp, chapter.chapter_id, source)
    return chapter


def _validate_kp_semantics(kp: KnowledgePoint, chapter_id: str, source: Path) -> None:
    where = f"知识点 `{kp.kp_id}`"

    if not kp.kp_id.strip():
        raise ContentSchemaError(source, "存在空的 kp_id")
    if kp.chapter_id != chapter_id:
        raise ContentSchemaError(
            source, f"{where} 的 chapter_id `{kp.chapter_id}` 与章节 `{chapter_id}` 不一致"
        )
    if not kp.title.strip():
        raise ContentSchemaError(source, f"{where} 的 title 不能为空")
    if not kp.summary.strip():
        raise ContentSchemaError(source, f"{where} 的 summary 不能为空")

    # mindmap：至少根 + 1 子节点，且节点 id 在同一知识点内唯一
    if not kp.mindmap.children:
        raise ContentSchemaError(source, f"{where} 的 mindmap 至少需要根节点 + 1 个子节点")
    seen_ids: set[str] = set()
    for node in _iter_mindmap_nodes(kp.mindmap):
        if not node.id.strip():
            raise ContentSchemaError(source, f"{where} 的 mindmap 存在空节点 id")
        if node.id in seen_ids:
            raise ContentSchemaError(
                source, f"{where} 的 mindmap 节点 id `{node.id}` 重复"
            )
        seen_ids.add(node.id)

    # quiz：≥2 且至少 1 题非 single_choice，qid 唯一
    if len(kp.quiz) < 2:
        raise ContentSchemaError(source, f"{where} 的 quiz 至少需要 2 道题")
    if all(item.type == "single_choice" for item in kp.quiz):
        raise ContentSchemaError(
            source, f"{where} 的 quiz 至少需要 1 道非 single_choice 的题目"
        )
    qids: set[str] = set()
    for item in kp.quiz:
        if not item.qid.strip():
            raise ContentSchemaError(source, f"{where} 存在空的 qid")
        if item.qid in qids:
            raise ContentSchemaError(source, f"{where} 的 qid `{item.qid}` 重复")
        qids.add(item.qid)
        if item.type == "short_answer" and not item.keywords:
            raise ContentSchemaError(
                source, f"{where} 的简答题 `{item.qid}` 缺少 keywords"
            )


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


class ContentRepository:
    """章节与知识点内容仓库（进程内只读索引）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._chapters: dict[str, Chapter] = {}
        self._kp_index: dict[str, KnowledgePoint] = {}
        self._kp_meta: dict[str, KnowledgePointMeta] = {}
        self._loaded = False

    # -- 加载 ---------------------------------------------------------------

    def load_all(self) -> None:
        """加载并校验全部章节文件；失败抛 `ContentSchemaError`。幂等，可重复调用。"""
        self._chapters = {}
        self._kp_index = {}
        self._kp_meta = {}

        if not self.data_dir.is_dir():
            logger.warning("内容目录不存在，跳过章节加载：%s", self.data_dir)
            self._loaded = True
            return

        files = sorted(self.data_dir.glob(CHAPTER_FILE_GLOB))
        if not files:
            logger.warning(
                "内容目录中未找到章节文件（模式 %s），跳过加载：%s",
                CHAPTER_FILE_GLOB,
                self.data_dir,
            )
            self._loaded = True
            return

        for path in files:
            chapter = self._load_file(path)
            if chapter is None:
                continue
            if chapter.chapter_id in self._chapters:
                raise ContentSchemaError(
                    path, f"chapter_id `{chapter.chapter_id}` 与其它文件重复"
                )
            for kp in chapter.knowledge_points:
                if kp.kp_id in self._kp_index:
                    raise ContentSchemaError(
                        path,
                        f"kp_id `{kp.kp_id}` 在多个章节间重复（全局唯一约束）",
                    )
            self._chapters[chapter.chapter_id] = chapter
            for kp in chapter.knowledge_points:
                self._kp_index[kp.kp_id] = kp
                self._kp_meta[kp.kp_id] = self._build_meta(kp)

        self._loaded = True
        logger.info(
            "内容加载完成：章节 %d 个，知识点 %d 个",
            len(self._chapters),
            len(self._kp_index),
        )

    def _load_file(self, path: Path) -> Chapter | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContentSchemaError(path, f"无法读取文件：{exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContentSchemaError(path, f"JSON 解析失败：{exc}") from exc

        return _validate_chapter(data, path)

    @staticmethod
    def _build_meta(kp: KnowledgePoint) -> KnowledgePointMeta:
        return KnowledgePointMeta(
            kp_id=kp.kp_id,
            chapter_id=kp.chapter_id,
            title=kp.title,
            summary=kp.summary,
            tags=list(kp.tags),
            difficulty=kp.difficulty,
            importance=kp.importance,
            est_minutes=kp.est_minutes,
            has_images=len(kp.images) > 0,
            quiz_count=len(kp.quiz),
        )

    # -- 查询 ---------------------------------------------------------------

    def list_chapters(self) -> list[ChapterMeta]:
        """返回全部章节元数据（保持加载顺序）。"""
        return [
            ChapterMeta(
                chapter_id=chapter.chapter_id,
                chapter_title=chapter.chapter_title,
                chapter_summary=chapter.chapter_summary,
                kp_count=len(chapter.knowledge_points),
            )
            for chapter in self._chapters.values()
        ]

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        """按 chapter_id 返回完整章节；不存在返回 None。"""
        return self._chapters.get(chapter_id)

    def list_knowledge_points(
        self,
        chapter_id: str | None = None,
        tags: list[str] | None = None,
        difficulty_min: int | None = None,
        difficulty_max: int | None = None,
    ) -> list[KnowledgePointMeta]:
        """按条件筛选知识点元数据。

        - `tags`：与传入标签集合**存在交集**即命中（任一匹配）；
        - `difficulty_min` / `difficulty_max`：闭区间。
        """
        results: list[KnowledgePointMeta] = []
        tag_filter = {tag for tag in (tags or []) if tag}
        for meta in self._kp_meta.values():
            if chapter_id is not None and meta.chapter_id != chapter_id:
                continue
            if tag_filter and not (tag_filter & set(meta.tags)):
                continue
            if difficulty_min is not None and meta.difficulty < difficulty_min:
                continue
            if difficulty_max is not None and meta.difficulty > difficulty_max:
                continue
            results.append(meta)
        return results

    def get_knowledge_point(self, kp_id: str) -> KnowledgePoint | None:
        """按 kp_id 返回完整知识点；不存在返回 None。"""
        return self._kp_index.get(kp_id)

    def get_knowledge_point_meta(self, kp_id: str) -> KnowledgePointMeta | None:
        """按 kp_id 返回知识点元数据；不存在返回 None。"""
        return self._kp_meta.get(kp_id)

    # -- 辅助 ---------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded


# ---------------------------------------------------------------------------
# 进程级单例（供依赖注入与启动初始化复用）
# ---------------------------------------------------------------------------

_repository: ContentRepository | None = None


def get_repository(data_dir: Path | None = None) -> ContentRepository:
    """返回进程级 `ContentRepository` 单例（首次调用时按需创建）。"""
    global _repository
    if _repository is None:
        _repository = ContentRepository(data_dir or get_settings().data_dir)
    return _repository


def reset_repository() -> None:
    """重置进程级单例（供测试使用）。"""
    global _repository
    _repository = None