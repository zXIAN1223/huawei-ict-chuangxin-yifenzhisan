"""``C-LLM-ADAPTER@1`` 契约测试（plan 2.2.2 / 2.3.1 / 2.3.2，T5）。

覆盖范围
--------
1. :meth:`AIConfig.resolve_endpoint` —— 2.2.2 归一化规则表**逐行**（空 / 尾斜杠 /
   已含 ``/v1`` / 已含 ``/chat/completions`` / 含查询串 / 无版本前缀）；
2. :attr:`AIAdapter.provider` 判定 —— 地址与密钥均非空 → ``"llm"``，任一缺失或纯空白
   → ``"local"``；
3. 回退矩阵（plan 2.3.1）—— ``complete`` 失败/空正文 → 整段本地；
   ``stream`` 未输出即失败 → 整段本地；已输出后失败 → 追加 ``LOCAL_FALLBACK_MARKER``；
4. 参数生效链（plan 2.3.2）—— ``temperature``/``max_tokens`` 缺省取 ``AIConfig`` 值，
   显式入参优先；非法值回退文档化默认；
5. :func:`build_ai_config` —— 由 ``Settings.llm_*`` 构建，缺属性回退 ``pangu_*``/默认；
6. 密钥不外泄（R5.4）—— 不出现在对象 ``repr`` 与回退后的响应文本中。

测试不依赖真实网络：远程路径全部通过 ``monkeypatch`` 注入替身方法（成功 / 抛错 /
先产出再抛错），断言的是适配层自身的编排语义。

.. note::
    pytest 进程被网络沙箱限制，**无法创建 asyncio 事件循环**（``socketpair`` 回退为
    TCP 环回被阻断，见 ``tests/conftest.py`` 模块文档 / plan 2.1.1）。因此本文件以
    ``send(None)`` 协议**手动驱动**协程与异步生成器：被测路径上的 ``await`` 均为
    立即完成的内联替身（无 sleep / 线程池 / Future），故驱动一次即到达 ``StopIteration``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest

from backend.app.config import Settings
from backend.app.services.ai_adapter import (
    LOCAL_FALLBACK_MARKER,
    AIAdapter,
    AIConfig,
    ChatTurn,
    LocalSimulator,
    build_ai_config,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _run(coroutine):
    """手动驱动协程至完成（无需事件循环，见模块文档 note）。

    被测协程均在首次 ``send(None)`` 内完成；若出现需要事件循环的 ``await``（如
    ``asyncio.sleep`` / ``to_thread``），会显式报错而不是误判为通过。
    """
    try:
        coroutine.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError(
        "协程未在无事件循环下完成：被测路径出现了需要事件循环的 await"
    )


def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    """手动驱动异步生成器，收集全部字符串分片（无需事件循环）。

    说明：对 ``async_generator_asend`` 调 ``send(None)`` 时，每个已产出的分片以
    ``StopIteration.value`` 返回，生成器结束时抛 ``StopAsyncIteration``。
    """
    chunks: list[str] = []
    iterator = stream.__aiter__()
    while True:
        pending = iterator.__anext__()
        try:
            chunk = pending.send(None)
        except StopIteration as stop:
            chunk = stop.value
        except StopAsyncIteration:
            break
        chunks.append(chunk)
    return chunks


def _make_remote_complete(
    *,
    result: str = "远程回答",
    error: Optional[BaseException] = None,
    recorder: Optional[list[tuple[float, int]]] = None,
):
    """构造 ``AIAdapter._remote_complete`` 替身（可记录 temperature/max_tokens）。"""

    async def _fake(
        self: AIAdapter,
        system: str,
        messages: list[ChatTurn],
        temperature: float,
        max_tokens: int,
    ) -> str:
        if recorder is not None:
            recorder.append((temperature, max_tokens))
        if error is not None:
            raise error
        return result

    return _fake


def _make_remote_stream(
    *,
    pieces: tuple[str, ...] = ("远程片段",),
    error: Optional[BaseException] = None,
    recorder: Optional[list[tuple[float, int]]] = None,
):
    """构造 ``AIAdapter._remote_stream`` 替身（可为「先产出再抛错」）。"""

    async def _fake(
        self: AIAdapter,
        system: str,
        messages: list[ChatTurn],
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        if recorder is not None:
            recorder.append((temperature, max_tokens))
        for piece in pieces:
            yield piece
        if error is not None:
            raise error

    return _fake


def _llm_adapter(**config_kwargs: Any) -> AIAdapter:
    """构建指向「有效远程配置」的适配器（实际调用由替身接管）。"""
    params: dict[str, Any] = {
        "api_url": "https://llm.example/v1",
        "api_key": "test-key",
    }
    params.update(config_kwargs)
    return AIAdapter(AIConfig(**params), mode="teacher", kp=None)


def _local_text(message: str = "请讲解细胞膜") -> str:
    """本地模拟器在 ``teacher``/无知识点上下文下的完整回复。"""
    return LocalSimulator().render("teacher", None, message)


def _normalize_whitespace(text: str) -> str:
    """移除全部空白，用于比较「按句分片拼接」与「完整文本」的等价性。"""
    return "".join(text.split())


_MESSAGES = [ChatTurn(role="user", content="请讲解细胞膜")]


# ---------------------------------------------------------------------------
# 1) resolve_endpoint 规则表（逐行）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_url", "expected"),
    [
        pytest.param(None, None, id="empty-none"),
        pytest.param("", None, id="empty-str"),
        pytest.param("   ", None, id="empty-blank"),
        pytest.param(
            "https://h/v1/chat/completions",
            "https://h/v1/chat/completions",
            id="already-endpoint",
        ),
        pytest.param(
            "https://h/v1/chat/completions?x=1",
            "https://h/v1/chat/completions?x=1",
            id="endpoint-with-query",
        ),
        pytest.param(
            "https://h/v1/",
            "https://h/v1/chat/completions",
            id="base-trailing-slash",
        ),
        pytest.param(
            "https://h/v1", "https://h/v1/chat/completions", id="base-with-version"
        ),
        pytest.param(
            "https://h/api",
            "https://h/api/chat/completions",
            id="base-without-version-no-v1-injection",
        ),
        # 规则顺序：先判定「已含 /chat/completions」再处理尾斜杠 → 原样返回
        pytest.param(
            "https://h/v1/chat/completions/",
            "https://h/v1/chat/completions/",
            id="endpoint-trailing-slash-kept",
        ),
        pytest.param(
            "  https://h/v1  ",
            "https://h/v1/chat/completions",
            id="surrounding-whitespace-stripped",
        ),
        pytest.param(
            "https://h/api///",
            "https://h/api/chat/completions",
            id="multiple-trailing-slashes",
        ),
    ],
)
def test_resolve_endpoint_rule_table(api_url: Optional[str], expected: Optional[str]) -> None:
    """``resolve_endpoint()`` 必须逐行符合 2.2.2 规则表。"""
    assert AIConfig(api_url=api_url).resolve_endpoint() == expected


# ---------------------------------------------------------------------------
# 2) provider 判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_url", "api_key", "expected"),
    [
        pytest.param("https://h/v1", "secret", "llm", id="url+key"),
        pytest.param("https://h/v1", None, "local", id="url-only"),
        pytest.param(None, "secret", "local", id="key-only"),
        pytest.param(None, None, "local", id="both-none"),
        pytest.param("", "", "local", id="both-empty"),
        pytest.param("   ", "   ", "local", id="both-blank"),
        pytest.param("https://h/v1", "   ", "local", id="blank-key"),
        pytest.param("   ", "secret", "local", id="blank-url"),
    ],
)
def test_adapter_provider_decision(
    api_url: Optional[str], api_key: Optional[str], expected: str
) -> None:
    """provider 当且仅当「地址+密钥均非空且 endpoint 可归一化」时为 llm。"""
    adapter = AIAdapter(AIConfig(api_url=api_url, api_key=api_key))
    assert adapter.provider == expected
    assert adapter.last_provider == expected


def test_enabled_property_matches_provider_rule() -> None:
    """``AIConfig.enabled`` 语义：地址与密钥均非空。"""
    assert AIConfig(api_url="https://h/v1", api_key="k").enabled is True
    assert AIConfig(api_url="https://h/v1", api_key="").enabled is False
    assert AIConfig(api_url="", api_key="k").enabled is False
    assert AIConfig().enabled is False


# ---------------------------------------------------------------------------
# 3) 回退矩阵
# ---------------------------------------------------------------------------


def test_complete_remote_success(monkeypatch) -> None:
    """远程成功 → provider/last_provider 均为 llm，且返回远程正文。"""
    adapter = _llm_adapter()
    monkeypatch.setattr(AIAdapter, "_remote_complete", _make_remote_complete(result="远程讲解"))

    result = _run(adapter.complete("系统提示", _MESSAGES))

    assert result.provider == "llm"
    assert result.text == "远程讲解"
    assert adapter.last_provider == "llm"


def test_complete_falls_back_to_local_on_error(monkeypatch) -> None:
    """远程抛错 → 整段本地模拟，last_provider 变为 local（plan 2.3.1）。"""
    adapter = _llm_adapter()
    monkeypatch.setattr(
        AIAdapter,
        "_remote_complete",
        _make_remote_complete(error=RuntimeError("连接被拒绝")),
    )

    result = _run(adapter.complete("系统提示", _MESSAGES))

    assert result.provider == "local"
    assert adapter.last_provider == "local"
    assert result.text == _local_text()


def test_complete_falls_back_on_empty_body(monkeypatch) -> None:
    """远程返回空正文视为失败（R2.6：不得产生空回复）。

    适配层的空正文判定位于 ``_remote_complete`` 内部（``raise RuntimeError``）；
    此处以「等价替身」验证其上层编排仍是整段本地回退。
    """
    adapter = _llm_adapter()
    monkeypatch.setattr(
        AIAdapter,
        "_remote_complete",
        _make_remote_complete(error=RuntimeError("远程 LLM 返回正文为空")),
    )

    result = _run(adapter.complete("系统提示", _MESSAGES))

    assert result.provider == "local"
    assert result.text and result.text == _local_text()


def test_complete_skips_remote_when_not_configured(monkeypatch) -> None:
    """无凭证时不发起远程调用（provider == local）。"""
    calls: list[str] = []

    async def _explode(*args: Any, **kwargs: Any) -> str:
        calls.append("called")
        raise AssertionError("无凭证时不应发起远程调用")

    monkeypatch.setattr(AIAdapter, "_remote_complete", _explode)
    adapter = AIAdapter(AIConfig(api_url=None, api_key=None))

    result = _run(adapter.complete("系统提示", _MESSAGES))

    assert calls == []
    assert result.provider == "local"
    assert result.text == _local_text()


def test_stream_remote_success(monkeypatch) -> None:
    """远程流式成功 → 分片按序产出，last_provider 为 llm。"""
    adapter = _llm_adapter()
    monkeypatch.setattr(
        AIAdapter,
        "_remote_stream",
        _make_remote_stream(pieces=("你好", "，我是", "远程模型")),
    )

    chunks = _collect_stream(adapter.stream("系统提示", _MESSAGES))

    assert chunks == ["你好", "，我是", "远程模型"]
    assert adapter.last_provider == "llm"


def test_stream_falls_back_before_any_output(monkeypatch) -> None:
    """未输出即失败 → **整段**本地模拟，且不追加回退标记。"""
    adapter = _llm_adapter()
    monkeypatch.setattr(
        AIAdapter,
        "_remote_stream",
        _make_remote_stream(pieces=(), error=RuntimeError("HTTP 500")),
    )

    chunks = _collect_stream(adapter.stream("系统提示", _MESSAGES))

    expected = _local_text()
    # 分片按句切分（每句 strip），拼接后与完整文本「空白无关」等价
    assert _normalize_whitespace("".join(chunks)) == _normalize_whitespace(expected)
    assert chunks == AIAdapter._chunk(expected)  # 逐句分片与本地模拟一致
    assert LOCAL_FALLBACK_MARKER not in chunks
    assert adapter.last_provider == "local"


def test_stream_appends_marker_after_partial_output(monkeypatch) -> None:
    """已输出部分片段后失败 → 结束流并追加 ``LOCAL_FALLBACK_MARKER``。"""
    adapter = _llm_adapter()
    monkeypatch.setattr(
        AIAdapter,
        "_remote_stream",
        _make_remote_stream(pieces=("远程前半段",), error=RuntimeError("流中断")),
    )

    chunks = _collect_stream(adapter.stream("系统提示", _MESSAGES))

    assert chunks == ["远程前半段", LOCAL_FALLBACK_MARKER]
    assert LOCAL_FALLBACK_MARKER == "[已切换本地模式]"
    assert adapter.last_provider == "local"


def test_stream_local_when_not_configured(monkeypatch) -> None:
    """无凭证流式 → 直接本地分片，last_provider 为 local。"""
    adapter = AIAdapter(AIConfig())

    chunks = _collect_stream(adapter.stream("系统提示", _MESSAGES))

    assert _normalize_whitespace("".join(chunks)) == _normalize_whitespace(_local_text())
    assert chunks
    assert adapter.last_provider == "local"


# ---------------------------------------------------------------------------
# 4) 参数生效链（默认取 config，显式入参优先）
# ---------------------------------------------------------------------------


def test_complete_uses_config_generation_params_by_default(monkeypatch) -> None:
    """``complete`` 未传参时取 ``AIConfig.temperature``/``max_tokens``。"""
    recorded: list[tuple[float, int]] = []
    monkeypatch.setattr(
        AIAdapter,
        "_remote_complete",
        _make_remote_complete(result="ok", recorder=recorded),
    )
    adapter = _llm_adapter(temperature=0.35, max_tokens=321)

    _run(adapter.complete("系统提示", _MESSAGES))

    assert recorded == [(0.35, 321)]


def test_complete_explicit_params_override_config(monkeypatch) -> None:
    """显式入参优先于 ``AIConfig`` 默认值。"""
    recorded: list[tuple[float, int]] = []
    monkeypatch.setattr(
        AIAdapter,
        "_remote_complete",
        _make_remote_complete(result="ok", recorder=recorded),
    )
    adapter = _llm_adapter(temperature=0.35, max_tokens=321)

    _run(adapter.complete("系统提示", _MESSAGES, temperature=1.1, max_tokens=64))

    assert recorded == [(1.1, 64)]


def test_stream_uses_config_generation_params_by_default(monkeypatch) -> None:
    """``stream`` 未传参时同样取 ``AIConfig`` 值。"""
    recorded: list[tuple[float, int]] = []
    monkeypatch.setattr(
        AIAdapter,
        "_remote_stream",
        _make_remote_stream(pieces=("a",), recorder=recorded),
    )
    adapter = _llm_adapter(temperature=1.2, max_tokens=777)

    chunks = _collect_stream(adapter.stream("系统提示", _MESSAGES))

    assert chunks == ["a"]
    assert recorded == [(1.2, 777)]


@pytest.mark.parametrize(
    ("temperature", "max_tokens", "expected_temperature", "expected_max_tokens"),
    [
        pytest.param(5.0, 10, 2.0, 10, id="temperature-clamped-high"),
        pytest.param(-1.0, 10, 0.0, 10, id="temperature-clamped-low"),
        pytest.param("非法", 10, 0.7, 10, id="temperature-invalid-default"),
        pytest.param(0.5, 0, 0.5, 1024, id="max-tokens-lt-1-default"),
        pytest.param(0.5, -3, 0.5, 1024, id="max-tokens-negative-default"),
        pytest.param(0.5, "非法", 0.5, 1024, id="max-tokens-invalid-default"),
    ],
)
def test_generation_params_validation(
    monkeypatch,
    temperature: Any,
    max_tokens: Any,
    expected_temperature: float,
    expected_max_tokens: int,
) -> None:
    """非法/越界的生成参数回退文档化默认（plan 2.3.2）。"""
    recorded: list[tuple[float, int]] = []
    monkeypatch.setattr(
        AIAdapter,
        "_remote_complete",
        _make_remote_complete(result="ok", recorder=recorded),
    )
    adapter = _llm_adapter(temperature=temperature, max_tokens=max_tokens)

    _run(adapter.complete("系统提示", _MESSAGES))

    assert recorded == [(expected_temperature, expected_max_tokens)]


# ---------------------------------------------------------------------------
# 5) build_ai_config（Settings.llm_* → AIConfig）
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> Settings:
    """构造 ``Settings``：默认无凭证，可按需覆盖任意字段。"""
    base: dict[str, Any] = {
        "db_path": Path("test.db"),
        "data_dir": Path("data"),
        "frontend_dir": Path("frontend"),
        "pangu_api_url": None,
        "pangu_api_key": None,
        "pangu_model": "pangu",
        "pangu_timeout": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def test_build_ai_config_from_llm_fields() -> None:
    """``build_ai_config`` 以 ``llm_*`` 为准填充 AIConfig。"""
    settings = _make_settings(
        llm_base_url="https://llm/v1",
        llm_api_key="llm-key",
        llm_model="llm-model",
        llm_timeout=7.5,
        llm_temperature=0.4,
        llm_max_tokens=256,
        # 旧别名应当被忽略（优先级低于 llm_*）
        pangu_api_url="https://pangu/v1",
        pangu_api_key="pangu-key",
        pangu_model="pangu-model",
        pangu_timeout=99.0,
    )

    config = build_ai_config(settings)

    assert config.api_url == "https://llm/v1"
    assert config.api_key == "llm-key"
    assert config.model == "llm-model"
    assert config.timeout == 7.5
    assert config.temperature == 0.4
    assert config.max_tokens == 256
    assert config.enabled is True


def test_build_ai_config_falls_back_to_pangu_fields() -> None:
    """``llm_*`` 缺失/空白时回退 ``pangu_*``（R6.3 向后兼容）。"""
    settings = _make_settings(
        llm_base_url=None,
        llm_api_key="",
        llm_model="",
        llm_timeout=None,
        pangu_api_url="https://pangu/v1",
        pangu_api_key="pangu-key",
        pangu_model="pangu-model",
        pangu_timeout=12.0,
    )

    config = build_ai_config(settings)

    assert config.api_url == "https://pangu/v1"
    assert config.api_key == "pangu-key"
    assert config.model == "pangu-model"
    assert config.timeout == 12.0


def test_build_ai_config_clamps_and_validates() -> None:
    """``llm_temperature`` clamp 至 [0,2]；``llm_max_tokens`` < 1 回退默认。"""
    hot = build_ai_config(_make_settings(llm_temperature=9.0, llm_max_tokens=0))
    assert hot.temperature == 2.0
    assert hot.max_tokens == 1024

    cold = build_ai_config(_make_settings(llm_temperature=-3.0, llm_max_tokens=-1))
    assert cold.temperature == 0.0
    assert cold.max_tokens == 1024

    invalid = build_ai_config(_make_settings(llm_temperature="非法", llm_max_tokens="非法"))
    assert invalid.temperature == 0.7
    assert invalid.max_tokens == 1024


def test_build_ai_config_without_credentials_is_disabled() -> None:
    """无凭证 → AIConfig.enabled 为 False，适配器判定 local。"""
    config = build_ai_config(_make_settings())
    assert config.enabled is False
    assert config.resolve_endpoint() is None
    assert AIAdapter(config).provider == "local"


# ---------------------------------------------------------------------------
# 6) 密钥不外泄（R5.4）
# ---------------------------------------------------------------------------


def test_api_key_absent_from_repr() -> None:
    """密钥不得出现在 ``repr``/``str``（含异常回溯中的局部变量打印）。"""
    secret = "super-secret-key"
    config = AIConfig(api_url="https://h/v1", api_key=secret)
    adapter = AIAdapter(config)

    for rendered in (repr(config), str(config), repr(adapter), str(adapter)):
        assert secret not in rendered
    assert "api_url" in repr(config)  # 其余字段可见，仅密钥被隐藏（field(repr=False)）


def test_api_key_absent_from_fallback_text(monkeypatch) -> None:
    """远程异常信息含密钥时，回退结果文本也不得携带密钥。"""
    secret = "super-secret-key"

    async def _raise_with_secret(self: AIAdapter, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError(f"请求失败 Authorization=Bearer {self.config.api_key}")

    monkeypatch.setattr(AIAdapter, "_remote_complete", _raise_with_secret)
    adapter = _llm_adapter(api_key=secret)

    result = _run(adapter.complete("系统提示", _MESSAGES))

    assert result.provider == "local"
    assert secret not in result.text