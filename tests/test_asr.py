"""``C-ASR-ENGINE@1`` / ``C-ASR-API@1`` 契约测试（plan 2.2.9 / 2.2.10，T5）。

覆盖范围
--------
1. **可用性判定**：``available()`` 在「被禁用 / 未安装 vosk / 模型目录缺失」时为
   ``False`` 且**不抛异常**；可用（替身 vosk + 存在的模型目录）时为 ``True``；
2. **音频校验**：``transcribe()`` 对空字节 / 非 WAV / 非 16kHz / 非单声道 / 非
   16-bit 抛 :class:`AsrBadAudioError`；超 ``max_bytes`` / 超 ``max_duration_ms`` 抛
   :class:`AsrTooLongError`；不可用抛 :class:`AsrUnavailableError`；识别异常抛
   :class:`AsrFailedError`；无语音时 ``text=""``（非错误）；
3. **``POST /api/asr`` 契约**：缺 ``X-User-Id`` → ``400 MISSING_USER_ID``；模型缺失
   → ``503 ASR_UNAVAILABLE``（含 ``detail.reason``）；坏音频 → ``400 ASR_BAD_AUDIO``；
   超长 → ``413 ASR_TOO_LONG``；正常 → 200 且响应形状符合 2.2.10；
4. **``C-ASR-CONFIG@1``**：``ASR_*`` 真值解析、相对模型目录解析、非法数值回退默认，
   以及 :func:`build_asr_engine` 从 ``Settings`` 构造。

实现说明
--------
* 音频一律在进程内用标准库 ``wave`` 构造（16kHz / 单声道 / PCM16），无外部依赖；
* ``vosk`` 未安装时真实路径天然覆盖 ``vosk_missing``；需要「vosk 存在」的分支通过
  ``sys.modules`` 注入替身模块（含 ``Model`` / ``KaldiRecognizer``）验证；
* ``POST /api/asr`` 是 ``async`` 端点且内部 ``await asyncio.wait_for(asyncio.to_thread(...))``；
  但 pytest 进程被网络沙箱限制、**无法创建事件循环**（见 ``tests/conftest.py`` 文档）。
  故用例把 ``routers.asr`` 模块内的 ``asyncio`` 替换为同步替身（``to_thread`` /
  ``wait_for`` 立即完成），再以 ``send(None)`` 手动驱动端点协程——仍真实执行阀值判定、
  音频校验、错误映射与响应装配，只是不经过线程池与超时定时器。路由注册与鉴权仍走
  ``DirectClient``（``client`` 夹具）验证。数据库使用 ``ALA_DB_PATH`` 临时库，不触碰运行库。
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import wave
from pathlib import Path
from typing import Any, Optional

import pytest

from backend.app import config as config_module
from backend.app import routers as routers_package
from backend.app.deps import ApiError
from backend.app.routers.asr import post_asr
from backend.app.services.asr import (
    ASR_PROVIDER,
    DEFAULT_ASR_MODEL_DIR,
    UNAVAILABLE_REASONS,
    AsrBadAudioError,
    AsrEngine,
    AsrFailedError,
    AsrTooLongError,
    AsrTranscription,
    AsrUnavailableError,
    build_asr_engine,
)

USER_ID = "test-user-0001"


@pytest.fixture(autouse=True)
def _synchronous_asyncio_in_asr_router(monkeypatch):
    """把 ``routers.asr`` 内的 ``asyncio`` 换成同步替身（沙箱无法创建事件循环）。"""
    asr_router = routers_package.asr

    async def _to_thread(func, /, *args: Any, **kwargs: Any) -> Any:
        """同步执行线程池任务（不跨线程，便于手动驱动）。"""
        return func(*args, **kwargs)

    async def _wait_for(awaitable: Any, timeout: Optional[float] = None) -> Any:
        """直接等待内部 awaitable（超时语义由 ``ASR_FAILED/timeout`` 分支另行覆盖）。"""
        return await awaitable

    shim = types.SimpleNamespace(
        to_thread=_to_thread,
        wait_for=_wait_for,
        TimeoutError=asyncio.TimeoutError,
    )
    monkeypatch.setattr(asr_router, "asyncio", shim)


# ---------------------------------------------------------------------------
# 辅助：WAV 构造 / 引擎构造 / vosk 替身
# ---------------------------------------------------------------------------


def make_wav(
    *,
    duration_ms: int = 500,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """构造指定规格的静音 WAV 字节流（默认 16kHz / 单声道 / PCM16）。"""
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * (frame_count * channels * sample_width))
    return buffer.getvalue()


def make_engine(tmp_path: Path, **overrides: Any) -> AsrEngine:
    """构造引擎：默认指向**不存在**的模型目录（覆盖不可用分支）。"""
    params: dict[str, Any] = {
        "enabled": True,
        "model_dir": tmp_path / "absent-model-dir",
        "lang": "zh-CN",
        "timeout": 5.0,
        "max_duration_ms": 60_000,
        "max_bytes": 10 * 1024 * 1024,
    }
    params.update(overrides)
    return AsrEngine(**params)


def install_fake_vosk(
    monkeypatch,
    *,
    final_text: str = "你好世界",
    recognizer_error: Optional[BaseException] = None,
) -> types.ModuleType:
    """把替身 ``vosk`` 模块注入 ``sys.modules``（模拟依赖已安装）。

    :param final_text: ``FinalResult()`` 返回的识别文本（空串用于「无语音」场景）。
    :param recognizer_error: 若提供，``KaldiRecognizer`` 构造即抛该异常。
    """
    module = types.ModuleType("vosk")

    class _FakeModel:
        def __init__(self, model_path: str) -> None:
            self.model_path = model_path

    class _FakeRecognizer:
        def __init__(self, model: Any, sample_rate: int) -> None:
            if recognizer_error is not None:
                raise recognizer_error
            self.sample_rate = sample_rate
            self.chunks: list[bytes] = []

        def AcceptWaveform(self, chunk: bytes) -> bool:
            self.chunks.append(chunk)
            return False

        def Result(self) -> str:
            return json.dumps({"text": ""}, ensure_ascii=False)

        def FinalResult(self) -> str:
            return json.dumps({"text": final_text}, ensure_ascii=False)

    module.Model = _FakeModel  # type: ignore[attr-defined]
    module.KaldiRecognizer = _FakeRecognizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vosk", module)
    return module


class StubAsrEngine:
    """替身引擎：不加载 vosk，返回固定识别结果（用于 200 响应形状断言）。"""

    def __init__(
        self,
        *,
        text: str = "识别到的文本",
        max_bytes: int = 10 * 1024 * 1024,
        timeout: float = 5.0,
        lang: str = "zh-CN",
    ) -> None:
        self.text = text
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.lang = lang
        self.calls: list[Optional[str]] = []

    def transcribe(self, audio: bytes, *, lang: Optional[str] = None) -> AsrTranscription:
        self.calls.append(lang)
        return AsrTranscription(
            text=self.text,
            lang=lang or self.lang,
            duration_ms=1234,
            model="stub-model",
        )


class FakeUploadFile:
    """``UploadFile`` 替身：仅实现端点使用的异步 ``read()``。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._payload[self._offset :]
        else:
            chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def call_post_asr(
    *,
    engine: Any,
    payload: Optional[bytes] = None,
    lang: Optional[str] = None,
    user_id: str = USER_ID,
) -> tuple[int, dict[str, Any]]:
    """在无事件循环环境下驱动 ``post_asr`` 端点协程，返回 ``(status, body)``。

    端点内部依赖（``user_id`` / ``engine``）以显式入参注入，等价于 FastAPI 的
    ``Depends`` 解析结果；业务异常按 ``main.py`` 的统一错误体形状还原。
    ``asyncio`` 已由模块级夹具替换为同步替身，故 ``send(None)`` 一次即可完成。
    """
    upload = FakeUploadFile(payload) if payload is not None else None
    coroutine = post_asr(audio=upload, lang=lang, user_id=user_id, engine=engine)
    try:
        try:
            coroutine.send(None)
        except StopIteration as stop:
            result = stop.value
        else:
            raise RuntimeError("端点协程未在无事件循环下完成（存在需要事件循环的 await）")
    except ApiError as exc:
        body: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.detail is not None:
            body["detail"] = exc.detail
        return exc.status_code, body
    return 200, dict(result)


