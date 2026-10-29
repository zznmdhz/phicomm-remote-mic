@echo off
rem 启动斐讯遥控器麦克风（双击运行；报错时窗口会停住显示信息）
cd /d "%~dp0"
setlocal

set "SCRIPT=%~dp0remote_mic.py"

rem 1) 优先用 app 目录下的便携 Python（如有）
if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" "%SCRIPT%" %*
    goto :done
)

rem 2) 其次用 PATH 里的 python / py 启动器
where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%" %*
    goto :done
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%" %*
    goto :done
)

echo.
echo [错误] 找不到 Python。请安装 Python 3.11+ 并加入 PATH，
echo        或把便携 Python 放到本目录的 python 文件夹下。
echo.
pause
exit /b 1

:done
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出（错误码 %errorlevel%）。请检查：
    echo        1. app\deps 依赖目录存在，或已 pip install -r requirements.txt
    echo        2. FFmpeg 已加入 PATH
    echo        3. 已安装 VB-CABLE 虚拟声卡
    echo.
    pause
)
endlocal
