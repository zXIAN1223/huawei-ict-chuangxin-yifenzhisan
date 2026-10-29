# AI 辅助学习助手

面向高中生物「细胞的结构与功能」「遗传的分子基础」两章的 AI 辅助学习应用。
后端为 FastAPI + SQLite，前端为原生 HTML/CSS/JS（离线优先）。

## 技术栈

- Python 3.11+（本机验证 3.13）
- FastAPI / uvicorn / pydantic v2 / python-multipart
- SQLite（Python 内置 `sqlite3`，手写参数化 DAO）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备配置（可选，缺省即可运行并自动回退本地模拟）
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS

# 3. 启动服务
python run.py
```

启动后：

- 前端页面：<http://127.0.0.1:8000/>
- API 文档：<http://127.0.0.1:8000/docs>
- 静态资源：`/static/**`（映射到仓库 `frontend/`）

## 配置

所有配置均通过环境变量或 `.env` 注入，**源码中不含任何明文密钥**。详见 `.env.example`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ALA_DB_PATH` | `backend/app/data/app.db` | SQLite 数据库路径（相对仓库根解析） |
| `PANGU_API_URL` | 空 | 盘古接口地址，留空则走本地模拟 |
| `PANGU_API_KEY` | 空 | 盘古密钥（切勿提交） |
| `PANGU_MODEL` | `pangu` | 模型名称 |
| `PANGU_TIMEOUT` | `30` | 请求超时（秒） |
| `ALA_HOST` / `ALA_PORT` | `127.0.0.1` / `8000` | 监听地址与端口 |

## 目录结构（后端骨架）

```
backend/app/
├─ config.py           # 环境变量/配置读取
├─ db.py               # SQLite 连接、建表、事务、UTC 时间工具
├─ models.py           # Pydantic 模型 + 全部表的 DDL 常量（唯一来源）
├─ deps.py             # FastAPI 依赖：get_user_id / get_db / get_content_repo
├─ main.py             # 应用组装、静态挂载、路由容错注册、统一错误体
├─ routers/            # 各业务路由（渐进式实现）
├─ services/           # 业务服务（渐进式实现）
└─ data/
   ├─ content_loader.py  # 章节 JSON 加载、schema 校验与索引
   └─ *.json             # 章节内容（UTF-8）
```

## 数据库

启动时自动建表，共 7 张：`review_state`、`quiz_attempt`、`chat_session`、
`chat_message`、`user_profile`、`behavior_log`、`strategy_snapshot`。

统一时间格式为 UTC ISO-8601（`YYYY-MM-DDTHH:MM:SSZ`）。

## 用户身份

前端在 `localStorage['ala_user_id']` 保存 UUID4，请求通过 `X-User-Id` 头传递；
缺失时后端返回 400 `MISSING_USER_ID`。

## 统一错误体

所有错误响应均为 `{"code": <string>, "message": <string>, "detail"?: <object>}`。