# ---------------------------------------------------------------------------
# 1) available()：不抛异常
# ---------------------------------------------------------------------------


def test_available_false_when_disabled(tmp_path) -> None:
    """``enabled=False`` → available() 为 False，原因为 ``disabled``。"""
    engine = make_engine(tmp_path, enabled=False)
    assert engine.available() is False
    assert engine.unavailable_reason() == "disabled"


def test_available_false_when_model_dir_missing(tmp_path) -> None:
    """模型目录不存在 → False（vosk 未安装时为 ``vosk_missing``，否则 ``model_missing``）。"""
    engine = make_engine(tmp_path, model_dir=tmp_path / "no-such-model")
    assert engine.available() is False
    assert engine.unavailable_reason() in ("vosk_missing", "model_missing")
    # 契约要求「不得抛异常」：重复调用与 warmup 同样不应抛出
    assert engine.available() is False
    assert engine.warmup() is False


def test_available_false_when_vosk_present_but_model_missing(tmp_path, monkeypatch) -> None:
    """vosk 可导入但模型目录缺失 → False，原因精确为 ``model_missing``。"""
    install_fake_vosk(monkeypatch)
    engine = make_engine(tmp_path, model_dir=tmp_path / "no-such-model")
    assert engine.available() is False
    assert engine.unavailable_reason() == "model_missing"
    assert str(engine.unavailable_reason()) in UNAVAILABLE_REASONS


