# -*- mode: python ; coding: utf-8 -*-
# =====================================================================
# 墨痕（manual-box-newspaper-ocr）—— macOS PyInstaller 打包脚本（onedir → .app）
# ---------------------------------------------------------------------
# 与 Windows 的 box_tool.spec 平行，互不影响；仅在本机 macOS 上由 build_mac.sh 调用。
# 入口 box_launcher.py（pywebview 窗体），hiddenimports 仅含人工框选链路子模块。
#
# 用法（必须在 macOS 上）：
#   macOS/build_mac.sh        # 会自动调本 spec，并产出 .dmg
# 或直接：
#   pyinstaller macOS/box_tool_mac.spec
# 产物：dist/墨痕.app（onedir），再由 hdiutil 打成 deploy/Output/墨痕-vX-macos.dmg
# =====================================================================

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = os.path.dirname(os.path.abspath(SPEC))          # skill 根
MAC_DIR = os.path.join(HERE, "macOS")
SCRIPTS = os.path.join(HERE, "scripts")
ICON_ICNS = os.path.join(MAC_DIR, "AppIcon.icns")        # 由 build_mac.sh 从 newspaper.ico 生成

a = Analysis(
    [os.path.join(SCRIPTS, "box_launcher.py")],
    pathex=[SCRIPTS],
    binaries=[],
    datas=[
        # 子进程以 [主程序 --run-script <script>] 执行，必须把这些 .py 源文件打进 bundle
        # （hiddenimports 只编译进 pyc，不会保留 .py 文本，exec(open(...)) 会找不到文件）。
        (os.path.join(SCRIPTS, "extract_original.py"), "."),
        (os.path.join(SCRIPTS, "postprocess.py"), "."),
        (os.path.join(SCRIPTS, "stop_flag.py"), "."),
        (os.path.join(SCRIPTS, "box_config.example.json"), "."),
    ],
    hiddenimports=[
        # 第三方
        "webview",
        "numpy",
        "PIL",
        "opencc",
        "openai",
        "pypdf",
        # 人工框选链路子模块（import 模式冻结进 app；均已有 main()）
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

# webview 子模块（macOS 用 Cocoa/WebKit 后端）运行时动态加载，需递归收集
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
    upx=False,                 # macOS 上 upx 易与签名/启动冲突，关闭
    console=False,             # 无控制台
    windowed=True,
    icon=ICON_ICNS if os.path.exists(ICON_ICNS) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="墨痕",
)

app = BUNDLE(
    coll,
    name="墨痕.app",
    icon=ICON_ICNS if os.path.exists(ICON_ICNS) else None,
    bundle_identifier="com.mohen.ocr",
    info_plist={
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'CFBundleDisplayName': '墨痕',
        'CFBundleName': '墨痕',
    },
)
