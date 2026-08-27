# -*- mode: python ; coding: utf-8 -*-
# =====================================================================
# 墨痕（manual-box-newspaper-ocr）—— PyInstaller 打包脚本（onedir 模式）
# ---------------------------------------------------------------------
# 入口 box_launcher.py（pywebview 窗体 + 人工框选画布），
# hiddenimports 仅含人工框选链路需要的子模块（无 PP-Server / X-AnyLabeling）。
#
# 用法（本 skill 根目录、已建好打包专用 venv 前提下）：
#   pyinstaller box_tool.spec
# 产物：dist\墨痕\（含 exe 与依赖）
#
# 打包专用 venv 需装：pywebview numpy Pillow opencc-python-reimplemented openai
# （modern 的 ocr_tool.spec 已验证可用同一 venv）。
# =====================================================================

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.dirname(os.path.abspath(SPEC))
SCRIPTS = os.path.join(HERE, "scripts")
ICON = os.path.join(HERE, "icon", "newspaper.ico")

a = Analysis(
    [os.path.join(SCRIPTS, "box_launcher.py")],
    pathex=[SCRIPTS],
    binaries=[],
    datas=[
        (os.path.join(SCRIPTS, "box_config.example.json"), ".") if os.path.exists(os.path.join(SCRIPTS, "box_config.example.json")) else (os.path.join(SCRIPTS, "launcher_config.example.json"), "."),
        (ICON, "icon"),
        # 子进程以 [主exe --run-script <script>] 执行，必须把这些 .py 源文件打进 exe 的 _internal
        # （hiddenimports 只编译进 pyc，不会保留 .py 文本，exec(open(...)) 会找不到文件）。
        (os.path.join(SCRIPTS, "extract_original.py"), "."),
        (os.path.join(SCRIPTS, "postprocess.py"), "."),
        (os.path.join(SCRIPTS, "stop_flag.py"), "."),
    ],
    hiddenimports=[
        # 第三方
        "webview",
        "numpy",
        "PIL",
        "opencc",
        "openai",
        "pypdf",
        "msvcrt",  # 单实例文件锁（Windows 专属，冻结态需显式收集）
        # 人工框选链路子模块（import 模式冻结进 exe；均已有 main()）
        "extract_original",
        "postprocess",
        "stop_flag",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# opencc 词典（json）是包内数据文件，补齐为 3 元组避免 COLLECT 崩溃
_opencc_datas, _opencc_bins, _opencc_hidden = collect_all("opencc")
a.datas += [(os.path.join(d, os.path.basename(s)), s, "DATA")
            for s, d in _opencc_datas if os.path.isfile(s)]
a.binaries += _opencc_bins
a.hiddenimports += _opencc_hidden

# webview 子模块（Edge WebView2 后端）运行时动态加载，需递归收集
a.hiddenimports += collect_submodules('webview')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="墨痕",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 无控制台黑框（等同 .pyw）
    windowed=True,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="墨痕",
)
