"""应用启动入口：构建 app 后以 uvicorn 启动。

启动顺序（见 plan 2.3.1）：
    create_app() -> init_db()（建表） -> ContentRepository.load_all()（校验内容）
    -> 注册路由 -> 挂载静态 -> uvicorn 启动

其中 `import backend.app.main` 已在模块级完成 create_app()，故此处直接复用 `app`。
"""

from __future__ import annotations

import logging
import os

import uvicorn

from backend.app.config import get_settings
from backend.app.main import app


def main() -> None:
    """解析运行参数并启动 HTTP 服务。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    host = os.environ.get("ALA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALA_PORT", "8000"))

    print(f"数据库文件: {settings.db_path}")
    print(f"内容目录  : {settings.data_dir}")
    print(f"访问地址  : http://{host}:{port}/  （API 文档: http://{host}:{port}/docs）")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()