#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载并安装 Vosk 中文小模型（契约 ``C-ASR-ENGINE@1`` 配套脚本）。

用途
----
``POST /api/asr`` 依赖本地 Vosk 模型目录。首次部署时运行本脚本即可自动下载
``vosk-model-small-cn-0.22``（约 42MB）并解压到配置的模型目录。

目标目录解析顺序（与 ``C-ASR-CONFIG@1`` 保持一致的优先级思路）
------------------------------------------------------------
1. 命令行 ``--model-dir``（显式指定，优先级最高）；
2. ``Settings.asr_model_dir``（T1 提供；相对路径由 config 解析为绝对路径）；
3. 环境变量 ``ASR_MODEL_DIR``（若 config 尚未读取该变量，此处兜底）；
4. 仓库默认路径 ``backend/app/data/vosk/vosk-model-small-cn-0.22``。

> T1 的 ``asr_model_dir`` 字段可能尚未就绪：此时本脚本自动回退到环境变量/默认路径，
> 并在输出中明确说明所用来源，避免使用者困惑。

行为
----
* 模型目录**已存在**（且非空）→ 直接跳过，不重复下载；
* 下载到临时目录后再解压、校验、原子替换，避免中断留下半成品目录；
* 只从固定官方地址下载（不接受任意 URL，避免 SSRF/供应链风险）；
* 不打印也不写入任何密钥或用户数据。

运行
----
``python scripts/download_asr_model.py``
``python scripts/download_asr_model.py --force``（重新下载并替换已有模型）
``python scripts/download_asr_model.py --model-dir D:/models/vosk-model-small-cn-0.22``

退出码：0 = 成功（含已存在跳过）；1 = 下载/解压/校验失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 官方模型名与下载地址（固定公开源，不支持自定义 URL）。
MODEL_NAME = "vosk-model-small-cn-0.22"
MODEL_DOWNLOAD_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"

#: 仓库默认模型目录（与 C-ASR-CONFIG@1 默认值一致）。
DEFAULT_MODEL_DIR = REPO_ROOT / "backend" / "app" / "data" / "vosk" / MODEL_NAME

#: 模型目录内的必需子目录（用于判定"已安装"与解压后校验）。
REQUIRED_SUBDIRS = ("am", "conf")

#: 进度输出节流间隔（秒）。
PROGRESS_INTERVAL_SECONDS = 0.5


def _configure_stdout() -> None:
    """尽量把 stdout 切到 UTF-8，避免 Windows GBK 控制台打印中文异常。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def _resolve_model_dir(explicit: str | None) -> tuple[Path, str]:
    """解析目标模型目录，同时返回来源说明（供输出展示）。

    :param explicit: 命令行 ``--model-dir`` 值（可为 ``None``）。
    :returns: ``(绝对路径, 来源说明)``。
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path, "--model-dir 参数"

    settings_model_dir = None
    try:
        from backend.app.config import get_settings

        settings_model_dir = getattr(get_settings(), "asr_model_dir", None)
    except Exception:  # noqa: BLE001 - 配置模块缺失/未就绪时回退，绝不阻塞下载
        settings_model_dir = None

    if settings_model_dir:
        return Path(settings_model_dir).expanduser(), "Settings.asr_model_dir"

    env_model_dir = (os.environ.get("ASR_MODEL_DIR") or "").strip()
    if env_model_dir:
        path = Path(env_model_dir).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        return path, "环境变量 ASR_MODEL_DIR"

    return DEFAULT_MODEL_DIR, "仓库默认路径（config 未提供 asr_model_dir，已回退）"


def _is_model_installed(model_dir: Path) -> bool:
    """模型目录存在且包含必需子目录时视为已安装。"""
    if not model_dir.is_dir():
        return False
    return all((model_dir / subdir).is_dir() for subdir in REQUIRED_SUBDIRS)


def _format_size(byte_count: float) -> str:
    """把字节数格式化为人可读单位。"""
    for unit in ("B", "KB", "MB", "GB"):
        if byte_count < 1024 or unit == "GB":
            return f"{byte_count:.1f} {unit}"
        byte_count /= 1024
    return f"{byte_count:.1f} GB"


