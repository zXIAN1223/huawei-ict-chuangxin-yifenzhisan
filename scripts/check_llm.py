#!/usr/bin/env python
"""LLM 连通性自检脚本（契约 C-CHECK-LLM@1，plan 2.2.5 / 需求 R5.3）。

用途：在启动服务前确认「配置存在 + 端点可达 + 密钥有效 + 返回可用正文」，
避免出现「配置看起来对、运行时却静默回退本地模拟」的情况。

用法::

    python scripts/check_llm.py
    python scripts/check_llm.py --timeout 10 --max-tokens 16

退出码：

* ``0``：成功（一次最小非流式 chat 调用返回非空正文）
* ``1``：调用失败（网络错误、超时、非 2xx、返回正文为空、配置读取异常）
* ``2``：未配置 LLM（端点或密钥为空，或归一化后的 endpoint 为空）

安全约束（需求 R5.4）：密钥只经 ``AIConfig`` 在内存中流转；本脚本**不打印、
不写入日志、不回显** ``api_key``，异常信息中的密钥字面量与 ``Bearer <token>``
片段一律替换为 ``***``。

与运行时一致性：URL 归一化复用 ``AIConfig.resolve_endpoint()``，与问答服务
实际请求地址完全相同；因此「脚本通过」等价于「运行时大概率可用」。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
import urllib.parse
from urllib.parse import urlsplit, urlunsplit

# 仓库根目录：scripts/check_llm.py -> scripts -> <repo root>
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_CONFIGURED = 2

SYSTEM_PROMPT = "你是连通性测试助手"
USER_PROMPT = "请回复：OK"
NOT_CONFIGURED_MESSAGE = "未配置 LLM（请设置 LLM_BASE_URL / LLM_API_KEY）"

#: 最小调用使用的输出上限（验证连通性足够，可被 --max-tokens 覆盖）。
DEFAULT_MAX_TOKENS = 16
#: 成功摘要中回复的截取长度。
REPLY_PREVIEW_CHARS = 60
#: 失败摘要中错误详情/响应体的截取长度，避免刷屏。
ERROR_DETAIL_CHARS = 300


# ---------------------------------------------------------------------------
# 输出工具（安全、跨平台编码友好）
# ---------------------------------------------------------------------------


def emit(message: str = "") -> None:
    """打印一行文本。

    Windows 控制台可能使用 cp936 编码，遇到无法编码的字符时以 ``?`` 替换，
    而不是抛出 :class:`UnicodeEncodeError` 中断自检流程。
    """
    try:
        print(message)
    except UnicodeEncodeError:  # pragma: no cover - 取决于终端编码
        encoding = sys.stdout.encoding or "utf-8"
        print(message.encode(encoding, "replace").decode(encoding, "replace"))


def scrub_secret(text: Optional[str], api_key: Optional[str]) -> str:
    """从任意文本中移除密钥痕迹，保证错误信息可安全打印。"""
    if not text:
        return ""
    cleaned = str(text)
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
        # 兼容 URL 查询串中出现同一密钥的编码形式
        cleaned = cleaned.replace(urllib.parse.quote(api_key, safe=""), "***")
    # 兜底：任何形如 Bearer <token> 的片段
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{4,}", r"\1***", cleaned)


def mask_endpoint_for_display(endpoint: str) -> str:
    """将 endpoint 脱敏为 ``scheme://host/path``（去掉 userinfo、查询串与片段）。"""
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return "<无法解析的地址>"
    host = parts.hostname or ""
    if host and parts.port:
        host = f"{host}:{parts.port}"
    netloc = host or parts.netloc.split("@")[-1]
    path = parts.path or ""
    masked = urlunsplit((parts.scheme, netloc, path, "", ""))
    return masked or "<空地址>"


# ---------------------------------------------------------------------------
# 运行时配置与适配层（复用 T1 提供的公开符号）
# ---------------------------------------------------------------------------