def test_available_true_with_vosk_and_model_dir(tmp_path, monkeypatch) -> None:
    """vosk 可导入且模型目录存在 → True；warmup 可预加载。"""
    install_fake_vosk(monkeypatch)
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    engine = make_engine(tmp_path, model_dir=model_dir)

    assert engine.available() is True
    assert engine.unavailable_reason() is None
    assert engine.warmup() is True
    assert engine.model_name == "fake-model"


# ---------------------------------------------------------------------------
# 2) transcribe()：音频校验与识别语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload_factory",
    [
        pytest.param(lambda: b"", id="empty-bytes"),
        pytest.param(lambda: b"not-a-wav-file", id="not-wav"),
        pytest.param(lambda: make_wav(sample_rate=8000), id="wrong-sample-rate"),
        pytest.param(lambda: make_wav(channels=2), id="wrong-channels"),
        pytest.param(lambda: make_wav(sample_width=1), id="wrong-sample-width"),
    ],
)
def test_transcribe_rejects_bad_audio(tmp_path, payload_factory) -> None:
    """空字节 / 非 WAV / 非 16kHz / 非单声道 / 非 16-bit → ``AsrBadAudioError``。"""
    engine = make_engine(tmp_path)
    payload = payload_factory()

    with pytest.raises(AsrBadAudioError):
        engine.transcribe(payload)


def test_transcribe_rejects_oversized_bytes(tmp_path) -> None:
    """超过 ``max_bytes`` → ``AsrTooLongError``（体积优先于格式判定）。"""
    engine = make_engine(tmp_path, max_bytes=200)
    payload = make_wav(duration_ms=500)
    assert len(payload) > 200

    with pytest.raises(AsrTooLongError):
        engine.transcribe(payload)


def test_transcribe_rejects_oversized_duration(tmp_path) -> None:
    """超过 ``max_duration_ms`` → ``AsrTooLongError``。"""
    engine = make_engine(tmp_path, max_duration_ms=1000)
    payload = make_wav(duration_ms=2000)

    with pytest.raises(AsrTooLongError):
        engine.transcribe(payload)


