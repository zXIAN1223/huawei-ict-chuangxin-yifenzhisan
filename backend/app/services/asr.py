"""离线语音识别引擎封装（契约 ``C-ASR-ENGINE@1``，plan 2.2.9）。

职责边界
--------
本模块只负责**把一段合规 WAV 字节流转成文本**，不做 HTTP、不读上传表单、不写日志。
配置来自 ``backend.app.config.Settings.asr_*``（契约 ``C-ASR-CONFIG@1``，由 T1 提供），
本模块以 :func:`build_asr_engine` 读取，并使用 ``getattr`` 兜底默认值，从而在
T1 尚未提供 ``asr_*`` 字段时仍可工作（前向兼容，不修改 ``config.py``）。

支持的音频规格（**不接受 mp3/ogg/m4a，不引入 ffmpeg**）
-------------------------------------------------------
* 容器：WAV（RIFF/PCM，``wave`` 标准库可解析）
* 采样率：16000 Hz
* 声道：1（单声道）
* 位深：16 bit（采样宽度 2 字节）
* 编码：``comptype == "NONE"``（未压缩 PCM）

异常语义（供 API 层映射为统一错误体）
------------------------------------
======================================  ==============================================
异常                                    触发条件
======================================  ==============================================
:class:`AsrUnavailableError`            ``enabled=False`` / 未安装 vosk / 模型目录缺失
:class:`AsrBadAudioError`               空音频、非 WAV、非 PCM16/16kHz/单声道
:class:`AsrTooLongError`                超过 ``max_bytes`` 或 ``max_duration_ms``
:class:`AsrFailedError`                 模型加载后识别过程异常
======================================  ==============================================

线程模型
--------
* :class:`AsrEngine` 实例可被多个请求共享；
* ``vosk.Model`` 的加载由 ``threading.Lock`` 保护（并发首批请求串行加载，之后直接复用缓存）；
* ``vosk.KaldiRecognizer`` **非线程安全**，每次 :meth:`AsrEngine.transcribe` 内部新建，不共享。

隐私约束
--------
音频字节与识别文本**不写日志**；:class:`AsrEngine` 内部不保留音频副本（识别完即释放）。
"""

from __future__ import annotations

import importlib
import io
import json
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from backend.app import config as config_module

# ---------------------------------------------------------------------------
# 常量（契约固定值）
# ---------------------------------------------------------------------------

#: 提供方/厂商标识（响应体 ``provider`` / ``vendor``）。
ASR_PROVIDER = "vosk"

#: vosk 模块名（懒加载目标）。
VOSK_MODULE_NAME = "vosk"

#: 唯一受支持的音频规格。
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_SAMPLE_WIDTH_BYTES = 2  # 16-bit
EXPECTED_CHANNELS = 1

#: 送入 recognizer 的分块大小（字节）；较小的块可降低长音频的瞬时内存占用。
RECOGNITION_CHUNK_BYTES = 32 * 1024

#: 默认模型目录（相对路径基于仓库根；与 ``C-ASR-CONFIG@1`` 的默认值保持一致）。
DEFAULT_MODEL_DIRNAME = "vosk-model-small-cn-0.22"
DEFAULT_ASR_MODEL_DIR: Path = (
    config_module.REPO_ROOT / "backend" / "app" / "data" / "vosk" / DEFAULT_MODEL_DIRNAME
)

#: ``Settings`` 缺失 ``asr_*`` 字段（T1 未就绪）时的兜底默认值（契约 C-ASR-CONFIG@1）。
DEFAULT_ASR_ENABLED = True
DEFAULT_ASR_LANG = "zh-CN"
DEFAULT_ASR_TIMEOUT_SECONDS = 30.0
DEFAULT_ASR_MAX_DURATION_MS = 60_000
DEFAULT_ASR_MAX_BYTES = 10 * 1024 * 1024

