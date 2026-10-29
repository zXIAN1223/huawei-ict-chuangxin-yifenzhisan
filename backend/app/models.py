"""共享数据模型。

本模块是以下两类定义的**单一来源**：

1. 内容模型（C1-KNOWLEDGE@1）：`ContentBlock`（判别联合）、`ImageRef`、
   `MindmapNode`（递归）、`QuizItem`、`KnowledgePoint`、`Chapter` 及其元数据模型。
   供 `content_loader`、各 router/service 与测试导入。
2. 数据库表结构（C2~C5 全部 7 张表）：以 `SCHEMA_STATEMENTS` 常量集中定义 DDL，
   由 `db.init_db()` 执行。其它模块**不得**另建表或修改此处 DDL。

同时导出各表的只读视图模型（`ReviewState`/`ReviewItemView`/`QuizAttemptView`/...
与 `*Model` 别名），供其它任务按契约导入。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ===========================================================================
# 内容模型（C1-KNOWLEDGE@1）
# ===========================================================================

QuizType = Literal["single_choice", "multi_choice", "true_false", "short_answer"]
CalloutVariant = Literal["tip", "note", "warning"]


class ParagraphBlock(BaseModel):
    """段落块。"""

    type: Literal["paragraph"]
    text: str


class ListBlock(BaseModel):
    """列表块。"""

    type: Literal["list"]
    ordered: bool = False
    items: list[str] = Field(default_factory=list)


class ImageBlock(BaseModel):
    """正文内嵌图片块。"""

    type: Literal["image"]
    src: str
    caption: str = ""
    alt: str = ""


class TableBlock(BaseModel):
    """表格块。"""

    type: Literal["table"]
    caption: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class CalloutBlock(BaseModel):
    """提示框块。"""

    type: Literal["callout"]
    variant: CalloutVariant
    text: str


class FormulaBlock(BaseModel):
    """公式块（LaTeX）。"""

    type: Literal["formula"]
    latex: str
    caption: str | None = None


# 判别联合：由 `type` 字段决定具体形状
ContentBlock = Annotated[
    Union[ParagraphBlock, ListBlock, ImageBlock, TableBlock, CalloutBlock, FormulaBlock],
    Field(discriminator="type"),
]


class ImageRef(BaseModel):
    """知识点配图引用（src 以 /static/img/biology/{cells|genetics}/ 开头）。"""

    src: str
    caption: str
    alt: str


class MindmapNode(BaseModel):
    """思维导图节点（递归）。children 为空数组表示叶子。"""

    id: str
    label: str
    note: str | None = None
    children: list["MindmapNode"] = Field(default_factory=list)


class QuizItem(BaseModel):
    """题目（内嵌于知识点 quiz 数组，结构同 C3-QUIZ@1）。"""

    qid: str
    type: QuizType
    difficulty: int = Field(ge=1, le=5)
    importance: int = Field(ge=1, le=5)
    question: str
    options: list[str] | None = None
    answer: str | list[str]
    keywords: list[str] | None = None
    explanation: str
    points: int = 1


class KnowledgePoint(BaseModel):
    """完整知识点对象。"""

    kp_id: str
    chapter_id: str
    title: str
    summary: str
    content_blocks: list[ContentBlock] = Field(min_length=1)
    images: list[ImageRef] = Field(min_length=1)
    mindmap: MindmapNode
    quiz: list[QuizItem] = Field(min_length=2)
    tags: list[str] = Field(default_factory=list)
    difficulty: int = Field(ge=1, le=5)
    importance: int = Field(ge=1, le=5)
    est_minutes: int = Field(ge=1)
    theory_tags: list[str] = Field(default_factory=list)


class ChapterMeta(BaseModel):
    """章节元数据（列表接口返回）。"""

    chapter_id: str
    chapter_title: str
    chapter_summary: str
    kp_count: int


class KnowledgePointMeta(BaseModel):
    """知识点元数据（列表接口返回）。"""

    kp_id: str
    chapter_id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    difficulty: int
    importance: int
    est_minutes: int
    has_images: bool
    quiz_count: int


class Chapter(BaseModel):
    """完整章节对象（对应章节 JSON 结构 + 完整知识点列表）。"""

    subject: str
    chapter_id: str
    chapter_title: str
    chapter_summary: str
    theory_notes: str | None = None
    knowledge_points: list[KnowledgePoint] = Field(default_factory=list)


# 递归模型需显式重建以解析自引用类型
MindmapNode.model_rebuild()


# ===========================================================================
# 角色 / 类型枚举（C4 / C2 / C3 共享）
# ===========================================================================

ChatRole = Literal["teacher", "student", "feynman"]
MessageRole = Literal["user", "assistant", "system"]
#: 聊天回复提供方（契约 C-LLM-PROVIDER@1）：
#: - ``"llm"``：真实远程 OpenAI 兼容调用成功（新逻辑产生）；
#: - ``"pangu"``：保留为向后兼容别名（旧库历史数据可读，新逻辑不再产生）；
#: - ``"local"``：本地规则化模拟/回退。
AIProvider = Literal["llm", "pangu", "local"]
GradedBy = Literal["local", "ai", "local+ai"]


# ===========================================================================
# 表视图模型
# ===========================================================================


class ReviewState(BaseModel):
    """`review_state` 表的一行（供 C2 算法 `apply_grade` 使用）。"""

    user_id: str
    kp_id: str
    chapter_id: str
    status: Literal["active", "removed"] = "active"
    level: int = 0
    interval_days: int = 1
    ease: float = 2.5
    due_at: str
    last_reviewed_at: str | None = None
    review_count: int = 0
    lapses: int = 0
    mastery: float = 0.0
    selected_at: str
    updated_at: str

    def copy(self, **updates: object) -> "ReviewState":
        """返回更新指定字段后的深拷贝（不修改原对象）。"""
        return self.model_copy(update=updates)


class ReviewStateView(BaseModel):
    """复习状态视图（不含知识点静态属性）。"""

    kp_id: str
    chapter_id: str
    status: Literal["active", "removed"]
    level: int
    interval_days: int
    due_at: str
    last_reviewed_at: str | None = None
    review_count: int
    lapses: int
    mastery: float


class ReviewItemView(ReviewStateView):
    """复习项视图（状态 + 知识点静态属性）。"""

    title: str
    difficulty: int
    importance: int
    est_minutes: int


class QuizAttemptView(BaseModel):
    """`quiz_attempt` 表视图。"""

    id: int
    kp_id: str
    qid: str
    question_type: QuizType
    user_answer: str | list[str]
    score: int
    is_correct: bool
    keyword_hits: list[str] = Field(default_factory=list)
    graded_by: GradedBy
    feedback: str = ""
    created_at: str


class ChatSessionView(BaseModel):
    """`chat_session` 表视图。"""

    session_id: str
    user_id: str
    kp_id: str | None = None
    mode: ChatRole
    title: str
    created_at: str
    updated_at: str


class ChatMessageView(BaseModel):
    """`chat_message` 表视图。"""

    id: int
    session_id: str
    role: MessageRole
    content: str
    provider: AIProvider | None = None
    created_at: str


class UserProfileView(BaseModel):
    """`user_profile` 表视图（JSON 列已解析为对象）。"""

    user_id: str
    nickname: str = "学习者"
    daily_minutes: int = 30
    preferred_slots: list[str] = Field(default_factory=list)
    learning_style: Literal["visual", "auditory", "reading", "kinesthetic", "mixed"] = "mixed"
    style_scores: dict[str, int] = Field(default_factory=dict)
    questionnaire_answers: dict = Field(default_factory=dict)
    questionnaire_completed: bool = False
    assessment_completed: bool = False
    reminder_enabled: bool = True
    reminder_time: str = "20:00"
    created_at: str
    updated_at: str


class BehaviorLogView(BaseModel):
    """`behavior_log` 表视图。"""

    id: int
    user_id: str
    action_type: str
    kp_id: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: str


class StrategySnapshotView(BaseModel):
    """`strategy_snapshot` 表视图。"""

    id: int
    user_id: str
    payload: dict = Field(default_factory=dict)
    rationale: str
    created_at: str


# 兼容别名：供 C5 契约中的 `*Model` 命名导入
UserProfileModel = UserProfileView
BehaviorLogModel = BehaviorLogView
ReviewStateModel = ReviewState


# ===========================================================================
# 数据库 DDL（唯一来源）
# ===========================================================================

#: `review_state`：每 user×kp 一行（C2-REVIEW@1）
DDL_REVIEW_STATE = """
CREATE TABLE IF NOT EXISTS review_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL,
    kp_id           TEXT    NOT NULL,
    chapter_id      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    level           INTEGER NOT NULL DEFAULT 0 CHECK (level >= 0),
    interval_days   INTEGER NOT NULL DEFAULT 1 CHECK (interval_days >= 1),
    ease            REAL    NOT NULL DEFAULT 2.5 CHECK (ease >= 1.3 AND ease <= 3.0),
    due_at          TEXT    NOT NULL,
    last_reviewed_at TEXT,
    review_count    INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    lapses          INTEGER NOT NULL DEFAULT 0 CHECK (lapses >= 0),
    mastery         REAL    NOT NULL DEFAULT 0.0 CHECK (mastery >= 0.0 AND mastery <= 1.0),
    selected_at     TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (user_id, kp_id)
);
"""

#: `quiz_attempt`：作答记录（C3-QUIZ@1）
DDL_QUIZ_ATTEMPT = """
CREATE TABLE IF NOT EXISTS quiz_attempt (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    kp_id         TEXT    NOT NULL,
    qid           TEXT    NOT NULL,
    question_type TEXT    NOT NULL,
    user_answer   TEXT    NOT NULL,
    score         INTEGER NOT NULL CHECK (score >= 0 AND score <= 100),
    is_correct    INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    keyword_hits  TEXT,
    graded_by     TEXT    NOT NULL,
    feedback      TEXT,
    created_at    TEXT    NOT NULL
);
"""

#: `chat_session`：聊天会话（C4-CHAT@1）
DDL_CHAT_SESSION = """
CREATE TABLE IF NOT EXISTS chat_session (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kp_id      TEXT,
    mode       TEXT NOT NULL CHECK (mode IN ('teacher', 'student', 'feynman')),
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: `chat_message`：聊天消息（C4-CHAT@1）
DDL_CHAT_MESSAGE = """
CREATE TABLE IF NOT EXISTS chat_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    provider   TEXT CHECK (provider IS NULL OR provider IN ('llm', 'pangu', 'local')),
    created_at TEXT NOT NULL
);
"""

#: `user_profile`：用户画像（C5-PROFILE@1）
DDL_USER_PROFILE = """
CREATE TABLE IF NOT EXISTS user_profile (
    user_id                  TEXT PRIMARY KEY,
    nickname                 TEXT    NOT NULL DEFAULT '学习者',
    daily_minutes            INTEGER NOT NULL DEFAULT 30 CHECK (daily_minutes >= 0),
    preferred_slots          TEXT    NOT NULL DEFAULT '[]',
    learning_style           TEXT    NOT NULL DEFAULT 'mixed',
    style_scores             TEXT    NOT NULL DEFAULT '{"visual":0,"auditory":0,"reading":0,"kinesthetic":0}',
    questionnaire_answers    TEXT    NOT NULL DEFAULT '{}',
    questionnaire_completed  INTEGER NOT NULL DEFAULT 0 CHECK (questionnaire_completed IN (0, 1)),
    assessment_completed     INTEGER NOT NULL DEFAULT 0 CHECK (assessment_completed IN (0, 1)),
    reminder_enabled         INTEGER NOT NULL DEFAULT 1 CHECK (reminder_enabled IN (0, 1)),
    reminder_time            TEXT    NOT NULL DEFAULT '20:00',
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);
"""

#: `behavior_log`：行为日志（C5-PROFILE@1）
DDL_BEHAVIOR_LOG = """
CREATE TABLE IF NOT EXISTS behavior_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    action_type TEXT NOT NULL,
    kp_id       TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
"""

#: `strategy_snapshot`：自适应策略快照（C5-PROFILE@1）
DDL_STRATEGY_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS strategy_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    rationale  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

#: 索引（不影响契约，仅用于查询加速）
DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_review_state_user_due "
    "ON review_state (user_id, status, due_at);",
    "CREATE INDEX IF NOT EXISTS idx_review_state_user_kp "
    "ON review_state (user_id, kp_id);",
    "CREATE INDEX IF NOT EXISTS idx_quiz_attempt_user_kp "
    "ON quiz_attempt (user_id, kp_id);",
    "CREATE INDEX IF NOT EXISTS idx_quiz_attempt_user_created "
    "ON quiz_attempt (user_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_chat_session_user "
    "ON chat_session (user_id, updated_at);",
    "CREATE INDEX IF NOT EXISTS idx_chat_message_session "
    "ON chat_message (session_id, id);",
    "CREATE INDEX IF NOT EXISTS idx_behavior_log_user_created "
    "ON behavior_log (user_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_strategy_snapshot_user "
    "ON strategy_snapshot (user_id, created_at);",
]

