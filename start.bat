@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   AI 辅助学习助手 - 一键启动
echo   项目目录: %cd%
echo ============================================================
echo.

echo [1/3] 检查并安装依赖 (首次较慢, 之后很快)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。请确认已安装 Python 3.10+ 且网络可用。
    echo        可尝试更换 pip 源: python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo.

echo [2/3] 准备打开浏览器...
start "" http://127.0.0.1:8000/
echo     若浏览器未自动打开, 请手动访问: http://127.0.0.1:8000/
echo.

echo [3/3] 启动服务中... (保持本窗口开启; 关闭本窗口或按 Ctrl+C 即停止)
echo ------------------------------------------------------------
python run.py

echo.
echo ------------------------------------------------------------
echo 服务已退出。若上方出现红色报错，请截图该报错内容后再反馈。
echo.
pause