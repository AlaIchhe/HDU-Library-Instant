@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   HDU 图书馆自动预约 — 开机自启配置
echo ========================================
echo.

:: 找到 pythonw.exe（无窗口运行）
set PYTHONW=
for %%p in (pythonw python) do (
    where %%p >nul 2>nul
    if not errorlevel 1 (
        set "PYTHONW=%%p"
        goto :found
    )
)

echo [错误] 没有找到 python 或 pythonw。请先安装 Python。
pause
exit /b 1

:found
echo Python: %PYTHONW%
echo 脚本目录: %cd%
echo.

:: 移除旧任务（如果存在）
schtasks /delete /tn "HDU-Library-AutoBook" /f >nul 2>nul

:: 创建新的计划任务：用户登录 30 秒后，无窗口启动 web 服务
schtasks /create ^
  /tn "HDU-Library-AutoBook" ^
  /tr "\"%PYTHONW%\" \"%cd%\web_app.py\"" ^
  /sc onlogon ^
  /delay 0000:30 ^
  /f

if errorlevel 1 (
    echo.
    echo [失败] 创建任务计划失败。请以管理员身份运行此脚本。
) else (
    echo.
    echo ========================================
    echo   配置完成！开机后将自动启动预约服务。
    echo   - 服务会在后台静默运行
    echo   - 浏览器访问 http://127.0.0.1:8765 打开管理界面
    echo   - 运行 remove_autostart.bat 可以移除此配置
    echo ========================================
)

echo.
pause
