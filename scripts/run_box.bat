@echo off
REM 民国报纸 OCR · 人工框选版 桌面启动器（无 CMD 黑框）
REM 用法：双击本文件即可启动 pywebview 窗体；关闭窗口即退出。
REM 注意：请用 pythonw（不是 python）启动，才不会弹黑框。
REM 若未单独建 venv，把下面 PYTHONW 改成你的 pythonw 完整路径即可。
setlocal
set PYTHONW=pythonw
if exist "%~dp0..\build_venv\Scripts\pythonw.exe" set PYTHONW=%~dp0..\build_venv\Scripts\pythonw.exe
"%PYTHONW%" "%~dp0box_launcher.py" %*
endlocal