def load_runtime_modules() -> tuple[Any, Any]:
    """导入 ``config`` 与 ``ai_adapter``，返回 ``(settings, ai_adapter_module)``。

    导入失败（依赖缺失/模块异常）时抛出 :class:`RuntimeError`，由调用方转成
    可读错误信息，而不是让脚本带着堆栈崩溃。
    """
    try:
        from backend.app.config import get_settings
        from backend.app.services import ai_adapter
    except Exception as exc:  # noqa: BLE001 - 任意导入失败都应可读报错
        raise RuntimeError(f"无法导入后端模块：{type(exc).__name__}: {exc}") from exc

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 配置解析失败同样视为环境问题
        raise RuntimeError(f"读取配置失败：{type(exc).__name__}: {exc}") from exc
    return settings, ai_adapter


def build_runtime_ai_config(ai_adapter_module: Any, settings: Any) -> Any:
    """经 ``ai_adapter.build_ai_config()`` 构建运行期 ``AIConfig``。"""
    builder = getattr(ai_adapter_module, "build_ai_config", None)
    if not callable(builder):
        raise RuntimeError("ai_adapter 未提供 build_ai_config()（等待 T1 实现）")
    try:
        return builder(settings)
    except TypeError:
        # 兼容旧签名 build_ai_config() 不接受 settings 参数
        return builder()
    except Exception as exc:  # noqa: BLE001 - 构建失败需可读报错
        raise RuntimeError(f"构建 AIConfig 失败：{type(exc).__name__}: {exc}") from exc


def fallback_resolve_endpoint(raw_url: Optional[str]) -> Optional[str]:
    """``AIConfig.resolve_endpoint()`` 缺席时的等价归一化（plan 2.2.2 规则表）。

    ① 去首尾空白；② 含 ``/chat/completions`` 则原样；③ 去掉结尾 ``/``；
    ④ 追加 ``/chat/completions``（不自动补版本前缀）。
    """
    if raw_url is None:
        return None
    value = str(raw_url).strip()
    if not value:
        return None
    if "/chat/completions" in value:
        return value
    trimmed = value.rstrip("/")
    if not trimmed:
        return None
    return f"{trimmed}/chat/completions"


def resolve_chat_endpoint(ai_config: Any) -> tuple[Optional[str], bool]:
    """返回 ``(endpoint, used_native_resolver)``。

    优先复用 ``AIConfig.resolve_endpoint()``；若 T1 尚未提供该方法，则退回
    本脚本内置的等价归一化，并在输出中提示依赖状态（不中断自检）。
    """
    resolver = getattr(ai_config, "resolve_endpoint", None)
    if callable(resolver):
        return resolver(), True
    return fallback_resolve_endpoint(getattr(ai_config, "api_url", None)), False


def is_llm_enabled(ai_config: Any, api_key: Optional[str]) -> bool:
    """判定是否具备真实远程调用条件（端点与密钥均非空）。"""
    enabled = getattr(ai_config, "enabled", None)
    if enabled is None:
        return bool(getattr(ai_config, "api_url", None)) and bool(api_key)
    return bool(enabled)


def resolve_provider(ai_adapter_module: Any, ai_config: Any) -> str:
    """复用 ``AIAdapter.provider`` 得到与运行时一致的 provider 判定。"""
    adapter_class = getattr(ai_adapter_module, "AIAdapter", None)
    if adapter_class is not None:
        try:
            return str(adapter_class(ai_config).provider)
        except Exception:  # noqa: BLE001 - provider 判定失败不应中断自检
            pass
    return "local"


# ---------------------------------------------------------------------------
# HTTP 调用
# ---------------------------------------------------------------------------


def build_request_body(model: str, max_tokens: int) -> dict[str, Any]:
    """构造最小非流式 chat 请求体（与运行时适配层字段一致）。"""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }


