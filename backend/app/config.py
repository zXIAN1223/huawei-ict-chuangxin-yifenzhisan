"""应用配置读取。

配置来源优先级：进程环境变量 > 仓库根 `.env` 文件 > 内置默认值。

LLM 配置（契约 `C-LLM-CONFIG@1`，plan 2.2.1）：
- 新键 `LLM_*` 优先；未设置（或空白）时回退到兼容别名 `PANGU_*`；仍为空则用默认值。
- 解析完成后把有效值**回填**到 `pangu_*` 字段，使 `routers/quiz.py` 与
  `build_ai_config()` 零改动即获得有效配置（R6.3）。

ASR 配置（契约 `C-ASR-CONFIG@1`，plan 2.2.1 子节）：均为 `ASR_*` 独立键，
带默认值；`asr_model_dir` 的相对路径基于**仓库根**解析为绝对路径。

安全约束（R5.4）：源码中不得出现任何明文密钥；密钥仅经由环境变量或 `.env` 注入，
并通过本模块集中读取，不打印、不写入日志。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

# 仓库根目录：backend/app/config.py -> app -> backend -> <repo root>
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH: Path = REPO_ROOT / "backend" / "app" / "data" / "app.db"
DATA_DIR: Path = REPO_ROOT / "backend" / "app" / "data"
FRONTEND_DIR: Path = REPO_ROOT / "frontend"
ENV_FILE: Path = REPO_ROOT / ".env"

DEFAULT_PANGU_MODEL = "pangu"
DEFAULT_PANGU_TIMEOUT = 30.0

#: LLM 默认值（契约 `C-LLM-CONFIG@1`）。模型名沿用 `pangu` 作占位，
#: 用户须在环境变量/`.env` 中显式填写真实模型名。
DEFAULT_LLM_MODEL = DEFAULT_PANGU_MODEL
DEFAULT_LLM_TIMEOUT = DEFAULT_PANGU_TIMEOUT
DEFAULT_LLM_TEMPERATURE = 0.7
DEFAULT_LLM_MAX_TOKENS = 1024

#: ASR 默认值（契约 `C-ASR-CONFIG@1`）。默认目录为离线中文小模型，
#: 由 `scripts/download_asr_model.py` 下载，且被 `.gitignore` 忽略。
DEFAULT_ASR_ENABLED = True
DEFAULT_ASR_MODEL_DIR: Path = DATA_DIR / "vosk" / "vosk-model-small-cn-0.22"
DEFAULT_ASR_LANG = "zh-CN"
DEFAULT_ASR_TIMEOUT = 30.0
DEFAULT_ASR_MAX_DURATION_MS = 60000
DEFAULT_ASR_MAX_BYTES = 10 * 1024 * 1024

#: `ASR_ENABLED` 真值/假值字面量（大小写不敏感）；其它取值回退默认。
_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


def _parse_dotenv(path: Path) -> dict[str, str]:
    """解析极简 `.env` 文件。

    支持：`KEY=VALUE`、`#` 注释、可选的 `export` 前缀、可选的单/双引号包裹。
    非法行将被忽略；文件不存在时返回空字典。不覆盖已存在的进程环境变量
    （由调用方决定优先级）。
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    """运行时配置快照。

    `pangu_*` 为既有字段，语义已变为「LLM 有效值」（见 :func:`get_settings` 的
    向后兼容填充）；新增字段均带默认值，故既有构造方式仍然有效。
    """

    db_path: Path
    data_dir: Path
    frontend_dir: Path
    pangu_api_url: str | None
    pangu_api_key: str | None
    pangu_model: str
    pangu_timeout: float

    # -- LLM（C-LLM-CONFIG@1） ------------------------------------------------
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_timeout: float = DEFAULT_LLM_TIMEOUT
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS

    # -- ASR（C-ASR-CONFIG@1） ------------------------------------------------
    asr_enabled: bool = DEFAULT_ASR_ENABLED
    asr_model_dir: Path = DEFAULT_ASR_MODEL_DIR
    asr_lang: str = DEFAULT_ASR_LANG
    asr_timeout: float = DEFAULT_ASR_TIMEOUT
    asr_max_duration_ms: int = DEFAULT_ASR_MAX_DURATION_MS
    asr_max_bytes: int = DEFAULT_ASR_MAX_BYTES

    @property
    def pangu_enabled(self) -> bool:
        """当且仅当有效 LLM 地址与密钥均非空（既有语义：启用真实远程调用）。"""
        return bool(self.pangu_api_url and self.pangu_api_key)

    @property
    def llm_enabled(self) -> bool:
        """当且仅当 `llm_base_url` 与 `llm_api_key` 均非空时启用真实远程调用。"""
        return bool(self.llm_base_url and self.llm_api_key)


