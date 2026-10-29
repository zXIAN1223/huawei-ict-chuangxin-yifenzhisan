"""业务服务包。

各业务模块由后续任务实现：

- spaced_repetition.py （T5）
- ai_adapter.py        （T7）
- learning_strategy.py （T8）

本包不导入任何子模块，以保证渐进式开发时不会因缺失模块而阻塞启动。
"""