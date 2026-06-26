@echo off
chcp 65001 >nul

echo 正在移除 HDU 图书馆自动预约开机自启...

schtasks /delete /tn "HDU-Library-AutoBook" /f

if errorlevel 1 (
    echo [失败] 移除失败，或任务不存在。
) else (
    echo 已成功移除开机自启配置。
)

pause