def _resolve_db_path(raw: str) -> Path:
    """将配置中的库路径解析为绝对路径（相对路径基于仓库根）。"""
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _resolve_dir_path(raw: str | None, default: Path) -> Path:
    """将目录配置解析为绝对路径（相对路径基于仓库根）。"""
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _first_non_blank(read, names: tuple[str, ...]) -> str | None:
    """按优先级取第一个非空白配置值；全部缺失/空白时返回 ``None``。"""
    for name in names:
        raw = read(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _parse_float(raw: str | None, default: float) -> float:
    """解析浮点配置；缺失、非法或非有限数值时回退默认值。"""
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def _parse_temperature(raw: str | None, default: float = DEFAULT_LLM_TEMPERATURE) -> float:
    """解析采样温度并 clamp 至 ``[0.0, 2.0]``（非法值回退默认）。"""
    value = _parse_float(raw, default)
    return max(0.0, min(2.0, value))


def _parse_int(raw: str | None, default: int, *, minimum: int) -> int:
    """解析整数配置；缺失、非法或小于 ``minimum`` 时回退默认值。"""
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _parse_bool(raw: str | None, default: bool) -> bool:
    """真值解析：`1/true/yes/on` 为真，`0/false/no/off` 为假，其它回退默认。"""
    if raw is None:
        return default
    key = str(raw).strip().lower()
    if key in _TRUE_LITERALS:
        return True
    if key in _FALSE_LITERALS:
        return False
    return default


def get_settings() -> Settings:
    """读取并返回当前配置。

    不做缓存：以便测试通过设置环境变量（如 `ALA_DB_PATH`）即可切换数据库，
    避免运行库被污染（AC-R8-3）。
    """
    file_env = _parse_dotenv(ENV_FILE)

    def read(name: str, default: str | None = None) -> str | None:
        if name in os.environ:
            return os.environ[name]
        return file_env.get(name, default)

    # -- LLM：LLM_* 优先，空白视为缺失并回退 PANGU_* -------------------------
    llm_base_url = _first_non_blank(read, ("LLM_BASE_URL", "PANGU_API_URL"))
    llm_api_key = _first_non_blank(read, ("LLM_API_KEY", "PANGU_API_KEY"))
    llm_model = _first_non_blank(read, ("LLM_MODEL", "PANGU_MODEL")) or DEFAULT_LLM_MODEL
    llm_timeout = _parse_float(
        _first_non_blank(read, ("LLM_TIMEOUT", "PANGU_TIMEOUT")), DEFAULT_LLM_TIMEOUT
    )
    llm_temperature = _parse_temperature(
        _first_non_blank(read, ("LLM_TEMPERATURE",)), DEFAULT_LLM_TEMPERATURE
    )
    llm_max_tokens = _parse_int(
        _first_non_blank(read, ("LLM_MAX_TOKENS",)), DEFAULT_LLM_MAX_TOKENS, minimum=1
    )

    # -- ASR（C-ASR-CONFIG@1） ----------------------------------------------
    asr_enabled = _parse_bool(read("ASR_ENABLED"), DEFAULT_ASR_ENABLED)
    asr_model_dir = _resolve_dir_path(
        _first_non_blank(read, ("ASR_MODEL_DIR",)), DEFAULT_ASR_MODEL_DIR
    )
    asr_lang = _first_non_blank(read, ("ASR_LANG",)) or DEFAULT_ASR_LANG
    asr_timeout = _parse_float(_first_non_blank(read, ("ASR_TIMEOUT",)), DEFAULT_ASR_TIMEOUT)
    asr_max_duration_ms = _parse_int(
        _first_non_blank(read, ("ASR_MAX_DURATION_MS",)),
        DEFAULT_ASR_MAX_DURATION_MS,
        minimum=1000,
    )
    asr_max_bytes = _parse_int(
        _first_non_blank(read, ("ASR_MAX_BYTES",)), DEFAULT_ASR_MAX_BYTES, minimum=1024
    )

    db_raw = read("ALA_DB_PATH", str(DEFAULT_DB_PATH)) or str(DEFAULT_DB_PATH)

    return Settings(
        db_path=_resolve_db_path(db_raw),
        data_dir=DATA_DIR,
        frontend_dir=FRONTEND_DIR,
        # 向后兼容填充：pangu_* := LLM 有效值（供 quiz.py / build_ai_config 零改动受益）
        pangu_api_url=llm_base_url,
        pangu_api_key=llm_api_key,
        pangu_model=llm_model,
        pangu_timeout=llm_timeout,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_timeout=llm_timeout,
        llm_temperature=llm_temperature,
        llm_max_tokens=llm_max_tokens,
        asr_enabled=asr_enabled,
        asr_model_dir=asr_model_dir,
        asr_lang=asr_lang,
        asr_timeout=asr_timeout,
        asr_max_duration_ms=asr_max_duration_ms,
        asr_max_bytes=asr_max_bytes,
    )