"""AI 适配层（契约 C4-CHAT@1 / C-LLM-ADAPTER@1，plan 2.2.2）。

本模块向上层提供统一的 AI 调用接口，屏蔽「真实远程大模型（任意 OpenAI 兼容端点）」
与「本地规则化模拟」两种后端：

* `AIAdapter`：`complete`（非流式）/ `stream`（流式）/ `grade_short_answer`（简答判分）；
* `LocalSimulator`：无凭证时依据 `mode`（teacher/student/feynman）与知识点上下文
  生成规则化文本，**只使用知识点已有内容，不臆造知识点之外的具体事实**；
* `AIConfig`：由 `config.get_settings()` 的 `LLM_*` 有效配置构建
  （见 :func:`build_ai_config`）；`api_url` 语义放宽为「base_url 或完整 endpoint」，
  统一经 :meth:`AIConfig.resolve_endpoint` 归一化。

安全约束（R5.4）：API 密钥**仅**来自 `AIConfig`，绝不硬编码、打印、写入日志或
回显到响应中；`AIConfig` 的 ``repr`` 亦不包含密钥。

回退规则（plan 2.3.1）：

* `provider == "llm"` 当且仅当 `config.enabled` 且 `resolve_endpoint()` 非空；
  否则 `"local"`；
* 远程调用抛错/超时/返回空正文：
  - `complete` 回退 `local` 并返回 `provider="local"`；
  - `stream` 若尚未输出则整段回退 `local`；若已输出部分则结束流并追加
    ``[已切换本地模式]``。

流式调用结束后的**实际**提供方可由 :attr:`AIAdapter.last_provider` 读取，供
`chat.py` 在 `done` 帧与落库时使用。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from backend.app.config import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_PANGU_MODEL,
    DEFAULT_PANGU_TIMEOUT,
    get_settings,
)
from backend.app.models import AIProvider, ChatRole, KnowledgePoint

# 真实 HTTP 客户端为可选依赖：缺失时 provider 仍可判定，唯独远程路径抛错并回退本地。
try:  # pragma: no cover - 取决于运行环境是否安装 httpx
    import httpx
except Exception:  # noqa: BLE001 - 任何导入错误都不应阻止本地模拟
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# `AIProvider` 由契约 C-LLM-PROVIDER@1 在 `models` 中定义，本模块重新导出。


#: 本地回退时，远程已输出部分内容后追加的提示标记。
LOCAL_FALLBACK_MARKER = "[已切换本地模式]"

#: 输入/输出文本长度保护上限，避免异常输入导致超大内存或超长请求。
_MAX_INPUT_CHARS = 4000
_MAX_OUTPUT_CHARS = 8000

__all__ = [
    "AIProvider",
    "AIConfig",
    "AIResult",
    "ChatTurn",
    "AIAdapter",
    "LocalSimulator",
    "LOCAL_FALLBACK_MARKER",
    "build_ai_config",
]


# ---------------------------------------------------------------------------
# 数据契约（plan 2.2.4）
# ---------------------------------------------------------------------------


@dataclass
class ChatTurn:
    """单轮对话消息（仅 user/assistant，system 由 `system` 参数单独传入）。"""

    role: Literal["user", "assistant"]
    content: str


@dataclass
class AIResult:
    """AI 调用结果。"""

    text: str
    provider: AIProvider


@dataclass
class AIConfig:
    """AI 运行配置。密钥只在此对象中流转，不落日志/响应。

    `api_url` 语义放宽：可以是 base_url（如 ``https://api.deepseek.com/v1``），
    也可以是完整 endpoint（如 ``.../chat/completions``），由
    :meth:`resolve_endpoint` 统一归一化（plan 2.2.2）；不会自动补 ``/v1``。
    """

    api_url: Optional[str] = None
    #: API 密钥：`repr=False` 保证对象表示（含异常回溯中的局部变量打印）不泄露密钥。
    api_key: Optional[str] = field(default=None, repr=False)
    model: str = DEFAULT_PANGU_MODEL
    timeout: float = DEFAULT_PANGU_TIMEOUT
    #: 采样温度（未显式传入时使用 `config` 默认值）。
    temperature: float = DEFAULT_LLM_TEMPERATURE
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS

    @property
    def enabled(self) -> bool:
        """当且仅当地址与密钥均非空时为 True（是否远程由 :meth:`resolve_endpoint` 决定）。"""
        return bool((self.api_url or "").strip()) and bool((self.api_key or "").strip())

    def resolve_endpoint(self) -> Optional[str]:
        """把 ``api_url`` 归一化为 OpenAI 兼容的 ``/chat/completions`` 端点。

        判定顺序（契约 C-LLM-ADAPTER@1 规则表）：

        1. 去首尾空白；空 → ``None``；
        2. 已含子串 ``/chat/completions``（含带查询串）→ 原样返回；
        3. 去掉结尾的一个或多个 ``/``；
        4. 追加 ``/chat/completions``（**不自动补** ``/v1``）。
        """
        raw = (self.api_url or "").strip()
        if not raw:
            return None
        if "/chat/completions" in raw:
            return raw
        return raw.rstrip("/") + "/chat/completions"


def build_ai_config(settings=None) -> AIConfig:
    """从运行时配置构建 :class:`AIConfig`。

    由 ``settings.llm_*`` 填充；缺属性或值为空时回退 ``pangu_*`` / 默认值
    （`get_settings()` 已把 ``pangu_*`` 回填为 LLM 有效值，故两者通常等价）。

    :param settings: 可选的 `config.Settings`；缺省时读取当前进程配置。
    """
    cfg = settings if settings is not None else get_settings()

    def first_non_blank(*names: str) -> Optional[str]:
        for name in names:
            raw = getattr(cfg, name, None)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                return value
        return None

    def number(raw: object, default: float) -> float:
        if raw is None:
            return default
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    base_url = first_non_blank("llm_base_url", "pangu_api_url")
    api_key = first_non_blank("llm_api_key", "pangu_api_key")
    model = first_non_blank("llm_model", "pangu_model") or DEFAULT_PANGU_MODEL
    timeout = number(
        getattr(cfg, "llm_timeout", None),
        number(getattr(cfg, "pangu_timeout", None), DEFAULT_PANGU_TIMEOUT),
    )

    temperature = number(getattr(cfg, "llm_temperature", None), DEFAULT_LLM_TEMPERATURE)
    temperature = max(0.0, min(2.0, temperature))
    try:
        max_tokens = int(getattr(cfg, "llm_max_tokens", DEFAULT_LLM_MAX_TOKENS))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_LLM_MAX_TOKENS
    if max_tokens < 1:
        max_tokens = DEFAULT_LLM_MAX_TOKENS

    return AIConfig(
        api_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
    )


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】<>《》~—-]+")


def _clean(text: object) -> str:
    """收缩空白并限制长度。"""
    if text is None:
        return ""
    value = str(text).replace("\r", "").strip()
    value = re.sub(r"[ \t]+", " ", value)
    return value[:_MAX_INPUT_CHARS]


def _split_sentences(text: str) -> list[str]:
    """按中文/英文句末标点与换行切分文本，保留标点。"""
    if not text:
        return []
    chunks: list[str] = []
    buffer = ""
    for ch in text:
        buffer += ch
        if ch in "。！？!?；;\n" or (ch == "." and len(buffer) > 1):
            piece = buffer.strip()
            if piece:
                chunks.append(piece)
            buffer = ""
    tail = buffer.strip()
    if tail:
        chunks.append(tail)
    return chunks


def _ngrams(text: str, n: int = 2) -> set[str]:
    """对去除标点后的文本抽取 n-gram（用于轻量覆盖度估算）。"""
    stripped = _PUNCT_RE.sub("", text or "")
    if len(stripped) < n:
        return {stripped} if stripped else set()
    return {stripped[i : i + n] for i in range(len(stripped) - n + 1)}


def _overlap_ratio(reference: str, answer: str) -> float:
    """answer 对 reference 的 n-gram 覆盖率 ∈ [0, 1]。"""
    ref = _ngrams(reference)
    if not ref:
        return 1.0 if (answer or "").strip() else 0.0
    return len(ref & _ngrams(answer)) / len(ref)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _extract_points(kp: Optional[KnowledgePoint], limit: int = 6) -> list[str]:
    """从知识点的 `content_blocks` 提取可讲解的要点文本。

    仅取知识点中**已存在**的文本（段落/列表/提示框/表格说明/公式说明/配图说明），
    不做任何扩写，避免臆造。
    """
    if kp is None:
        return []
    points: list[str] = []
    for block in getattr(kp, "content_blocks", None) or []:
        btype = getattr(block, "type", None)
        if btype == "paragraph":
            points.append(_clean(getattr(block, "text", "")))
        elif btype == "list":
            for item in getattr(block, "items", None) or []:
                points.append(_clean(item))
        elif btype == "callout":
            points.append(_clean(getattr(block, "text", "")))
        elif btype == "table":
            caption = _clean(getattr(block, "caption", "") or "")
            headers = "、".join(_clean(h) for h in (getattr(block, "headers", None) or []))
            points.append(caption or headers)
        elif btype == "formula":
            caption = _clean(getattr(block, "caption", "") or "")
            latex = _clean(getattr(block, "latex", ""))
            points.append(caption or latex)
        elif btype == "image":
            points.append(_clean(getattr(block, "caption", "") or ""))
    # 去重并限制数量
    result: list[str] = []
    seen: set[str] = set()
    for point in points:
        if not point or point in seen:
            continue
        seen.add(point)
        result.append(point)
        if len(result) >= limit:
            break
    return result


def _last_user_content(messages: list[ChatTurn]) -> str:
    """返回消息列表中最后一条 user 消息的内容（无则空串）。"""
    for turn in reversed(messages or []):
        if getattr(turn, "role", None) == "user":
            return _clean(getattr(turn, "content", ""))
    return ""


# ---------------------------------------------------------------------------
# 本地规则化模拟器（plan 2.2.4）
# ---------------------------------------------------------------------------


class LocalSimulator:
    """依据 `mode` 与知识点上下文生成规则化文本。

    三种角色语义：

    * ``teacher``  —— 分段讲解知识点 `content_blocks` 要点；
    * ``student``  —— 生成引导式提问，以学生口吻反问；
    * ``feynman``  —— 输出「肯定 + 纠错 + 追问」点评。

    上下文缺失（`kp is None`）时使用通用模板；绝不臆造知识点外的具体事实。
    """

    #: teacher 讲解分段标题
    _TEACHER_HINTS = (
        "我们先把它拆成几段来看：",
        "下面按条理逐点讲清楚：",
        "这段内容可以这样理解：",
    )

    def render(self, mode: ChatRole, kp: Optional[KnowledgePoint], user_message: str = "") -> str:
        """生成某角色下的本地模拟回复文本。"""
        if mode == "student":
            return self._render_student(kp)
        if mode == "feynman":
            return self._render_feynman(kp, user_message)
        return self._render_teacher(kp)

    # -- teacher ------------------------------------------------------------

    def _render_teacher(self, kp: Optional[KnowledgePoint]) -> str:
        if kp is None:
            return (
                "我可以带你系统梳理任意一个知识点。你可以：\n"
                "1. 在「知识点」页面选择想要学习的内容，再回到这里继续；\n"
                "2. 或者直接告诉我它的名称，我会按要点分段讲解；\n"
                "3. 学完后切换到「费曼」角色，用你自己的话讲一遍，我来帮你查漏补缺。\n\n"
                "（当前未关联具体知识点，我先在这里等你挑选。）"
            )

        title = _clean(getattr(kp, "title", "该知识点"))
        summary = _clean(getattr(kp, "summary", ""))
        points = _extract_points(kp)

        lines = [f"我们来看「{title}」。"]
        if summary:
            lines.append(f"一句话概括：{summary}")
        if points:
            lines.append(self._TEACHER_HINTS[len(title) % len(self._TEACHER_HINTS)])
            for index, point in enumerate(_split_sentences("".join(points)) or points, start=1):
                lines.append(f"{index}. {point}")
        else:
            lines.append("这个知识点暂时没有可展开的正文要点，你可以先看它的配图与思维导图。")
        lines.append(
            "你可以在任意一段后打断我提问；也可以切换到「费曼」角色，"
            "用你自己的话复述一遍，我会帮你指出遗漏。"
        )
        return "\n".join(lines)

    # -- student ------------------------------------------------------------

    def _render_student(self, kp: Optional[KnowledgePoint]) -> str:
        if kp is None:
            return (
                "那我来当学生，先向你请教几个问题，我们一起把它想清楚：\n"
                "1. 你现在最想弄明白的是哪个知识点？\n"
                "2. 关于它，你已经知道哪些、又卡在哪里？\n"
                "3. 你能先用一句话说说你的理解吗？\n\n"
                "你先回答任意一个，我会接着追问。"
            )

        title = _clean(getattr(kp, "title", "这个知识点"))
        summary = _clean(getattr(kp, "summary", ""))
        points = _extract_points(kp, limit=3)

        lines = [f"那我换个身份，当你的学生，来向你请教「{title}」："]
        if summary:
            lines.append(f"1. 你能用自己的话说说：{summary} 到底是什么意思吗？")
        for point in points:
            question = _split_sentences(point)[0] if _split_sentences(point) else point
            lines.append(f"2. 关于「{question}」，它为什么会这样？你能给出理由吗？")
        lines.append("3. 如果让你把「" + title + "」讲给完全不懂的同学，你会先讲哪一步？")
        lines.append("你任选一个先回答，我会顺着你的回答继续追问。")
        return "\n".join(lines)

    # -- feynman ------------------------------------------------------------

    def _render_feynman(self, kp: Optional[KnowledgePoint], user_message: str) -> str:
        answer = _clean(user_message)
        if kp is None:
            if not answer:
                return (
                    "欢迎来到费曼模式：请先把你理解的内容用自己的话讲给我听，"
                    "我会从「肯定、纠错、追问」三个角度给你反馈。"
                )
            return (
                "谢谢你的讲解，我先回应两点：\n"
                "✅ 肯定：你愿意主动复述，这一步很关键。\n"
                "❓ 追问：能再举一个具体的例子，说明它是怎么发生的吗？\n\n"
                "（当前未关联具体知识点，无法对照要点纠错；可先选择知识点再来。）"
            )

        title = _clean(getattr(kp, "title", "该知识点"))
        points = _extract_points(kp)

        if not answer:
            return (
                f"我们来做「{title}」的费曼练习：请用你自己的话把它讲一遍，"
                "就像讲给完全不懂的人听。我会在你说完后给出「肯定 + 纠错 + 追问」点评。"
            )

        mentioned = [p for p in points if _overlap_ratio(p, answer) >= 0.2]
        missed = [p for p in points if p not in mentioned]

        lines = [f"我来点评你对「{title}」的讲解："]
        if mentioned:
            head = _split_sentences(mentioned[0])
            shown = head[0] if head else mentioned[0]
            lines.append(f"✅ 肯定：你已经抓住了要点「{shown}」，这说明方向是对的。")
        else:
            lines.append(
                "✅ 肯定：你愿意把它讲出来，这一步最重要；表达本身已经在帮你梳理思路。"
            )

        if missed:
            miss_head = _split_sentences(missed[0])
            miss_shown = miss_head[0] if miss_head else missed[0]
            lines.append(f"🔧 还可以补充：知识点里还提到「{miss_shown}」，这一点容易被漏掉。")
            lines.append(f"❓ 追问：你能说说「{miss_shown}」和刚才那点之间是什么关系吗？")
        else:
            lines.append("🔧 纠错：对照要点，你的讲解基本覆盖完整，暂未发现明显遗漏。")
            lines.append("❓ 追问：你能举一个生活或实验中的具体例子，把它讲得更直观吗？")

        lines.append("（费曼学习法提示：讲到让完全不懂的人也能听懂，才算真正掌握。）")
        return "\n".join(lines)

    # -- 简答判分（本地兜底） ------------------------------------------------

    def grade(
        self,
        question: str,
        reference: str,
        keywords: list[str],
        user_answer: str,
    ) -> str:
        """对照参考答案给出本地反馈文本，并在文本中标注 AI 分（0..1）。

        文本中的 ``AI评分：0.xx`` 便于上层（如 T6 判分）解析 ai 部分分；
        keyword 命中部分由上层公式（0.6*关键词 + 0.4*AI）单独计算。
        """
        answer = _clean(user_answer)
        valid_keywords = [k for k in (keywords or []) if _clean(k)]
        hits = [k for k in valid_keywords if _clean(k) in answer]
        keyword_ratio = len(hits) / len(valid_keywords) if valid_keywords else (1.0 if answer else 0.0)

        ref = _clean(reference)
        overlap = _overlap_ratio(ref, answer) if ref else (1.0 if answer else 0.0)
        length_ratio = min(1.0, len(answer) / len(ref)) if ref else (1.0 if answer else 0.0)
        ai_score = round(_clamp01(0.6 * overlap + 0.4 * length_ratio), 2)

        hit_text = "、".join(hits) if hits else "（无）"
        if keyword_ratio >= 0.8:
            hint = "覆盖较完整。"
        elif keyword_ratio >= 0.4:
            hint = "方向基本正确，建议补充遗漏要点。"
        else:
            hint = "关键要点覆盖不足，建议回到知识点复习后再作答。"

        lines = [
            "【本地模拟 · 简答判分】",
            f"命中关键词：{hit_text}（{len(hits)}/{len(valid_keywords) if valid_keywords else 0}）",
            f"整体评价：{hint}",
        ]
        if ref:
            lines.append(f"参考要点：{ref}")
        lines.append(f"AI评分：{ai_score:.2f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class AIAdapter:
    """统一的 AI 适配器：远程 OpenAI 兼容端点 + 本地模拟回退。

    角色上下文（`mode` / `kp`）用于本地模拟；远程调用亦会据此构建 messages。
    每次流式/非流式调用结束后，实际提供方记录在 :attr:`last_provider`。
    """

    def __init__(
        self,
        config: AIConfig,
        *,
        mode: ChatRole = "teacher",
        kp: Optional[KnowledgePoint] = None,
        simulator: Optional[LocalSimulator] = None,
    ) -> None:
        self.config = config
        self.mode: ChatRole = mode
        self.kp = kp
        self.simulator = simulator or LocalSimulator()
        #: 最近一次调用的实际提供方（回退后为 "local"）。
        self._last_provider: AIProvider = self.provider

    # -- 提供方判定 ---------------------------------------------------------

    @property
    def provider(self) -> AIProvider:
        """当且仅当配置有效且 `resolve_endpoint()` 非空时为 ``"llm"``，否则 ``"local"``。"""
        if self.config.enabled and self.config.resolve_endpoint():
            return "llm"
        return "local"

    @property
    def last_provider(self) -> AIProvider:
        """最近一次 `complete`/`stream`/`grade_short_answer` 的实际提供方。"""
        return self._last_provider

    # -- 公共接口 -----------------------------------------------------------

    async def complete(
        self,
        system: str,
        messages: list[ChatTurn],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIResult:
        """非流式补全；远程失败/超时/空正文回退本地并返回 ``provider="local"``。"""
        temperature, max_tokens = self._generation_params(temperature, max_tokens)

        if self.provider == "llm":
            try:
                text = await self._remote_complete(system, messages, temperature, max_tokens)
                self._last_provider = "llm"
                return AIResult(text=text, provider="llm")
            except Exception as exc:  # noqa: BLE001 - 任意失败均回退本地
                logger.warning("远程 LLM complete 调用失败，回退本地模拟：%s", type(exc).__name__)

        self._last_provider = "local"
        text = self.simulator.render(self.mode, self.kp, _last_user_content(messages))
        return AIResult(text=text, provider="local")

    async def stream(
        self,
        system: str,
        messages: list[ChatTurn],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """流式补全。

        回退语义（plan 2.3.1）：

        * 远程在**尚未输出**任何片段时失败/返回空正文 → 整段回退本地（逐句输出本地全文）；
        * 远程在**已输出**部分片段后失败 → 结束流并追加 ``[已切换本地模式]``。

        结束时实际提供方可通过 :attr:`last_provider` 获取。
        """
        self._last_provider = self.provider
        temperature, max_tokens = self._generation_params(temperature, max_tokens)

        if self.provider == "llm":
            produced = False
            try:
                async for piece in self._remote_stream(system, messages, temperature, max_tokens):
                    produced = True
                    yield piece
            except Exception as exc:  # noqa: BLE001 - 任意失败均回退本地
                logger.warning("远程 LLM stream 调用失败，回退本地模拟：%s", type(exc).__name__)
                if produced:
                    self._last_provider = "local"
                    yield LOCAL_FALLBACK_MARKER
                    return
                produced = False  # 未输出任何片段 → 走整段本地回退

            if produced:
                self._last_provider = "llm"
                return

        # 本地整段回退（或 provider 本就是 local）
        self._last_provider = "local"
        local_text = self.simulator.render(self.mode, self.kp, _last_user_content(messages))
        for piece in self._chunk(local_text):
            yield piece

    async def grade_short_answer(
        self,
        question: str,
        reference: str,
        keywords: list[str],
        user_answer: str,
    ) -> AIResult:
        """简答判分：远程可用时请求模型判分，失败回退本地规则化判分。"""
        if self.provider == "llm":
            try:
                text = await self._remote_grade(question, reference, keywords, user_answer)
                self._last_provider = "llm"
                return AIResult(text=text, provider="llm")
            except Exception as exc:  # noqa: BLE001
                logger.warning("远程 LLM 判分调用失败，回退本地判分：%s", type(exc).__name__)

        self._last_provider = "local"
        text = self.simulator.grade(question, reference, keywords, user_answer)
        return AIResult(text=text, provider="local")

    # -- 参数默认值 ---------------------------------------------------------

    def _generation_params(
        self, temperature: Optional[float], max_tokens: Optional[int]
    ) -> tuple[float, int]:
        """解析生成本次请求的采样参数：显式入参优先，``None`` 时取 `config` 默认值。

        对应 plan 2.3.2 参数生效链：`LLM_TEMPERATURE`/`LLM_MAX_TOKENS` →
        `AIConfig.temperature/max_tokens` → 请求体。非法值回退文档化默认值。
        """
        resolved_temperature: object = (
            self.config.temperature if temperature is None else temperature
        )
        resolved_max_tokens: object = self.config.max_tokens if max_tokens is None else max_tokens

        try:
            temperature_value = float(resolved_temperature)  # type: ignore[arg-type]
            if temperature_value != temperature_value:  # NaN
                raise ValueError
        except (TypeError, ValueError):
            temperature_value = DEFAULT_LLM_TEMPERATURE
        temperature_value = max(0.0, min(2.0, temperature_value))

        try:
            max_tokens_value = int(resolved_max_tokens)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            max_tokens_value = DEFAULT_LLM_MAX_TOKENS
        if max_tokens_value < 1:
            max_tokens_value = DEFAULT_LLM_MAX_TOKENS

        return temperature_value, max_tokens_value

    # -- 远程实现 -----------------------------------------------------------

    def _request_endpoint(self) -> str:
        """返回归一化 endpoint；配置不完整时抛错以触发本地回退。"""
        endpoint = self.config.resolve_endpoint()
        if not endpoint:
            raise RuntimeError("未配置有效的 LLM 端点")
        return endpoint

    def _headers(self) -> dict[str, str]:
        """构建请求头。密钥仅在此处使用，不进入日志。"""
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_payload(
        self,
        system: str,
        messages: list[ChatTurn],
        *,
        stream: bool,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """构建 OpenAI 兼容请求体。"""
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": _clean(system)})
        for turn in messages or []:
            role = getattr(turn, "role", None)
            if role not in ("user", "assistant"):
                continue
            payload_messages.append({"role": role, "content": _clean(getattr(turn, "content", ""))})
        return {
            "model": self.config.model,
            "messages": payload_messages,
            "stream": bool(stream),
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }

    def _require_httpx(self) -> None:
        if httpx is None:  # pragma: no cover - 取决于运行环境
            raise RuntimeError("未安装 httpx，无法进行远程 LLM HTTP 调用")

    async def _remote_complete(
        self,
        system: str,
        messages: list[ChatTurn],
        temperature: float,
        max_tokens: int,
    ) -> str:
        self._require_httpx()
        endpoint = self._request_endpoint()
        payload = self._build_payload(
            system, messages, stream=False, temperature=temperature, max_tokens=max_tokens
        )
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:  # type: ignore[union-attr]
            response = await client.post(endpoint, json=payload, headers=self._headers())
            response.raise_for_status()
            data = response.json()
        text = self._extract_completion_text(data)
        if not text:
            raise RuntimeError("远程 LLM 返回正文为空")
        return text[:_MAX_OUTPUT_CHARS]

    async def _remote_stream(
        self,
        system: str,
        messages: list[ChatTurn],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        self._require_httpx()
        endpoint = self._request_endpoint()
        payload = self._build_payload(
            system, messages, stream=True, temperature=temperature, max_tokens=max_tokens
        )
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:  # type: ignore[union-attr]
            async with client.stream(
                "POST", endpoint, json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise RuntimeError(f"远程 LLM 流式返回 HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    piece = self._extract_stream_delta(obj)
                    if piece:
                        yield piece

    async def _remote_grade(
        self,
        question: str,
        reference: str,
        keywords: list[str],
        user_answer: str,
    ) -> str:
        """请求远程模型对简答作答给出结构化评分；解析失败则抛错以触发本地回退。"""
        system = (
            "你是一名严谨的高中生物阅卷助手。请依据参考答案与关键词，对学生的简答作答评分，"
            "评分范围 0.00~1.00（仅输出 ai 部分分，关键词命中由系统另行计算）。"
            "只输出一行 JSON：{\"score\": <number>, \"feedback\": \"<中文点评>\"}。"
        )
        user = (
            f"题目：{_clean(question)}\n"
            f"参考答案：{_clean(reference)}\n"
            f"关键词：{'、'.join(_clean(k) for k in (keywords or []))}\n"
            f"学生作答：{_clean(user_answer)}"
        )
        raw = await self._remote_complete(
            system,
            [ChatTurn(role="user", content=user)],
            temperature=0.2,
            max_tokens=512,
        )
        score, feedback = self._parse_grade(raw)
        if score is None:
            raise RuntimeError("远程 LLM 判分返回无法解析")
        base = self.simulator.grade(question, reference, keywords, user_answer)
        return f"{feedback}\nAI评分：{score:.2f}\n{base}".strip()

    # -- 解析工具 -----------------------------------------------------------

    @staticmethod
    def _extract_completion_text(data: object) -> str:
        """从 OpenAI 兼容响应体提取文本，兼容 `message.content` / `text`。"""
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):  # 部分实现返回分段数组
                joined = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
                if joined.strip():
                    return joined.strip()
        text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return ""

    @staticmethod
    def _extract_stream_delta(obj: object) -> str:
        """从 OpenAI 兼容流式分片提取增量文本。"""
        if not isinstance(obj, dict):
            return ""
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                return content
        return ""

    @staticmethod
    def _parse_grade(raw: str) -> tuple[Optional[float], str]:
        """解析远程模型返回的评分 JSON；失败返回 (None, 原始文本)。"""
        text = (raw or "").strip()
        # 允许被 ```json ... ``` 包裹
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                return None, text
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None, text
        if not isinstance(obj, dict):
            return None, text
        score_raw = obj.get("score")
        try:
            score = _clamp01(float(score_raw))
        except (TypeError, ValueError):
            return None, str(obj.get("feedback", text))
        feedback = str(obj.get("feedback", "") or "").strip()
        return score, feedback

    # -- 本地流式切片 -------------------------------------------------------

    @staticmethod
    def _chunk(text: str, max_len: int = 40) -> list[str]:
        """把本地完整回复切成适合逐帧下发的片段（按句，过长再按长度切）。"""
        pieces: list[str] = []
        for line in (text or "").split("\n"):
            if line == "":
                continue
            for sentence in _split_sentences(line) or [line]:
                while len(sentence) > max_len:
                    pieces.append(sentence[:max_len])
                    sentence = sentence[max_len:]
                if sentence:
                    pieces.append(sentence)
        if not pieces and text:
            pieces.append(text)
        return pieces