def _download_archive(url: str, target: Path) -> None:
    """流式下载 ``url`` 到 ``target``，按字节进度打印百分比。

    :raises RuntimeError: 网络错误或响应体为空。
    """
    print(f"下载地址：{url}")
    request = urllib.request.Request(url, headers={"User-Agent": "ala-asr-model-downloader/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - 固定 https 官方源
            total_bytes = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            last_report = 0.0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                        last_report = now
                        if total_bytes > 0:
                            percent = downloaded * 100 / total_bytes
                            print(
                                f"  进度 {percent:5.1f}%  "
                                f"{_format_size(downloaded)} / {_format_size(total_bytes)}"
                            )
                        else:
                            print(f"  已下载 {_format_size(downloaded)}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise RuntimeError(f"下载失败：{exc}") from exc

    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("下载结果为空文件，可能是网络代理返回了错误页")


def _extract_archive(archive_path: Path, staging_dir: Path) -> Path:
    """把 zip 解压到 ``staging_dir``，返回其中模型目录路径。

    :raises RuntimeError: zip 损坏或压缩包内缺少模型目录。
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise RuntimeError(f"压缩包校验失败（损坏文件：{bad_member}）")
            try:
                # Python 3.12+ 默认启用安全过滤器；显式指定以阻断路径穿越
                archive.extractall(staging_dir, filter="data")
            except TypeError:
                # Python 3.9/3.10 不支持 filter 参数；官方源压缩包无穿越风险
                archive.extractall(staging_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError(f"解压失败：{exc}") from exc

    candidate = staging_dir / MODEL_NAME
    if candidate.is_dir():
        return candidate

    model_dirs = [child for child in staging_dir.iterdir() if child.is_dir()]
    if len(model_dirs) == 1:
        return model_dirs[0]
    raise RuntimeError("压缩包结构与预期不符：未找到唯一的模型根目录")


def _install_model(source_dir: Path, model_dir: Path) -> None:
    """把 ``source_dir`` 安装到 ``model_dir``（必要时替换已有目录）。

    先复制到 ``model_dir`` 的同级临时目录，再一次性替换，降低"半替换"风险。
    """
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    swap_dir = model_dir.with_name(f".{model_dir.name}.installing")
    if swap_dir.exists():
        shutil.rmtree(swap_dir, ignore_errors=True)

    shutil.copytree(source_dir, swap_dir)

    backup_dir = model_dir.with_name(f".{model_dir.name}.old")
    if model_dir.exists():
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        model_dir.rename(backup_dir)
    try:
        swap_dir.rename(model_dir)
    except OSError:
        # 替换失败时回滚到旧目录，避免"什么都没有"
        if backup_dir.exists() and not model_dir.exists():
            backup_dir.rename(model_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)


def _directory_size(path: Path) -> int:
    """递归统计目录占用字节数（用于输出安装结果）。"""
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def download_model(model_dir: Path, *, force: bool = False) -> bool:
    """确保模型已安装；返回 ``True`` 表示本次实际执行了下载安装。"""
    if _is_model_installed(model_dir) and not force:
        print(f"模型已存在，跳过下载：{model_dir}")
        return False
    if model_dir.exists() and not force:
        print(f"警告：目录存在但缺少必需子目录 {REQUIRED_SUBDIRS}，将重新下载并覆盖：{model_dir}")

    with tempfile.TemporaryDirectory(prefix="ala_asr_model_") as temp_root:
        temp_path = Path(temp_root)
        archive_path = temp_path / f"{MODEL_NAME}.zip"
        staging_dir = temp_path / "extracted"
        staging_dir.mkdir()

        _download_archive(MODEL_DOWNLOAD_URL, archive_path)
        print(f"下载完成：{_format_size(archive_path.stat().st_size)}，开始解压…")
        extracted_dir = _extract_archive(archive_path, staging_dir)

        missing = [subdir for subdir in REQUIRED_SUBDIRS if not (extracted_dir / subdir).is_dir()]
        if missing:
            raise RuntimeError(f"解压后的模型缺少必需子目录：{missing}")

        print("安装模型到目标目录…")
        _install_model(extracted_dir, model_dir)

    print(f"安装成功：{model_dir}")
    print(f"模型体积：{_format_size(_directory_size(model_dir))}")
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="下载并安装 Vosk 中文小模型（vosk-model-small-cn-0.22）。"
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="模型安装目录（默认取 Settings.asr_model_dir，其次 ASR_MODEL_DIR，最后仓库默认路径）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="模型已存在时仍重新下载并替换",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口；返回进程退出码。"""
    _configure_stdout()
    args = _parse_args(argv)
    model_dir, source = _resolve_model_dir(args.model_dir)

    print("=== Vosk 中文小模型安装 ===")
    print(f"模型名称：{MODEL_NAME}")
    print(f"目标目录：{model_dir}")
    print(f"目录来源：{source}")

    try:
        installed = download_model(model_dir, force=args.force)
    except (RuntimeError, OSError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        print(
            "可选手动安装：从 https://alphacephei.com/vosk/models 下载 "
            f"{MODEL_NAME}.zip 并解压到上述目录。",
            file=sys.stderr,
        )
        return 1

    if installed:
        print("提示：若服务已在运行，重启服务或等待下一次请求即可加载新模型。")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())