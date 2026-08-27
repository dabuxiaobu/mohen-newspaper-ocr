@echo off
chcp 65001 >nul
REM ============================================================
REM  manual-box-newspaper-ocr 纯净 exe 打包脚本
REM  用法：在 skill 根目录双击本文件
REM  产物：dist\墨痕\（纯净版，不含密钥/图片/运行产物）
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SKILL_DIR=%CD%"
set "VENV=%SKILL_DIR%\build_venv"
set "SPEC=%SKILL_DIR%\box_tool.spec"

echo WorkDir: %SKILL_DIR%

if not exist "scripts" (
    echo [ERR] scripts folder missing, run this bat in skill root.
    echo Current: %SKILL_DIR%
    pause
    exit /b 1
)
if not exist "%SPEC%" (
    echo [ERR] box_tool.spec missing, run this bat in skill root.
    pause
    exit /b 1
)

echo [0/5] kill leftover pythonw （this tool's bg process may lock dist）
taskkill /f /im pythonw.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

echo [0.5/5] check port 8788 （a stuck HTTP server would hijack the GUI after rebuild）
set "NETTMP=%TEMP%\box_netstat_8788.txt"
netstat -ano 2>nul | findstr ":8788 " | findstr "LISTENING" > "%NETTMP%"
if exist "%NETTMP%" (
    for /f "tokens=1,2,3,4,5" %%a in (%NETTMP%) do (
        set "PID8788=%%e"
        if defined PID8788 (
            echo   [WARN] port 8788 occupied by PID !PID8788!
            echo   [WARN] killing it so the rebuilt exe won't connect to a stale server...
            taskkill /f /pid !PID8788! >nul 2>&1
            if errorlevel 1 (
                echo   [ERR] could not kill PID !PID8788! （may need admin）. Close it manually before running the exe.
            ) else (
                echo   [OK] killed stale server PID !PID8788!
            )
        )
    )
    del /q "%NETTMP%" >nul 2>&1
)
ping -n 2 127.0.0.1 >nul
echo   [OK] port 8788 check done.

echo [1/5] clean caches
if exist "scripts\*.py_bak" (del /q "scripts\*.py_bak" >nul 2>&1)
if exist "scripts\__pycache__" (rmdir /s /q "scripts\__pycache__")
if exist "build" (rmdir /s /q "build")
if exist "dist" (rmdir /s /q "dist")
echo        done.

echo [2/5] prepare venv
if not exist "%VENV%\Scripts\python.exe" (
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERR] venv create failed, make sure Python 3.13 in PATH.
        pause
        exit /b 1
    )
)
call "%VENV%\Scripts\activate.bat"

REM 先探测依赖是否已就位（最常见情况：venv 已装过，直接跳过联网，避免 SSL/镜像异常卡死）
"%VENV%\Scripts\python.exe" -c "import PyInstaller, webview, numpy, PIL, opencc, openai, pypdf" >nul 2>&1
if not errorlevel 1 (
    echo        deps already present, skip network install.
    goto :build
)

echo        deps missing, installing via pip (fast-fail if offline)...
pip install --disable-pip-version-check --timeout 15 --retries 2 pyinstaller pywebview numpy Pillow opencc-python-reimplemented openai pypdf
if errorlevel 1 (
    echo   [WARN] primary mirror failed, trying aliyun mirror...
    pip install --disable-pip-version-check --timeout 15 --retries 2 -i https://mirrors.aliyun.com/pypi/simple pyinstaller pywebview numpy Pillow opencc-python-reimplemented openai pypdf
)
"%VENV%\Scripts\python.exe" -c "import PyInstaller, webview, numpy, PIL, opencc, openai, pypdf" >nul 2>&1
if errorlevel 1 (
    echo   [ERR] 依赖缺失且联网安装失败。请切换镜像源后重试，或等网络恢复。
    pause
    exit /b 1
)
echo   [OK] deps ready.

:build
echo [3/5] run PyInstaller
pyinstaller "%SPEC%" --noconfirm --clean
if errorlevel 1 (
    echo [ERR] build failed, see above.
    pause
    exit /b 1
)

echo [4/5] purity self-check
set "OUT=%SKILL_DIR%\dist\墨痕"
set "BAD=0"
if exist "%OUT%\_internal\box_config.json" (echo   [WARN] real box_config.json & set BAD=1)
if exist "%OUT%\_internal\token_log.csv"   (echo   [WARN] token_log.csv & set BAD=1)
if exist "%OUT%\_internal\source"          (echo   [WARN] source/ & set BAD=1)
if exist "%OUT%\_internal\output"          (echo   [WARN] output/ & set BAD=1)
if exist "%OUT%\_internal\cropped_hi"      (echo   [WARN] cropped_hi/ & set BAD=1)
if exist "%OUT%\_internal\*.py_bak"        (echo   [WARN] py_bak backup & set BAD=1)
if "%BAD%"=="0" (echo   [OK] clean: no key/image/artifact/backup inside _internal)

echo [4.5/5] strip runtime residues alongside exe （these are NOT bundled by PyInstaller,
echo         but may persist from previous runs / dev usage and would mislead the user）
if exist "%OUT%\box_config.json"    (del /q "%OUT%\box_config.json"    & echo   [clean] removed stale box_config.json)
if exist "%OUT%\token_log.csv"      (del /q "%OUT%\token_log.csv"      & echo   [clean] removed stale token_log.csv)
if exist "%OUT%\source"             (rmdir /s /q "%OUT%\source"         & echo   [clean] removed stale source/)
if exist "%OUT%\output"             (rmdir /s /q "%OUT%\output"         & echo   [clean] removed stale output/)
if exist "%OUT%\cropped_hi"         (rmdir /s /q "%OUT%\cropped_hi"     & echo   [clean] removed stale cropped_hi/)
if exist "%OUT%\*.py_bak"           (del /q "%OUT%\*.py_bak"            & echo   [clean] removed stale py_bak)

echo [5/5] done.
echo    exe: %OUT%\墨痕.exe
echo    copy whole folder to another PC; first run auto-creates blank box_config.json.
echo.
pause
endlocal