def test_transcribe_raises_unavailable_when_model_missing(tmp_path) -> None:
    """合规音频但引擎不可用 → ``AsrUnavailableError``。"""
    engine = make_engine(tmp_path, model_dir=tmp_path / "no-such-model")

    with pytest.raises(AsrUnavailableError) as excinfo:
        engine.transcribe(make_wav())
    assert excinfo.value.reason in ("vosk_missing", "model_missing")


def test_transcribe_returns_text_with_vosk(tmp_path, monkeypatch) -> None:
    """vosk 可用 → 返回识别文本、语言、时长与模型名。"""
    install_fake_vosk(monkeypatch, final_text="细胞膜由磷脂双分子层构成")
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    engine = make_engine(tmp_path, model_dir=model_dir)

    transcription = engine.transcribe(make_wav(duration_ms=1000))

    assert transcription.text == "细胞膜由磷脂双分子层构成"
    assert transcription.lang == "zh-CN"
    assert transcription.duration_ms == 1000
    assert transcription.model == "fake-model"


def test_transcribe_empty_speech_is_not_an_error(tmp_path, monkeypatch) -> None:
    """无语音（识别结果为空串）不是错误。"""
    install_fake_vosk(monkeypatch, final_text="")
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    engine = make_engine(tmp_path, model_dir=model_dir)

    transcription = engine.transcribe(make_wav())

    assert transcription.text == ""


def test_transcribe_lang_override(tmp_path, monkeypatch) -> None:
    """``lang`` 入参覆盖实例默认语言。"""
    install_fake_vosk(monkeypatch)
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    engine = make_engine(tmp_path, model_dir=model_dir, lang="zh-CN")

    assert engine.transcribe(make_wav(), lang="en-US").lang == "en-US"
    assert engine.transcribe(make_wav(), lang="   ").lang == "zh-CN"


def test_transcribe_wraps_recognition_failure(tmp_path, monkeypatch) -> None:
    """识别过程异常 → ``AsrFailedError``（reason 为 ``recognition_error``）。"""
    install_fake_vosk(monkeypatch, recognizer_error=RuntimeError("解码失败"))
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()
    engine = make_engine(tmp_path, model_dir=model_dir)

    with pytest.raises(AsrFailedError) as excinfo:
        engine.transcribe(make_wav())
    assert excinfo.value.reason == "recognition_error"


# ---------------------------------------------------------------------------
# 3) POST /api/asr 契约
# ---------------------------------------------------------------------------


def test_post_asr_missing_user_id_returns_400(client) -> None:
    """缺 ``X-User-Id`` → 400 ``MISSING_USER_ID``（既有 deps 鉴权）。"""
    response = client.post("/api/asr")

    assert response.status_code != 404, "「/api/asr」未注册：需 T7 交付 routers/asr.py 并注册路由"
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_USER_ID"


def test_post_asr_unavailable_returns_503(tmp_path) -> None:
    """模型缺失（未安装 vosk / 无模型目录）→ 503 ``ASR_UNAVAILABLE`` + ``detail.reason``。"""
    engine = make_engine(tmp_path, model_dir=tmp_path / "no-such-model")

    status, body = call_post_asr(engine=engine, payload=make_wav())

    assert status == 503
    assert body["code"] == "ASR_UNAVAILABLE"
    assert body["message"]
    assert body["detail"]["reason"] in ("vosk_missing", "model_missing")


def test_post_asr_missing_audio_returns_400(tmp_path) -> None:
    """缺少 ``audio`` 文件字段 → 400 ``ASR_BAD_AUDIO``。"""
    status, body = call_post_asr(engine=StubAsrEngine(), payload=None)

    assert status == 400
    assert body["code"] == "ASR_BAD_AUDIO"


def test_post_asr_bad_audio_returns_400(tmp_path) -> None:
    """非 WAV 字节 → 400 ``ASR_BAD_AUDIO``。"""
    engine = make_engine(tmp_path, model_dir=tmp_path / "no-such-model")

    status, body = call_post_asr(engine=engine, payload=b"definitely-not-a-wav")

    assert status == 400
    assert body["code"] == "ASR_BAD_AUDIO"


