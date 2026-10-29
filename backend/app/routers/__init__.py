"""业务路由包。

各业务模块由后续任务实现，须在模块级暴露 `router: APIRouter`：

- knowledge.py （T4，前缀 /api/knowledge）
- review.py    （T5，前缀 /api/review）
- quiz.py      （T6，前缀 /api/quiz）
- chat.py      （T7，前缀 /api/chat）
- profile.py   （T8，前缀 /api/profile 与 /api/log）

本包不导入任何子模块，以保证渐进式开发时 `main.py` 的容错注册可用。
"""