def build_request_headers(api_key: str) -> dict[str, str]:
    """构造请求头（密钥只在此处出现，绝不打印）。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def post_chat_completion(
    endpoint: str,
    api_key: str,
    body: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    """发起一次 POST，返回 ``(status_code, response_text)``。

    优先使用 httpx（与运行时适配层同一客户端库）；不可用时退回标准库
    ``urllib.request``，以保证脚本在最小环境下仍能工作。
    """
    headers = build_request_headers(api_key)
    try:
        import httpx  # noqa: PLC0415 - 可选依赖，运行时按需导入
    except ImportError:
        return _post_with_urllib(endpoint, body, headers, timeout)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(endpoint, json=body, headers=headers)
    return int(response.status_code), response.text


def _post_with_urllib(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str]:
    """标准库兜底实现；HTTP 错误码作为正常返回值，网络错误继续抛出。"""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read().decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响状态码上报
            error_body = ""
        return int(exc.code), error_body


def extract_reply_text(response_text: str) -> str:
    """从 OpenAI 兼容响应中提取正文；无法解析时返回空串。"""
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message")
    content: Any = message.get("content") if isinstance(message, dict) else None
    if not content:
        content = first_choice.get("text")
    if isinstance(content, list):
        # 少数服务商返回分段内容：[{"type": "text", "text": "..."}]
        content = "".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content or "").strip()


def extract_error_message(response_text: str) -> str:
    """尽力从错误响应中提取可读信息（code/message），找不到则截断原文。"""
    if not response_text:
        return "（响应体为空）"
    try:
        payload = json.loads(response_text)
    except (TypeError, ValueError):
        return response_text.strip()[:ERROR_DETAIL_CHARS]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code") or error.get("type") or ""
            message = error.get("message") or ""
            detail = " ".join(str(item) for item in (code, message) if item)
            if detail:
                return detail[:ERROR_DETAIL_CHARS]
        message = payload.get("message")
        if message:
            return str(message)[:ERROR_DETAIL_CHARS]
    return json.dumps(payload, ensure_ascii=False)[:ERROR_DETAIL_CHARS]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_cli_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析命令行参数；数值校验在 :func:`main` 中完成以统一退出码语义。"""
    parser = argparse.ArgumentParser(
        description="LLM 连通性自检：读取运行时配置并发起一次最小非流式 chat 调用。",
        epilog="退出码：0=成功，1=调用失败，2=未配置 LLM。",
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        default=None,
        help="覆盖请求超时（秒，正数）；缺省沿用 LLM_TIMEOUT/PANGU_TIMEOUT",
    )
    parser.add_argument(
        "--max-tokens",
        dest="max_tokens",
        metavar="N",
        default=None,
        help=f"覆盖单次最大生成 token（正整数，缺省 {DEFAULT_MAX_TOKENS}）",
    )
    return parser.parse_args(argv)


def parse_positive_float(raw: Optional[str], default: float, label: str) -> tuple[float, Optional[str]]:
    """解析正浮点参数；返回 ``(value, error_message)``。"""
    if raw is None:
        return default, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default, f"{label} 需要数字，收到：{raw!r}"
    if value <= 0:
        return default, f"{label} 需要正数，收到：{raw!r}"
    return value, None