def test_post_asr_too_long_bytes_returns_413(tmp_path) -> None:
    """超 ``ASR_MAX_BYTES`` → 413 ``ASR_TOO_LONG``。"""
    engine = make_engine(tmp_path, max_bytes=200, model_dir=tmp_path / "no-such-model")

    status, body = call_post_asr(engine=engine, payload=make_wav(duration_ms=500))

    assert status == 413
    assert body["code"] == "ASR_TOO_LONG"


def test_post_asr_too_long_duration_returns_413(tmp_path) -> None:
    """超 ``ASR_MAX_DURATION_MS`` → 413 ``ASR_TOO_LONG``。"""
    engine = make_engine(
        tmp_path, max_duration_ms=1000, model_dir=tmp_path / "no-such-model"
    )

    status, body = call_post_asr(engine=engine, payload=make_wav(duration_ms=2000))

    assert status == 413
    assert body["code"] == "ASR_TOO_LONG"


def test_post_asr_success_response_shape() -> None:
    """正常识别 → 200 且响应形状符合 2.2.10（替身引擎避免真实模型依赖）。"""
    engine = StubAsrEngine(text="细胞膜是选择透过性膜")

    status, body = call_post_asr(engine=engine, payload=make_wav())

    assert status == 200
    assert body == {
        "text": "细胞膜是选择透过性膜",
        "provider": ASR_PROVIDER,
        "vendor": ASR_PROVIDER,
        "lang": "zh-CN",
        "duration_ms": 1234,
        "model": "stub-model",
    }
    assert body["provider"] == "vosk" and body["vendor"] == "vosk"


def test_post_asr_lang_override_reaches_engine() -> None:
    """表单 ``lang`` 覆盖值应传入引擎并回显。"""
    engine = StubAsrEngine()

    status, body = call_post_asr(engine=engine, payload=make_wav(), lang=" en-US ")

    assert status == 200
    assert body["lang"] == "en-US"
    assert engine.calls == ["en-US"]


def test_post_asr_blank_lang_uses_engine_default() -> None:
    """空白 ``lang`` 视为未提供 → 使用引擎默认语言。"""
    engine = StubAsrEngine(lang="zh-CN")

    status, body = call_post_asr(engine=engine, payload=make_wav(), lang="   ")

    assert status == 200
    assert body["lang"] == "zh-CN"
    assert engine.calls == [None]


# ---------------------------------------------------------------------------
# 4) C-ASR-CONFIG@1 配置读取与 build_asr_engine
# ---------------------------------------------------------------------------

_ASR_ENV_KEYS = (
    "ASR_ENABLED",
    "ASR_MODEL_DIR",
    "ASR_LANG",
    "ASR_TIMEOUT",
    "ASR_MAX_DURATION_MS",
    "ASR_MAX_BYTES",
)


@pytest.fixture()
def asr_env(monkeypatch, tmp_path):
    """隔离 ``ASR_*`` 配置来源（进程环境变量 + 不存在的 ``.env``）。"""
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / "absent.env")
    for key in _ASR_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize("literal", ["1", "true", "YES", "on"])
def test_asr_enabled_true_literals(asr_env, literal: str) -> None:
    """``ASR_ENABLED`` 真值字面量 → True。"""
    asr_env.setenv("ASR_ENABLED", literal)
    assert config_module.get_settings().asr_enabled is True


@pytest.mark.parametrize("literal", ["0", "false", "No", "off"])
def test_asr_enabled_false_literals(asr_env, literal: str) -> None:
    """``ASR_ENABLED`` 假值字面量 → False。"""
    asr_env.setenv("ASR_ENABLED", literal)
    assert config_module.get_settings().asr_enabled is False