#: 不可用原因 → 面向用户的说明（原因取值即 API ``detail.reason`` 的合法集合）。
UNAVAILABLE_REASONS: dict[str, str] = {
    "disabled": "后端语音识别已通过 ASR_ENABLED=false 关闭",
    "vosk_missing": "未安装 vosk 依赖，请执行 pip install -r requirements.txt",
    "model_missing": "未找到 ASR 模型目录，请先运行 python scripts/download_asr_model.py",
}


# ---------------------------------------------------------------------------
# 数据类型与异常
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsrTranscription:
    """一次识别结果（不可变）。

    :param text: 识别文本；**无语音时为空串**（不是错误）。
    :param lang: 实际使用的识别语言（请求覆盖值优先于配置默认值）。
    :param duration_ms: 音频时长（毫秒），由 WAV 头部的帧数与采样率计算。
    :param model: 模型目录名（仅用于展示/日志，不含密钥）。
    """

    text: str
    lang: str
    duration_ms: int
    model: str


class AsrUnavailableError(RuntimeError):
    """ASR 不可用：被禁用 / 未安装 vosk / 模型目录缺失。

    :param reason: ``"disabled"`` / ``"vosk_missing"`` / ``"model_missing"``，
        直接作为 API 错误体的 ``detail.reason`` 输出，故必须是上述固定集合之一。
    """

    def __init__(self, reason: str = "vosk_missing", message: str | None = None) -> None:
        self.reason = reason if reason in UNAVAILABLE_REASONS else "vosk_missing"
        super().__init__(message or UNAVAILABLE_REASONS[self.reason])


class AsrBadAudioError(ValueError):
    """音频不可用：空字节、非 WAV、非 PCM、非 16kHz、非单声道、非 16-bit。"""


class AsrTooLongError(ValueError):
    """音频超限：超过 ``max_bytes``（体积）或 ``max_duration_ms``（时长）。"""


class AsrFailedError(RuntimeError):
    """识别失败：模型已就绪但识别过程抛异常（或由 API 层标注超时）。

    :param reason: 具体的失败原因，供 API 错误体 ``detail.reason`` 使用
        （如 ``"recognition_error"`` / ``"timeout"``）。
    """

    def __init__(self, reason: str = "recognition_error", message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or "语音识别失败")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _resolve_model_dir(model_dir: Path | str | None) -> Path:
    """把配置中的模型目录解析为绝对路径（相对路径基于仓库根）。

    空值（``None`` / 空串 / 纯空白）回退 :data:`DEFAULT_ASR_MODEL_DIR`。
    """
    if model_dir is None:
        return DEFAULT_ASR_MODEL_DIR
    raw = str(model_dir).strip()
    if not raw:
        return DEFAULT_ASR_MODEL_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_module.REPO_ROOT / path
    return path


def _format_expected_spec() -> str:
    """返回面向用户的受支持音频规格说明（用于错误消息）。"""
    return "WAV / PCM / 16000Hz / 单声道 / 16-bit"