def parse_positive_int(raw: Optional[str], default: int) -> tuple[int, Optional[str]]:
    """解析正整数参数；返回 ``(value, error_message)``。"""
    if raw is None:
        return default, None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default, f"--max-tokens 需要整数，收到：{raw!r}"
    if value < 1:
        return default, f"--max-tokens 需要 >= 1，收到：{raw!r}"
    return value, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """执行自检并返回进程退出码（0/1/2）。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:  # pragma: no cover - 仅影响终端编码容错
            sys.stdout.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    args = parse_cli_args(argv)

    # -- 步骤 1：加载运行时配置（失败=环境问题，退出码 1）-------------------
    try:
        settings, ai_adapter_module = load_runtime_modules()
        ai_config = build_runtime_ai_config(ai_adapter_module, settings)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        emit(f"自检环境加载失败：{scrub_secret(str(exc), None)}")
        emit("提示：请在仓库根目录运行，并确认已执行 pip install -r requirements.txt。")
        return EXIT_FAILED

    api_key = getattr(ai_config, "api_key", None)
    api_key_text = str(api_key) if api_key else ""
    endpoint, used_native_resolver = resolve_chat_endpoint(ai_config)
    enabled = is_llm_enabled(ai_config, api_key_text or None)

    # -- 步骤 2：未配置判定（退出码 2）--------------------------------------
    if not enabled or not endpoint:
        emit(NOT_CONFIGURED_MESSAGE)
        return EXIT_NOT_CONFIGURED

    # -- 步骤 3：解析可选参数（非法参数=调用失败，退出码 1）------------------
    config_timeout = float(getattr(ai_config, "timeout", 30.0) or 30.0)
    timeout, timeout_error = parse_positive_float(args.timeout, config_timeout, "--timeout")
    max_tokens, max_tokens_error = parse_positive_int(args.max_tokens, DEFAULT_MAX_TOKENS)
    if timeout_error or max_tokens_error:
        emit(timeout_error or max_tokens_error or "")
        return EXIT_FAILED

    model = str(getattr(ai_config, "model", "") or "")
    provider = resolve_provider(ai_adapter_module, ai_config)
    masked_endpoint = mask_endpoint_for_display(endpoint)

    # -- 步骤 4：最小非流式调用 --------------------------------------------
    emit("LLM 连通性自检（C-CHECK-LLM@1）")
    emit(f"  provider : {provider}")
    emit(f"  model    : {model or '（未配置模型名）'}")
    emit(f"  endpoint : {masked_endpoint}")
    emit(f"  timeout  : {timeout:g} 秒")
    emit(f"  max_tokens: {max_tokens}")
    if not used_native_resolver:
        emit("  提示：ai_adapter 尚未提供 AIConfig.resolve_endpoint()（等待 T1），")
        emit("        本次使用与之等价的本地归一化规则，运行时归一化结果需复核。")
    emit("正在发起一次最小非流式 chat 调用 ...")

    body = build_request_body(model, max_tokens)
    started_at = time.perf_counter()
    try:
        status_code, response_text = post_chat_completion(
            endpoint, api_key_text, body, timeout
        )
    except Exception as exc:  # noqa: BLE001 - 网络/超时/协议异常统一上报
        elapsed = time.perf_counter() - started_at
        emit("")
        emit("连通性测试失败")
        emit(f"  异常类型 : {type(exc).__name__}")
        emit(f"  详情     : {scrub_secret(str(exc), api_key_text)[:ERROR_DETAIL_CHARS]}")
        emit("  HTTP 状态码: -")
        emit(f"  endpoint : {masked_endpoint}")
        emit(f"  耗时     : {elapsed:.2f} 秒")
        emit("提示：请检查网络/代理、地址是否可达，以及 LLM_TIMEOUT 是否过小。")
        return EXIT_FAILED

    elapsed = time.perf_counter() - started_at

    # -- 步骤 5：结果判定与摘要 --------------------------------------------
    if status_code < 200 or status_code >= 300:
        emit("")
        emit("连通性测试失败")
        emit("  异常类型 : HTTPStatusError")
        emit(f"  HTTP 状态码: {status_code}")
        emit(f"  详情     : {scrub_secret(extract_error_message(response_text), api_key_text)}")
        emit(f"  endpoint : {masked_endpoint}")
        emit(f"  耗时     : {elapsed:.2f} 秒")
        emit("提示：401/403 多为密钥无效；404 多为地址缺少版本前缀或路径拼写错误。")
        return EXIT_FAILED

    reply_text = extract_reply_text(response_text)
    if not reply_text:
        emit("")
        emit("连通性测试失败")
        emit("  异常类型 : EmptyResponse")
        emit("  HTTP 状态码: 0" if status_code == 0 else f"  HTTP 状态码: {status_code}")
        emit("  详情     : 响应可解析但正文为空（choices[0].message.content 为空）")
        emit(f"  endpoint : {masked_endpoint}")
        emit(f"  耗时     : {elapsed:.2f} 秒")
        emit("提示：确认 LLM_MODEL 是否为服务商支持的模型名。")
        return EXIT_FAILED

    preview = " ".join(reply_text.split())[:REPLY_PREVIEW_CHARS]
    emit("")
    emit("连通性测试通过")
    emit(f"  provider : {provider}")
    emit(f"  model    : {model or '（未配置模型名）'}")
    emit(f"  endpoint : {masked_endpoint}")
    emit(f"  耗时     : {elapsed:.2f} 秒")
    emit(f"  回复(前{REPLY_PREVIEW_CHARS}字): {preview}")
    if provider != "llm":
        emit("  提示：provider 未标记为 \"llm\"，可能服务端尚未完成 provider 值域扩展（T1）。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())