#: 全部建表语句（顺序即建表顺序）
SCHEMA_STATEMENTS: list[str] = [
    DDL_REVIEW_STATE,
    DDL_QUIZ_ATTEMPT,
    DDL_CHAT_SESSION,
    DDL_CHAT_MESSAGE,
    DDL_USER_PROFILE,
    DDL_BEHAVIOR_LOG,
    DDL_STRATEGY_SNAPSHOT,
]

#: 契约要求的全部表名（用于自检）
TABLE_NAMES: tuple[str, ...] = (
    "review_state",
    "quiz_attempt",
    "chat_session",
    "chat_message",
    "user_profile",
    "behavior_log",
    "strategy_snapshot",
)


__all__ = [
    # 内容模型
    "QuizType",
    "CalloutVariant",
    "ParagraphBlock",
    "ListBlock",
    "ImageBlock",
    "TableBlock",
    "CalloutBlock",
    "FormulaBlock",
    "ContentBlock",
    "ImageRef",
    "MindmapNode",
    "QuizItem",
    "KnowledgePoint",
    "Chapter",
    "ChapterMeta",
    "KnowledgePointMeta",
    # 枚举
    "ChatRole",
    "MessageRole",
    "AIProvider",
    "GradedBy",
    # 表视图
    "ReviewState",
    "ReviewStateView",
    "ReviewItemView",
    "QuizAttemptView",
    "ChatSessionView",
    "ChatMessageView",
    "UserProfileView",
    "BehaviorLogView",
    "StrategySnapshotView",
    "UserProfileModel",
    "BehaviorLogModel",
    "ReviewStateModel",
    # DDL
    "SCHEMA_STATEMENTS",
    "DDL_INDEXES",
    "TABLE_NAMES",
]