def _coerce_positive_float(value: Any, default: float) -> float:
    """把配置值转为正浮点数；非法或非正值回退 ``default``。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_positive_int(value: Any, default: int) -> int:
    """把配置值转为正整数；非法或非正值回退 ``default``。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class AsrEngine:
    """Vosk 识别引擎：可用性判定 + WAV 校验 + 懒加载识别。

    实例可跨请求共享；模型在**首次** :meth:`transcribe` 时才导入 ``vosk`` 并加载，
    之后复用缓存。构造本对象不会导入 ``vosk``，也不会加载模型。

    :param enabled: 是否允许使用（``False`` 时 :meth:`available` 恒为 ``False``）。
    :param model_dir: 模型目录；``None``/空值回退 :data:`DEFAULT_ASR_MODEL_DIR`。
    :param lang: 默认识别语言（请求可通过 ``lang`` 覆盖）。
    :param timeout: 识别调用超时（秒）。引擎自身不实施超时，由 API 层使用。
    :param max_duration_ms: 单次音频最大时长（毫秒）。
    :param max_bytes: 单次音频最大字节数。
    """

    def __init__(
        self,
        *,
        enabled: bool,
        model_dir: Path | str | None,
        lang: str,
        timeout: float,
        max_duration_ms: int,
        max_bytes: int,
    ) -> None:
        self.enabled = bool(enabled)
        self.model_dir: Path = _resolve_model_dir(model_dir)
        self.lang: str = lang.strip() if isinstance(lang, str) and lang.strip() else DEFAULT_ASR_LANG
        # 构造期即做兜底校验：避免调用方传入 None/非正值时在运行时才暴露 TypeError
        self.timeout: float = _coerce_positive_float(timeout, DEFAULT_ASR_TIMEOUT_SECONDS)
        self.max_duration_ms: int = _coerce_positive_int(
            max_duration_ms, DEFAULT_ASR_MAX_DURATION_MS
        )
        self.max_bytes: int = _coerce_positive_int(max_bytes, DEFAULT_ASR_MAX_BYTES)

        # 模型缓存与加载锁（首次 transcribe 才真正加载）
        self._model: Any | None = None
        self._model_lock = threading.Lock()
        # vosk 模块缓存（导入失败不缓存，便于安装依赖后同一进程内重试）
        self._vosk_module: ModuleType | None = None
        self._vosk_lock = threading.Lock()

    # -- 只读属性 -----------------------------------------------------------

    @property
    def model_name(self) -> str:
        """模型目录名（响应体 ``model`` 字段；不含绝对路径与密钥）。"""
        return self.model_dir.name or DEFAULT_MODEL_DIRNAME

    # -- 可用性 -------------------------------------------------------------

    def _import_vosk(self) -> ModuleType | None:
        """导入 ``vosk``；导入失败返回 ``None``（不抛异常）。

        成功结果会被缓存，避免每次请求重复导入。
        """
        if self._vosk_module is not None:
            return self._vosk_module
        with self._vosk_lock:
            if self._vosk_module is not None:  # 双检：并发首个请求只导入一次
                return self._vosk_module
            try:
                module = importlib.import_module(VOSK_MODULE_NAME)
            except Exception:  # noqa: BLE001 - 依赖缺失/损坏都视为不可用，绝不向上抛
                return None
            self._vosk_module = module
            return module

    def unavailable_reason(self) -> str | None:
        """返回不可用原因；可用时返回 ``None``。

        顺序与 ``available()`` 判定一致：``enabled`` → ``vosk`` 可导入 → 模型目录存在。
        **本方法不抛异常**（任何意外均视为 ``vosk_missing``）。
        """
        try:
            if not self.enabled:
                return "disabled"
            if self._import_vosk() is None:
                return "vosk_missing"
            if not (self.model_dir.is_dir()):
                return "model_missing"
            return None
        except Exception:  # noqa: BLE001 - 契约要求可用性判定绝不抛出
            return "vosk_missing"

    def available(self) -> bool:
        """当且仅当 ``enabled`` 为真、vosk 可导入、模型目录存在且为目录时返回 ``True``。

        **绝不抛异常**（含文件系统权限异常等意外情况）。
        """
        try:
            return self.unavailable_reason() is None
        except Exception:  # noqa: BLE001 - 契约要求 available() 绝不抛出
            return False

    def warmup(self) -> bool:
        """预加载模型（可选）；成功返回 ``True``，不可用/加载失败返回 ``False``。

        该方法不抛异常，适合在应用启动或健康检查中调用以消除首个请求的加载延迟。
        """
        try:
            self._load_model()
            return True
        except Exception:  # noqa: BLE001 - warmup 是尽力而为的预热
            return False

    # -- 模型懒加载（线程安全） ---------------------------------------------

    def _load_model(self) -> Any:
        """加载并缓存 ``vosk.Model``；不可用时抛 :class:`AsrUnavailableError`。

        ``threading.Lock`` 保证并发首批请求只加载一次，后续请求命中缓存直接返回。
        """
        if self._model is not None:
            return self._model

        with self._model_lock:
            if self._model is not None:  # 双检：等待锁期间可能已被其他线程加载
                return self._model

            reason = self.unavailable_reason()
            if reason is not None:
                raise AsrUnavailableError(reason)

            vosk_module = self._vosk_module or self._import_vosk()
            if vosk_module is None:
                raise AsrUnavailableError("vosk_missing")

            try:
                # vosk.Model 参数为模型目录（绝对路径，避免依赖进程 CWD）
                model = vosk_module.Model(str(self.model_dir))
            except Exception as exc:  # noqa: BLE001 - 模型损坏/加载失败统一归类
                raise AsrUnavailableError(
                    "model_missing",
                    f"ASR 模型加载失败（{self.model_dir.name}）：{exc.__class__.__name__}",
                ) from exc

            self._model = model
            return model

    # -- 音频校验 -----------------------------------------------------------

    def _validate_audio(self, audio: bytes) -> tuple[bytes, int]:
        """校验音频并返回 ``(PCM 帧字节, 时长毫秒)``。

        校验顺序（契约 2.2.9 第 1~3 步）：体积 → WAV 格式 → 时长。

        :raises AsrBadAudioError: 空字节 / 非 WAV / 规格不符。
        :raises AsrTooLongError: 超 ``max_bytes`` 或超 ``max_duration_ms``。
        """
        if not audio:
            raise AsrBadAudioError("音频为空：请上传一段 WAV 录音")

        if len(audio) > self.max_bytes:
            raise AsrTooLongError(
                f"音频体积 {len(audio)} 字节超过上限 {self.max_bytes} 字节"
            )

        try:
            with wave.open(io.BytesIO(audio), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                declared_frame_count = wav_file.getnframes()
                compression_type = wav_file.getcomptype()
                frames = wav_file.readframes(declared_frame_count)
        except (wave.Error, EOFError, OSError) as exc:
            raise AsrBadAudioError(
                f"无法解析音频，仅支持 {_format_expected_spec()}"
            ) from exc

        if compression_type != "NONE":
            raise AsrBadAudioError(f"音频必须为未压缩 PCM（当前编码 {compression_type}）")
        if sample_width != EXPECTED_SAMPLE_WIDTH_BYTES:
            raise AsrBadAudioError(
                f"音频位深必须为 16-bit（当前 {sample_width * 8}-bit）"
            )
        if sample_rate != EXPECTED_SAMPLE_RATE:
            raise AsrBadAudioError(f"音频采样率必须为 {EXPECTED_SAMPLE_RATE}Hz（当前 {sample_rate}Hz）")
        if channels != EXPECTED_CHANNELS:
            raise AsrBadAudioError(f"音频必须为单声道（当前 {channels} 声道）")

        # 以**实际读取到的帧数**计算时长：即使 WAV 头部声明的帧数被伪造/截断，
        # 也不会低估时长（保持契约 frames / framerate * 1000 的口径）。
        actual_frame_count = len(frames) // (channels * sample_width)
        if actual_frame_count <= 0:
            raise AsrBadAudioError("音频不含任何采样帧")

        duration_ms = int(round(actual_frame_count * 1000 / sample_rate))
        if duration_ms > self.max_duration_ms:
            raise AsrTooLongError(
                f"音频时长 {duration_ms}ms 超过上限 {self.max_duration_ms}ms"
            )
        return frames, duration_ms

    # -- 识别 ---------------------------------------------------------------

    def _recognize(self, model: Any, frames: bytes) -> str:
        """用新建的 ``KaldiRecognizer`` 识别 PCM 帧，返回拼接后的文本。

        ``KaldiRecognizer`` 非线程安全，故一律**每次调用新建**，不得跨请求共享。

        :raises AsrFailedError: 识别过程任何异常（包含结果解析失败）。
        """
        vosk_module = self._vosk_module
        if vosk_module is None:
            # 防御：正常路径下 _load_model() 已完成导入；走到这里说明状态异常
            raise AsrUnavailableError("vosk_missing")

        try:
            # 显式传入采样率，避免 recognizer 使用模型默认采样率导致解码异常
            recognizer = vosk_module.KaldiRecognizer(model, EXPECTED_SAMPLE_RATE)
            segments: list[str] = []
            for offset in range(0, len(frames), RECOGNITION_CHUNK_BYTES):
                chunk = frames[offset : offset + RECOGNITION_CHUNK_BYTES]
                if recognizer.AcceptWaveform(chunk):
                    segment = json.loads(recognizer.Result()).get("text", "")
                    if segment:
                        segments.append(segment)
            tail = json.loads(recognizer.FinalResult()).get("text", "")
            if tail:
                segments.append(tail)
        except Exception as exc:  # noqa: BLE001 - 识别异常统一映射为 AsrFailedError
            raise AsrFailedError(
                "recognition_error",
                f"语音识别过程异常：{exc.__class__.__name__}",
            ) from exc

        return " ".join(part.strip() for part in segments if part and part.strip())

    def transcribe(self, audio: bytes, *, lang: str | None = None) -> AsrTranscription:
        """识别一段 WAV 音频（同步方法，超时由调用方用 ``asyncio.wait_for`` 实施）。

        :param audio: 完整 WAV 字节流（PCM16 / 16kHz / 单声道）。
        :param lang: 可选的识别语言覆盖值；``None``/空值时使用实例默认语言。
        :returns: :class:`AsrTranscription`；静音/无语音时 ``text`` 为空串（非错误）。

        :raises AsrBadAudioError: 音频为空或规格不符。
        :raises AsrTooLongError: 音频体积/时长超限。
        :raises AsrUnavailableError: 引擎不可用（禁用/依赖缺失/模型目录缺失）。
        :raises AsrFailedError: 识别过程异常。
        """
        frames, duration_ms = self._validate_audio(audio)
        model = self._load_model()
        text = self._recognize(model, frames)

        effective_lang = lang.strip() if isinstance(lang, str) and lang.strip() else self.lang
        return AsrTranscription(
            text=text,
            lang=effective_lang,
            duration_ms=duration_ms,
            model=self.model_name,
        )


def build_asr_engine(settings: Any | None = None) -> AsrEngine:
    """按 ``Settings.asr_*`` 构造 :class:`AsrEngine`（契约 ``C-ASR-CONFIG@1`` 消费点）。

    字段缺失时使用契约默认值（``getattr`` 兜底），确保 T1 尚未交付 ``asr_*`` 字段时
    本模块仍可正常工作；T1 交付后自动生效，本模块**不需要**任何改动。

    :param settings: 配置快照；``None`` 时调用 ``config.get_settings()``。
    """
    if settings is None:
        settings = config_module.get_settings()

    enabled = getattr(settings, "asr_enabled", DEFAULT_ASR_ENABLED)
    model_dir = getattr(settings, "asr_model_dir", None)
    lang = getattr(settings, "asr_lang", DEFAULT_ASR_LANG)
    timeout = getattr(settings, "asr_timeout", DEFAULT_ASR_TIMEOUT_SECONDS)
    max_duration_ms = getattr(settings, "asr_max_duration_ms", DEFAULT_ASR_MAX_DURATION_MS)
    max_bytes = getattr(settings, "asr_max_bytes", DEFAULT_ASR_MAX_BYTES)

    return AsrEngine(
        enabled=bool(enabled),
        model_dir=model_dir,
        lang=str(lang) if lang is not None else DEFAULT_ASR_LANG,
        timeout=_coerce_positive_float(timeout, DEFAULT_ASR_TIMEOUT_SECONDS),
        max_duration_ms=_coerce_positive_int(max_duration_ms, DEFAULT_ASR_MAX_DURATION_MS),
        max_bytes=_coerce_positive_int(max_bytes, DEFAULT_ASR_MAX_BYTES),
    )


__all__ = [
    "ASR_PROVIDER",
    "DEFAULT_ASR_MODEL_DIR",
    "EXPECTED_CHANNELS",
    "EXPECTED_SAMPLE_RATE",
    "EXPECTED_SAMPLE_WIDTH_BYTES",
    "UNAVAILABLE_REASONS",
    "AsrBadAudioError",
    "AsrEngine",
    "AsrFailedError",
    "AsrTooLongError",
    "AsrTranscription",
    "AsrUnavailableError",
    "build_asr_engine",
]