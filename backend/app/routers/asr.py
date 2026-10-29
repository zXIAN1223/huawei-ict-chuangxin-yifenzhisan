"""语音识别 API（契约 ``C-ASR-API@1``，plan 2.2.10）。

``POST /api/asr`` —— ``multipart/form-data`` 上传 WAV（PCM16 / 16kHz / 单声道），
返回离线识别文本。前端录音主通道使用该接口；不可用时前端降级到浏览器原生识别。

请求
----
========================  ========  ===========================================
字段 / 请求头              必填      说明
========================  ========  ===========================================
``X-User-Id`` 请求头       是       用户身份（复用 ``deps.get_user_id``）
``audio``（file）          是       ``audio/wav``，PCM16/16kHz/单声道，≤ ASR_MAX_BYTES
``lang``（string）         否       覆盖 ``ASR_LANG`` 默认识别语言
========================  ========  ===========================================

响应 ``200``
-------------
``{"text","provider":"vosk","vendor":"vosk","lang","duration_ms","model"}``

错误（统一错误体 ``{code,message,detail?}``，经 :class:`~backend.app.deps.ApiError`）
----------------------------------------------------------------------------------
======  ==================  =======================================================
状态     code                触发
======  ==================  =======================================================
400     ``MISSING_USER_ID`` 缺 ``X-User-Id``（既有依赖）
400     ``ASR_BAD_AUDIO``   缺 ``audio`` / 空字节 / 非 WAV / 规格不符
413     ``ASR_TOO_LONG``    超 ``ASR_MAX_BYTES`` 或 ``ASR_MAX_DURATION_MS``
503     ``ASR_UNAVAILABLE`` ``available()==False``（reason: disabled/vosk_missing/model_missing）
500     ``ASR_FAILED``      识别异常或超时（reason 如 ``timeout``/``recognition_error``）
======  ==================  =======================================================

实现要点
--------
* 识别是**阻塞 CPU 操作**：用 ``asyncio.to_thread`` 放到线程池执行，并用
  ``asyncio.wait_for(..., timeout=engine.timeout)`` 施加超时，避免阻塞事件循环。
* 超时后底层线程无法被强制终止（Python 线程语义），但请求立即以 500 返回，
  防止前端长时间挂起。
* 隐私：**不记录音频字节或识别文本**到日志，仅记录非内容性元数据（可选）。
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, File, Form, UploadFile

from backend.app.deps import ApiError, get_user_id
from backend.app.services.asr import (
    ASR_PROVIDER,
    UNAVAILABLE_REASONS,
    AsrBadAudioError,
    AsrEngine,
    AsrFailedError,
    AsrTooLongError,
    AsrUnavailableError,
    build_asr_engine,
)

router = APIRouter(prefix="/api/asr", tags=["asr"])

#: 进程级引擎缓存：模型加载成本高（数十 MB），按进程复用同一实例。
_ENGINE_LOCK = threading.Lock()
_ENGINE_INSTANCE: AsrEngine | None = None


def get_asr_engine() -> AsrEngine:
    """返回进程级 :class:`AsrEngine` 实例（首次调用时按当前配置构造）。"""
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        if _ENGINE_INSTANCE is None:
            _ENGINE_INSTANCE = build_asr_engine()
        return _ENGINE_INSTANCE


def reset_asr_engine() -> None:
    """清空引擎缓存（供测试或配置变更后重新加载；下一次请求重建实例）。"""
    global _ENGINE_INSTANCE
    with _ENGINE_LOCK:
        _ENGINE_INSTANCE = None


def _resolve_effective_lang(raw_lang: str | None) -> str | None:
    """把可选的 ``lang`` 表单字段整理为覆盖值（空白视为未提供）。"""
    if raw_lang is None:
        return None
    cleaned = raw_lang.strip()
    return cleaned or None


@router.post("", response_model=None, summary="离线语音识别（WAV 上传）")
async def post_asr(
    audio: UploadFile | None = File(default=None, description="WAV 音频（PCM16/16kHz/单声道）"),
    lang: str | None = Form(default=None, description="识别语言，覆盖 ASR_LANG"),
    user_id: str = Depends(get_user_id),
    engine: AsrEngine = Depends(get_asr_engine),
) -> dict[str, object]:
    """识别上传的 WAV 音频，返回文本与非内容性元数据。

    :param audio: 上传的 ``audio/wav`` 文件；缺失按 ``ASR_BAD_AUDIO`` 处理（而非 422）。
    :param lang: 识别语言覆盖值。
    :param user_id: 由 ``X-User-Id`` 注入（当前不落库，仅用于身份约束与统一鉴权）。
    :param engine: 进程级 ASR 引擎。
    """
    del user_id  # 身份校验已完成；识别结果当前不落库

    if audio is None:
        raise ApiError(400, "ASR_BAD_AUDIO", "缺少 audio 文件字段")

    # 读取上限 +1 字节：既能判定超限，又不会把超大文件全部读入内存
    try:
        payload = await audio.read(engine.max_bytes + 1)
    except Exception as exc:  # noqa: BLE001 - 上传流中断等 IO 异常统一按坏音频处理
        raise ApiError(400, "ASR_BAD_AUDIO", "无法读取上传的音频") from exc

    effective_lang = _resolve_effective_lang(lang)

    try:
        transcription = await asyncio.wait_for(
            asyncio.to_thread(engine.transcribe, payload, lang=effective_lang),
            timeout=engine.timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        # asyncio.wait_for 超时（3.11+ 内置 TimeoutError 与 asyncio.TimeoutError 为同一类型）
        raise ApiError(
            500,
            "ASR_FAILED",
            "语音识别超时，请缩短录音后重试",
            {"reason": "timeout"},
        ) from None
    except AsrUnavailableError as exc:
        raise ApiError(
            503,
            "ASR_UNAVAILABLE",
            UNAVAILABLE_REASONS.get(exc.reason, "语音识别不可用"),
            {"reason": exc.reason},
        ) from exc
    except AsrTooLongError as exc:
        raise ApiError(413, "ASR_TOO_LONG", str(exc) or "音频过长") from exc
    except AsrBadAudioError as exc:
        raise ApiError(400, "ASR_BAD_AUDIO", str(exc) or "音频格式不合法") from exc
    except AsrFailedError as exc:
        raise ApiError(
            500,
            "ASR_FAILED",
            "语音识别失败，请重试",
            {"reason": exc.reason},
        ) from exc

    return {
        "text": transcription.text,
        "provider": ASR_PROVIDER,
        "vendor": ASR_PROVIDER,
        "lang": transcription.lang,
        "duration_ms": transcription.duration_ms,
        "model": transcription.model,
    }


__all__ = ["router", "get_asr_engine", "reset_asr_engine"]