def test_asr_enabled_unknown_literal_falls_back_to_default(asr_env) -> None:
    """无法识别的 ``ASR_ENABLED`` → 默认 True。"""
    asr_env.setenv("ASR_ENABLED", "maybe")
    assert config_module.get_settings().asr_enabled is True


def test_asr_settings_defaults(asr_env) -> None:
    """全部未设置 → 契约默认值（模型目录为仓库根下的绝对路径）。"""
    settings = config_module.get_settings()

    assert settings.asr_enabled is True
    assert settings.asr_model_dir == DEFAULT_ASR_MODEL_DIR
    assert settings.asr_model_dir.is_absolute()
    assert settings.asr_lang == "zh-CN"
    assert settings.asr_timeout == 30.0
    assert settings.asr_max_duration_ms == 60000
    assert settings.asr_max_bytes == 10 * 1024 * 1024


def test_asr_model_dir_resolves_relative_to_repo_root(asr_env) -> None:
    """相对 ``ASR_MODEL_DIR`` 基于仓库根解析为绝对路径；空白回退默认。"""
    asr_env.setenv("ASR_MODEL_DIR", "backend/app/data/vosk/custom-model")

    settings = config_module.get_settings()
    expected = (config_module.REPO_ROOT / "backend/app/data/vosk/custom-model").resolve()
    assert settings.asr_model_dir == expected
    assert settings.asr_model_dir.is_absolute()

    asr_env.setenv("ASR_MODEL_DIR", "   ")
    assert config_module.get_settings().asr_model_dir == DEFAULT_ASR_MODEL_DIR


def test_asr_settings_valid_overrides(asr_env) -> None:
    """合法 ``ASR_*`` 覆盖值生效（语言去空白、数值按类型转换）。"""
    asr_env.setenv("ASR_LANG", " en-US ")
    asr_env.setenv("ASR_TIMEOUT", "12.5")
    asr_env.setenv("ASR_MAX_DURATION_MS", "30000")
    asr_env.setenv("ASR_MAX_BYTES", "2048")

    settings = config_module.get_settings()

    assert settings.asr_lang == "en-US"
    assert settings.asr_timeout == 12.5
    assert settings.asr_max_duration_ms == 30000
    assert settings.asr_max_bytes == 2048


def test_asr_settings_invalid_numbers_fall_back(asr_env) -> None:
    """非法/低于下限的数值配置回退默认（``<1000`` 时长、``<1024`` 体积）。"""
    asr_env.setenv("ASR_TIMEOUT", "abc")
    asr_env.setenv("ASR_MAX_DURATION_MS", "500")
    asr_env.setenv("ASR_MAX_BYTES", "100")

    settings = config_module.get_settings()

    assert settings.asr_timeout == config_module.DEFAULT_ASR_TIMEOUT
    assert settings.asr_max_duration_ms == config_module.DEFAULT_ASR_MAX_DURATION_MS
    assert settings.asr_max_bytes == config_module.DEFAULT_ASR_MAX_BYTES


def test_build_asr_engine_consumes_settings(asr_env) -> None:
    """``build_asr_engine()`` 按 ``Settings.asr_*`` 构造，禁用时 available() 为 False。"""
    asr_env.setenv("ASR_ENABLED", "false")
    asr_env.setenv("ASR_MODEL_DIR", "backend/app/data/vosk/custom-model")
    asr_env.setenv("ASR_LANG", "en-US")
    asr_env.setenv("ASR_TIMEOUT", "9.5")
    asr_env.setenv("ASR_MAX_DURATION_MS", "45000")
    asr_env.setenv("ASR_MAX_BYTES", "2048")

    engine = build_asr_engine()

    assert engine.enabled is False
    assert engine.model_dir == (
        config_module.REPO_ROOT / "backend/app/data/vosk/custom-model"
    ).resolve()
    assert engine.lang == "en-US"
    assert engine.timeout == 9.5
    assert engine.max_duration_ms == 45000
    assert engine.max_bytes == 2048
    assert engine.available() is False
    assert engine.unavailable_reason() == "disabled"