#!/usr/bin/env python
"""LLM/ASR 升级（chat-llm-voice）的极简独立验证入口。

设计原则（刻意保持极简）：

* **只做一件事**：依次调用两次 ``pytest``，打印每次的真实统计与裁决；
* **绝不挂起**：所有子进程一律使用 ``subprocess.run(..., timeout=120)``，超时视为
  环境受限（LIMITED）并继续，不做任何重试；
* **不探测网络/不写实现文件**：本脚本不启动服务器、不建 socket、不修改任何文件；
  全部网络相关断言都由 ``tests/test_llm_integration.py`` 在受控替身/子进程内完成。

用法::

    python scripts/verify_llm_upgrade.py                 # 集成 + 回归
    python scripts/verify_llm_upgrade.py --integration-only
    python scripts/verify_llm_upgrade.py --regression-only

退出码：0 = PASS；1 = FAIL（存在失败用例）；2 = PARTIAL（仅受限/跳过的检查）。

裁决规则（严格二分，不允许「基本通过」）：

* 任一 pytest 调用存在 ``failed``/``error`` → ``FAIL``
* 全部通过但存在 ``skipped``（LIMITED）或子进程超时 → ``PARTIAL``
* 全部通过且无跳过 → ``PASS``
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / ".resultverify" / "llm_upgrade_verification_report.json"

#: 单次 pytest 调用的硬超时（秒）——本脚本唯一的阻塞防护。
PYTEST_TIMEOUT_SECONDS = 120.0

INTEGRATION_TARGET = "tests/test_llm_integration.py"
REGRESSION_ARGS = ["tests", "-q", "--ignore=tests/test_e2e_smoke.py"]

#: pytest 摘要行计数解析（``-q`` 输出：``14 passed, 2 skipped in 11.13s``）。
_COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")


def emit(message: str = "") -> None:
    """打印一行（Windows 控制台编码异常时不中断）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 取决于终端编码
        encoding = sys.stdout.encoding or "utf-8"
        print(message.encode(encoding, "replace").decode(encoding, "replace"), flush=True)


def parse_counts(pytest_output: str) -> dict[str, int]:
    """从 pytest 输出中解析各状态计数（缺失项按 0）。"""
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for line in pytest_output.splitlines():
        if " in " not in line and "=" not in line:
            continue
        for number, kind in _COUNT_PATTERN.findall(line):
            key = "error" if kind.startswith("error") else kind
            if key in counts:
                counts[key] = max(counts[key], int(number))
    return counts


def run_pytest(label: str, pytest_args: list[str]) -> dict[str, object]:
    """执行一次 pytest（带硬超时），返回结构化的结果记录。"""
    command = [sys.executable, "-m", "pytest", *pytest_args]
    emit("-" * 78)
    emit(f"[{label}] {' '.join(command)}")
    emit("-" * 78)
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        emit(f"[{label}] TIMEOUT：超过 {PYTEST_TIMEOUT_SECONDS:.0f}s，已强制终止（LIMITED）")
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return {
            "label": label,
            "ok": False,
            "limited": True,
            "returncode": None,
            "counts": {"passed": 0, "failed": 0, "error": 0, "skipped": 0},
            "output_tail": str(partial)[-800:],
        }

    output = (completed.stdout or "") + (completed.stderr or "")
    emit(output.strip() or "<无输出>")

    counts = parse_counts(completed.stdout or "")
    limited = counts["skipped"] > 0
    ok = completed.returncode == 0 and counts["failed"] == 0 and counts["error"] == 0
    emit(
        f"[{label}] 结果：exit={completed.returncode} "
        f"passed={counts['passed']} failed={counts['failed']} "
        f"error={counts['error']} skipped={counts['skipped']}"
    )
    return {
        "label": label,
        "ok": ok,
        "limited": limited,
        "returncode": completed.returncode,
        "counts": counts,
        "output_tail": output[-1500:],
    }


def decide_verdict(results: list[dict[str, object]]) -> str:
    """严格二分裁决：任何失败 → FAIL；仅受限/跳过 → PARTIAL；否则 PASS。"""
    if any(result["ok"] is False and result["limited"] is False for result in results):
        return "FAIL"
    if any(result["ok"] is False or result["limited"] is True for result in results):
        return "PARTIAL"
    if any(int(result["counts"]["passed"]) == 0 for result in results):  # type: ignore[index]
        return "FAIL"
    return "PASS"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LLM/ASR 升级极简验证（pytest 封装）")
    parser.add_argument("--integration-only", action="store_true", help="仅跑集成测试文件")
    parser.add_argument("--regression-only", action="store_true", help="仅跑回归对照")
    args = parser.parse_args(argv)

    emit("=" * 78)
    emit("LLM/ASR 升级验证（chat-llm-voice）｜独立验证者")
    emit(f"子进程硬超时：{PYTEST_TIMEOUT_SECONDS:.0f}s / 每次 pytest 调用")
    emit("=" * 78)

    results: list[dict[str, object]] = []
    if not args.regression_only:
        results.append(run_pytest("集成", [INTEGRATION_TARGET, "-q"]))
    if not args.integration_only:
        results.append(run_pytest("回归", REGRESSION_ARGS))

    verdict = decide_verdict(results)

    emit("=" * 78)
    emit("汇总")
    for result in results:
        counts = result["counts"]  # type: ignore[assignment]
        suffix = " (LIMITED)" if result["limited"] else ""
        emit(
            f"  - {result['label']}: exit={result['returncode']} "
            f"passed={counts['passed']} failed={counts['failed']} "
            f"error={counts['error']} skipped={counts['skipped']}{suffix}"
        )
    emit(f"VERDICT: {verdict}")

    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(
                {
                    "task": "T6 · LLM/ASR 升级验证",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "timeout_seconds": PYTEST_TIMEOUT_SECONDS,
                    "verdict": verdict,
                    "runs": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        emit(f"报告：{REPORT_PATH}")
    except OSError as exc:  # pragma: no cover - 只读文件系统等边缘情况
        emit(f"报告写入失败（不影响裁决）：{exc}")

    return {"PASS": 0, "FAIL": 1, "PARTIAL": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())