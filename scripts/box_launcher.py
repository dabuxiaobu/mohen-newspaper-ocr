# -*- coding: utf-8 -*-
"""
墨痕 启动器（pywebview 桌面窗体，无 CMD 黑框、无浏览器地址栏）
==================================================================================
本脚本是 manual-box-newspaper-ocr（人工框选变体 skill）的桌面前端 + 后端入口。

替代 modern-newspaper-ocr 的「阶段 2 标注 + 阶段 2.5 归档 + 阶段 3 OCR 转录」三段：
  用户在窗口画布上按阅读顺序框选每篇文章区域 → 前端在本地裁切小图
  → 发本服务 /api/ocr（默认火山方舟 Seed，可换任意 OpenAI 兼容视觉模型）
  → /api/export 落盘：单页模式 output/{整版名}.txt + .json（平铺）；跨页模式按每次跨页任务归集到 output/{跨页基名}/{合并名}.txt + .json（每次跨页各自专属文件夹）
  → postprocess.py（阶段 4，已修标题一致性 bug）递归消费按题录取名，跨页最终题录落在 output/{跨页基名}/{标题}/ 下。

本脚本自备全部后端接口（/api/config /config/save /list_images /image /ocr /export
/extract /group /extract_and_group /postprocess），并复用同目录的 extract_original.py /
group_articles.py / postprocess.py / stop_flag。

运行：
  pythonw box_launcher.py            # pywebview 内嵌窗口（默认最大化），关窗即退出
  pythonw box_launcher.py --port 8788   # 自定义端口
打包：见本 skill 根目录 box_tool.spec（console=False，无 CMD 黑框）。
"""
import os
import sys
import shutil
import io
import json
import base64
import argparse
import subprocess
import threading
import time
import socket
import webbrowser
import ctypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 主窗口对象引用（在 main() 中绑定），供后端接口调整窗口大小/位置
_MAIN_WINDOW = None

# 悬浮窗（右下角小窗）状态
_MINI_STATE = {"active": False, "prev": None}

# 程序根目录：冻结（exe）时写到 exe 所在目录，否则写到脚本同目录。
# 注意：onedir 模式下 sys.executable 在 Windows 上会被 PyInstaller 解析为
# scripts/ 下的路径（而非 dist 内的 exe），导致 dirname(sys.executable) 落到
# scripts/，所有运行数据误写进 skill 源码目录、且与 dist 副本永不同步。
# 因此冻结态改用 sys.argv[0]（实测 = 用户启动的 exe 绝对路径），并兼容落在
# _internal 子目录的情况上移一层。
def _app_dir():
    if getattr(sys, "frozen", False):
        _a0 = sys.argv[0] if sys.argv and sys.argv[0] else sys.executable
        d = os.path.dirname(os.path.abspath(_a0))
        if os.path.basename(d).lower() == "_internal":
            d = os.path.dirname(d)
        return d
    return HERE

# 配置落盘目录：Windows 仍随 exe 目录；macOS .app bundle 只读，且受 App Translocation
# 影响，.app 包旁目录并不可靠，直接把配置放到 ~/Library/Application Support/墨痕。
def _config_dir():
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        support = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "墨痕")
        os.makedirs(support, exist_ok=True)
        return support
    return _app_dir()

APP_DIR = _app_dir()
CONFIG_DIR = _config_dir()

# macOS：兼容旧版把配置放在 .app/Contents/MacOS/ 内部的情况，启动时迁移到 CONFIG_DIR。
def _migrate_macos_config():
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return
    old = os.path.join(APP_DIR, "box_config.json")
    new = os.path.join(CONFIG_DIR, "box_config.json")
    if old != new and os.path.exists(old) and not os.path.exists(new):
        try:
            shutil.copyfile(old, new)
        except Exception as _e:
            sys.stderr.write(f"[migrate] macOS 配置迁移失败：{_e}\n")
_migrate_macos_config()

# 用户产物目录：默认落到「文档/墨痕数据」，与版本无关。
# 若 box_config.json 配置了 DATA_DIR（自定义存盘位置），优先使用之；否则回退默认。
# 无论新版本解压到哪、几个版本并存，识别产物 output/ 都自动共享、不会随旧版本丢失。
def _user_data_dir():
    # 读取自定义数据目录（若存在且可创建、可写则用之）
    _cfg_path = os.path.join(CONFIG_DIR, "box_config.json")
    _custom = ""
    if os.path.isfile(_cfg_path):
        try:
            _custom = (json.load(open(_cfg_path, encoding="utf-8")).get("DATA_DIR") or "").strip()
        except Exception:
            _custom = ""
    if _custom:
        try:
            os.makedirs(_custom, exist_ok=True)
            # 简单可写性校验：尝试写一个临时文件再删除
            _touch = os.path.join(_custom, ".moh__wtest")
            with open(_touch, "w", encoding="utf-8") as _f:
                _f.write("")
            os.remove(_touch)
            return _custom
        except Exception:
            sys.stderr.write("[data_dir] 自定义目录不可写，回退默认：%s\n" % _custom)
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, "Documents", "墨痕数据"),
                 os.path.join(home, "墨痕数据")):
        try:
            os.makedirs(cand, exist_ok=True)
            return cand
        except Exception:
            continue
    return os.path.join(home, "墨痕数据")
DATA_DIR = _user_data_dir()
RUNTIME_DIR = DATA_DIR

# 首次启动迁移：把 exe 旁遗留的旧产物/日志搬到 DATA_DIR，避免旧版本文件夹残留数据。
_LEGACY_ITEMS = ["output", "source", "cropped_hi", "token_log.csv", "ocr_runs.csv", "box_launcher.log"]
def _migrate_legacy_data():
    if any(os.path.exists(os.path.join(DATA_DIR, n)) for n in _LEGACY_ITEMS):
        return  # DATA_DIR 已含数据，视为已迁移完成，跳过以防重复搬运/覆盖
    for name in _LEGACY_ITEMS:
        src = os.path.join(APP_DIR, name)
        if not os.path.exists(src):
            continue
        try:
            shutil.move(src, os.path.join(DATA_DIR, name))
        except Exception as _e:
            sys.stderr.write(f"[migrate] 迁移 {name} 失败：{_e}\n")
_migrate_legacy_data()


# ── 网络下载标记（Mark of the Web）自动解除 ──────────────────────────────
# 从浏览器下载的 zip 解压后，Windows 会给其中每个文件写入 Zone.Identifier 备用数据流。
# .NET 的 Assembly.LoadFrom 拒绝加载带该标记的托管程序集，pythonnet 因此报：
#   RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from ...Python.Runtime.dll
# 本机实测：仅给 Python.Runtime.dll 写入该标记即可 100% 复现此错误（本机 .NET 4.8 完好，
# 与环境无关），删除标记后立即恢复。它是该错误最常见、也最容易被误判成
# 「缺 .NET Framework」的成因——portable.zip 用户几乎必踩，setup.exe 安装版不受影响。
# 只删安全元数据流、不改文件内容；无标记时每个文件仅一次 stat（157 个 dll 约 17ms）。
def _clear_motw():
    if sys.platform != "win32":
        return 0
    roots = [APP_DIR]
    _mei = getattr(sys, "_MEIPASS", "")
    if _mei:
        _a_mei, _a_app = os.path.abspath(_mei), os.path.abspath(APP_DIR)
        # onedir 打包态 _MEIPASS 就是 APP_DIR\_internal，已被上面覆盖，避免重复遍历
        if not (_a_mei == _a_app or _a_mei.startswith(_a_app + os.sep)):
            roots.append(_mei)
    n = 0
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".dll", ".exe")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.exists(p + ":Zone.Identifier"):
                        os.remove(p + ":Zone.Identifier")
                        n += 1
                except OSError:
                    pass  # 非 NTFS 卷 / 权限不足 / 文件正被占用
    return n


try:
    _n_motw = _clear_motw()
    if _n_motw:
        with open(os.path.join(DATA_DIR, "box_launcher.log"), "a", encoding="utf-8") as _lf:
            _lf.write(f"[motw] 启动时解除 {_n_motw} 个文件的网络下载标记（否则 .NET 拒绝加载）\n")
except Exception:
    pass


def _startup_fail(raw):
    """启动期致命错误统一出口：中文可操作提示 → 写日志 → 弹系统框 → 退出。
    双击 exe 时没有控制台，不弹窗就表现为「点了没反应」。"""
    low = raw.lower()
    if ("python.runtime" in low or "pythonnet" in low
            or "failed to resolve" in low or "clr_loader" in low):
        _msg = ("内嵌窗口组件（pythonnet / .NET）加载失败，程序无法启动。\n\n"
                "最常见原因：程序包是网络下载的 zip 解压出来的，Windows 给其中的\n"
                "Python.Runtime.dll 打了「来自 Internet」标记，.NET 拒绝加载它。\n"
                "程序启动时已自动尝试解除该标记。若仍失败，按顺序尝试：\n\n"
                "1) 右键点击下载的 zip → 属性 → 勾选「解除锁定」→ 确定，再重新解压\n"
                "   （这一步可根治，务必先做）；\n"
                "2) 或在程序文件夹内用 PowerShell 执行：\n"
                "   Get-ChildItem -Path . -Recurse | Unblock-File\n"
                "3) 精简版或较旧系统若缺 .NET Framework，请安装 4.8：\n"
                "   https://dotnet.microsoft.com/download/dotnet-framework/net48\n"
                "4) 确认没有把新版文件直接覆盖到旧版目录（新旧依赖混用同样会报此错）。\n\n"
                f"原始错误：{raw}")
    elif "webview" in low and ("no module" in low or "modulenotfound" in low):
        _msg = ("缺少 pywebview（内嵌窗口依赖）。请先安装：\n"
                "  pip install pywebview\n"
                f"\n原始错误：{raw}")
    else:
        _msg = ("内嵌窗口初始化失败，程序无法启动。\n\n"
                "建议：删除程序文件夹后重新解压/安装（避免新旧文件混用），"
                "并确认系统已安装 .NET Framework 4.8 与 VC++ 运行库。\n\n"
                f"原始错误：{raw}")
    print(_msg)
    try:
        sys.stdout = open(os.path.join(DATA_DIR, "box_launcher.log"), "a", encoding="utf-8")
        sys.stderr = sys.stdout
        print(_msg)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(None, _msg, "墨痕 · 启动失败", 0x10)
        except Exception:
            pass
    os._exit(1)


try:
    import webview  # type: ignore
except Exception as _e:
    # 注意：pywebview 6.x 的 import 本身很轻，不加载 pythonnet；
    # pythonnet 真正初始化在 webview.start() 里（见文件末尾，那里也做了兜底）。
    _startup_fail(f"{type(_e).__name__}: {_e}")

VERSION = "4.1.1"

# ---------- OCR 服务商（豆包 / 千问 自由切换） ----------
# 每个服务商独立保存一组凭据（API Key / Base URL / 模型名），切换后各自记住，
# 互不影响。BOX_OCR_PROVIDER 记录当前用哪一个。旧版仅有的 BOX_OCR_* 字段会在
# 首次启动时迁移进 QWEN_*（见 _migrate_cfg）。
OCR_PROVIDERS = {
    "qwen": {
        "label": "千问",
        "key_prefix": "QWEN",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.6-plus",
        "base_placeholder": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_placeholder": "sk-...",
        "model_placeholder": "",
    },
    "doubao": {
        "label": "豆包（推荐）",
        "key_prefix": "DOUBAO",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
        "base_placeholder": "https://ark.cn-beijing.volces.com/api/v3",
        "key_placeholder": "ark-",
        "model_placeholder": "ep-2024xxxxxxxx-xxxxx（火山方舟接入点 ID）",
    },
    "kandian": {
        "label": "看典古籍（免费）",
        "key_prefix": "KANDIAN",
        "default_base_url": "https://ocr.kandianguji.com",
        "default_model": "v2",
        "base_placeholder": "https://ocr.kandianguji.com",
        "key_placeholder": "看典 API Token",
        "model_placeholder": "v2（推荐）/ beta / default",
    },
    "baidu": {
        "label": "百度智能云",
        "key_prefix": "BAIDU",
        "default_base_url": "https://aip.baidubce.com",
        "default_model": "accurate_basic",
        "base_placeholder": "https://aip.baidubce.com",
        "key_placeholder": "百度应用的 API Key",
        "model_placeholder": "accurate_basic（高精度，推荐）/ accurate（含位置）/ general_basic（标准）",
    },
    "other": {
        "label": "其他（自定义）",
        "key_prefix": "OTHER",
        "default_base_url": "",
        "default_model": "",
        "base_placeholder": "",
        "key_placeholder": "",
        "model_placeholder": "",
    },
}

# ---------- 自动更新（方案甲：下载 zip → TEMP 更新器覆盖 → 重启） ----------
# 更新源：Gitee 优先（需先在 Gitee 对应仓库建 Releases 并发布 exe 包），
# 失败则 fallback GitHub。留空则只用 GitHub。启用 Gitee 优先：改成 "owner/repo"。
GITEE_REPO = "dabuxiaobu/mohen-newspaper-ocr"
GITHUB_REPO = "dabuxiaobu/mohen-newspaper-ocr"
# 自动更新挑选 Windows 包的关键字（release asset 名称含其一且以 .zip 结尾）
WIN_PKG_KEYWORDS = ("windows", "win", "onedir", "exe")
# 自动更新挑选 macOS 包的关键字（release asset 名称含 macos 且以 .zip 结尾，即 build_mac.sh 的绿色版 zip）
MAC_PKG_KEYWORDS = ("macos",)
# 跨「下载→应用」的更新状态（单进程内存；下载后写入，重启时读取）
_UPDATE_STATE = {}

def _sha256_of_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _parse_sha256(text, filename):
    """从 checksum 文本里取与目标文件名匹配的 64 位 hex；无匹配则返回首个 hex。"""
    best = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.replace("*", " ").split()
        hexes = [t for t in toks if len(t) == 64 and all(c in "0123456789abcdefABCDEF" for c in t)]
        if not hexes:
            continue
        h = hexes[0]
        if filename and filename.lower() in line.lower():
            return h
        best = h
    return best

def _ssl_ctx():
    """构造跨平台一致的 TLS 上下文。

    macOS 打包态（PyInstaller 冻结）下 ssl.create_default_context() 无系统根证书，
    会导致所有 HTTPS 请求报 CERTIFICATE_VERIFY_FAILED。优先用 truststore 桥接
    系统证书存储（macOS 钥匙串 / Windows CryptoAPI，与 openai→httpx 结构化链路
    同源、已在两平台验证可用）；truststore 不可用时退 certifi CA 包；再退默认上下文。
    """
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _query_latest_release(repo, provider):
    """查询 Gitee/GitHub 最新 release。
    成功：{"ok": True, "tag":..., "notes":..., "assets":..., "page_url":...}
    失败：{"ok": False, "error": "<具体原因>"}（含 URL、错误类型、HTTP 状态码/异常详情，
          便于排查网络不通 / SSL / 404 限流等问题，不再笼统报「均失败」）。
    """
    import urllib.request, urllib.error, ssl, json as _json_mod
    if provider == "gitee":
        api = f"https://gitee.com/api/v5/repos/{repo}/releases/latest"
    else:
        api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "mohen-updater")
        ctx = _ssl_ctx()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            status = getattr(resp, "status", resp.getcode())
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"{provider} 接口返回 HTTP {e.code} @ {api}（{e.reason}）"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        rname = type(reason).__name__ if reason is not None else "URLError"
        return {"ok": False, "error": f"{provider} 请求失败 @ {api}：{rname} {reason}"}
    except ssl.SSLError as e:
        return {"ok": False, "error": f"{provider} SSL 证书错误 @ {api}：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"{provider} 异常 @ {api}：{type(e).__name__} {e}"}
    try:
        data = _json_mod.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"{provider} 响应非 JSON（HTTP {status}）@ {api}：{type(e).__name__} {e}"}
    tag = data.get("tag_name") or data.get("name") or ""
    notes = data.get("body") or ""
    page_url = data.get("html_url") or ""
    if provider == "gitee":
        page_url = f"https://gitee.com/{repo}/releases"
    assets = []
    for a in (data.get("assets") or []):
        assets.append({"name": a.get("name", ""),
                       "url": a.get("browser_download_url") or a.get("url") or ""})
    return {"ok": True, "tag": tag, "notes": notes, "assets": assets, "page_url": page_url}

def _mac_arch_keyword():
    """当前 Mac 架构关键词（arm64 / x86_64 / None），用于匹配 release 中对应架构的包。"""
    try:
        import platform as _platform
        m = _platform.machine()
    except Exception:
        m = ""
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return None

def _mac_update_env_problem():
    """macOS：判断当前运行位置是否不能做「程序内自我替换」。
    返回 (code, hint)；code 为空串表示环境正常。
    code: translocation=系统临时隔离位置；dmg=仍在 dmg 只读卷里；readonly=所在目录不可写。
    必须单独判这个的原因：用户在 dmg 里直接双击运行时，替换脚本对只读卷必然失败，
    而旧脚本会无条件删掉旧包备份，直接把用户的程序毁掉（2026-11-03 实际故障）。
    """
    if sys.platform != "darwin":
        return "", ""
    # APP_DIR 在 mac 冻结态 = <xxx>.app/Contents/MacOS，上溯两级即 .app 本体
    app_bundle = os.path.dirname(os.path.dirname(APP_DIR))
    if "AppTranslocation" in app_bundle:
        return "translocation", "墨痕当前运行在 macOS 的临时隔离位置，说明它还没被拖进「应用程序」文件夹。"
    if app_bundle.startswith("/Volumes/"):
        return "dmg", "墨痕正在 dmg 安装包里直接运行，该磁盘是只读的，无法自我更新。"
    parent = os.path.dirname(app_bundle) or "/"
    try:
        _t = os.path.join(parent, ".moh_write_test")
        with open(_t, "w") as _f:
            _f.write("")
        os.remove(_t)
    except Exception:
        return "readonly", "墨痕所在的 %s 目录不可写，无法自我替换。" % parent
    return "", ""

# 更新失败记忆：替换开始前把「目标版本」写进 update_pending.json，成功启动后（VERSION 已等于
# 目标版本）自动清除。作用是打断「每次启动都重下一遍、每次都失败」的死循环——上次没装成就
# 不再自动重试，改提示手动下载，否则用户看到的就是「一直在自动更新」。
UPDATE_PENDING_FILE = os.path.join(CONFIG_DIR, "update_pending.json")

def _update_pending_target():
    """返回「上次更新未完成」的目标版本号；无标记或已成功则返回空串。"""
    try:
        if not os.path.exists(UPDATE_PENDING_FILE):
            return ""
        with open(UPDATE_PENDING_FILE, "r", encoding="utf-8") as _f:
            _d = json.load(_f) or {}
        _tgt = str(_d.get("target") or "").strip()
    except Exception:
        return ""
    if _tgt and _tgt == VERSION:
        # 当前版本已追平目标 → 上次更新成功，清掉标记
        try:
            os.remove(UPDATE_PENDING_FILE)
        except Exception:
            pass
        return ""
    return _tgt

def _update_pending_write(target):
    try:
        with open(UPDATE_PENDING_FILE, "w", encoding="utf-8") as _f:
            json.dump({"target": str(target or ""), "ts": int(time.time()), "stage": "applying"}, _f)
    except Exception:
        pass

def _build_update_result(current, rel, rel2=None):
    is_mac = (sys.platform == "darwin")
    local_arch = _mac_arch_keyword() if is_mac else None
    # macOS 只在「运行位置无法自我替换」或「上次更新没完成」时才禁用自动更新；
    # 正常装在「应用程序」里的 mac 用户照常自动更新（位置检测全部通过即允许）。
    env_code, env_hint = _mac_update_env_problem()
    _pend = _update_pending_target()
    if is_mac and not env_code and _pend:
        env_code = "pending"
        env_hint = ("上次更新到 %s 没有完成。请手动下载安装包：双击 dmg，"
                    "把里面的「墨痕」拖进「应用程序」文件夹覆盖。" % _pend)
    manual_only = bool(is_mac and env_code)
    def _pick(r):
        if not r: return None, None
        win_asset=None; mac_asset=None; mac_other=None; fallback_zip=None
        for a in r["assets"]:
            n=a["name"].lower()
            if not n.endswith(".zip"): continue
            if fallback_zip is None: fallback_zip=a
            if is_mac:
                if "macos" in n:
                    if local_arch is None or local_arch in n:
                        mac_asset=a; break
                    if mac_other is None: mac_other=a   # 架构不匹配的 Mac 包，仅作最后兜底
            else:
                if any(k in n for k in WIN_PKG_KEYWORDS):
                    win_asset=a; break
        # Mac 上绝不回退到 Windows 包：装不上，还会让用户以为「更新后打不开」。
        # 同架构 Mac 包 > 异架构 Mac 包（并置 arch_warn 提示）> 无。
        tgt = (mac_asset or mac_other) if is_mac else (win_asset or fallback_zip)
        if not tgt: return None, None
        sha=None
        want=tgt["name"]+".sha256.txt"
        for a in r["assets"]:
            if a["name"]==want: sha=a; break
        if sha is None:
            for a in r["assets"]:
                if a["name"].lower() in ("sha256.txt","sha256sums.txt","checksums.txt","checksums.sha256"):
                    sha=a
        return tgt, sha
    tgt, sha = _pick(rel)
    if not tgt:
        return {"ok": True, "current": current, "latest": (rel or {}).get("tag",""), "notes": (rel or {}).get("notes",""), "page_url": (rel or {}).get("page_url",""),
                "download_url":"", "asset_name":"", "sha256_url":"", "alt_download_url":"", "alt_sha256_url":"",
                "need": False, "platform":"macos" if is_mac else "windows",
                "arch_warn": False, "local_arch": local_arch or "",
                "manual_only": manual_only, "env_warn": env_code, "env_hint": env_hint}
    tgt2, sha2 = _pick(rel2) if rel2 else (None, None)
    need = bool(tgt) and bool((rel or {}).get("tag")) and (rel or {}).get("tag") != current
    return {
        "ok": True, "current": current, "latest": (rel or {}).get("tag",""),
        "notes": (rel or {}).get("notes",""), "page_url": (rel or {}).get("page_url",""),
        "download_url": tgt["url"], "asset_name": tgt["name"],
        "sha256_url": sha["url"] if sha else "",
        "alt_download_url": tgt2["url"] if tgt2 else "",
        "alt_sha256_url": sha2["url"] if sha2 else "",
        "need": need,
        "platform": "macos" if is_mac else "windows",
        # 该版本没提供与本机芯片匹配的 Mac 包时置 True，前端据此提示「手动去发布页选包」
        "arch_warn": bool(is_mac and local_arch and local_arch not in tgt["name"].lower()),
        "local_arch": local_arch or "",
        # manual_only：mac 上仅在「运行位置不可自替换」或「上次更新未完成」时为 True，
        # 前端据此只给「手动打开发布页」、不给「立即更新」；位置正常的 mac 用户照常自动更新
        "manual_only": manual_only,
        # env_warn：运行位置不适合自更新时给出原因码，前端额外提示
        "env_warn": env_code, "env_hint": env_hint,
    }

def _check_update():
    current = VERSION
    errors = []
    g_res = None
    gh_res = None
    if GITEE_REPO:
        r = _query_latest_release(GITEE_REPO, "gitee")
        if r.get("ok"): g_res = r
        elif not r.get("ok"): errors.append(r.get("error", "Gitee 未知错误"))
    r = _query_latest_release(GITHUB_REPO, "github")
    if r.get("ok"): gh_res = r
    elif not r.get("ok"): errors.append(r.get("error", "GitHub 未知错误"))
    # 主源优先 Gitee（国内顺畅，海外可在下载环节自动回退 GitHub）；无 Gitee 时直接用 GitHub
    primary = g_res if g_res else gh_res
    if not primary:
        detail = "；".join(errors) if errors else "Gitee/GitHub 均无可用版本"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as _lf:
                _lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [update] 检查失败：{detail}\n")
        except Exception:
            pass
        return {"ok": False, "error": f"无法获取更新信息：{detail}"}
    secondary = gh_res if primary is g_res else g_res
    res = _build_update_result(current, primary, secondary)
    # 主源无可用下载包但备用源有，则互换主备
    if not res.get("download_url") and secondary:
        res2 = _build_update_result(current, secondary, primary)
        if res2.get("download_url"):
            res = res2
    return res

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# 结构化产物顶层归类目录（位于 output/ 下）：按模式分库，每轮各自单独子文件夹。
KB_DIR = "knowledge_base"     # 知识库模式（.md 题录）
PLAIN_DIR = "plain_text"      # 纯文本模式（结构化_*.txt）

# token 用量日志等：落到 DATA_DIR（文档/墨痕数据），与产物同目录、跨版本共享。
_TOKEN_BASE = DATA_DIR
TOKEN_LOG = os.path.join(_TOKEN_BASE, "token_log.csv")
# 前端操作日志持久化文件（关闭 exe 后历史可恢复）
LOG_FILE = os.path.join(_TOKEN_BASE, "box_launcher.log")
# OCR 运行记录：每次「识别全部」成功记一行，用量统计「OCR 调用次数」按此计（=一次识别动作，而非每框 API 次数）
OCR_RUN_LOG = os.path.join(_TOKEN_BASE, "ocr_runs.csv")

def log_token(stage, image, model, prompt_tokens, completion_tokens, total_tokens, duration_s):
    """追加一条 token 用量记录到 TOKEN_LOG；文件不存在时自动写表头。"""
    import csv
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    row = [ts, stage, image, model or "", int(prompt_tokens or 0), int(completion_tokens or 0),
           int(total_tokens or 0), round(float(duration_s or 0), 3)]
    header = ["timestamp", "stage", "image", "model", "prompt_tokens", "completion_tokens", "total_tokens", "duration_s"]
    write_header = not os.path.exists(TOKEN_LOG)
    try:
        with open(TOKEN_LOG, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(header)
            w.writerow(row)
    except Exception:
        pass

SYSTEM_PROMPT = """你是近代文献（图书、期刊、报纸等）OCR 与转录专家。

任务：转录用户提供的文献图像区域中的文字，可按需对单篇做文章级结构化输出。

硬性约束：
1. 原文多为繁体中文，常见竖排（右起左行、上起下行）与横排两种版式；**标题、作者、出版信息亦可能横排**，横排有左起（从左往右）与右起（从右往左）两种方向。转录时**横排按实际阅读方向读（左起横排从左到右，右起横排从右到左）、竖排元素按从右到左**，严禁按图像像素行顺序误排，严禁把横排内容按竖排方向读。**横排文本若为右起（从右往左读），须按实际阅读顺序输出，不得机械从左到右拼接**。输出横排，繁/简依用户要求（未指明则保留繁体）。
2. 严格忠实原文：不增删、不臆造、不擅自改写文意、不把旧用法改成现代用法。**判断依据是"这个字在图上是否看得清"，不是"这里应该是什么字"**：
   - 字形完整、主要笔画齐全 → 照实转录；
   - 关键笔画缺失、上下或左右部件分辨不清、墨迹粘连成块 → 以 □ 占位；
   - 整字只剩墨团、或残存笔画不足三成 → 以 □ 占位。
   **严禁依上下文补全**：人名、地名、机构名、书刊名等专名中若有看不清的字，不得替换为更常见、更知名的字或词，也不得因标题或前文已出现过某个名字，就把此处补成那个名字。例：见"林□"（第二字笔画残缺）不得补为"林伯渠""林则徐"，应原样输出"林□"。
   不要加任何"疑为…""照录""（注：…）"之类的注释或说明；□ 本身是占位符而非注释，可以正常出现。
3. 异体字、俗字、旧字形、缺笔字属刻印字形写法（字仍可辨认、非图像缺损），直接转录其字形，不擅自改成通行写法、也不加注记；但若连部件轮廓都无法分辨、无法判断是哪个字（属缺墨或图像缺损），按第 2 条以 □ 占位，不得凭字形或上下文猜测。
4. 保留标题、副题、正文、图注、页码、书眉、版权页信息之间的层级与分隔；同一视觉块内的内容归为一段。**框内可见的主标题、引题、副题（含双副题、多行题组）须逐一转录，不得只留主标题。**
5. 若区域含多篇文章/多栏接续，按视觉分块分别输出并明确标注边界，不要擅自拼接为一段。
6. 不输出与转录无关的说明、寒暄或"以下是转录结果"之类前缀；直接给内容。
7. 同一栏/同一视觉块内连续排印的正文，不得因框选边缘几何截断而断行；栏内文字连排为一段，仅在确属新段落（上一句已有句末标点收尾，或存在明显层级/空行分隔）处换行。"""

SINGLE_INSTRUCTION = """这是民国竖排报纸中的一篇文章（出处：{src}）。
请你完成该篇文章的转录：
1. 图中文字多为竖排繁体，按「从右到左逐列、每列从上到下」的顺序读取；若遇横排（左起或右起）按实际阅读方向读，严禁按图像像素行机械拼接。
   **必须完整读满全图**：竖排的阅读起点即图像最右侧的第一列，即使该列紧贴图像边缘、或字形被裁切得不完整，也必须逐字转录，严禁漏掉最右一列；横排起始端（左起为最左列、右起为最右列）同理不得漏读。**宁可把整列中的模糊字标成 □，也不可整列跳过**。
   **同一框内若有多列或多行并列的短文字（主标题与副题、双副题、并列署名、并列小标题等），每一列、每一行都要逐条转录，按阅读顺序用换行分隔，不得只取其中最醒目的一条。**
2. 输出结构必须严格为三行，不要写其他前缀、分篇标记或说明：
   标题：
   作者：
   正文：
3. 字段规则：
   - 三个字段名固定为"标题：""作者：""正文："，缺一不可。
   - 无标题或无署名时，对应字段名后必须留空（空着，连"无""佚名"都不要写）。
   - 作者字段**只接受文章真正署名**（如文末"记者某某""某某写""某某撰"）。如果整篇读完后找不到任何署名，作者字段**必须留空**，绝不可把正文、标题、引题或正文首句填进作者字段。
   - 引题、副标题、正文首句、刊头信息一律归入正文或标题，绝不可塞入作者字段。
   - 严禁把字段名"正文：""标题："写入作者或正文的内容中。
4. 将竖排繁体转为简体中文输出；忠实原文，不擅自改写文意、不把旧用法改成现代用法。
5. **不要对任何字词加注释或疑似说明**（如"疑为…""照录""（注：…）""（原文…）"等）。遇到模糊或缺墨的字，判断依据是"在图上是否看得清"：笔画完整就照实转录；关键笔画缺失、部件分辨不清、只剩墨团，一律以 □ 占位。
   **尤须警惕人名、地名等专名**：写得模糊的字，绝不可因标题或前文已出现过某个人名，便把它补成那个名字，也不可补成更常见的知名人物。例：见"林□"（第二字笔画残缺）应原样输出"林□"，不得补为"林伯渠""林则徐"。□ 是占位符，不是注释，允许出现。
6. 若本图是该文的接续页（跨页续文，正文起首即承接上一版末句），则把**接续处上行尾与下行首连成一句完整输出**，不要在版/栏衔接处强行插入分段或空行；跨页的最终合并由系统完成，单页转录只需保证本页文字连续、不擅自断句。仅当确属新段落（上一版末已是完整句且有句号等收尾）时，才在段首正常起段。
7. 本篇正文中，若文字因框选边缘被几何截断（如某句跨框、框底/框右切到句中），须保持句子连续，在截断处不得换行断句；仅在确属新段落时正常起段。

示例（仅展示格式，不代表本文内容）：
标题：五一劳动节纪
作者：本报通讯员
正文：今日为国际劳动节，本市工人举行盛大游行……

（若文章无署名，则作者字段留空，例如：）
标题：本市商会昨日召开年会
作者：
正文：昨日本市商会于礼堂召开年会……"""


# ---------- 配置 ----------
def _read_post_default_prompt():
    """从 postprocess.py 源码以 AST 提取 SYSTEM_PROMPT_SHORT 常量（不执行模块，避免拉起 openai 等重依赖）。

    结构化提示词的出厂默认定义在 postprocess.py 中，本工具借此把默认文本回填到「提示词」抽屉，
    让用户能看到并修改当前生效的默认提示词。"""
    p = os.path.join(HERE, "postprocess.py")
    if not os.path.exists(p):
        return ""
    try:
        import ast
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT_SHORT":
                        val = ast.literal_eval(node.value)
                        return val if isinstance(val, str) else ""
    except Exception:
        pass
    return ""


def _read_post_default_prompt_plain():
    """从 postprocess.py 源码以 AST 提取 SYSTEM_PROMPT_SHORT_PLAIN 常量（纯文本模式默认提示词）。"""
    p = os.path.join(HERE, "postprocess.py")
    if not os.path.exists(p):
        return ""
    try:
        import ast
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT_SHORT_PLAIN":
                        val = ast.literal_eval(node.value)
                        return val if isinstance(val, str) else ""
    except Exception:
        pass
    return ""


def _read_post_default_prompt_history():
    """从 postprocess.py 源码以 AST 提取 SYSTEM_PROMPT_SHORT_HISTORY 常量（《历史研究》知识库模式默认提示词）。"""
    p = os.path.join(HERE, "postprocess.py")
    if not os.path.exists(p):
        return ""
    try:
        import ast
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT_SHORT_HISTORY":
                        val = ast.literal_eval(node.value)
                        return val if isinstance(val, str) else ""
    except Exception:
        pass
    return ""


def _read_post_default_prompt_plain_history():
    """从 postprocess.py 源码以 AST 提取 SYSTEM_PROMPT_SHORT_PLAIN_HISTORY 常量（《历史研究》纯文本模式默认提示词）。"""
    p = os.path.join(HERE, "postprocess.py")
    if not os.path.exists(p):
        return ""
    try:
        import ast
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT_SHORT_PLAIN_HISTORY":
                        val = ast.literal_eval(node.value)
                        return val if isinstance(val, str) else ""
    except Exception:
        pass
    return ""


def _load_cfg():
    cfg = {}
    explicit = set()
    p = os.path.join(CONFIG_DIR, "box_config.json")
    _CFG_KEYS = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                 "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                 "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                 "KANDIAN_API_KEY", "KANDIAN_EMAIL", "KANDIAN_MODEL", "KANDIAN_BASE_URL", "KANDIAN_DET_MODE",
                 "KANDIAN_HP_ORDER",
                 "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "BAIDU_MODEL", "BAIDU_BASE_URL", "BAIDU_AUTO_FALLBACK",
                 "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                 "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN", "PROMPT_POST_HISTORY", "PROMPT_POST_PLAIN_HISTORY",
                 "CITATION_FORMAT", "KEEP_TRADITIONAL", "EYE_CARE", "RENAME_POLICY",
                 "OCR_CONCURRENCY", "OCR_TEMPERATURE")
    if os.path.exists(p):
        try:
            data = json.load(open(p, encoding="utf-8"))
            for k in _CFG_KEYS:
                if data.get(k):
                    cfg[k] = data[k]; explicit.add(k)
        except Exception:
            pass
    for k in _CFG_KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]; explicit.add(k)
    cfg = {k: v for k, v in cfg.items() if v not in (None, "")}
    # 缺服务商键时的兜底：全新配置默认豆包；旧配置（千问时代）通常已填 QWEN_API_KEY，
    # 对这类沿用千问，避免升级后被切成豆包却无豆包 Key。
    if not cfg.get("BOX_OCR_PROVIDER"):
        cfg["BOX_OCR_PROVIDER"] = "qwen" if cfg.get("QWEN_API_KEY") else "doubao"
    return cfg, explicit


def _ensure_blank_config():
    p = os.path.join(CONFIG_DIR, "box_config.json")
    if os.path.exists(p):
        # 旧配置迁移：若缺少 AUTO_UPDATE 键，默认补 false（避免旧包/旧配置默认开启）
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if "AUTO_UPDATE" not in data:
                data["AUTO_UPDATE"] = False
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return
    blank = {k: "" for k in ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                             "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                             "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                             "KANDIAN_API_KEY", "KANDIAN_EMAIL", "KANDIAN_MODEL", "KANDIAN_BASE_URL", "KANDIAN_DET_MODE",
                             "KANDIAN_HP_ORDER",
                             "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "BAIDU_MODEL", "BAIDU_BASE_URL", "BAIDU_AUTO_FALLBACK",
                             "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                             "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN", "PROMPT_POST_HISTORY", "PROMPT_POST_PLAIN_HISTORY",
                             "CITATION_FORMAT", "KEEP_TRADITIONAL", "EYE_CARE", "RENAME_POLICY")}
    blank["BOX_OCR_PROVIDER"] = "doubao"
    blank["AUTO_UPDATE"] = False
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(blank, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _cfg_status(cfg, explicit):
    keys = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
            "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
            "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
            "KANDIAN_API_KEY", "KANDIAN_EMAIL", "KANDIAN_MODEL", "KANDIAN_BASE_URL", "KANDIAN_DET_MODE",
            "KANDIAN_HP_ORDER",
            "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "BAIDU_MODEL", "BAIDU_BASE_URL", "BAIDU_AUTO_FALLBACK",
            "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
            "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN", "PROMPT_POST_HISTORY", "PROMPT_POST_PLAIN_HISTORY",
            "CITATION_FORMAT", "KEEP_TRADITIONAL", "EYE_CARE", "RENAME_POLICY")
    return {k: (k in explicit) for k in keys}


def _mask(s):
    if not s:
        return ""
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "****"


def _migrate_cfg():
    """一次性迁移：旧版配置只含 BOX_OCR_*（千问），迁到 QWEN_* 并补 BOX_OCR_PROVIDER。
    仅在文件确实存在且含旧字段时改写，避免无谓写盘。"""
    p = os.path.join(CONFIG_DIR, "box_config.json")
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    changed = False
    if data.get("BOX_OCR_API_KEY") and not data.get("QWEN_API_KEY"):
        data["QWEN_API_KEY"] = data.pop("BOX_OCR_API_KEY", "")
        data["QWEN_BASE_URL"] = data.pop("BOX_OCR_BASE_URL", "")
        data["QWEN_MODEL"] = data.pop("BOX_OCR_MODEL", "")
        changed = True
    if not data.get("BOX_OCR_PROVIDER"):
        # 有千问 Key 说明是千问时代的老配置，沿用千问；否则按新配置默认豆包
        data["BOX_OCR_PROVIDER"] = "qwen" if data.get("QWEN_API_KEY") else "doubao"
        changed = True
    if changed:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _safe_format(tpl, **kw):
    """安全格式化提示词：缺失的占位符（如自定义 OCR 提示词不含 {src}）保留 {key} 原样，不抛错。"""
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return tpl.format_map(_SafeDict(**kw))


def _translate_api_error(detail, status_code=None):
    """把模型服务商返回的原始错误报文翻成中文友好提示，便于普通用户定位。

    仅做关键词识别，匹配不到时原样返回，绝不吞掉原始信息。
    """
    d = (detail or "").lower()
    code = status_code
    # 额度 / 免费额度耗尽（最常见，用户易误以为程序坏了）
    if (code == 403 or "forbidden" in d or "free quota" in d or "freeter" in d
            or "free tier" in d or "allocationquota" in d or "quota" in d):
        return ("API 额度耗尽或账号处于「仅免费额度」模式：当前 API Key / 账号的免费额度已用完，"
                "请求被服务商拒绝（HTTP 403）。\n"
                "解决办法（任选其一）：\n"
                "  1. 到对应平台控制台充值 / 绑卡；\n"
                "  2. 关闭「仅使用免费额度（use free tier only）」开关；\n"
                "  3. 更换仍有额度的 API Key 或模型后重试。")
    if code == 401 or "unauthorized" in d or "invalid api key" in d or "authentication" in d:
        return ("API Key 无效或已过期：请检查设置中填写的 API Key 是否正确、是否仍有有效额度，"
                "确认后重新保存再试。")
    if code == 429 or "rate limit" in d or "too many requests" in d:
        return ("触发限流（请求过于频繁，HTTP 429）：请稍候再试；若经常触发，"
                "可降低并发或换用额度更高的账号。")
    if code in (502, 503, 504) or "bad gateway" in d or "service unavailable" in d or "timeout" in d:
        return "模型服务暂时不可用（HTTP %s）：请稍后重试；若持续，可能是服务商侧故障。" % code
    if code == 400 or "bad request" in d:
        return "请求参数错误（HTTP 400）：" + (detail or "")[:300]
    return detail or "未知错误（未返回任何信息）"


def _translate_net_error(e, base_url=""):
    """把网络层异常（连接被远端中断 / 超时 / 域名解析失败）翻成可自行处置的中文提示。

    典型场景：OCR Base URL 经图书馆 WebVPN 等代理网关转发时，单张整版裁切图
    base64 后常达数 MB，中间节点往往直接掐断连接（WinError 10054 等）。此类
    失败程序侧无从重试成功，只能给用户可执行的自救指引。

    绝不吞掉原始报错，一律原样附在末尾。
    """
    raw = "%s: %s" % (type(e).__name__, e)
    s = raw.lower()
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in s or "10060" in s:
        head = "连接超时"
    elif (isinstance(e, ConnectionResetError) or "10054" in s or "reset" in s
          or "aborted" in s or "refused" in s or "10061" in s):
        head = "连接被远端中断"
    elif "nodename" in s or "11004" in s or "dns" in s or "name resolution" in s:
        head = "域名解析失败"
    else:
        head = "网络请求失败"
    msg = (f"{head}（属网络链路问题，不是识别出错）：{raw}\n"
           "常见原因：\n"
           "  1. 网络 / 代理节点不稳定（多位用户反馈切换节点后即恢复正常）；\n"
           "  2. OCR Base URL 经图书馆 WebVPN 等代理网关转发时，"
           "单张整版裁切图 base64 后可达数 MB，中间节点会直接掐断连接。\n"
           "解决办法（任选其一）：\n"
           "  1. 切换网络节点、换 Wi-Fi 或重新联网后重试；\n"
           "  2. 关闭网络代理 / VPN（含图书馆 WebVPN 等网关），改直连后重试；\n"
           "  3. 仍失败请把这段提示反馈给开发者。")
    bu = (base_url or "").lower()
    if "vpn" in bu or "/https/" in bu or "webvpn" in bu:
        msg += "\n提示：当前 OCR Base URL 填的是代理网关地址，单张整版图经网关转发时容易被掐断，建议先按第 2 条关闭代理、改直连后重试。"
    return msg


def _norm_region_boxes(rb):
    """把 region_boxes 规范成看典要求的**字符串**：'[[x1,y1,x2,y2], ...]'。

    官方规定：坐标是**整图像素**、左上角原点，且必须是字符串（不是 JSON 数组）；
    硬约束是「用 region_boxes 必须 det_layout=False」，两者互斥。
    接受 list（[x1,y1,x2,y2]，或 {x,y,w,h}）与已是 JSON 字符串两种入参；
    任何非法内容都返回空串＝不启用，绝不把脏坐标发给接口。
    """
    if not rb:
        return ""
    if isinstance(rb, str):
        s = rb.strip()
        if not s:
            return ""
        try:
            arr = json.loads(s)
        except Exception:
            return ""
    else:
        arr = rb
    try:
        out = []
        for b in arr:
            if isinstance(b, dict):
                x1, y1 = float(b.get("x", 0) or 0), float(b.get("y", 0) or 0)
                if b.get("x2") is not None and b.get("y2") is not None:
                    x2, y2 = float(b["x2"]), float(b["y2"])
                else:
                    x2, y2 = x1 + float(b.get("w", 0) or 0), y1 + float(b.get("h", 0) or 0)
            else:
                seq = list(b)
                if len(seq) < 4:
                    continue
                x1, y1, x2, y2 = float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3])
            if x2 < x1:
                x1, x2 = x2, x1
            if y2 < y1:
                y1, y2 = y2, y1
            if x2 - x1 < 1 or y2 - y1 < 1:
                continue
            out.append([int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))])
    except Exception:
        return ""
    return json.dumps(out, ensure_ascii=False) if out else ""


def _kandian_ocr(b64, token, email, version="v2", base_url="https://ocr.kandianguji.com",
                 det_mode="auto", hp_order="right2left", src="", label="",
                 region_boxes="", return_position=False):
    """看典古籍 OCR（免费）：同步接口，一次请求即出结果。

    与豆包/千问的根本差异：判别式引擎（检测+识别），**不吃提示词、不做字段判断、
    不加现代标点**，见字出字。它独有的能力是显式的排版方向参数：
      det_mode  = auto 自判 / sp 强制竖排 / hp 强制横排
      hp_order  = hp_line_words_angel，横排字序 left2right / right2left（仅 v2）
      sp_order  = sp_line_words_angel，竖排字序 top2bottom / bottom2top（仅 v2）

    hp_order 是本轮实测的关键：民国报纸横排标题多为**右起排版**（视觉首字在右），
    模型默认 left2right 只按视觉顺序照抄，于是「烟臺紀行」被输出成「行紀台烟」、
    「雲姑」被输出成「姑雲」，题录直接废掉。PaddleOCR-VL 与 MinerU 同样是照抄
    视觉序列，但**没有参数可纠正**；看典提供了 right2left，是四家里唯一能把
    「右起重排」交给引擎完成的。
    """
    import urllib.request
    import urllib.error

    base = (base_url or "https://ocr.kandianguji.com").strip().rstrip("/")
    if "/ocr_api" in base:                 # 允许直接粘接口全地址
        base = base.split("/ocr_api")[0]
    mode = (det_mode or "auto").strip().lower()
    if mode not in ("auto", "sp", "hp"):
        mode = "auto"
    hp = (hp_order or "right2left").strip().lower()
    if hp not in ("left2right", "right2left"):
        hp = "right2left"
    ver = (version or "v2")
    payload = {
        "token": token or "",
        "email": email or "",
        "image": b64,
        "det_mode": mode,
        "version": ver,
        "image_size": 0,
        "return_position": bool(return_position),
    }
    # hp_line_words_angel 仅在 version=v2 受支持（官方文档标注），低版本传入会被忽略
    if ver == "v2":
        payload["hp_line_words_angel"] = hp
    # region_boxes（仅 v2）：在一张**整版图**上一次指定多个框同时识别，坐标为整图像素。
    # 留空即完全不发送，主流程（一框一次、裁剪图）零变化；只有显式传入时才启用。
    _rb = _norm_region_boxes(region_boxes)
    if _rb and ver == "v2":
        payload["region_boxes"] = _rb
        payload["det_layout"] = False   # 官方硬约束：与 region_boxes 互斥，必须关闭版面识别
    req = urllib.request.Request(base + "/ocr_api",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl_ctx()) as resp:
            raw = resp.read()
        j = json.loads(raw.decode("utf-8", "replace"))
        if (j.get("message") or "").strip().lower() != "success":
            raise RuntimeError("看典古籍返回错误：%s" % (j.get("info") or j.get("message") or "未知原因"))
        d = j.get("data")
        if isinstance(d, list):
            text = "\n".join([str(x) for x in d if str(x).strip()])
        elif isinstance(d, dict):
            text = "\n".join([str(x) for x in (d.get("texts") or []) if str(x).strip()])
        else:
            text = ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError("看典古籍接口返回 %s：%s" % (e.code, detail))
    except OSError as e:
        raise RuntimeError(_translate_net_error(e, base))
    except ValueError:
        raise RuntimeError("看典古籍返回内容不是合法 JSON，前 300 字：" + raw[:300].decode("utf-8", "replace"))

    dur = round(time.time() - t0, 3)
    log_token(stage="box_ocr", image=src or "", model="kandian-" + (version or "v2"),
              prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_s=dur)
    return {"text": text, "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                    "total_tokens": 0, "duration_s": dur},
            "finish_reason": ""}


# ---------- 百度智能云 OCR（通用文字识别） ----------
# 同步接口，一次 POST 即出结果；鉴权是 API Key + Secret Key 先换 access_token（约 30 天）。
# 端点由「模型名」决定，四档对应四个 URL 后缀：
#   accurate_basic 高精度 / accurate 高精度含位置 / general_basic 标准 / general 标准含位置
_BAIDU_ENDPOINTS = ("accurate_basic", "accurate", "general_basic", "general")
# 额度用尽的错误码：17=调用量超限（免费额度或付费包用尽）、19=请求总量超限。
# 4（集群超限额）与 18（QPS 超限）属于限流，不算额度，不换档——换档只会同样被限。
_BAIDU_QUOTA_CODES = (17, 19)
# 换档配对：两档额度独立计，任一档用尽就切到另一档重试一次。
_BAIDU_ALT_ENDPOINT = {"accurate_basic": "accurate", "accurate": "accurate_basic"}
_BAIDU_TOKEN_CACHE = {"api_key": "", "secret": "", "token": "", "exp": 0.0}


class BaiduQuotaExhausted(RuntimeError):
    """百度报「额度用尽」（error_code 17 / 19）。

    单独成类是为了让 do_ocr 能把这种错误与真正的失败区分开：前者换档重试有意义，
    后者（鉴权、参数、网络）换档只会同样失败。
    """

    def __init__(self, endpoint, code, msg):
        RuntimeError.__init__(
            self, "百度智能云 OCR（%s）报额度用尽，错误 %s：%s" % (endpoint, code, msg))
        self.endpoint = endpoint
        self.code = code
        self.msg = msg


def _persist_baidu_model(model):
    """换档后把新档位写回 box_config.json，避免下次启动又先撞一次额度错误。

    只改 BAIDU_MODEL 一个键，其余键原样保留；任何异常都不影响本次识别结果。
    """
    p = os.path.join(CONFIG_DIR, "box_config.json")
    try:
        data = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f) or {}
        data["BAIDU_MODEL"] = model
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _probe_log("[BAIDU] 档位已写回配置：BAIDU_MODEL=%s" % model)
    except Exception as e:
        _probe_log("[BAIDU] 新档位写回配置失败（不影响本次识别）：%s" % e)


def _baidu_access_token(api_key, secret_key, base_url=""):
    """用 API Key + Secret Key 换 access_token，进程内缓存（失效或被服务端拒绝时清空重取）。"""
    import urllib.request
    import urllib.parse
    import urllib.error

    c = _BAIDU_TOKEN_CACHE
    now = time.time()
    if (c["token"] and c["api_key"] == api_key and c["secret"] == secret_key
            and now < float(c["exp"]) - 60):
        return c["token"]
    base = (base_url or "https://aip.baidubce.com").strip().rstrip("/")
    if "/rest/" in base:                    # 允许直接粘接口全地址
        base = base.split("/rest/")[0]
    url = base + "/oauth/2.0/token?" + urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": secret_key,
    })
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as resp:
            j = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("百度智能云换 token 失败（HTTP %s）：%s" % (e.code, detail))
    except OSError as e:
        raise RuntimeError(_translate_net_error(e, base))
    if not j.get("access_token"):
        raise RuntimeError("百度智能云换 token 失败：%s"
                           % (j.get("error_description") or j.get("error") or "未知原因，请核对 API Key 与 Secret Key"))
    c.update({"api_key": api_key, "secret": secret_key, "token": j["access_token"],
              "exp": now + float(j.get("expires_in") or 2592000)})
    return c["token"]


def _baidu_ocr(b64, api_key, secret_key, endpoint="accurate_basic", base_url="", src="", label=""):
    """百度智能云 OCR（通用文字识别）：同步单次 POST，判别式引擎。

    与豆包/千问的根本差异：**不吃提示词、不做字段判断、不加现代标点**，见字出字，
    也没有任何控制字序/方向的参数（detect_direction 只管整图旋转 90/180/270 度，
    不是列序）。返回是行级结果 words_result[]，一行一条，本函数按返回顺序以换行拼接。

    probability=true 打开后会带回逐行置信度，本函数只做统计（写进探针日志），
    不改文本——字数校验抓不到的整段跳读，靠低置信度行提示人工复核。
    """
    import urllib.request
    import urllib.parse
    import urllib.error

    ep = (endpoint or "accurate_basic").strip()
    for _p in ("https://", "http://"):
        if ep.startswith(_p):
            ep = ep[len(_p):]
    if "/" in ep:
        ep = ep.rstrip("/").split("/")[-1]
    if ep not in _BAIDU_ENDPOINTS:
        ep = "accurate_basic"
    base = (base_url or "https://aip.baidubce.com").strip().rstrip("/")
    if "/rest/" in base:
        base = base.split("/rest/")[0]

    # 验证用模拟开关（不进界面）：MOHEN_BAIDU_FAKE_QUOTA=accurate_basic 或 1 时，
    # 该档直接按「额度用尽」报错，用来观察换档过程而不必真把额度跑光。默认为空，无任何影响。
    _fake = (os.environ.get("MOHEN_BAIDU_FAKE_QUOTA") or "").strip()
    if _fake and _fake in ("1", ep):
        raise BaiduQuotaExhausted(ep, 17, "模拟额度用尽（MOHEN_BAIDU_FAKE_QUOTA=%s）" % _fake)

    j = {}
    for attempt in (1, 2):
        token = _baidu_access_token(api_key, secret_key, base)
        form = urllib.parse.urlencode({
            "image": b64,
            "language_type": "CHN_ENG",
            "detect_direction": "false",
            "paragraph": "true",
            "probability": "true",
            "multidirectional_recognize": "true",
        }).encode("utf-8")
        url = (base + "/rest/2.0/ocr/v1/" + ep + "?access_token="
               + urllib.parse.quote(token))
        req = urllib.request.Request(url, data=form, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        t0 = time.time()
        raw = b""
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ssl_ctx()) as resp:
                raw = resp.read()
            j = json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError("百度智能云 OCR 返回 HTTP %s：%s" % (e.code, detail))
        except OSError as e:
            raise RuntimeError(_translate_net_error(e, base))
        except ValueError:
            raise RuntimeError("百度智能云返回内容不是合法 JSON，前 300 字："
                               + raw[:300].decode("utf-8", "replace"))
        ec = j.get("error_code")
        if ec in (110, 111) and attempt == 1:
            _BAIDU_TOKEN_CACHE["token"] = ""      # token 无效/过期，清缓存重换一次
            continue
        if ec:
            if ec in _BAIDU_QUOTA_CODES:
                # 不在这里决定换档：由 do_ocr 依据「额度用尽自动切档」设置统一处理
                raise BaiduQuotaExhausted(ep, ec, j.get("error_msg") or "未知原因")
            raise RuntimeError("百度智能云 OCR 返回错误 %s：%s"
                               % (ec, j.get("error_msg") or "未知原因"))
        break

    items = j.get("words_result") or []
    lines = [(it.get("words") or "").strip() for it in items]
    lines = [x for x in lines if x]
    text = "\n".join(lines)
    scores, lows = [], 0
    for it in items:
        p = ((it.get("probability") or {}).get("average"))
        if isinstance(p, (int, float)):
            scores.append(float(p))
            if float(p) < 0.9:
                lows += 1
    avg = (sum(scores) / len(scores)) if scores else 0.0

    dur = round(time.time() - t0, 3)
    log_token(stage="box_ocr", image=src or "", model="baidu-" + ep,
              prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_s=dur)
    return {"text": text, "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                    "total_tokens": 0, "duration_s": dur},
            "finish_reason": ""}


def do_ocr(b64, src, prompt_override=None, overrides=None, provider=None, label=None):
    import urllib.request
    import urllib.error
    import ssl
    cfg, _ = _load_cfg()
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    ov = overrides or {}
    # provider 身份优先跟随前端当前选中（overrides.provider），
    # 仅在未传时回退 BOX_OCR_PROVIDER，确保切换即时生效、不静默回退千问
    provider = ov.get("provider") or provider or cfg.get("BOX_OCR_PROVIDER", "doubao")
    if provider not in OCR_PROVIDERS:
        provider = "doubao"
    ppre = OCR_PROVIDERS[provider]["key_prefix"]
    pdef = OCR_PROVIDERS[provider]
    if provider == "kandian":
        # 看典古籍：同步单次 POST，不吃提示词；必须同时传 Token 与账号（邮箱/手机号）。
        # 独有能力是 det_mode=sp 强制竖排（右起、每列自上而下），正是另两家缺的参数。
        _kt = ov.get("api_key") or cfg.get("KANDIAN_API_KEY", "")
        _ke = ov.get("email") or cfg.get("KANDIAN_EMAIL", "")
        if not _kt or not _ke:
            return ("【未配置看典古籍凭据】看典需要同时填写 API Token 与账号（注册看典用的邮箱或手机号）。\n"
                    "请在设置中选择「看典古籍」、把这两项填好并保存，再执行识别。\n"
                    "标题：\n作者：\n正文：（未配置凭据，未调用真实模型）")
        return _kandian_ocr(b64, _kt, _ke,
                            ov.get("model") or cfg.get("KANDIAN_MODEL", "") or pdef["default_model"],
                            ov.get("base_url") or cfg.get("KANDIAN_BASE_URL", "") or pdef["default_base_url"],
                            ov.get("det_mode") or cfg.get("KANDIAN_DET_MODE", "") or "auto",
                            ov.get("hp_order") or cfg.get("KANDIAN_HP_ORDER", "") or "right2left",
                            src=src, label=label)
    if provider == "baidu":
        # 百度智能云：同步单次 POST；鉴权为 API Key + Secret Key 换 access_token。
        _bk = ov.get("api_key") or cfg.get("BAIDU_API_KEY", "")
        _bs = ov.get("secret") or cfg.get("BAIDU_SECRET_KEY", "")
        if not _bk or not _bs:
            return ("【未配置百度智能云凭据】百度需要同时填写 API Key 与 Secret Key（控制台「应用列表」里的那一对）。\n"
                    "请在设置中选择「百度智能云」、把这两项填好并保存，再执行识别。\n"
                    "标题：\n作者：\n正文：（未配置凭据，未调用真实模型）")
        _ep = (ov.get("model") or cfg.get("BAIDU_MODEL", "")
               or pdef["default_model"] or "accurate_basic").strip()
        _burl = ov.get("base_url") or cfg.get("BAIDU_BASE_URL", "") or ""
        # 「额度用尽自动换档」默认开启，只有显式关掉（false/0/off/no）才不换
        _af = str(ov.get("auto_fallback") if ov.get("auto_fallback") is not None
                  else cfg.get("BAIDU_AUTO_FALLBACK", "true")).strip().lower()
        _auto = _af not in ("false", "0", "off", "no")
        try:
            return _baidu_ocr(b64, _bk, _bs, _ep, _burl, src=src, label=label)
        except BaiduQuotaExhausted as e1:
            _alt = _BAIDU_ALT_ENDPOINT.get(e1.endpoint) or "accurate_basic"
            if not _auto:
                raise RuntimeError(
                    "百度智能云（%s）额度用尽（错误 %s：%s）。设置里未开启「额度用尽自动换档」，"
                    "请手动切换接口档位，或到百度控制台确认额度后重试。"
                    % (e1.endpoint, e1.code, e1.msg))
            _probe_log("[BAIDU] 档位 %s 报额度用尽（error_code=%s），自动切到 %s 重试"
                       % (e1.endpoint, e1.code, _alt))
            try:
                _res = _baidu_ocr(b64, _bk, _bs, _alt, _burl, src=src, label=label)
            except BaiduQuotaExhausted as e2:
                _probe_log("[BAIDU] 两档均报额度用尽：%s(code=%s) / %s(code=%s)"
                           % (e1.endpoint, e1.code, e2.endpoint, e2.code))
                raise RuntimeError(
                    "百度智能云两档均报额度用尽：%s（%s）、%s（%s）。"
                    "请到百度智能云控制台「文字识别」确认额度，或开通付费后重试。"
                    % (e1.endpoint, e1.code, e2.endpoint, e2.code))
            _persist_baidu_model(_alt)
            _probe_log("[BAIDU] 已切到 %s 并识别成功（档位已写回配置）" % _alt)
            if isinstance(_res, dict):
                # 交给前端在日志区显示，避免用户不知道档位被换过
                _res["note"] = ("百度档位 %s 额度用尽（错误 %s），已自动切到 %s 并识别成功；"
                                "档位已写回配置，后续直接用 %s。"
                                % (e1.endpoint, e1.code, _alt, _alt))
            return _res
    api_key = ov.get("api_key") or cfg.get(ppre + "_API_KEY", "")
    if not api_key:
        return ("【未配置 %s 的 API Key】当前 OCR 服务商为「%s」，但其 API Key 未填写。\n"
                "请在设置中选择该服务商、填入 %s 并保存，再执行识别。\n"
                "标题：\n作者：\n正文：（未配置密钥，未调用真实模型）"
                % (ppre, pdef["label"], ppre + "_API_KEY"))
    base_url = ov.get("base_url") or cfg.get(ppre + "_BASE_URL", "") or pdef["default_base_url"]
    model = ov.get("model") or cfg.get(ppre + "_MODEL", "") or pdef["default_model"]
    if not model:
        # 豆包（现为默认服务商）走火山方舟，model 是用户自建的接入点 ID，内置无法预设；
        # 这里提前给出可操作提示，避免把空 model 发给接口后只回一条费解的 400。
        return ("【未配置模型】当前 OCR 服务商为「%s」，但未填写模型名。\n"
                "请在设置中填写 %s_MODEL 后保存，再执行识别。\n"
                "%s"
                "标题：\n作者：\n正文：（未配置模型，未调用真实模型）"
                % (pdef["label"], ppre,
                   ("豆包填火山方舟的接入点 ID，形如 ep-2024xxxxxxxx-xxxxx（在火山方舟控制台「在线推理-接入点」里创建/复制），不是 doubao-xxx 模型名。\n"
                    if provider == "doubao" else "")))
    user_text = _safe_format(prompt_override or SINGLE_INSTRUCTION, src=src)
    # 字段模式：单框裁切（作者/标题等）套用整篇提示词会让模型把孤立短文字误判为标题或留空。
    # 显式告知模型本图是某一字段的裁切区域，直接转录纯文本、不要输出字段名结构。
    if label == "author":
        user_text += ("\n\n【字段模式·作者署名】本图仅为一篇文章的「作者署名」裁切区域（可能只有一个署名，"
                      "也可能是两个或更多人并列署名；竖排或横排），请直接转录图中全部文字（并列署名逐个转录，"
                      "按阅读顺序用换行分隔），输出为不带任何字段名前缀的纯文本；" +
                      "不要输出「标题：/作者：/正文：」结构，也不要因文字过少而判定为无署名而留空。")
    elif label == "title":
        user_text += ("\n\n【字段模式·标题】本图仅为一篇文章的「标题」裁切区域，可能是单行短题（只有一至数字），"
                      "也可能是主标题与副题、双副题、引题等多列/多行组成的题组（竖排或横排）。" +
                      "**本框内每一列、每一行只要肉眼可辨，都必须逐条转录**，按竖排从右到左（横排按实际阅读方向）的"
                      "顺序用换行分隔输出；**不得只挑最醒目的主标题、把副题整列跳过**。" +
                      "输出为不带任何字段名前缀的纯文本；不要输出「标题：/作者：/正文：」结构。")
    elif label == "section":
        user_text += ("\n\n【字段模式·节标题】本图仅为一篇文章内部的分节小标题（如「（一）从人的纪念到人的教训」「二、……」），" +
                      "不是文章标题。请直接转录图中全部文字并保留原有的（一）/一、等序号，输出为不带任何字段名前缀的纯文本；" +
                      "不要输出「标题：/作者：/正文：」结构，也不要把它补写成完整句子。")
    elif label == "text":
        # 正文框此前漏了这一支，模型仍按整篇提示词输出「标题/作者/正文」三段，
        # 首行短文字（如某列开头十几字）被填进「标题：」，前端又全盘收下，
        # 表现为正文按错位置拆出一行、并冒充该篇标题。正文框就是一个正文框，不该再分三段。
        user_text += ("\n\n【字段模式·正文】本图仅为一篇文章的「正文」裁切区域，标题与署名由其他框负责，"
                      "本框不需要判断哪一行是标题、也不需要给出署名。请直接转录图中全部文字，"
                      "按阅读顺序（竖排从右到左逐列、每列从上到下）连续输出，段与段之间用空行分隔；"
                      "输出为不带任何字段名前缀的纯文本，**不要输出「标题：/作者：/正文：」结构，"
                      "也不要把某一行单独提升为标题或署名单列出来**。")
    _ucontent = [{"type": "text", "text": user_text},
                 {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _ucontent},
        ],
    }
    # 确定性输出：转录任务不该有随机性，同一张图两次识别应当一致。
    # OCR_TEMPERATURE=-1 表示不发送该参数（复现厂商默认温度的基线批次）；默认 0。
    _temp_v = str(cfg.get("OCR_TEMPERATURE", "") or "0").strip()
    try:
        _temp = float(_temp_v)
    except Exception:
        _temp = 0.0
    if _temp >= 0:
        payload["temperature"] = _temp
    # 关闭深度思考：各厂商语法不同
    # 豆包/DeepSeek/OpenAI 系 -> thinking.type=disabled
    # Qwen(dashscope) 默认开启，须 enable_thinking=false 显式关
    # Gemini 经 OpenAI 兼容端点 -> google.thinking_config.thinking_budget=0
    ml = model.lower()
    if "qwen" in ml:
        payload["enable_thinking"] = False
    elif "gemini" in ml:
        payload["google"] = {"thinking_config": {"thinking_budget": 0}}
    else:
        payload["thinking"] = {"type": "disabled"}
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    t0 = time.time()
    try:
        ctx = _ssl_ctx()
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        text = j["choices"][0]["message"]["content"]
        usage = j.get("usage") or {}
        duration = round(time.time() - t0, 3)
        # finish_reason=length 即输出被截断（大框整列消失的典型成因）
        finish_reason = str(j["choices"][0].get("finish_reason") or "")
        log_token(stage="box_ocr", image=src or "", model=model or "",
                  prompt_tokens=usage.get("prompt_tokens", 0),
                  completion_tokens=usage.get("completion_tokens", 0),
                  total_tokens=usage.get("total_tokens", 0),
                  duration_s=duration)
        return {"text": text, "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "duration_s": duration,
        }, "finish_reason": finish_reason}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(_translate_api_error(detail, e.code))
    except OSError as e:
        # URLError / 超时 / ConnectionResetError / RemoteDisconnected 均为 OSError 子类，
        # 统一翻成可行动提示（详见 _translate_net_error）
        raise RuntimeError(_translate_net_error(e, base_url))
    except KeyError:
        raise RuntimeError("响应缺少 choices[0].message.content，返回体：" +
                          json.dumps(j, ensure_ascii=False)[:400])


def _aggregate_usage(model_filter="", stage_filter=""):
    """读取 token_log.csv，按 (model, stage) 聚合；可按模型/阶段筛选。

    token_log.csv 列：timestamp,stage,image,model,prompt_tokens,completion_tokens,total_tokens,duration_s
    供 /api/usage 端点给网页用量统计抽屉使用。"""
    import csv
    empty = {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "duration_s": 0.0}
    by_model = {}
    calls = 0; pt = 0; ct = 0; tt = 0; dur = 0.0
    models = set()
    # OCR「运行次数」= 一次「识别全部」算 1 次（按 ocr_runs.csv 行数计），与框数/组数无关。
    # 平均耗时 = 该模型累计耗时 ÷ 运行次数。
    ocr_run_count = {}
    if os.path.exists(OCR_RUN_LOG):
        with open(OCR_RUN_LOG, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                mdl = (row.get("model") or "").strip()
                if model_filter and model_filter != "全部" and mdl != model_filter:
                    continue
                ocr_run_count[mdl] = ocr_run_count.get(mdl, 0) + 1  # 每行即一次「识别全部」
    if not os.path.exists(TOKEN_LOG):
        return {"summary": dict(empty), "by_model": [], "models": []}
    with open(TOKEN_LOG, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            stage = (row.get("stage") or "").strip()
            model = (row.get("model") or "").strip()
            if not stage:
                continue
            if model_filter and model_filter != "全部" and model != model_filter:
                continue
            if stage_filter and stage_filter != "全部" and stage != stage_filter:
                continue
            try:
                p = int(row.get("prompt_tokens") or 0)
                c = int(row.get("completion_tokens") or 0)
                t = int(row.get("total_tokens") or 0)
                d = float(row.get("duration_s") or 0)
            except (ValueError, TypeError):
                continue
            calls += 1; pt += p; ct += c; tt += t; dur += d
            models.add(model)
            bm = by_model.setdefault((model, stage), dict(empty))
            bm["calls"] += 1; bm["prompt"] += p; bm["completion"] += c
            bm["total"] += t; bm["duration_s"] += d
    # OCR 阶段「调用/平均耗时」用运行次数（一次识别=1），结构化阶段用实际 API 调用数
    for (model, stage), bm in by_model.items():
        if stage == "box_ocr":
            bm["ocr_calls"] = ocr_run_count.get(model, 0)
        else:
            bm["ocr_calls"] = bm["calls"]
    summary = {"calls": calls, "prompt": pt, "completion": ct, "total": tt,
               "duration_s": round(dur, 2)}
    by_model_list = [{"model": k[0], "stage": k[1],
                      "ocr_calls": v.get("ocr_calls", v["calls"]),
                      **{kk: round(vv, 2) if kk == "duration_s" else vv for kk, vv in v.items() if kk != "ocr_calls"}}
                     for k, v in sorted(by_model.items(),
                                        key=lambda kv: (-kv[1]["calls"], kv[0][0], kv[0][1]))]
    return {"summary": summary, "by_model": by_model_list, "models": sorted(models)}


def _img_dir(sub):
    return os.path.join(RUNTIME_DIR, sub)


def _probe_log(*parts):
    """postprocess 诊断探针日志：写 RUNTIME_DIR/postprocess_probe.log，失败静默。"""
    try:
        with open(os.path.join(RUNTIME_DIR, "postprocess_probe.log"), "a", encoding="utf-8") as f:
            f.write("%s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " | ".join(str(p) for p in parts)))
    except Exception:
        pass


def _py():
    """子进程要用的 Python 解释器。

    冻结态没有独立 python.exe（PyInstaller onedir 把解释器编进主 exe）。
    主 exe 能执行任意 .py，但默认入口会启动 webview GUI。为此用 --run-script
    模式：主 exe 识别该参数后 exec 目标脚本并退出，不会重开 GUI。故冻结态直接
    返回 sys.executable（主 exe）。开发态返回 sys.executable 即可。
    """
    return sys.executable


def _open_folder(path):
    """结构化完成后在文件管理器中打开 output/ 目录（方案 A）。"""
    if not os.path.isdir(path):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(path) if hasattr(os, "startfile") else subprocess.run(
                ["explorer", path], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
        return True
    except Exception:
        return False


# —— 轮次目录别名索引（配合「产物按标题改名」）——
# 结构化完成后本轮目录会被改名为「文章标题」（单篇）或「来源名」（多篇），
# 但前端与 rebuild_ref 仍按轮次基名（如 先驱第十五期(19230115)1_跨页）拼路径。
# 这里在 output/{top}/ 下维护 .rounds.json：{轮次基名: 实际目录名}，
# 供 _resolve_round_dir 反查。改名与索引写入由 postprocess.py 完成（见 _write_round_alias）。
_ROUNDS_MAP = ".rounds.json"


def _read_rounds(top_dir):
    p = os.path.join(top_dir, _ROUNDS_MAP)
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _resolve_round_dir(parent_dir, round_base):
    """把轮次基名解析为该轮的真实目录路径。

    优先命中改名后的目录（.rounds.json 别名索引），其次回退到未改名的同名目录。
    """
    real = _read_rounds(parent_dir).get(round_base)
    if real:
        cand = os.path.join(parent_dir, real)
        if os.path.isdir(cand):
            return cand
    plain = os.path.join(parent_dir, round_base)
    return plain if os.path.isdir(plain) else plain


def _find_renamed_ocr(round_dir, ocr_base):
    """在轮次目录里按 .rename_map.json 反查改名后的 OCR txt（ocr_{标题}.txt）路径。"""
    try:
        mp = os.path.join(round_dir, ".rename_map.json")
        if not os.path.isfile(mp):
            return None
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        stem = m.get(ocr_base) if isinstance(m, dict) else None
        if not stem:
            return None
        for fn in os.listdir(round_dir):
            if fn == ("ocr_" + stem + ".txt"):
                return os.path.join(round_dir, fn)
    except Exception:
        pass
    return None


def _find_round_dir_by_map(top_dir, round_base):
    """按各目录下的 .rename_map.json 是否登记了本轮基名，反查本轮被改名后的真实目录。

    .rounds.json 缺失或写入失败时的兜底：.rename_map.json 的键就是 OCR 基名（跨页单篇即轮次基名）。
    只在 top_dir 的直接子目录里找，且要求该映射表登记的标题与目录名一致（改名已完成）。
    """
    if not os.path.isdir(top_dir):
        return None
    try:
        for fn in os.listdir(top_dir):
            d = os.path.join(top_dir, fn)
            if not os.path.isdir(d):
                continue
            mp = os.path.join(d, ".rename_map.json")
            if not os.path.isfile(mp):
                continue
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict):
                continue
            for k, v in m.items():
                if k == round_base and isinstance(v, str) and v and v == fn:
                    return d
    except Exception:
        pass
    return None


def _locate_ocr_txt(out_root, top_root, out_dir, ocr_base):
    """定位某篇的 OCR txt 路径（兼容结构化产物/目录已按标题改名的情况）。

    查找顺序：
      1. output/{out_dir}/{基名}.txt                —— 未改名的原始布局
      2. output/{out_dir}/ocr_{标题}.txt             —— 同目录已改名
      3. output/{top}/[.rounds.json 映射到的目录]/…  —— 目录已改名（先按别名索引，再全扫）
    找不到返回 None。
    """
    names = [ocr_base + ".txt"]
    def _try(d):
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
        alt = _find_renamed_ocr(d, ocr_base)
        return alt or None
    # 1/2：out_dir 指定的目录（原始布局或同目录改名）
    cands = []
    if out_dir:
        cands.append(os.path.join(out_root, out_dir))
    else:
        cands.append(out_root)
    # 3：output/{top}/ 下按轮次别名索引定位改名后的目录
    rb = os.path.splitext(os.path.basename(out_dir or ocr_base))[0]
    real = _read_rounds(top_root).get(rb)
    if real:
        cands.append(os.path.join(top_root, real))
    # 兜底：扫 output/{top}/ 下所有目录（别名索引缺失时仍能找到改名后的目录）
    if os.path.isdir(top_root):
        for fn in sorted(os.listdir(top_root)):
            p = os.path.join(top_root, fn)
            if os.path.isdir(p) and p not in cands:
                cands.append(p)
    seen = set()
    for d in cands:
        ap = os.path.abspath(d)
        if ap in seen:
            continue
        seen.add(ap)
        r = _try(d)
        if r:
            return r
    return None


def _clean_renamed_stale(work):
    """删除 work 目录里上一轮「按标题改名」留下的旧 OCR txt/json，避免与新一轮并存。

    依据 .rename_map.json 的 {旧基名: 标题} 反查出 ocr_{标题}.txt / {标题}.json（含加序号的
    (2)(3) 变体）。映射表本身保留（重建时覆盖）。返回被删文件名列表。
    """
    removed = []
    mp = os.path.join(work, ".rename_map.json")
    if not os.path.isfile(mp):
        return removed
    try:
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict):
            return removed
        stems = set(v for v in m.values() if isinstance(v, str) and v)
        for fn in os.listdir(work):
            hit = False
            for s in stems:
                for pat in ("ocr_" + s, s):
                    if fn == pat + ".txt" or fn == pat + ".json" or \
                       (fn.startswith(pat + "(") and fn.endswith((".txt", ".json"))):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                try:
                    os.remove(os.path.join(work, fn))
                    removed.append(fn)
                except OSError:
                    pass
    except Exception:
        pass
    return removed


def _clean_export_variants(out_root, src, ext, multi):
    """写新一批导出文件前，清掉同一来源「另一种拆分形态」的残留，避免两种形态并存。

    拆篇形态（multi=True，本次写 {src}__{i}.{ext}）→ 删旧的合并单文件 {src}.{ext}；
    合并形态（multi=False，本次写 {src}.{ext}）→ 删旧的拆篇文件 {src}__*.{ext}。
    只动本工具自己写出的 raw 中转文件（.txt/.json），不碰图片与结构化产物。
    不清的后果：上一轮的另一种形态会被 _gather_round 一并收进结构化目录，
    多出一篇「旧文本」或把 N 篇糊成一篇。
    """
    removed = []
    try:
        if not os.path.isdir(out_root):
            return removed
        if multi:
            targets = [src + "." + ext]
        else:
            pre = src + "__"
            targets = [fn for fn in os.listdir(out_root)
                       if fn.startswith(pre) and fn.endswith("." + ext)]
        for fn in targets:
            p = os.path.join(out_root, fn)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed.append(fn)
                except OSError:
                    pass
    except Exception:
        pass
    return removed


def _gather_round(out_root, work, pages, old_cross_dir):
    """结构化前把该轮 OCR 产物（整版原图 + .txt + .json）从 output/ 根或旧跨页目录
    收拢进 work（output/{top}/{轮次基名}/）。目标已存在则跳过，避免覆盖。
    返回 (moved, skipped) 计数。"""
    moved = []; skipped = []
    try:
        # 候选来源：output/ 根目录 + 可能的旧跨页目录（兼容历史散落）
        src_dirs = [out_root]
        if old_cross_dir:
            d = os.path.join(out_root, old_cross_dir)
            if os.path.isdir(d):
                src_dirs.append(d)
        # 收集该轮所有 basename（来自 pages 列表，单页时 pages=[整版名]）
        bases = set()
        for p in (pages or []):
            bases.add(os.path.splitext(os.path.basename(p))[0])
        # 跨页模式：工作集以合并基名 old_cross_dir 命名——保存修改/识别全部写出
        # output/{old_cross_dir}/{old_cross_dir}.txt|.json（basename == old_cross_dir），
        # 必须并入 bases，否则 gather 只按各版原文名查找，永远匹配不到合并产物。
        if old_cross_dir:
            bases.add(os.path.splitext(os.path.basename(old_cross_dir))[0])
        if not bases:
            # 单页但未传 pages：不应发生，直接返回空
            return moved, skipped
        # 先扫描是否有可收拢的文件；无文件时不创建空 work 目录
        to_move = []  # [(src, dst, fn)]
        for sd in src_dirs:
            if not os.path.isdir(sd) or os.path.abspath(sd) == os.path.abspath(work):
                continue
            for fn in os.listdir(sd):
                base = os.path.splitext(fn)[0]
                ext = os.path.splitext(fn)[1].lower()
                # 一篇一个产物：{基名}__{篇序}.txt/.json。跨页（基名=跨页基名）与单页
                # （基名=整版名，同版内按组/标题分了多篇）都会写这种拆篇文件，
                # 所以按「本轮任一本名 + __ + 纯数字」放行，不再只认跨页目录名前缀。
                is_split = ext in (".txt", ".json") and any(
                    base.startswith(b + "__") and base[len(b) + 2:].isdigit()
                    for b in bases)
                if (base not in bases) and not is_split:
                    continue
                # 只收拢该轮的原图/ocr txt/ocr json（排除已结构化的 _题录.md / 题录_*.md 与 结构化_*.txt）
                if fn.endswith("_题录.md") or fn.startswith("结构化_") or fn.startswith("题录_"):
                    continue
                if ext not in IMG_EXTS and ext != ".txt" and ext != ".json":
                    continue
                src = os.path.join(sd, fn)
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(work, fn)
                to_move.append((src, dst, fn))
        if not to_move:
            return moved, skipped
        # 有产物时才创建目标目录并执行移动
        os.makedirs(work, exist_ok=True)
        for src, dst, fn in to_move:
            if os.path.exists(dst):
                # 重新结构化（如隔天重识别同一批图）：用本次最新 OCR 产物覆盖 work 中的旧文件，
                # 否则上次/昨日产物会残留在 work 里，postprocess 会拿旧 OCR 当输入，
                # 导致结构化的 知识库/纯文本 内容不更新（但仍被一键导出复制，呈现「文件夹更新了、产物没更新」）。
                try:
                    os.remove(dst)
                except OSError:
                    pass
                shutil.move(src, dst)
                moved.append(fn)
            else:
                shutil.move(src, dst)
                moved.append(fn)
        if moved or skipped:
            print(f"[gather] 该轮产物已归集到 {os.path.basename(work)}/：移动 {len(moved)} 个，跳过已存在 {len(skipped)} 个。")
    except Exception as e:
        print(f"[gather] 归集失败：{e}")
    return moved, skipped


def _safe_delete(name, sub):
    """安全删除 _img_dir(sub) 下名为 name 的文件。

    防御目录穿越：拒绝含路径分隔符、以 '.' 开头的名字，并校验解析后的绝对
    路径仍在目标目录内。返回被删文件的绝对路径，非法/不存在/失败返回 None。
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith("."):
        return None
    d = os.path.normpath(_img_dir(sub))
    fp = os.path.normpath(os.path.join(d, name))
    if not (fp == d or fp.startswith(d + os.sep)):
        return None
    if not os.path.isfile(fp):
        return None
    try:
        os.remove(fp)
        return fp
    except Exception:
        return None


# ---------- HTTP Handler ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, o):
        self._send(200, json.dumps(o, ensure_ascii=False))

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/config":
            cfg, explicit = _load_cfg()
            provider = cfg.get("BOX_OCR_PROVIDER", "doubao")
            if provider not in OCR_PROVIDERS:
                provider = "doubao"
            ppre = OCR_PROVIDERS[provider]["key_prefix"]
            active_key = cfg.get(ppre + "_API_KEY", "")
            active_model = cfg.get(ppre + "_MODEL", "")
            active_base = cfg.get(ppre + "_BASE_URL", "") or OCR_PROVIDERS[provider]["default_base_url"]
            return self._json({
                "version": VERSION,
                "provider": provider,
                "dry_run": not bool(active_key),
                "model": _mask(active_model),
                "base_url_host": urlparse(active_base).netloc,
                "workdir": HERE,
                "runtime_dir": RUNTIME_DIR,
                "data_dir": RUNTIME_DIR,
                "has_ds_key": bool(cfg.get("DEEPSEEK_API_KEY")),
                "config": _cfg_status(cfg, explicit),
                # 明文回填用（仅本地窗体内返回，不外传）：供前端启动填入输入框
                "values": {k: cfg.get(k, "") for k in
                           ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                            "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                            "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                            "KANDIAN_API_KEY", "KANDIAN_EMAIL", "KANDIAN_MODEL", "KANDIAN_BASE_URL", "KANDIAN_DET_MODE",
                            "KANDIAN_HP_ORDER",
                            "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "BAIDU_MODEL", "BAIDU_BASE_URL", "BAIDU_AUTO_FALLBACK",
                            "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                            "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN", "PROMPT_POST_HISTORY", "PROMPT_POST_PLAIN_HISTORY",
                            "DATA_DIR", "OCR_CONCURRENCY", "OCR_TEMPERATURE")},
                # 提示词出厂默认（抽屉「恢复默认」回填；自定义留空则回落到此）
                "prompt_ocr_default": SINGLE_INSTRUCTION,
                "prompt_post_default": _read_post_default_prompt(),
                "prompt_post_plain_default": _read_post_default_prompt_plain(),
                "prompt_post_history_default": _read_post_default_prompt_history(),
                "prompt_post_plain_history_default": _read_post_default_prompt_plain_history(),
            })
        if u.path == "/api/pick_dir":
            # 弹系统文件夹选择框，返回选中目录（打包/源码态均可用）
            try:
                import webview
                w = webview.windows[0] if webview.windows else None
                if not w:
                    return self._json({"ok": False, "error": "无可用窗口"})
                # webview.FOLDER_DIALOG == 2
                res = w.create_file_dialog(webview.FOLDER_DIALOG)
                if res is None:
                    return self._json({"ok": True, "path": ""})
                if isinstance(res, (list, tuple)):
                    res = res[0] if res else ""
                return self._json({"ok": True, "path": (res or "")})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if u.path == "/api/list_images":
            qs = parse_qs(u.query)
            sub = qs.get("dir", ["cropped_hi"])[0]
            d = _img_dir(sub)
            if not os.path.isdir(d):
                return self._json({"dir": sub, "files": []})
            raw = [f for f in os.listdir(d)
                   if os.path.splitext(f)[1].lower() in IMG_EXTS]
            # 各文件修改时间（秒，int），供前端「整版排序」抽屉按修改时间排序
            mtimes = {}
            for _f in raw:
                try:
                    mtimes[_f] = int(os.path.getmtime(os.path.join(d, _f)))
                except Exception:
                    mtimes[_f] = 0
            # 优先按抽图产物顺序索引（.order.json）排序，使翻页顺序 = 导入/抽图先后
            _op = os.path.join(d, ".order.json")
            _order = []
            if os.path.isfile(_op):
                try:
                    _order = json.load(open(_op, encoding="utf-8")).get("order", [])
                except Exception as _e:
                    _order = []
                    _probe_log("list_images .order.json 解析失败(空/损坏): %r -> 自然排序兜底" % _e)

            # 自然排序 key：`xx_p123.png` 按页码数字比较，避免 p1,p10,p2 字典序乱序
            def _natk(f):
                m = re.search(r"_p(\d+)", f)
                return (0, int(m.group(1)), f) if m else (1, 0, f)

            fs = sorted(raw, key=lambda f: (0, _order.index(f), "") if f in _order else (1,) + _natk(f))
            return self._json({"dir": sub, "files": fs, "mtimes": mtimes})
        if u.path == "/api/list_source":
            return self._list_source()
        if u.path == "/api/image":
            qs = parse_qs(u.query)
            name = qs.get("name", [""])[0]
            sub = qs.get("dir", ["cropped_hi"])[0]
            fp = os.path.join(_img_dir(sub), name)
            if not os.path.exists(fp):
                # 前端 pname 已去扩展名，自动补扩展名兜底（新版整版图为 .jpg，旧版为 .png）
                fp = None
                for _e in (".jpg", ".jpeg", ".png"):
                    cand = os.path.join(_img_dir(sub), name + _e)
                    if os.path.exists(cand):
                        fp = cand
                        break
                if fp is None:
                    return self._send(404, "not found")
            ext = os.path.splitext(fp)[1].lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
                    ".tif": "image/tiff", ".tiff": "image/tiff"}.get(ext, "image/png")
            b = base64.b64encode(open(fp, "rb").read()).decode()
            return self._json({"name": name, "data": f"data:{mime};base64,{b}"})
        if u.path == "/api/rotate_image":
            # 物理旋转整版图片（顺时针 deg 度），PNG 无损重编码；旋转后框选坐标失效，前端负责清空
            try:
                qs = parse_qs(u.query)
                name = qs.get("name", [""])[0]
                sub = qs.get("dir", ["cropped_hi"])[0]
                try:
                    deg = int(qs.get("deg", ["90"])[0] or 90)
                except Exception:
                    deg = 90
                if deg not in (90, 180, 270):
                    deg = 90
                fp = os.path.join(_img_dir(sub), name)
                if not os.path.exists(fp):
                    fp = None
                    for _e in (".jpg", ".jpeg", ".png"):
                        cand = os.path.join(_img_dir(sub), name + _e)
                        if os.path.exists(cand):
                            fp = cand
                            break
                    if fp is None:
                        return self._send(404, "not found")
                from PIL import Image
                with Image.open(fp) as im:
                    # 90° 整倍旋转用 transpose（C 层像素重排，远快于逐像素仿射 rotate）
                    _T = getattr(Image, "Transpose", Image)   # 兼容旧版 Pillow 常量
                    _rot_map = {90: _T.ROTATE_270, 180: _T.ROTATE_180, 270: _T.ROTATE_90}
                    rot = im.transpose(_rot_map[deg])
                    # .tmp 扩展名无法让 PIL 推断格式，须显式指定（沿用原图格式，缺省 PNG）；
                    # PNG 用低压缩级别（大图高压缩编码是主要耗时之一），JPEG 重编码 quality=95
                    fmt = (im.format or "PNG").upper()
                    if fmt == "JPG":
                        fmt = "JPEG"
                    tmp = fp + ".rot.tmp"
                    if fmt == "PNG":
                        rot.save(tmp, format="PNG", compress_level=1)
                    elif fmt == "JPEG":
                        rot.save(tmp, format="JPEG", quality=95, subsampling=0)
                    else:
                        rot.save(tmp, format=fmt)
                    os.replace(tmp, fp)
                return self._json({"ok": True, "name": name, "deg": deg})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:160]})
        if u.path == "/api/logs":
            # 读取持久化日志文件，供前端启动时恢复历史日志（关闭 exe 不丢失）
            try:
                if os.path.exists(LOG_FILE):
                    with open(LOG_FILE, encoding="utf-8") as f:
                        content = f.read()
                else:
                    content = ""
                return self._json({"ok": True, "content": content})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:120]})
        if u.path == "/api/usage":
            qs = parse_qs(u.query)
            model = (qs.get("model") or [""])[0]
            stage = (qs.get("stage") or [""])[0]
            try:
                data = _aggregate_usage(model, stage)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "msg": "读取 token_log.csv 失败：" + str(e)[:120]}, ensure_ascii=False))
            data["ok"] = True
            data["filters"] = {"model": model or "全部", "stage": stage or "全部"}
            return self._json(data)
        if u.path == "/api/check_update":
            return self._json(_check_update())
        if u.path == "/api/auto_update":
            return self._json(self._read_auto_update())
        return self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        # 大文件分块上传走原始二进制（不经 base64/JSON），必须在读 body 之前分流，
        # 否则下面 json.loads 会把整块二进制当文本解析导致失败+内存暴涨。
        if u.path == "/api/import_chunk":
            return self._import_source_chunk()
        try:
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln) if ln else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))

        if u.path == "/api/log":
            # 前端日志持久化：接收一行日志追加到 LOG_FILE，关闭 exe 后可从历史恢复
            line = (data.get("line") or "").strip()
            if line:
                try:
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    pass
            return self._json({"ok": True})
        if u.path == "/api/ocr_run":
            # 每次「识别全部」成功记一行，「OCR 调用次数」按本次识别的组数计：
            # 每个 group 算 1 次；未标 group 的框自动合并为默认组，也算 1 次。
            try:
                import csv as _csv
                model = (data.get("model") or "").strip()
                pages = int(data.get("pages") or 0)
                boxes = int(data.get("boxes") or 0)
                calls = int(data.get("calls") or 1)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                hdr = ["timestamp", "model", "pages", "boxes", "calls"]
                write_hdr = not os.path.exists(OCR_RUN_LOG)
                with open(OCR_RUN_LOG, "a", encoding="utf-8", newline="") as f:
                    w = _csv.writer(f)
                    if write_hdr:
                        w.writerow(hdr)
                    w.writerow([ts, model, pages, boxes, calls])
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:120]})
        if u.path == "/api/reorder_cropped":
            # 持久化整版原图（cropped_hi）的前端排序到 cropped_hi/.order.json，
            # 供后续 /api/list_images 按该顺序返回（翻页/载入顺序一致）。
            try:
                ch = _img_dir("cropped_hi")
                if not os.path.isdir(ch):
                    os.makedirs(ch, exist_ok=True)
                raw = [f for f in os.listdir(ch)
                       if os.path.splitext(f)[1].lower() in IMG_EXTS]
                valid_l = set(x.lower() for x in raw)
                order = [x for x in (data.get("order") or [])
                         if isinstance(x, str) and x.lower() in valid_l]
                seen = set()
                uniq = []
                for x in order:
                    if x.lower() not in seen:
                        uniq.append(x); seen.add(x.lower())
                for x in raw:  # 补齐后端实际存在但前端未传的（按文件名排序追加）
                    if x.lower() not in seen:
                        uniq.append(x); seen.add(x.lower())
                # 原子写：先写临时文件再 os.replace，避免写中断留下空/半截 .order.json
                _op = os.path.join(ch, ".order.json")
                _tmp = _op + ".tmp"
                with open(_tmp, "w", encoding="utf-8") as f:
                    json.dump({"order": uniq}, f, ensure_ascii=False, indent=2)
                os.replace(_tmp, _op)
                return self._json({"ok": True, "order": uniq})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if u.path == "/api/rebuild_ref":
            # 手改作者/标题/日期后，本地确定性重算引用串并写回结构化产物（不调模型）。
            # 走 subprocess 调 postprocess.py --rebuild-ref（与结构化调用一致，隔离主进程）。
            try:
                src = data.get("source_name", "")
                out_dir = data.get("out_dir", "")
                if not src:
                    return self._json({"ok": False, "error": "缺少 source_name"})
                out_root = _img_dir("output")
                pm = (data.get("mode") or "plain").strip() or "plain"
                top_root = os.path.join(out_root, KB_DIR if pm != "plain" else PLAIN_DIR)
                # 目录已按标题/来源名改名（见 postprocess.py finalize_round），
                # 原轮次名路径已不存在 —— 按 .rounds.json 别名索引与 .rename_map.json 双层反查。
                txt_path = _locate_ocr_txt(out_root, top_root, out_dir, src)
                if not txt_path:
                    return self._json({"ok": False,
                                       "error": "未找到该篇的 OCR 文本（目录已改名且映射表缺失，请重新结构化一次）"})
                cfg, _ = _load_cfg()
                fmt = cfg.get("CITATION_FORMAT", "gb7714")
                kt = bool(cfg.get("KEEP_TRADITIONAL", False))
                cmd = [_py()]
                if getattr(sys, "frozen", False):
                    # 冻结态主 exe 当解释器：必须用 --run-script 拦截，否则会走默认入口
                    # 重开 GUI → 单实例拦截弹出“程序已经在运行了”。开发态 python.exe 不识别该参数，不加。
                    cmd.append("--run-script")
                cmd += [os.path.join(HERE, "postprocess.py"),
                        "--rebuild-ref", txt_path,
                        "--citation-format", fmt,
                        *(["--keep-traditional"] if kt else [])]
                r = subprocess.run(cmd,
                                   capture_output=True, text=True, cwd=HERE,
                                   env=dict(os.environ, MOHEN_DATA_DIR=RUNTIME_DIR))
                if r.returncode != 0:
                    return self._json({"ok": False, "error": "(stderr) " + (r.stderr or r.stdout)[:300]})
                try:
                    res = json.loads(r.stdout.strip().splitlines()[-1])
                    if not isinstance(res, dict):
                        res = {"ok": False, "error": "重算引用：子进程返回的不是 JSON 对象"}
                except Exception:
                    # 关键：不能把解析失败伪造成 ok:True —— 前端会据此打印
                    # 「已按手改字段重算引用：undefined」，把真实失败原因彻底掩盖。
                    _tail = (r.stdout or "").strip()[-300:]
                    res = {"ok": False,
                           "error": "重算引用失败：子进程未返回可解析结果。"
                                    + (("输出末段：" + _tail) if _tail else "")}
                return self._json(res)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if u.path == "/api/ocr":
            try:
                # 前端未显式传 prompt_override 时，自动用配置里的 PROMPT_OCR 覆盖（提示词抽屉保存后即时生效）
                cfg_ocr, _ = _load_cfg()
                ov_prompt = data.get("prompt_override") or cfg_ocr.get("PROMPT_OCR") or None
                res = do_ocr(data.get("image_b64", ""), data.get("src", ""),
                             ov_prompt, data.get("overrides") or {}, label=data.get("label") or None)
            except Exception as e:
                _err = f"{type(e).__name__}: {e}"
                if _err.startswith("RuntimeError: "):
                    _err = _err[len("RuntimeError: "):]   # 友好提示自带前缀，去掉冗余类型名
                return self._json({"ok": False, "error": _err})
            if isinstance(res, dict):
                # note：本次识别的过程提示（如百度额度用尽自动换档），由前端写进日志区
                return self._json({"ok": True, "text": res.get("text", ""), "usage": res.get("usage", {}),
                                   "finish_reason": res.get("finish_reason", ""),
                                   "note": res.get("note", "")})
            return self._json({"ok": True, "text": res, "usage": {}})
        if u.path == "/api/export":
            return self._export(data)
        if u.path == "/api/config/save":
            return self._save_cfg(data)
        if u.path in ("/api/extract", "/api/postprocess", "/api/group"):
            try:
                return self._run_script(u.path.split("/")[-1], data)
            except Exception as _e:
                import traceback as _tb
                _probe_log("!!! do_POST _run_script CRASH path=%s: %s\n%s" % (u.path, _e, _tb.format_exc()))
                return self._json({"ok": False, "error": "PROBE_CRASH: " + str(_e),
                                   "traceback": _tb.format_exc()[-1800:]})
        if u.path == "/api/extract_and_group":
            return self._extract_and_group(data)
        if u.path == "/api/export_json":
            return self._export_json(data)
        if u.path == "/api/export_all":
            return self._export_all(data)
        if u.path == "/api/open_folder":
            return self._open_folder_api(data)
        if u.path == "/api/import_source":
            return self._import_source(data)
        if u.path == "/api/import_clipboard":
            return self._import_clipboard()
        if u.path == "/api/list_source":
            return self._list_source()
        if u.path == "/api/clear_source":
            return self._clear_source()
        if u.path == "/api/delete":
            return self._delete_file(data)
        if u.path == "/api/cleanup_cropped":
            return self._cleanup_cropped(data)
        if u.path == "/api/cleanup_cross_raw":
            return self._cleanup_cross_raw(data)
        if u.path == "/api/clear_results":
            return self._clear_results(data)
        if u.path == "/api/open_url":
            return self._open_url(data)
        if u.path == "/api/update_download":
            return self._update_download(data)
        if u.path == "/api/update_apply":
            return self._update_apply()
        if u.path == "/api/auto_update":
            return self._set_auto_update(data)
        return self._send(404, "not found")

    def _clear_results(self, data):
        """「一键清空」：删除本轮各版已导出到 output/ 下的 raw 转录产物（.txt / .json）。

        清空范围以本轮载入的版名为边界，逐个精确匹配 `{基名}.txt`、`{基名}.json`、
        `{基名}__{篇序}.txt/.json`，不使用通配删除；只处理 .txt/.json，
        结构化产物（.md）与整版原图一律不动。目录不存在时静默跳过，返回空清单。
        跨页模式额外处理跨页基名（导出以 crossBaseName 命名）与其中转目录。
        """
        pages = [str(p) for p in (data.get("pages") or []) if p]
        out_dir = (data.get("out_dir") or "").strip()
        cross_base = (data.get("cross_base") or "").strip()
        out_root = _img_dir("output")
        # 安全阀（先于归一化校验）：版名只能是纯文件名。含路径分隔符 / 盘符 / 上级目录符号
        # 一律中止——不做静默 basename，避免「传错了反而删掉另一个同名版」的隐性误删。
        for raw in pages + ([cross_base] if cross_base else []) + ([out_dir] if out_dir else []):
            if ("/" in raw) or ("\\" in raw) or (".." in raw) or (":" in raw):
                return self._json({"ok": False, "error": "版名不合法，已中止：" + raw})
        bases = set()
        for p in pages:
            b = os.path.splitext(os.path.basename(p))[0]
            if b:
                bases.add(b)
        if cross_base:
            b = os.path.splitext(os.path.basename(cross_base))[0]
            if b:
                bases.add(b)
        if not bases:
            return self._json({"ok": True, "removed": []})
        # 安全阀：版名只能是文件名，含路径分隔符 / 盘符 / 上级目录符号一律中止，避免越界删除
        for b in bases:
            if ("/" in b) or ("\\" in b) or (".." in b) or (":" in b):
                return self._json({"ok": False, "error": "版名不合法，已中止：" + b})
        # 候选目录：output 根（单页/未收拢的 raw）、output/{out_dir}（跨页中转）、
        # output/{plain_text|knowledge_base}/{基名}（已结构化过的轮次目录内仍留有 raw）
        dirs = [out_root]
        if out_dir:
            dirs.append(os.path.join(out_root, out_dir))
        for top in ("plain_text", "knowledge_base"):
            for b in bases:
                dirs.append(os.path.join(out_root, top, b))
        pat = re.compile(r"^(?:" + "|".join(re.escape(b) for b in sorted(bases))
                         + r")(?:__\d+)?\.(?:txt|json)$")
        removed = []
        for d in dict.fromkeys(dirs):
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                if not pat.match(fn):
                    continue
                fp = os.path.join(d, fn)
                if not os.path.isfile(fp):
                    continue
                try:
                    os.remove(fp)
                    removed.append(os.path.relpath(fp, out_root).replace("\\", "/"))
                except OSError:
                    pass
        return self._json({"ok": True, "removed": removed, "bases": sorted(bases)})

    def _export(self, data):
        src = data.get("source_name", "")
        boxes = data.get("boxes", [])
        out_dir = data.get("out_dir", "")  # 跨页合并产物归集子目录（相对 output/），单页留空平铺
        if not src or not boxes:
            return self._json({"ok": False, "error": "缺少 source_name 或 boxes"})
        out_root = _img_dir("output")
        if out_dir:
            out_root = os.path.join(out_root, out_dir)
        os.makedirs(out_root, exist_ok=True)
        # 按篇拆分不再看 out_dir：跨页多篇、单页模式下「同一版内按组分了多篇」都必须
        # 一篇一个 txt（{src}__{篇序}.txt）。此前只在 out_dir 非空（跨页且载入≥2 版）时拆，
        # 单页模式/跨页只载 1 版的多篇会被拼进同一个 {src}.txt（【框1】【框2】…），
        # 结构化只能产出一篇——表现为「分了多篇，只有一个文档能结构化」。
        # 单篇（boxes 仅 1 个）仍维持单文件 {src}.txt 向后兼容。
        multi = len(boxes) > 1
        if multi:
            # 每篇单独写一个 txt，避免整轮合并成一篇
            _clean_export_variants(out_root, src, "txt", True)
            written = []
            for i, b in enumerate(boxes, 1):
                base = f"{src}__{i}"
                txt_path = os.path.join(out_root, base + ".txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    # 每篇单文件：该篇合并后的整段文本落为【框1】（postprocess 按单篇解析，不跨篇混淆）
                    f.write(f"出处：{src}\n\n【框1】{(b.get('text') or '').strip()}\n")
                written.append(base + ".txt")
            return self._json({"ok": True, "written": written, "out_root": out_root, "multi": True})
        # 单篇（向后兼容）：所有框按序拼进同一个 {src}.txt，编号【框N】
        _clean_export_variants(out_root, src, "txt", False)
        txt_path = os.path.join(out_root, src + ".txt")
        lines = [f"出处：{src}", ""]
        for i, b in enumerate(boxes, 1):
            lines.append(f"【框{i}】{(b.get('text') or '').strip()}")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return self._json({"ok": True, "written": [src + ".txt"], "out_root": out_root, "multi": False})

    def _export_json(self, data):
        # 自动导出 JSON（还原 X-AnyLabeling 习惯：框坐标 + 识别结果服务端落盘，无需手动下载）
        # 跨页·按篇拆分：与 _export 同构，每篇单独写 {src}__{篇序}.json；单篇/单页维持单文件。
        src = data.get("source_name", "")
        mode = data.get("mode", "")
        boxes = data.get("boxes", [])
        out_dir = data.get("out_dir", "")
        if not src or not boxes:
            return self._json({"ok": False, "error": "缺少 source_name 或 boxes"})
        out_root = _img_dir("output")
        if out_dir:
            out_root = os.path.join(out_root, out_dir)
        os.makedirs(out_root, exist_ok=True)
        # 与 _export 同构：多篇就一篇一个 json（{src}__{篇序}.json），不看 out_dir，
        # 否则单页模式/跨页只载 1 版的多篇仍会合成一个 json。
        multi = len(boxes) > 1
        if multi:
            # 每篇单独写一个 {src}__{篇序}.json（单 entry 数组）
            _clean_export_variants(out_root, src, "json", True)
            written = []
            last_path = ""
            for i, b in enumerate(boxes, 1):
                base = f"{src}__{i}"
                arr = [{"source": src, "mode": mode, "order": 1,
                        "label": b.get("label", ""), "group": b.get("group", ""),
                        "box": b.get("box"), "text": b.get("text", "")}]
                json_path = os.path.join(out_root, base + ".json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(arr, f, ensure_ascii=False, indent=2)
                written.append(base + ".json")
                last_path = json_path
            return self._json({"ok": True, "written": written, "out_root": out_root, "path": last_path, "multi": True})
        # 单篇（向后兼容）：所有框累积进同一个 json 数组
        _clean_export_variants(out_root, src, "json", False)
        arr = []
        for i, b in enumerate(boxes, 1):
            arr.append({"source": src, "mode": mode, "order": i,
                        "label": b.get("label", ""), "group": b.get("group", ""),
                        "box": b.get("box"), "text": b.get("text", "")})
        json_path = os.path.join(out_root, src + ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
        return self._json({"ok": True, "written": [src + ".json"], "out_root": out_root, "path": json_path, "multi": False})

    def _export_all(self, data):
        """一键导出：把 output/knowledge_base/** 下所有 .md 和 output/plain_text/** 下所有
        结构化_*.txt 复制到 output/oneclick/ 中（直接平铺，不再分「知识库」「纯文本」子文件夹，
        也不再每次生成独立的时间戳文件夹——无论走知识库模式还是纯文本模式、无论第几次导出，
        产物都统一落进同一个 output/oneclick/ 文件夹），原产物保持不变。若遇重名（含 .md 与
        .txt 同名）则追加 _1/_2 等序号。关键约束：两类都为空时不创建任何导出目录，直接返回错误提示。"""
        import shutil
        out_root = _img_dir("output")
        kb_src = os.path.join(out_root, KB_DIR)
        plain_src = os.path.join(out_root, PLAIN_DIR)
        exp_root = os.path.join(out_root, "oneclick")

        # 先做全量扫描计数（不落盘），判断各类是否有产物
        kb_files = []
        plain_files = []
        if os.path.isdir(kb_src):
            for root, dirs, files in os.walk(kb_src):
                for fn in files:
                    if fn.endswith(".md"):
                        kb_files.append(os.path.join(root, fn))
        if os.path.isdir(plain_src):
            for root, dirs, files in os.walk(plain_src):
                for fn in files:
                    if fn.startswith("结构化_") and fn.endswith(".txt"):
                        plain_files.append(os.path.join(root, fn))

        kb_count = len(kb_files)
        plain_count = len(plain_files)
        # 两类都为空：不创建任何导出目录，直接返回
        if kb_count == 0 and plain_count == 0:
            return self._json({
                "ok": False,
                "error": "没有可导出的产物：output/knowledge_base 下没有 .md，且 output/plain_text 下没有结构化_*.txt。请先完成识别并结构化。",
                "kb_count": 0,
                "plain_count": 0,
            })

        os.makedirs(exp_root, exist_ok=True)

        # 两类产物直接平铺进同一个 output/oneclick/ 文件夹（不再分子文件夹、不再分时间戳），
        # 共享 used 字典以处理 .md 与 .txt 间的同名冲突。
        # 逐文件容错：此前任一 copy2 抛 OSError（文件被占用、路径超长等）会中断整个循环，
        # 用户看到的是「文件夹里只有一半文件」；现改为记录失败项、继续复制其余文件。
        def _copy_flat(files, dst, used):
            failed = []
            for s in files:
                name = os.path.basename(s)
                if name in used:
                    stem, ext = os.path.splitext(name)
                    n = 1
                    while True:
                        cand = f"{stem}_{n}{ext}"
                        if cand not in used:
                            name = cand
                            break
                        n += 1
                try:
                    shutil.copy2(s, os.path.join(dst, name))
                    used[name] = True
                except Exception as e:
                    failed.append({"src": s, "name": name, "error": f"{type(e).__name__}: {e}"})
            return failed

        used = {}
        failed = []
        if kb_count > 0:
            failed += _copy_flat(kb_files, exp_root, used)
        if plain_count > 0:
            failed += _copy_flat(plain_files, exp_root, used)

        return self._json({
            "ok": True,
            "dir": exp_root,
            "rel": os.path.relpath(exp_root, _img_dir("")),
            "kb_count": kb_count,
            "plain_count": plain_count,
            "copied": kb_count + plain_count - len(failed),
            "failed": failed,
        })

    def _open_folder_api(self, data):
        path = (data.get("path") or "").strip()
        if not path:
            return self._json({"ok": False, "error": "文件夹不存在或路径为空"})
        # 支持相对 output/ 的路径（如 "knowledge_base" / "plain_text"），解析到 output/{path}
        if (not os.path.isabs(path)) or (not os.path.isdir(path)):
            cand = os.path.join(_img_dir("output"), path)
            if os.path.isdir(cand):
                path = cand
        if not os.path.isdir(path):
            return self._json({"ok": False, "error": "文件夹不存在或路径为空"})
        ok = _open_folder(path)
        return self._json({"ok": ok, "path": path})

    def _import_source_chunk(self):
        """大文件分块上传：请求体是原始二进制块，逐块追加到 source/.upload/<name>.part，
        收齐后原子改名到 source/<name> 并登记导入顺序。

        为什么不用 base64+JSON：178MB 文件走那条路时，浏览器侧要同时持有
        ArrayBuffer + 二进制字符串 + base64 字符串 + JSON 文本（峰值约 1GB），
        服务端再解码一遍（峰值约 0.9GB），两端都会 Out of Memory。
        分块二进制直传把峰值压到单块大小（8MB），2GB 级 PDF 也能导。
        """
        try:
            name = unquote(self.headers.get("X-Mh-Name") or "").strip()
            idx = int(self.headers.get("X-Mh-Chunk") or "0")
            total = int(self.headers.get("X-Mh-Total") or "0")
            size = int(self.headers.get("X-Mh-Size") or "0")
            ln = int(self.headers.get("Content-Length", 0))
        except Exception:
            return self._json({"ok": False, "error": "分块头无效"})
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return self._json({"ok": False, "error": "文件名非法"})
        ext = os.path.splitext(name)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".pdf"}:
            return self._json({"ok": False, "error": "不支持的文件类型：" + ext})
        if total <= 0 or idx < 0 or idx >= total:
            return self._json({"ok": False, "error": "分块序号非法"})
        src_dir = _img_dir("source")
        tmp_dir = os.path.join(src_dir, ".upload")
        os.makedirs(tmp_dir, exist_ok=True)
        part = os.path.join(tmp_dir, name + ".part")
        dst = os.path.join(src_dir, name)
        try:
            mode = "wb" if idx == 0 else "ab"
            got = 0
            with open(part, mode) as f:
                # 流式落盘：读一块写一块，不把整个 body 读进内存
                while got < ln:
                    chunk = self.rfile.read(min(1 << 20, ln - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            last = (idx == total - 1)
            if last:
                # 校验实际落盘字节数与前端声明一致（防传输截断产出半个文件）
                real = os.path.getsize(part)
                if size and real != size:
                    os.remove(part)
                    return self._json({"ok": False,
                                       "error": "上传不完整（收到 %d / 声明 %d 字节），已丢弃临时文件，请重试。" % (real, size)})
                os.replace(part, dst)
        except Exception as e:
            try:
                if os.path.isfile(part) and idx != total - 1:
                    os.remove(part)
            except Exception:
                pass
            return self._json({"ok": False, "error": "写入失败：" + str(e)})
        if not last:
            return self._json({"ok": True, "done": False, "received": idx + 1, "total": total})
        # 登记导入顺序（与 _import_source 共用 .import_order.json）
        try:
            op = os.path.join(src_dir, ".import_order.json")
            od = []
            if os.path.isfile(op):
                try:
                    od = json.load(open(op, encoding="utf-8")).get("order", [])
                except Exception:
                    od = []
            _exist = set(os.listdir(src_dir))
            od = [n for n in od if n in _exist]
            if name not in od:
                od.append(name)
            with open(op, "w", encoding="utf-8") as _f:
                json.dump({"order": od}, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        # 临时目录清空（只回收本次遗留的 .part，忽略其他并发上传）
        try:
            if not os.listdir(tmp_dir):
                os.rmdir(tmp_dir)
        except Exception:
            pass
        return self._json({"ok": True, "done": True, "written": [name], "skipped": [],
                           "source_dir": src_dir, "count": 1, "bytes": size})

    def _import_source(self, data):
        files = data.get("files", [])
        if not files:
            return self._json({"ok": False, "error": "未选择文件"})
        src_dir = _img_dir("source")
        os.makedirs(src_dir, exist_ok=True)
        written = []; skipped = []
        for item in files:
            name = (item.get("name") or "").strip()
            b64 = item.get("data", "")
            if not name or not b64:
                skipped.append(name or "(无名)")
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".pdf"}:
                skipped.append(name)
                continue
            try:
                raw = base64.b64decode(b64.split(",", 1)[-1])
                with open(os.path.join(src_dir, name), "wb") as f:
                    f.write(raw)
                written.append(name)
            except Exception as e:
                skipped.append(f"{name}: {e}")
        # 记录导入顺序，供后续抽图/翻页按导入先后排序（而非文件名序）
        try:
            _op = os.path.join(src_dir, ".import_order.json")
            _od = []
            if os.path.isfile(_op):
                try:
                    _od = json.load(open(_op, encoding="utf-8")).get("order", [])
                except Exception:
                    _od = []
            _exist = set(os.listdir(src_dir))
            _od = [n for n in _od if n in _exist]
            for _n in written:
                if _n not in _od:
                    _od.append(_n)
            with open(_op, "w", encoding="utf-8") as _f:
                json.dump({"order": _od}, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return self._json({"ok": True, "written": written, "skipped": skipped,
                           "source_dir": src_dir, "count": len(written)})

    def _import_clipboard(self):
        """从剪贴板读取图像（用户用外部截图工具复制的位图），存为 source/ 下一张源图。

        与「导入文件」完全等价：结果都进入 source/ 列表，后续「抽图并归档」逻辑不变。
        macOS 上 Pillow 的 grabclipboard 需要 pyobjc / 屏幕录制授权，失败会给出明确提示。
        """
        src_dir = _img_dir("source")
        os.makedirs(src_dir, exist_ok=True)
        try:
            from PIL import ImageGrab
        except Exception:
            return self._json({"ok": False, "error": "未安装 Pillow，无法读取剪贴板图像。"})
        try:
            img = ImageGrab.grabclipboard()
        except Exception as e:
            return self._json({"ok": False, "error": "读取剪贴板失败：" + str(e)})
        if img is None:
            return self._json({"ok": False,
                               "error": "剪贴板中没有图像。请先用截图工具（如微信截图 / Snipaste / 系统截图）复制一张图片，再点「粘贴截图」。"})
        # 有些截图工具会把多张图以「文件路径列表」形式放入剪贴板（而非位图对象）
        if isinstance(img, (list, tuple)):
            copied = []; skipped = []
            for p in img:
                if not isinstance(p, str) or not os.path.isfile(p):
                    continue
                ext = os.path.splitext(p)[1].lower()
                if ext not in IMG_EXTS:
                    skipped.append(os.path.basename(p)); continue
                dst = os.path.join(src_dir, os.path.basename(p))
                base, e = os.path.splitext(dst); i = 1
                while os.path.exists(dst):
                    dst = f"{base}-{i}{e}"; i += 1
                try:
                    shutil.copyfile(p, dst); copied.append(os.path.basename(dst))
                except Exception as e:
                    skipped.append(f"{os.path.basename(p)}: {e}")
            if not copied:
                return self._json({"ok": False, "error": "剪贴板里的文件不是可导入的图片。"})
            return self._json({"ok": True, "written": copied, "skipped": skipped,
                               "source_dir": src_dir, "count": len(copied)})
        if not hasattr(img, "save"):
            return self._json({"ok": False,
                               "error": "剪贴板内容不是可直接粘贴的图像（可能是文件或文本）。请把截图复制为图片位图后再试。"})
        ts = time.strftime("%Y%m%d-%H%M%S")
        fname = "clipboard-" + ts + ".png"
        base, _ = os.path.splitext(fname)
        i = 1
        while os.path.exists(os.path.join(src_dir, fname)):
            fname = base + "-" + str(i) + ".png"
            i += 1
        try:
            img.save(os.path.join(src_dir, fname), "PNG")
        except Exception as e:
            return self._json({"ok": False, "error": "保存剪贴板图像失败：" + str(e)})
        return self._json({"ok": True, "written": [fname], "skipped": [],
                           "source_dir": src_dir, "count": 1})

    def _list_source(self):
        d = _img_dir("source")
        if not os.path.isdir(d):
            return self._json({"files": []})
        fs = sorted(f for f in os.listdir(d) if not f.startswith('.'))
        return self._json({"files": fs})

    def _delete_file(self, data):
        """删除 source/ 或 cropped_hi/ 下某文件，并级联清理同名派生产物。

        - 删 source/ 文件：连带清理 cropped_hi/{base}.png 与 output/{base}.txt/.json
        - 删 cropped_hi/ 文件：连带清理 output/{base}.txt/.json
        返回被删主文件与级联清单，供前端日志记录。
        """
        sub = (data.get("sub") or "").strip()
        if sub not in ("source", "cropped_hi"):
            return self._json({"ok": False, "error": "非法目录"})
        name = (data.get("name") or "").strip()
        fp = _safe_delete(name, sub)
        if not fp:
            return self._json({"ok": False, "error": "文件不存在或非法"})
        base = os.path.splitext(name)[0]
        cascade = []
        if sub == "source":
            ch = _img_dir("cropped_hi")
            for ext in IMG_EXTS:
                c = os.path.join(ch, base + ext)
                if os.path.isfile(c):
                    try:
                        os.remove(c); cascade.append(os.path.basename(c))
                    except Exception:
                        pass
        out = _img_dir("output")
        for suf in (".txt", ".json"):
            p = os.path.join(out, base + suf)
            if os.path.isfile(p):
                try:
                    os.remove(p); cascade.append(base + suf)
                except Exception:
                    pass
        return self._json({"ok": True, "deleted": name, "sub": sub, "cascade": cascade})

    def _clear_source(self):
        """只清空 source/ 根目录的全部源文件，不级联删除 cropped_hi/ 与 output/ 派生产物。

        用于「抽图并归档」成功后自动腾出中转空间，同时保住已抽出的整版原图与识别结果。
        """
        d = _img_dir("source")
        removed = []
        try:
            for fn in os.listdir(d):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    try:
                        os.remove(fp); removed.append(fn)
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
        # 回收大文件分块上传的中断残留：.upload/ 下超过 1 小时未改动的 .part
        # （上传中途关窗/断网会留下半个大文件）。刚写入的块 mtime 是新的，不会被误删。
        try:
            up = os.path.join(d, ".upload")
            now = time.time()
            for fn in os.listdir(up):
                fp = os.path.join(up, fn)
                if os.path.isfile(fp) and fn.endswith(".part") and (now - os.path.getmtime(fp)) > 3600:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
            if not os.listdir(up):
                os.rmdir(up)
        except Exception:
            pass
        return self._json({"ok": True, "removed": removed, "count": len(removed)})

    def _cleanup_cropped(self, data):
        """结构化成功后真正删除 cropped_hi/ 下本次已处理的整版源图。

        仅删除指定的文件名列表（来自工作集 pageOrder），使用带目录穿越防护的
        _safe_delete，**不级联、不触碰 output/**（output/ 下已存有整版原图副本与
        OCR 产物，属最终交付，绝不可误删）。返回被成功删除的文件名清单。
        """
        files = (data.get("files") or [])
        if not isinstance(files, list):
            files = [files]
        removed = []
        diag = []
        d = os.path.normpath(_img_dir("cropped_hi"))
        try:
            diag.append("dir=" + d + " files=" + ",".join(sorted(os.listdir(d))[:20]))
        except Exception as _e:
            diag.append("dir=" + d + " listdir_error=" + repr(_e))
        for name in files:
            name = (name or "").strip()
            if not name:
                continue
            # pageOrder 里存的是去扩展名的 basename，而 cropped_hi/ 实际文件带扩展名；
            # 先按原名试删，失败且原名无扩展名时，补 .png/.jpg/.jpeg 候选再试。
            fp = _safe_delete(name, "cropped_hi")
            diag.append(repr(name) + ("->删除" if fp else "->未命中"))
            if not fp:
                base, ext = os.path.splitext(name)
                if not ext:
                    for e in (".png", ".jpg", ".jpeg"):
                        fp = _safe_delete(name + e, "cropped_hi")
                        diag.append(repr(name + e) + ("->删除" if fp else "->未命中"))
                        if fp:
                            break
            if fp:
                removed.append(os.path.basename(fp))
        return self._json({"ok": True, "removed": removed, "count": len(removed), "diag": diag})

    def _cleanup_cross_raw(self, data):
        """结构化成功后删除 output/ 下本次跨页的 raw 中转目录（output/{crossBaseName}）。

        该目录由 saveEdit 自动落盘（跨页模式写出 output/{基名}/{基名}.txt/.json）创建，
        _gather_round 把内容搬到 output/{top}/{基名}/（最终产物）后留下空壳。此处清掉它。
        防护：仅删该目录、仅删其中常规产物文件（图片/ .txt / .json），删空后才 rmdir；
        若目录含子目录或无法识别的文件则保留目录不删，绝不误删 output/ 其他内容。
        """
        base = (data.get("base") or "").strip()
        if not base or base in (".", "..") or "/" in base or "\\" in base or base.startswith("."):
            return self._json({"ok": False, "error": "非法目录名"})
        out_root = os.path.normpath(_img_dir("output"))
        d = os.path.normpath(os.path.join(out_root, base))
        if not (d.startswith(out_root + os.sep)) or d == out_root:
            return self._json({"ok": False, "error": "非法目录"})
        if not os.path.isdir(d):
            return self._json({"ok": True, "removed": [], "dir_removed": False, "count": 0})
        removed = []
        keep_dir = False
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                keep_dir = True  # 含子目录，整目录保留不删
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMG_EXTS or ext in (".txt", ".json"):
                try:
                    os.remove(fp); removed.append(fn)
                except Exception:
                    keep_dir = True
            else:
                keep_dir = True  # 含未知文件，保留目录不删，避免误删
        dir_removed = False
        if not keep_dir:
            try:
                os.rmdir(d); dir_removed = not os.path.exists(d)
            except Exception:
                pass
        return self._json({"ok": True, "removed": removed, "dir_removed": dir_removed, "count": len(removed)})

    def _open_url(self, data):
        """用系统浏览器打开外部 URL（版本更新页等），不在当前 pywebview 窗体内跳转。"""
        url = (data.get("url") or "").strip()
        if not url:
            return self._json({"ok": False, "error": "缺少 url"})
        try:
            webbrowser.open(url, new=2)
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)})

    def _update_download(self, data):
        """下载更新包 zip → 校验 SHA256 → 解压 → 排除 box_config.json → 生成 TEMP 更新器 bat。
        返回 ok 后由前端调 /api/update_apply 触发「退出主程序 + bat 覆盖 exe 目录 + 重启」。"""
        global _UPDATE_STATE
        # macOS 允许自动更新，但必须先过两道门禁：①运行位置能自我替换（非 dmg 只读卷、
        # 非 AppTranslocation 临时路径、父目录可写）；②上次更新不是未完成状态。
        # 过不了就拒绝下载，让用户手动装 dmg——比冒险替换、把程序弄坏要划算。
        if sys.platform == "darwin":
            _code, _hint = _mac_update_env_problem()
            if _code:
                return self._json({"ok": False, "error": _hint})
            if _update_pending_target():
                return self._json({"ok": False, "error": "上次更新未完成，请手动下载安装包覆盖安装（双击 dmg 后把「墨痕」拖进「应用程序」）。"})
        import os, zipfile, shutil, tempfile, urllib.request, urllib.error, ssl
        url = (data.get("download_url") or "").strip()
        sha_url = (data.get("sha256_url") or "").strip()
        if not url:
            return self._json({"ok": False, "error": "缺少 download_url"})
        tmp = tempfile.gettempdir()
        work = os.path.join(tmp, "mohen_update")
        os.makedirs(work, exist_ok=True)
        zip_path = os.path.join(work, "pkg.zip")
        try:
            ctx = _ssl_ctx()
            # 1) 下载更新包：主源失败自动切换备用源（Gitee↔GitHub），兼顾国内/海外网络
            srcs = [(url, sha_url)]
            alt_url = (data.get("alt_download_url") or "").strip()
            alt_sha = (data.get("alt_sha256_url") or "").strip()
            if alt_url:
                srcs.append((alt_url, alt_sha))
            downloaded = False
            last_err = None
            for _i, (_u, _s) in enumerate(srcs):
                try:
                    _req = urllib.request.Request(_u, method="GET")
                    _req.add_header("User-Agent", "mohen-updater")
                    with urllib.request.urlopen(_req, timeout=300, context=ctx) as _resp:
                        with open(zip_path, "wb") as _f:
                            while True:
                                _chunk = _resp.read(65536)
                                if not _chunk:
                                    break
                                _f.write(_chunk)
                    # 2) 校验 SHA256（若 release 附带 checksum 文件）
                    if _s:
                        try:
                            _sreq = urllib.request.Request(_s, method="GET")
                            _sreq.add_header("User-Agent", "mohen-updater")
                            with urllib.request.urlopen(_sreq, timeout=30, context=ctx) as _sresp:
                                _sha_text = _sresp.read().decode("utf-8", "ignore")
                            _expected = _parse_sha256(_sha_text, os.path.basename(_u))
                            if _expected:
                                _actual = _sha256_of_file(zip_path)
                                if _actual.lower() != _expected.lower():
                                    if os.path.exists(zip_path):
                                        try: os.remove(zip_path)
                                        except Exception: pass
                                    raise ValueError("SHA256 校验失败，下载可能被篡改或损坏")
                        except Exception as _se:
                            sys.stderr.write(f"[update] sha 校验跳过：{_se}\n")
                    downloaded = True
                    sys.stderr.write(f"[update] 已从源 {_i+1} 下载成功：{_u}\n")
                    break
                except Exception as _e:
                    last_err = _e
                    sys.stderr.write(f"[update] 源 {_i+1} 下载失败：{type(_e).__name__} {_e}\n")
                    if os.path.exists(zip_path):
                        try: os.remove(zip_path)
                        except Exception: pass
            if not downloaded:
                return self._json({"ok": False, "error": f"更新包下载失败（已尝试 {len(srcs)} 个源）：{type(last_err).__name__} {last_err}"})
            # 3) 解压
            pkg_dir = os.path.join(work, "pkg")
            if os.path.isdir(pkg_dir):
                shutil.rmtree(pkg_dir)
            os.makedirs(pkg_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(pkg_dir)
            # 4) 下钻一层：若只有一个子目录且含 exe，则以该子目录为源根
            entries = [e for e in os.listdir(pkg_dir) if not e.startswith(".")]
            src_root = pkg_dir
            if len(entries) == 1 and os.path.isdir(os.path.join(pkg_dir, entries[0])):
                src_root = os.path.join(pkg_dir, entries[0])

            # ===== macOS 分支：整包替换 .app 并回写用户配置 =====
            if sys.platform == "darwin":
                # 定位解压出的 .app 包（ditto --keepParent 打包，zip 顶层即 墨痕.app）
                app_candidates = []
                for _root, _dirs, _files in os.walk(pkg_dir):
                    for _d in _dirs:
                        if _d.lower().endswith(".app"):
                            app_candidates.append(os.path.join(_root, _d))
                if not app_candidates:
                    return self._json({"ok": False, "error": "解压后未找到 .app 包"})
                new_app = sorted(app_candidates, key=lambda p: len(p))[0]
                # 配置现在放在 .app 包外，新包内不应包含配置；如误带则删除。
                _cfg_pkg = os.path.join(new_app, "Contents", "MacOS", "box_config.json")
                if os.path.exists(_cfg_pkg):
                    os.remove(_cfg_pkg)
                # 兼容旧版：若当前运行实例的配置仍在 .app 内部，尝试迁移到 CONFIG_DIR。
                _old_cfg = os.path.join(APP_DIR, "box_config.json")
                _new_cfg = os.path.join(CONFIG_DIR, "box_config.json")
                if _old_cfg != _new_cfg and os.path.exists(_old_cfg) and not os.path.exists(_new_cfg):
                    try:
                        shutil.copyfile(_old_cfg, _new_cfg)
                    except Exception:
                        pass
                # .app 路径：APP_DIR 在 macOS = <app>/Contents/MacOS
                app_bundle = os.path.dirname(os.path.dirname(APP_DIR))
                sh_path = os.path.join(work, "apply.sh")
                sh_content = (
                    "#!/bin/sh\n"
                    "# $1=目标 .app  $2=新 .app  $3=日志  $4=失败标记  $5=pending标记(成功后删除)\n"
                    'APP_BUNDLE="$1"\n'
                    'NEW_APP="$2"\n'
                    'LOG="${3:-/tmp/mohen_update.log}"\n'
                    'FAILFLAG="$4"\n'
                    'PENDING="$5"\n'
                    'OLD_BAK="${APP_BUNDLE}.old"\n'
                    'PARENT=$(dirname "$APP_BUNDLE")\n'
                    'NEW_TMP="$PARENT/.mohen_new_$$.app"\n'
                    'say(){ echo "$(date "+%Y-%m-%d %H:%M:%S") $*" >> "$LOG" 2>/dev/null || true; }\n'
                    'fail(){ say "FAIL $*"; if [ -n "$FAILFLAG" ]; then echo "$*" > "$FAILFLAG" 2>/dev/null || true; fi; }\n'
                    'say "start bundle=$APP_BUNDLE"\n'
                    "# 1) 等主进程退出，最多 30 秒\n"
                    "i=0\n"
                    'while [ $i -lt 30 ]; do\n'
                    '  if ! pgrep -f "${APP_BUNDLE}/Contents/MacOS/" >/dev/null 2>&1; then break; fi\n'
                    "  sleep 1; i=$((i+1))\n"
                    "done\n"
                    "sleep 1\n"
                    "# 2) 父目录必须可写；只读卷（dmg）或无权限直接放弃，旧包分毫不动\n"
                    'if [ ! -w "$PARENT" ]; then fail "目录不可写 $PARENT"; exit 2; fi\n'
                    "# 3) 关键改动：先把新包拷到同目录的临时名。这一步失败时旧包完好，用户完全不受影响\n"
                    'rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    'if ! cp -R "$NEW_APP" "$NEW_TMP" 2>>"$LOG"; then\n'
                    '  rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    '  fail "拷贝新版失败，旧版未动"\n'
                    '  open "$APP_BUNDLE" 2>/dev/null || true\n'
                    "  exit 3\n"
                    "fi\n"
                    "# 4) 校验临时包完整性（Info.plist 与可执行文件都要在），不合格就丢弃，绝不换名\n"
                    'if [ ! -f "$NEW_TMP/Contents/Info.plist" ]; then\n'
                    '  rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    '  fail "新包缺少 Info.plist，已取消"\n'
                    '  open "$APP_BUNDLE" 2>/dev/null || true\n'
                    "  exit 4\n"
                    "fi\n"
                    'BIN=$(ls -1 "$NEW_TMP/Contents/MacOS/" 2>/dev/null | head -1)\n'
                    'if [ -z "$BIN" ] || [ ! -x "$NEW_TMP/Contents/MacOS/$BIN" ]; then\n'
                    '  rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    '  fail "新包缺少可执行文件，已取消"\n'
                    '  open "$APP_BUNDLE" 2>/dev/null || true\n'
                    "  exit 5\n"
                    "fi\n"
                    'xattr -dr com.apple.quarantine "$NEW_TMP" 2>/dev/null || true\n'
                    "# 5) 换名：旧 → .old，临时新包 → 原名。同一磁盘内 mv 是原子操作，不会出现半包状态\n"
                    'rm -rf "$OLD_BAK" 2>/dev/null || true\n'
                    'if [ -e "$APP_BUNDLE" ]; then\n'
                    '  if ! mv "$APP_BUNDLE" "$OLD_BAK" 2>>"$LOG"; then\n'
                    '    rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    '    fail "旧版改名失败，已取消"\n'
                    '    open "$APP_BUNDLE" 2>/dev/null || true\n'
                    "    exit 6\n"
                    "  fi\n"
                    "fi\n"
                    'if ! mv "$NEW_TMP" "$APP_BUNDLE" 2>>"$LOG"; then\n'
                    '  if [ -e "$OLD_BAK" ]; then mv "$OLD_BAK" "$APP_BUNDLE" 2>/dev/null || true; fi\n'
                    '  rm -rf "$NEW_TMP" 2>/dev/null || true\n'
                    '  fail "新版换名失败，已恢复旧版"\n'
                    '  open "$APP_BUNDLE" 2>/dev/null || true\n'
                    "  exit 7\n"
                    "fi\n"
                    "# 6) 成功：清隔离标记、清失败标记与 pending，打开程序，最后才删备份\n"
                    'say ok\n'
                    'xattr -dr com.apple.quarantine "$APP_BUNDLE" 2>/dev/null || true\n'
                    'if [ -n "$FAILFLAG" ]; then rm -f "$FAILFLAG" 2>/dev/null || true; fi\n'
                    'if [ -n "$PENDING" ]; then rm -f "$PENDING" 2>/dev/null || true; fi\n'
                    'open "$APP_BUNDLE" 2>/dev/null || true\n'
                    'rm -rf "$OLD_BAK" 2>/dev/null || true\n'
                    "exit 0\n"
                )
                with open(sh_path, "w", encoding="utf-8") as _f:
                    _f.write(sh_content)
                try:
                    os.chmod(sh_path, 0o755)
                except Exception:
                    pass
                # 记下「要装到哪个版本」：替换脚本成功后自己删，程序启动时若发现版本没追平
                # 就说明上次没装成，本次不再自动重试（打断无限循环）
                _tgt_ver = str(data.get("target") or "").strip()
                if _tgt_ver:
                    _update_pending_write(_tgt_ver)
                _UPDATE_STATE = {"script": sh_path, "app_bundle": app_bundle, "new_app": new_app,
                                 "log": os.path.join(CONFIG_DIR, "update.log"),
                                 "fail_flag": os.path.join(CONFIG_DIR, "update_failed.txt"),
                                 "pending": UPDATE_PENDING_FILE}
                return self._json({"ok": True, "exe_name": "墨痕.app", "asset": os.path.basename(url)})
            # 5) 排除用户配置：不覆盖 box_config.json（保留 API Key 等）
            cfg_in_pkg = os.path.join(src_root, "box_config.json")
            if os.path.exists(cfg_in_pkg):
                os.remove(cfg_in_pkg)
            # 6) 定位主 exe（优先 墨痕.exe）
            exe_name = "墨痕.exe"
            if not os.path.exists(os.path.join(src_root, exe_name)):
                exes = [f for f in os.listdir(src_root) if f.lower().endswith(".exe")]
                exe_name = exes[0] if exes else ""
            if not exe_name:
                return self._json({"ok": False, "error": "解压后未找到可执行文件"})
            # 7) 生成更新器 bat：纯 ASCII 内容，路径以参数传入，规避中文路径红线
            bat_path = os.path.join(work, "apply.bat")
            bat_content = (
                "@echo off\r\n"
                "timeout /t 3 /nobreak > nul\r\n"
                'xcopy /E /Y /I "%~2\\*" "%~1" > nul\r\n'
                'start "" /D "%~1" "%~1\\%~3"\r\n'
            )
            with open(bat_path, "w", encoding="ascii") as f:
                f.write(bat_content)
            _UPDATE_STATE = {"bat": bat_path, "app_dir": APP_DIR,
                             "src_root": src_root, "exe_name": exe_name}
            return self._json({"ok": True, "exe_name": exe_name, "asset": os.path.basename(url)})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _update_apply(self):
        """启动 TEMP 更新器（Windows: bat 覆盖 exe 目录；macOS: sh 替换 .app），随后退出主程序。"""
        global _UPDATE_STATE
        st = _UPDATE_STATE
        # macOS：临门再核一次运行环境，防止有人绕过前面的门禁直接调这个接口
        if sys.platform == "darwin":
            _code, _hint = _mac_update_env_problem()
            if _code:
                return self._json({"ok": False, "error": _hint})
        if not st:
            return self._json({"ok": False, "error": "未找到已下载的更新包，请先点「立即更新」"})
        # ===== macOS：/bin/sh 启动 apply.sh 替换 .app 后退出 =====
        if sys.platform == "darwin":
            if not os.path.exists(st.get("script", "")):
                return self._json({"ok": False, "error": "未找到已下载的更新包，请先点「立即更新」"})
            try:
                subprocess.Popen(
                    ["/bin/sh", st["script"], st["app_bundle"], st["new_app"],
                     st.get("log", ""), st.get("fail_flag", ""), st.get("pending", "")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            import threading, time as _t
            def _exit():
                _t.sleep(0.6)
                os._exit(0)
            threading.Thread(target=_exit, daemon=True).start()
            return self._json({"ok": True, "restarting": True})
        # ===== Windows：cmd 启动 apply.bat 覆盖 exe 目录后重启 =====
        if not os.path.exists(st.get("bat", "")):
            return self._json({"ok": False, "error": "未找到已下载的更新包，请先点「立即更新」"})
        try:
            subprocess.Popen(
                ["cmd", "/c", st["bat"], st["app_dir"], st["src_root"], st["exe_name"]],
                shell=False,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        import threading, time as _t
        def _exit():
            _t.sleep(0.6)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return self._json({"ok": True, "restarting": True})

    def _cn_num_to_int(self, s):
        """中文数字字符串转整数（支持 零~九千 常见组合，如 十三/二十/一百零五）；
        纯阿拉伯数字直接转；无法解析返回 None。"""
        if not s:
            return None
        s = s.strip()
        if s.isdigit():
            return int(s)
        d = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
        if "千" in s or "百" in s or "十" in s:
            total = 0
            num = 0
            for ch in s:
                if ch in d:
                    num = d[ch]
                elif ch == "十":
                    total += (num if num else 1) * 10
                    num = 0
                elif ch == "百":
                    total += (num if num else 1) * 100
                    num = 0
                elif ch == "千":
                    total += (num if num else 1) * 1000
                    num = 0
            total += num
            return total
        if s in d:
            return d[s]
        return None

    def _strip_wrapped_date(self, t, m):
        """日期匹配 m 后从 t 中删除匹配段；若匹配段被括号 ( ) / （ ）包裹，连括号一并删除。"""
        s, e = m.start(), m.end()
        if s > 0 and t[s - 1] in "（(" and e < len(t) and t[e] in "）)":
            return t[:s - 1] + t[e + 1:]
        return t[:s] + t[e:]

    def _parse_source_text(self, text):
        """把用户自由输入的来源串解析成 (名称, 日期, 版次, 期号)。

        支持格式示例：
          大公报 1943-01-17 第2版
          大公报 1943年1月17日 第2版
          中国青年 第十三期(1924年1月5日)
          近代史研究 2020
        返回 (name, date_iso, page_str, issue_str)。date 尽量归一化为 YYYY-MM-DD；
        page 只识别"第N版/No.N"（报纸版次），issue 只识别"第N期"（期刊期号，支持中文数字如「第十三」），
        二者互不混淆——避免「第十三期」被误当成报纸版次著录为 (13)。
        """
        if not text:
            return "", "", "", ""
        t = text.strip()
        date = ""
        page = ""
        issue = ""
        # 日期：优先 ISO，再中文；匹配段可能被括号包裹，连同括号一并清除
        date_patterns = [
            (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
            (r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
            (r"(\d{4})\.(\d{1,2})\.(\d{1,2})", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
            (r"(\d{4})年(\d{1,2})月(\d{1,2})日?", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
            (r"(\d{4})年(\d{1,2})月", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}"),
        ]
        for pat, fmt in date_patterns:
            m = re.search(pat, t)
            if m:
                date = fmt(m)
                t = self._strip_wrapped_date(t, m)
                break
        # 裸年兜底：期刊常只给"2013"这类独立年份（无"年/月"或 ISO 分隔符），卷(期)交给模型处理
        if not date:
            ym = re.search(r"(?<![\d.])\d{4}(?![\d.])", t)
            if ym:
                y = int(ym.group(0))
                if 1800 <= y <= 2100:
                    date = str(y)
                    t = self._strip_wrapped_date(t, ym)
        # 合刊：第X、Y期合刊 / 第X—Y期合刊（两期合并出版），期号归一为 "X/Y"（如 63/64）。
        # 须在单期识别前检测，否则「第六十三、六四期」中的「、」会阻断「第X期」匹配。
        hekan_pat = re.compile(
            r"第\s*([0-9零一二三四五六七八九十百千两]+)\s*"
            r"[、,，\-—–]\s*"
            r"([0-9零一二三四五六七八九十百千两]+)\s*期\s*合?\s*刊?")
        mh = hekan_pat.search(t)
        if mh:
            g1, g2 = mh.group(1), mh.group(2)
            i1 = self._cn_num_to_int(g1)
            i2 = self._cn_num_to_int(g2)
            if (i1 is None or i2 is None) and i1 is not None and i1 >= 10 and len(g2) >= 2:
                # 第二项可能省略「十」（如「第六十三、六四」中「六四」=64）：取第一项的十位数字，
                # 第二项 = 十位*10 + 末位中文数字（覆盖连续合刊共享十位的中文缩略写法）。
                _tens = i1 // 10
                _tens_ch = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
                            6: "六", 7: "七", 8: "八", 9: "九"}.get(_tens)
                if _tens_ch and g2[0] == _tens_ch:
                    _u = self._cn_num_to_int(g2[-1])
                    if _u is not None:
                        i2 = _tens * 10 + _u
            if i1 is not None and i2 is not None:
                issue = f"{i1}/{i2}"
            # 无论是否归一成功都移除「第X、Y期合刊」片段，保证刊名干净（不污染 _so_name）。
            # 归一失败（极罕见写法）时不设置 issue，原始合刊短语已移除，模型改从用户原始出处行解读。
            t = t[:mh.start()] + t[mh.end():]
        # 期号：第N期（期刊），优先于版次识别，支持中文数字（如「第十三」→13）
        issue_pat = re.compile(r"第\s*([0-9零一二三四五六七八九十百千两]+)\s*期")
        mi = issue_pat.search(t)
        if mi:
            iv = self._cn_num_to_int(mi.group(1))
            issue = str(iv) if iv is not None else ""
            t = t[:mi.start()] + t[mi.end():]
        # 版次：第N版 / No.N / No N（报纸），支持中文数字
        page_pat = re.compile(r"第\s*([0-9零一二三四五六七八九十百千两]+)\s*版|No\.?\s*(\d+)", re.IGNORECASE)
        m = page_pat.search(t)
        if m:
            raw = m.group(1) if m.group(1) else m.group(2)
            pv = self._cn_num_to_int(raw) if m.group(1) else int(raw)
            page = str(pv) if pv is not None else ""
            t = t[:m.start()] + t[m.end():]
        # 名称：清理多余空白与残留空括号（如「中国青年第十三期(1924-01-05)」掏空日期后剩的空括号）
        name = re.sub(r"\s+", " ", t).strip(" ,、；;").strip()
        name = re.sub(r"[（(]\s*[）)]", "", name).strip(" ,、；;").strip()
        return name, date, page, issue

    def _run_script(self, name, data):
        _probe_log("ENTER _run_script name=%s mode=%r out_dir=%r pages=%r no_open=%r source_override=%r"
                   % (name, data.get("mode"), data.get("out_dir"), data.get("pages"), data.get("no_open"), data.get("source_override")))
        cfg, _ = _load_cfg()
        script = os.path.join(HERE, {"extract": "extract_original.py",
                                     "postprocess": "postprocess.py",
                                     "group": "group_articles.py"}[name])
        env = dict(os.environ)
        for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"):
            if cfg.get(k):
                env[k] = cfg[k]
        env.setdefault("DEEPSEEK_MODEL", "deepseek-flash")
        # 子进程需能 import stop_flag（脚本目录 HERE），否则冻结态会 ModuleNotFoundError 崩溃
        env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
        # 数据目录统一传到子进程，避免 postprocess.py 在打包态把 token_log 写到 exe 目录
        env["MOHEN_DATA_DIR"] = RUNTIME_DIR
        extra = []
        if name == "postprocess":
            pmode = (data.get("mode") or "plain").strip() or "plain"
            top = KB_DIR if pmode != "plain" else PLAIN_DIR
            pages = data.get("pages") or []
            if not pages:
                return self._json({"ok": False, "error": "未载入任何整版工作集，无法结构化。请先用「载入所选」加入整版，并框选识别后再点结构化。"})
            # 该轮单独子文件夹：跨页=跨页基名，单页=整版名（均置于 output/{top}/ 下）
            round_dir = (data.get("out_dir") or "").strip() or (
                os.path.splitext(pages[0])[0] or "未命名")
            top_root = os.path.join(_img_dir("output"), top)
            # 上一轮结构化后目录已按标题/来源名改名，此处按别名索引解析回真实目录，
            # 保证「隔天重识别同一批图」再次结构化时写回同一目录、而不是新建一个。
            work = _resolve_round_dir(top_root, round_dir)
            # 把该轮 OCR 产物（整版原图 + .txt + .json）从 output/ 根 / 旧跨页目录收拢进 work
            moved, skipped = _gather_round(_img_dir("output"), work, pages, data.get("out_dir") or "")
            if not moved and not skipped:
                # 没有任何可结构化产物，不创建空目录、不调用 postprocess.py
                return self._json({"ok": True, "stdout": "该工作集没有可结构化的 OCR 产物（未生成 .txt / .json / 原图），未创建输出文件夹。", "skipped": True})
            # 重新结构化（如隔天重识别同一批图）：先清掉 work 里上一轮已生成的产品
            # （_题录.md / 题录_*.md / 结构化_*.txt）。否则 postprocess.py 检测到产物已存在会 skip，
            # 导致重识别后的新 OCR 无法重新生成题录，产物内容停留在旧版——one-click 导出
            # 复制到的仍是旧内容。清理后 postprocess 必重新调用模型生成最新产物。
            # 改名后产物前缀变为 题录_{标题}.md（纯文本仍是 结构化_{标题}.txt），两者都要清。
            if os.path.isdir(work):
                for _fn in os.listdir(work):
                    if (_fn.endswith("_题录.md") or _fn.startswith("结构化_")
                            or _fn.startswith("题录_")):
                        try:
                            os.remove(os.path.join(work, _fn))
                        except OSError:
                            pass
            # 清理上一轮改名产物：postprocess 会把 OCR txt/json 改成 ocr_{标题}.txt / {标题}.json，
            # 重新结构化时这些旧文件若不清，新一轮的 {轮次名}.txt / {轮次名}__{N}.txt 会与之并存，
            # 导致同名标题产出「结构化_xxx(2).txt」这类重复文件。按 .rename_map.json 精确清理。
            _stale = _clean_renamed_stale(work)
            if _stale:
                print("[清理] 上一轮改名产物 %d 个：%s" % (len(_stale), "、".join(_stale[:6])))
            extra = ["--root", work, "--post-mode", pmode]   # 串联断点修复：指向本工具 OCR 产物
            # 引用格式切换（GB/T 7714 ↔ 《历史研究》）与繁简转换开关：透传给 postprocess 子进程
            cf = (cfg.get("CITATION_FORMAT") or "gb7714").strip() or "gb7714"
            hist = (cf == "history_research")
            # 提示词抽屉保存的覆盖：按 (输出模式, 引用格式) 选对应覆盖槽；空=用内置默认
            if pmode == "kb":
                pp_key, pp_arg = ("PROMPT_POST_HISTORY" if hist else "PROMPT_POST"), "--prompt-post"
            else:
                pp_key, pp_arg = ("PROMPT_POST_PLAIN_HISTORY" if hist else "PROMPT_POST_PLAIN"), "--prompt-post-plain"
            pp = (cfg.get(pp_key) or "").strip()
            if pp:
                extra = extra + [pp_arg, pp]
            if hist:
                extra = extra + ["--citation-format", "history_research"]
            if (cfg.get("KEEP_TRADITIONAL") or "").strip().lower() == "true":
                extra = extra + ["--keep-traditional"]
            # 重命名规则：auto=自动识别（截图改名、用户命名保留）/ always=总是改 / never=从不改
            _rp = (cfg.get("RENAME_POLICY") or "auto").strip() or "auto"
            if _rp in ("auto", "always", "never"):
                extra = extra + ["--rename-policy", _rp]
            # 来源补充：当导入文件名未携带题录信息时，用户在 ④ 面板手动补充来源串（如"大公报 1943-01-17 第2版"），
            # 结构化前注入 OCR 转录 txt 首行的「出处：」行（载体名由模型从此行读取）并覆盖提示词占位符，
            # 使 DeepSeek 生成完整 GB/T 7714 引用。仅非空字段生效，未填则完全走原逻辑。
            so = data.get("source_override") or {}
            if isinstance(so, dict):
                _so_carrier = (so.get("carrier") or "").strip()
                if so.get("text"):
                    _so_name, _so_date, _so_page, _so_issue = self._parse_source_text(so.get("text"))
                else:
                    # 兼容旧版三字段格式（name/date/page）
                    _so_name = (so.get("name") or "").strip()
                    _so_date = (so.get("date") or "").strip()
                    _so_page = (so.get("page") or "").strip()
                    _so_issue = ""
                if _so_name or _so_date or _so_page or _so_issue:
                    _parts = []
                    if _so_name: _parts.append(_so_name)
                    if _so_date: _parts.append(_so_date)
                    if _so_page: _parts.append("第" + _so_page + "版")
                    if _so_issue:
                        if "/" in _so_issue:
                            # 合刊：归一为「第X、Y期合刊」，保留'合刊'标记供模型识别
                            _parts.append("第" + _so_issue.replace("/", "、") + "期合刊")
                        else:
                            _parts.append("第" + _so_issue + "期")
                    _new_src = " ".join(_parts)
                    if os.path.isdir(work):
                        for _fn in os.listdir(work):
                            if not _fn.endswith(".txt"):
                                continue
                            if _fn.startswith("结构化_") or _fn.endswith("_题录.md") \
                                    or _fn.startswith("题录_") or _fn.startswith("ocr_"):
                                continue
                            _fp = os.path.join(work, _fn)
                            try:
                                _lines = open(_fp, encoding="utf-8").read().split("\n")
                                for _i, _ln in enumerate(_lines):
                                    if _ln.startswith("出处：") or _ln.startswith("出处:"):
                                        _lines[_i] = "出处：" + _new_src
                                        break
                                open(_fp, "w", encoding="utf-8").write("\n".join(_lines))
                            except Exception as _e:
                                print(f"[来源补充] 重写出处失败 {_fn}: {_e}")
                    if _so_name: extra = extra + ["--src-name", _so_name]
                    if _so_date: extra = extra + ["--src-date", _so_date]
                    if _so_page: extra = extra + ["--src-page", _so_page]
                    if _so_issue: extra = extra + ["--src-issue", _so_issue]
                    if _so_carrier: extra = extra + ["--src-carrier", _so_carrier]
                    print(f"[来源补充] 已注入：名称={_so_name or '-'} 日期={_so_date or '-'} 版次={_so_page or '-'} 期号={_so_issue or '-'} 载体={_so_carrier or '未声明'}")
        elif name == "group":
            mode = (data.get("mode") or "auto").strip() or "auto"
            extra = ["--mode", mode, "--src", _img_dir("cropped_hi"), "--dst", _img_dir("民国报纸OCR")]
        try:
            # --run-script 只在冻结态需要（主 exe 拦截进脚本模式）；开发态 python.exe 不识别该参数，
            # 传了会直接 unknown option 退出（returncode=2）——抽图/归档/结构化全挂。
            _cmd = [_py()]
            if getattr(sys, "frozen", False):
                _cmd.append("--run-script")
            _cmd += [script, *extra]
            r = subprocess.run(_cmd, cwd=HERE,
                               capture_output=True, text=True, env=env, timeout=900)
            res = {"ok": r.returncode == 0, "returncode": r.returncode,
                   "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}
            if r.returncode != 0:
                # 失败原因要给前端能显示的字段：取 stderr/stdout 末几行，否则用户只看到空的「失败:」
                tail = "\n".join((r.stderr or "").strip().split("\n")[-3:]) \
                    or "\n".join((r.stdout or "").strip().split("\n")[-3:])
                res["error"] = tail or ("子进程退出码 %d，无输出" % r.returncode)
            if name == "postprocess" and r.returncode == 0:
                # 打开文件夹策略：
                #  - no_open=True（单页逐版调用）：后端不打开，交由前端统一打开父目录 output/{top}（避免多子文件夹冲突）
                #  - open_parent=True：打开 output/{top} 父目录（多子文件夹场景）
                #  - 否则（跨页合并）：打开该轮子文件夹 work（维持原行为）
                if not data.get("no_open"):
                    if data.get("open_parent"):
                        open_target = top_root
                    else:
                        # 目录改名回归修复：postprocess 在流程末尾把 work 改名为文章标题/来源名，
                        # 上面解析出的 work 是改名前路径，os.path.isdir 判定必然为 False → 静默不弹窗。
                        # 此处重新走一次别名索引（.rounds.json 此刻已写好）解析出改后的真实目录。
                        open_target = _resolve_round_dir(top_root, round_dir)
                        if not os.path.isdir(open_target):
                            # 兜底一：.rounds.json 缺失/写失败时，按 .rename_map.json 里是否登记了本轮基名反查目录
                            alt = _find_round_dir_by_map(top_root, round_dir)
                            if alt:
                                open_target = alt
                    if not os.path.isdir(open_target):
                        # 兜底二：仍定位不到就退回父目录，保证结构化后一定能看到产物（总比静默不弹窗好）
                        open_target = top_root
                    if _open_folder(open_target):
                        res["opened_dir"] = open_target
            return self._json(res)
        except Exception as e:
            import traceback as _tb2
            _probe_log("subprocess EXCEPTION: %s\n%s" % (e, _tb2.format_exc()))
            return self._json({"ok": False, "error": str(e)})

    def _extract_and_group(self, data):
        """抽图 + 归档：先抽 source/ -> cropped_hi/，再把整版 PNG 归档进 output/{整版名}/
        便于溯源原始图片（与 OCR 产物 {整版名}_框N/ 同处 output/ 下）。"""
        # 防重复触发：抽图并归档是整机单任务（对整个 source/ 操作），进入前若已在跑则直接拒绝。
        global _extract_busy
        with _extract_lock:
            if _extract_busy:
                return self._json({"ok": False,
                                   "error": "抽图并归档正在进行，请勿重复点击，稍候即可。"})
            _extract_busy = True
        try:
            cfg, _ = _load_cfg()
            env = dict(os.environ)
            for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"):
                if cfg.get(k):
                    env[k] = cfg[k]
            env.setdefault("DEEPSEEK_MODEL", "deepseek-flash")
            # 把数据目录注入子进程，确保 extract_original.py / postprocess.py 等子脚本
            # 在打包态下把产物写到「文档/墨痕数据」而不是 exe 目录。
            env["MOHEN_DATA_DIR"] = RUNTIME_DIR
            # 把脚本目录(HERE，冻结态为 _MEIPASS/scripts)加进子进程 PYTHONPATH，
            # 让 extract_original.py 能 import 到 stop_flag（否则子进程找不到模块会崩）。
            env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
            src_dir = _img_dir("source")
            # 自愈：source/ 不存在时先创建，避免「不存在」阻断；空目录再往下抽图仍会失败，届时给出明确提示
            os.makedirs(src_dir, exist_ok=True)
            if not any(os.listdir(src_dir)):
                return self._json({"ok": False,
                                   "error": "来源文件夹 source/ 已创建但仍为空。请先用卡片①的「导入文件」放入要抽图的 PDF / 图片，再点「抽图并归档」。"})
            src_files = [f for f in os.listdir(src_dir)
                         if os.path.splitext(f)[1].lower() in IMG_EXTS | {".pdf"}]
            if not src_files:
                return self._json({"ok": False,
                                   "error": "source/ 中没有可抽图的 PDF / 图片（支持 png/jpg/pdf 等）。请先用「导入文件」放入文件。"})
            out = {"ok": False, "steps": [], "stdout": "", "stderr": ""}
            try:
                # 1) 抽图：source/ -> cropped_hi/（绝对路径传给脚本，对齐 DATA_DIR，避免脚本回退到 exe 目录）
                #    --run-script 仅冻结态需要，开发态 python.exe 不识别（传了 returncode=2 直接失败）
                _c1 = [_py()]
                if getattr(sys, "frozen", False):
                    _c1.append("--run-script")
                _c1 += [os.path.join(HERE, "extract_original.py"),
                        "--src", src_dir, "--dst", _img_dir("cropped_hi")]
                r1 = subprocess.run(_c1,
                                    cwd=RUNTIME_DIR, capture_output=True, text=True, env=env, timeout=600)
                out["steps"].append({"step": "extract", "ok": r1.returncode == 0,
                                     "returncode": r1.returncode})
                out["stdout"] += r1.stdout[-4000:] + "\n"
                out["stderr"] += r1.stderr[-2000:] + "\n"
                if r1.returncode != 0:
                    out["ok"] = False
                    out["error"] = "抽图步骤失败，已停止"
                    return self._json(out)

                # 2) 归档：把 cropped_hi/ 下每个整版 PNG 平铺复制到 output/ 根目录（溯源原始图片）
                #    不按整版建子目录，避免 output/ 下散落大量 {篇名}/ 目录。
                out_root = _img_dir("output")
                cropped = _img_dir("cropped_hi")
                os.makedirs(out_root, exist_ok=True)
                os.makedirs(cropped, exist_ok=True)   # 防御：子进程若因异常未创建，则本进程兜底
                pngs = sorted(f for f in os.listdir(cropped)
                              if f.lower().endswith((".png", ".jpg", ".jpeg")))
                archived = 0; skipped = 0
                for png in pngs:
                    dst_png = os.path.join(out_root, png)
                    if os.path.exists(dst_png):
                        skipped += 1
                        continue
                    shutil.copy2(os.path.join(cropped, png), dst_png)
                    archived += 1
                out["steps"].append({"step": "archive", "ok": True,
                                     "returncode": 0})
                # 打包态（windowed exe）下子进程 stdout 被丢弃，前端只能看到 returncode。
                # 把子脚本落盘的抽取日志尾部附进来，让 [no image]/页级错误可见。
                _exlog = os.path.join(RUNTIME_DIR, "extract_original.log")
                if os.path.exists(_exlog):
                    try:
                        tail = open(_exlog, encoding="utf-8", errors="replace").read()[-2000:]
                        out["stdout"] += "\n[extract log]\n" + tail + "\n"
                    except Exception:
                        pass
                out["stdout"] += (f"\n[archive] 归档整版原图到 output/（平铺，不建子目录）：新增 {archived} 张，"
                                  f"跳过已存在 {skipped} 张。\n")
                if archived == 0 and skipped == 0:
                    # source 有文件但本次一张图都没产出：明确报错，不让用户误以为成功
                    out["ok"] = False
                    out["error"] = ("抽图结果为 0 张：PDF/图片中未抽出任何图像。常见原因："
                                    "① 文字型 PDF（页面为矢量文字，无内嵌扫描图）；"
                                    "② PDF 为 AES 加密但打包环境缺 cryptography（2.0.5 起已内置）；"
                                    "③ 图像编码不支持。详情见上方抽取日志，建议将 PDF 页面导出为图片后重新导入。")
                    return self._json(out)
                out["ok"] = True
                return self._json(out)
            except Exception as e:
                out["error"] = str(e)
                return self._json(out)
        finally:
            _extract_busy = False

    def _save_cfg(self, data):
        allowed = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                   "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                   "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                   "KANDIAN_API_KEY", "KANDIAN_EMAIL", "KANDIAN_MODEL", "KANDIAN_BASE_URL", "KANDIAN_DET_MODE",
                   "KANDIAN_HP_ORDER",
                   "BAIDU_API_KEY", "BAIDU_SECRET_KEY", "BAIDU_MODEL", "BAIDU_BASE_URL", "BAIDU_AUTO_FALLBACK",
                   "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                   "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN", "PROMPT_POST_HISTORY", "PROMPT_POST_PLAIN_HISTORY",
                   "CITATION_FORMAT", "KEEP_TRADITIONAL", "EYE_CARE",
                   "DATA_DIR", "RENAME_POLICY")
        p = os.path.join(CONFIG_DIR, "box_config.json")
        merged = {}
        if os.path.exists(p):
            try:
                merged.update(json.load(open(p, encoding="utf-8")))
            except Exception:
                pass
        for k in allowed:
            if k in data:
                v = (data.get(k) or "").strip()
                if v:
                    merged[k] = v
                else:
                    merged.pop(k, None)
        merged = {k: v for k, v in merged.items() if v}
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            return self._json({"ok": True, "path": p,
                               "config": _cfg_status(merged, set(merged.keys()))})
        except Exception as e:
            return self._json({"ok": False, "error": str(e), "path": p})

    def _read_auto_update(self):
        p = os.path.join(CONFIG_DIR, "box_config.json")
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("AUTO_UPDATE", False)
                return {"auto_update": bool(raw),
                        "skip_version": data.get("UPDATE_SKIP_VERSION") or None}
        except Exception:
            pass
        return {"auto_update": False, "skip_version": None}

    def _set_auto_update(self, data):
        p = os.path.join(CONFIG_DIR, "box_config.json")
        cur = {}
        if os.path.exists(p):
            try:
                cur = json.load(open(p, encoding="utf-8"))
            except Exception:
                cur = {}
        if "auto_update" in data:
            cur["AUTO_UPDATE"] = bool(data.get("auto_update", False))
        sv = data.get("skip_version")
        if sv:
            cur["UPDATE_SKIP_VERSION"] = str(sv)
        elif "skip_version" in data and sv in (None, ""):
            cur.pop("UPDATE_SKIP_VERSION", None)
        # 清理空值保持文件整洁，但保留布尔（含 False）键
        cur = {k: v for k, v in cur.items() if v not in (None, "")}
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            return self._json({"ok": True,
                               "auto_update": bool(cur.get("AUTO_UPDATE", False)),
                               "skip_version": cur.get("UPDATE_SKIP_VERSION") or None})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)})


# ---------- 前端 HTML（套 modern 的 CSS 外壳，内嵌 canvas 框选核心） ----------
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>墨痕 · 近代报刊转录助手</title>
<style>
  :root {
    --bg:#f5f6f8; --card:#fff; --bd:#e2e5ea; --ink:#1f2329; --mut:#8a9099;
    --pri:#2f6fed; --ok:#1f9d55; --err:#d23f3f; --warn:#b86e00;
    --logbg:#f8fafc; --logink:#1f2328;
  }
  /* 护眼模式：柔和豆沙绿系，仅作用于 UI 外壳（var 变量区）；画布区为固定色不受影响 */
  .eye-care {
    --bg:#D7E8D5; --card:#EAF3E8; --bd:#B9D4B4; --ink:#274534; --mut:#5C7A66;
    --logbg:#EAF3E8; --logink:#274534;
  }
  .eye-care input, .eye-care select, .eye-care textarea { background:#F2F8F1; color:var(--ink); }
  .eye-care .src-list-wrap { background:#F2F8F1; }
  .eye-care .src-list .item:hover { background:#DCEBD9; }
  .eye-care .src-list .item .ord { background:#cfe0cc; color:#274534; }
  .eye-care .src-list .item .ops .mv { background:#EAF3E8; border-color:#cfe0cc; color:#274534; }
  .eye-care .src-list .item .ops .mv:hover:not(:disabled) { background:#DCEBD9; }
  .eye-care .left, .eye-care #canvasWrap, .eye-care #canvasHost { background:#D7E8D5; }
  .eye-care #cv { background:#F2F8F1; }
  .eye-care #hint { background:#EAF3E8; }
  .eye-care #toolbar { background:#EAF3E8 !important; }
  .eye-care .gear:hover, .eye-care .logbtn:hover { background:#DCEBD9; }
  .gear.eyecare-on { background:#2E7D32; color:#fff; border-color:#2E7D32; }
  .gear.eyecare-on:hover { filter:brightness(.95); }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family: -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; font-size:14px; }
  .wrap { display:flex; height:calc(100vh - 56px); overflow:hidden; }
  .left { flex:1 1 auto; display:flex; flex-direction:column; min-width:0; min-height:0; background:#eef1f5; }
  /* 缩放栏与翻页栏必须固定在画布视口上，不能随内容滚动：
     此前它们带 float 放在 #canvasWrap（滚动容器）内，横向滚动时会被一起推走（放大后跑飞）。
     现改为在外层 #canvasHost（定位参照、不滚动）里绝对定位，滚动与缩放都影响不到它们。 */
  #canvasHost { position:relative; flex:1 1 auto; min-height:0; display:flex; background:#eef1f5; }
  #canvasWrap { flex:1 1 auto; min-width:0; min-height:0; overflow:auto; background:#eef1f5; }
  #zoomBar { position:absolute; top:12px; left:12px; z-index:10;
             display:flex; align-items:center; gap:6px; background:rgba(255,255,255,0.95);
             padding:6px 10px; border-radius:8px; border:1px solid var(--bd); box-shadow:0 2px 8px rgba(0,0,0,.08); }
  #zoomBar span { font-size:12.5px; color:var(--ink); min-width:44px; text-align:center; font-weight:600; }
  #zoomBar button { min-width:auto; padding:5px 10px; }
  #pageBar { position:absolute; top:12px; right:12px; z-index:10;
             display:flex; align-items:center; gap:6px; background:rgba(255,255,255,0.95);
             padding:6px 10px; border-radius:8px; border:1px solid var(--bd); box-shadow:0 2px 8px rgba(0,0,0,.08); }
  #pageBar button { min-width:auto; padding:5px 10px; }
  #pageBar .pg { font-size:12.5px; color:var(--ink); text-align:center; font-weight:600; white-space:nowrap; line-height:1.25; }
  #pageBar .pg small { display:block; font-size:10.5px; color:var(--mut); font-weight:400; margin-top:1px; }
  #cv { display:block; margin:16px auto; cursor:crosshair; max-width:none; background:#fff;
        box-shadow:0 4px 16px rgba(0,0,0,.08); border-radius:8px; }
  #cv.placeholder { background:#f8fafc; }
  #hint { padding:10px 16px; color:var(--mut); font-size:12.5px; background:#fff;
          border-top:1px solid var(--bd); line-height:1.7; }
  .right { flex:0 0 500px; overflow:auto; padding:16px; background:var(--bg); border-left:1px solid var(--bd); }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:12px;
          padding:16px; margin-bottom:14px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
  .card h2 { font-size:14px; margin:0 0 12px; font-weight:600; color:var(--ink); }
  label { display:block; font-size:13px; color:var(--mut); margin:10px 0 5px; }
  input, select { width:100%; padding:8px 11px; border:1px solid var(--bd); border-radius:8px;
                  font-size:13px; background:#fafbfc; box-sizing:border-box; color:var(--ink); }
  input:focus, select:focus { outline:2px solid #cfe0ff; border-color:var(--pri); }
  .row { display:flex; gap:10px; }
  .row > div { flex:1; min-width:0; }
  .btns { display:flex; flex-wrap:wrap; gap:10px; }
  button { cursor:pointer; border:1px solid transparent; border-radius:8px; padding:9px 14px;
           font-size:13px; font-weight:600; color:#fff; background:var(--pri); min-width:90px; }
  button.sm { min-width:auto; }
  button:hover { filter:brightness(.96); }
  button.sec { background:#f3f4f6; color:var(--ink); border-color:var(--bd); }
  button.sec:hover { background:#e9eaed; }
  button.ghost { background:#f8fafc; color:var(--ink); border-color:var(--bd); }
  button.ghost:hover { background:#eef1f5; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.run { background:var(--ok); }
  button.stop { background:var(--err); }
  button.purple { background:#7c3aed; }
  button.gray { background:#475569; }
  button.sm { padding:5px 10px; font-size:12px; }

  /* 顶部栏：与 modern 启动器一致 */
  .topbar { display:flex; align-items:center; justify-content:space-between; gap:12px;
            padding:12px 18px; background:var(--card); border-bottom:1px solid var(--bd); height:56px; }
  .topbar h1 { font-size:15px; margin:0; font-weight:600; color:var(--ink); }
  .topbar .cfg { font-size:12px; color:var(--mut); }
  .topbar .cfg b { color:var(--pri); }
  .topbar .cfg .warn { color:var(--err); font-weight:600; }
  .gear { background:transparent; border:1px solid var(--bd); padding:8px 12px;
          border-radius:8px; color:var(--ink); display:inline-flex; align-items:center;
          gap:6px; font-size:13px; font-weight:600; height:32px; }
  .gear:hover { background:#eef1f5; }
  .gear svg { width:16px; height:16px; display:block; }
  .logbtn { background:transparent; border:1px solid var(--bd); padding:8px 12px;
            border-radius:8px; color:var(--ink); display:inline-flex; align-items:center;
            gap:6px; font-size:13px; font-weight:600; height:32px; }
  .logbtn:hover { background:#eef1f5; }
  .logbtn.active { background:#eef1f5; border-color:var(--pri); color:var(--pri); }
  .headright { display:flex; gap:10px; align-items:center; }
  .headright button { white-space:nowrap; flex-shrink:0; }

  /* 主页顶部提示：建议关闭网络代理 / VPN（可关闭，记忆在 localStorage）
     高度由 JS 实测回填到 .wrap，避免行数变化时撑破视口 */
  #vpnTip { display:flex; align-items:center; gap:10px; padding:7px 18px;
            background:#fff8e6; border-bottom:1px solid #f0d9a8; color:#8a5a00;
            font-size:12.5px; line-height:1.55; }
  #vpnTip .vpn-txt { flex:1 1 auto; }
  #vpnTip b { color:#8a4b00; font-weight:700; }
  #vpnTip .vpn-x { flex:0 0 auto; background:transparent; border:1px solid #eccf94;
                   color:#8a5a00; border-radius:6px; padding:2px 10px; font-size:12px;
                   font-weight:600; cursor:pointer; }
  #vpnTip .vpn-x:hover { background:#fdf0d2; }
  .eye-care #vpnTip { background:#EDF6E4; border-bottom-color:#C6DEB6; color:#3F6B36; }
  .eye-care #vpnTip b { color:#2F5B2A; }
  .eye-care #vpnTip .vpn-x { border-color:#C6DEB6; color:#3F6B36; }
  .eye-care #vpnTip .vpn-x:hover { background:#DCEDD0; }

  /* 流程提示 */
  .hint .stage { display:inline-flex; align-items:center; gap:5px; margin-right:14px;
                 white-space:nowrap; font-size:12.5px; color:var(--mut); }
  .flow-num { display:inline-flex; align-items:center; justify-content:center; width:1.5em; height:1.5em;
              border-radius:50%; background:#e8eefd; color:var(--pri); font-weight:700; font-size:11px; }
  .sub { color:var(--mut); margin:10px 0 0; font-size:12px; line-height:1.6; }
  .version-row { align-items:center; gap:12px; flex-wrap:wrap; }
  .version-row .sub { margin:0; line-height:1.4; display:inline-flex; align-items:center; }
  .version-row button { display:inline-flex; align-items:center; justify-content:center; }
  .rdo { display:inline-flex; align-items:center; gap:4px; font-size:13px; white-space:nowrap; }
  .rdo input { margin:0; width:auto; min-width:auto; flex-shrink:0; vertical-align:middle; }
  .btns .sub { margin:0; font-size:12px; }

  /* 日志 */
  .log-head { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:10px; }
  .log-head h2 { margin:0; }
  .log-tools { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .log-search { width:140px; padding:5px 9px; border:1px solid var(--bd); border-radius:6px; font-size:12px; }
  .logbox { background:var(--logbg); color:var(--logink); border:1px solid var(--bd); border-radius:10px;
            padding:10px 12px; height:220px; overflow:auto; margin:0;
            font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; line-height:1.6; }
  .logline { display:flex; gap:8px; padding:3px 4px; align-items:flex-start; border-radius:4px; }
  .logline + .logline { border-top:1px solid #eef0f2; }
  .logline .ts { flex:0 0 auto; color:#9ca3af; font-size:11px; }
  .logline .lvtag { flex:0 0 auto; font-weight:600; font-size:10px; padding:1px 6px; border-radius:3px; color:#fff; }
  .logline .msg { flex:1 1 auto; white-space:pre-wrap; word-break:break-word; }
  .logline.lvl-cmd .lvtag { background:#2563eb; } .logline.lvl-cmd .msg { color:#1d4ed8; }
  .logline.lvl-ok .lvtag { background:#16a34a; } .logline.lvl-ok .msg { color:#15803d; }
  .logline.lvl-error .lvtag { background:#dc2626; } .logline.lvl-error .msg { color:#b91c1c; }
  .logline.lvl-warn .lvtag { background:#ea580c; } .logline.lvl-warn .msg { color:#c2410c; }

  /* 框列表 */
  #boxList .box { display:grid; grid-template-columns:24px auto minmax(0, 1fr); gap:4px 6px;
                  align-items:center; padding:6px 8px; border:1px solid var(--bd); border-radius:6px; margin-bottom:5px;
                  background:#fafbfc; overflow:hidden; }
  #boxList .box.sel { border-color:var(--pri); background:#eff6ff; }
  #boxList .bx-num { width:24px; height:24px; border-radius:4px; display:flex; align-items:center; justify-content:center;
                     font-size:11px; font-weight:700; color:#fff; flex-shrink:0; }
  #boxList .bx-page { font-size:10px; color:var(--mut); background:#eef1f4; border:1px solid var(--bd); border-radius:4px; padding:1px 5px; max-width:90px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #boxList .bx-main { display:flex; align-items:center; gap:4px; min-width:0; flex-wrap:wrap; }
  #boxList .box select { flex:1 1 70px; min-width:56px; max-width:90px; padding:3px 4px; font-size:12px; }
  #boxList .box input.grp { flex:0 0 50px; width:50px; padding:3px 4px; font-size:12px; border:1px solid var(--bd); border-radius:4px; }
  #boxList .bx-group { font-size:11px; color:var(--mut); flex:0 1 auto; min-width:0; max-width:100px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #boxList .bx-ops { grid-column:1 / -1; display:flex; gap:4px; margin-top:2px; }
  #boxList .bx-ops .mini { padding:2px 6px; font-size:11px; flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #resultList .card { border:1px solid var(--bd); border-radius:6px; padding:8px; margin-bottom:8px; min-width:0; transition:border-color .12s, box-shadow .12s; }
  #resultList .card:hover { border-color:var(--pri); box-shadow:0 1px 4px rgba(37,99,235,.18); }
  #resultList .card.flash { border-color:var(--pri); box-shadow:0 0 0 2px rgba(37,99,235,.22); }
  /* 识别失败的卡片：边框与提示条标红，明确区分「没读到」与「读到了但内容不对」 */
  #resultList .card.fail { border-color:#d9534f; background:#fff7f6; }
  #resultList .card.fail:hover { border-color:#b52b25; box-shadow:0 1px 4px rgba(181,43,37,.18); }
  #resultList .card.fail .failbar { font-size:11px; line-height:1.6; color:#a5342c; background:#fdecea;
    border:1px solid #f5c6c2; border-radius:4px; padding:4px 6px; margin-bottom:6px; word-break:break-all; }
  #resultList .card .hd .loc, #resultList .card .hd .exp { flex-shrink:0; padding:1px 6px; font-size:11px; line-height:1.5; border:1px solid var(--bd); border-radius:4px; background:#fff; color:var(--mut); cursor:pointer; font-family:inherit; }
  #resultList .card .hd .loc:hover, #resultList .card .hd .exp:hover { border-color:var(--pri); color:var(--pri); }
  #resultList .card .hd { display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:12px; color:var(--mut); margin-bottom:4px; min-width:0; }
  #resultList .card .hd .title { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #resultList .card .hd .meta { flex-shrink:0; white-space:nowrap; }
  /* 校对框默认高度：按窗口高度自适应（约 17 行），小屏也有 180px 垫底。
     此前固定 90px（约 5 行），长文只能靠框内小滚动条翻，频繁滚轮查看很费劲。 */
  #resultList textarea { width:100%; min-height:max(180px, 30vh); font-family:"Microsoft YaHei",monospace; font-size:var(--rs-fs,12px);
                         border:1px solid var(--bd); border-radius:4px; padding:6px; resize:vertical; }
  /* 单击画布框时的「片段标记」：只用底色标出该框对应的那段文字，不占用选区，
     所以点进去改字时标记不会消失，也不与你自己的选字冲突。
     一级走 CSS Custom Highlight API（Chromium 152+ 的 createValueRange），
     二级降级为叠在文本框上的半透明色带（见 paintHlBands）。 */
  ::highlight(mohen-boxfocus) { background-color:#fde68a; color:#111827; }
  .eye-care ::highlight(mohen-boxfocus) { background-color:#cfe3c8; color:#1f3a2b; }
  #resultList .ta-wrap { position:relative; }
  #resultList .ta-wrap .hl-layer { position:absolute; inset:0; overflow:hidden; pointer-events:none; border-radius:4px; }
  #resultList .ta-wrap .hl-layer .hl-inner { position:absolute; top:0; left:0; width:100%; }
  #resultList .ta-wrap .hl-layer .hl-band { position:absolute; background:rgba(253,230,138,.55); border-radius:2px; }
  .eye-care #resultList .ta-wrap .hl-layer .hl-band { background:rgba(176,214,168,.65); }
  .srcname { font-weight:600; color:var(--pri); }
  .kbd { font-family:ui-monospace,monospace; background:#e2e8f0; padding:1px 4px; border-radius:3px; font-size:11px; }

  /* 整版原图多选列表 */
  .src-list-wrap { border:1px solid var(--bd); border-radius:8px; padding:4px 5px 5px 1px; background:#fafbfc; }
  .src-list-head { display:flex; align-items:center; height:26px; gap:3px; margin-bottom:3px; padding:0; }
  .src-list-head .chk { display:inline-flex; align-items:center; justify-content:center; height:26px; gap:3px; margin:0; padding:0; font-size:0; color:var(--mut); cursor:pointer; white-space:nowrap; user-select:none; }
  .src-list-head .chk input { width:13px; height:13px; margin:0; flex-shrink:0; vertical-align:middle; }
  .src-list-head .chk span { font-size:12px; line-height:26px; vertical-align:middle; }
  .src-list-head .src-head-btn { display:inline-flex; align-items:center; justify-content:center; height:26px; padding:0 8px; font-size:12px; line-height:1; min-width:auto; border-radius:6px; font-weight:600; text-align:center; box-sizing:border-box; color:#fff; background:var(--pri); border:1px solid transparent; white-space:nowrap; }
  .src-list-head .src-head-btn.sec { background:#f3f4f6; color:var(--ink); border-color:var(--bd); }
  .src-list-head .src-head-btn.sec:hover { background:#e9eaed; }
  .src-list-head .src-head-btn.gray { background:#475569; }
  .src-list-head .src-head-btn.stop { background:var(--err); }
  .src-list { max-height:160px; overflow:auto; display:flex; flex-direction:column; gap:1px; }
  .src-list .item { display:flex; align-items:center; gap:6px; padding:3px 6px; border-radius:4px; font-size:12px; cursor:default; }
  .src-list .item input { width:13px; height:13px; margin:0; padding:0; flex-shrink:0; cursor:pointer; }
  .src-list .item .ord { flex-shrink:0; min-width:18px; height:18px; line-height:18px; text-align:center; border-radius:9px; background:#e2e8f0; color:#475569; font-size:11px; font-weight:600; }
  .src-list .item .nm { flex:1; min-width:0; word-break:break-all; line-height:1.4; }
  .src-list .item .ops { flex-shrink:0; display:inline-flex; gap:2px; }
  .src-list .item .ops .mv { width:20px; height:18px; line-height:1; padding:0; font-size:12px; border:1px solid var(--bd); border-radius:4px; background:#fff; color:#334155; cursor:pointer; }
  .src-list .item .ops .mv:hover:not(:disabled) { background:#eef1f5; }
  .src-list .item .ops .mv:disabled { color:#cbd5e1; cursor:default; border-color:#eef1f5; }
  .src-list .item:hover { background:#eef1f5; }
  .src-list .item { cursor:grab; }
  .src-list .item:active { cursor:grabbing; }
  .src-list .item.dragging { opacity:.5; background:#dbeafe; outline:1px dashed var(--pri); }
  .src-list .item.sel { background:#eff6ff; }
  .src-list .empty { color:var(--mut); font-size:12px; padding:2px 6px; }
  .src-list-head .fold-toggle { display:inline-flex; align-items:center; justify-content:center; width:8px; height:26px; min-width:0; min-height:0; flex:0 0 8px; padding:0; margin:0 4px 0 0; border:none; outline:none; background:transparent; background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='8' viewBox='0 0 8 8'%3E%3Cpath d='M1 1 L7 1 L4 7 z' fill='%2364748b'/%3E%3C/svg%3E"); background-size:8px 8px; background-repeat:no-repeat; background-position:center; cursor:pointer; transition:transform .12s ease; }
  .src-list-head .fold-toggle:hover { background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='8' viewBox='0 0 8 8'%3E%3Cpath d='M1 1 L7 1 L4 7 z' fill='%23334155'/%3E%3C/svg%3E"); }
  .src-list-wrap.collapsed .src-list { display:none; }
  .src-list-wrap.collapsed .fold-toggle { transform:rotate(-90deg); }
  .src-list-head .cnt { display:inline-flex; align-items:center; height:26px; font-size:11px; color:var(--mut); white-space:nowrap; padding-right:2px; }

  /* 提示词抽屉 */
  .prompt-area { width:100%; min-height:240px; resize:vertical; box-sizing:border-box;
    padding:10px 12px; border:1px solid var(--bd); border-radius:8px; font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12px; line-height:1.6; color:var(--ink); background:#fff; white-space:pre; overflow:auto; }
  .prompt-area:focus { outline:2px solid #cfe0ff; border-color:var(--pri); }
  .reset { float:right; font-size:11px; font-weight:500; color:var(--pri); cursor:pointer; user-select:none; }
  .reset:hover { text-decoration:underline; }
  .mode-tabs { display:flex; gap:14px; margin-bottom:8px; font-size:12.5px; color:var(--ink); }
  .mode-tabs label { display:inline-flex; align-items:center; gap:5px; cursor:pointer; user-select:none; }
  .mode-tabs input { width:13px; height:13px; margin:0; }

  /* 设置抽屉 */
  .drawer-mask { position:fixed; inset:0; background:rgba(15,18,23,.35); opacity:0; pointer-events:none; transition:opacity .25s; z-index:50; }
  .drawer-mask.open { opacity:1; pointer-events:auto; }
  .drawer { position:fixed; top:0; right:0; height:100vh; width:520px; max-width:96vw;
            background:var(--card); border-left:1px solid var(--bd); box-shadow:-8px 0 24px rgba(0,0,0,.08);
            transform:translateX(100%); transition:transform .25s; display:flex; flex-direction:column; z-index:60; }
  .drawer.open { transform:translateX(0); }
  .drawer-head { display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--bd); }
  .drawer-head h2 { margin:0; font-size:13px; font-weight:600; }
  .drawer-close { background:transparent; border:none; cursor:pointer; color:var(--mut); padding:0;
                  width:28px; height:28px; border-radius:6px; font-size:16px; line-height:1;
                  display:inline-flex; align-items:center; justify-content:center; }
  .drawer-close:hover { background:#eef1f5; color:var(--ink); }
  .drawer-body { flex:1; overflow:auto; padding:16px 18px; }
  .drawer-foot { padding:12px 18px; border-top:1px solid var(--bd); display:flex; gap:10px; align-items:center; }
  .set-group { background:#fafbfc; border:1px solid var(--bd); border-radius:8px; padding:10px 12px; margin-bottom:10px; }
  .set-group h3 { margin:0 0 10px; font-size:13px; font-weight:600; }
  .set-group label { display:block; font-size:12px; color:var(--mut); margin:0 0 4px; }
  .set-group input, .set-group select { padding:6px 10px; font-size:13px; line-height:1.4; }
  .set-group .row { margin-bottom:8px; }
  .set-group .row:last-child { margin-bottom:0; }
  /* 字号统一：抽屉内说明/标签/复选框行一律 12px（全局 .switch-row 是 13.5px，在设置抽屉里偏大） */
  .set-group .switch-row { font-size:12px; color:var(--mut); }
  .set-group label.sub { font-size:12px; }
  .key-field { display:flex; flex-direction:column; }
  .key-field .input-wrap { position:relative; }
  .key-field input { padding-right:38px !important; }
  .key-field .eye { position:absolute; right:8px; top:50%; transform:translateY(-50%);
                    cursor:pointer; user-select:none; color:var(--mut); display:flex; align-items:center; }
  .key-field .eye:hover { color:var(--pri); }
  .key-field .eye svg { display:block; }
  .key-field input::-ms-reveal { display:none; }
  .key-field input::-webkit-credentials-auto-fill-button { visibility:hidden; display:none !important; }
  /* API Key 用 type=text + CSS 遮挡：避免 macOS WebKit 把 password 字段识别为系统凭据而拦截粘贴；眼睛按钮切换 .secret 类 */
  .key-field input.secret { -webkit-text-security: disc; text-security: disc; }

  /* 用量统计抽屉：筛选 + 表格 */
  .filter-row { display:grid; grid-template-columns:1fr 1fr auto; gap:12px; align-items:end; }
  .filter-row label { display:flex; flex-direction:column; gap:4px; font-size:12px; }
  .filter-row select { width:100%; padding:5px 8px; border:1px solid var(--bd); border-radius:6px;
                       font-size:12px; background:#fff; box-sizing:border-box; }
  .filter-row .ghost { align-self:end; }
  .usage-table { width:100%; border-collapse:collapse; font-size:12px; margin-top:10px; }
  .usage-table th, .usage-table td { padding:6px 8px; border-bottom:1px solid var(--bd); }
  .usage-table th { color:var(--mut); font-weight:500; background:#fafbfc; text-align:right; }
  .usage-table th:first-child, .usage-table th:nth-child(2),
  .usage-table td:first-child, .usage-table td:nth-child(2) { text-align:left; }
  .usage-table td.num { text-align:right; font-variant-numeric: tabular-nums; }
  .usage-table td.dim { color:var(--mut); }

  /* 日志底部抽屉（bottom sheet，从底部滑出，不遮挡主区操作） */
  .log-sheet { position:fixed; left:0; right:0; bottom:0; height:42vh; max-height:420px;
               background:var(--card); border-top:1px solid var(--bd);
               box-shadow:0 -8px 24px rgba(0,0,0,.10); transform:translateY(100%);
               transition:transform .28s ease; display:flex; flex-direction:column; z-index:60; }
  .log-sheet.open { transform:translateY(0); }
  .log-sheet .sheet-head { display:flex; align-items:center; justify-content:space-between; gap:12px;
                           padding:12px 18px; border-bottom:1px solid var(--bd); flex:0 0 auto; }
  .log-sheet .sheet-head h2 { margin:0; font-size:13px; font-weight:600; }
  .log-sheet .logbox { flex:1 1 auto; height:auto; border:none; border-radius:0; margin:0; }
  .sheet-close { background:transparent; border:none; cursor:pointer; color:var(--mut); padding:0;
                  width:28px; height:28px; border-radius:6px; font-size:16px; line-height:1;
                  display:inline-flex; align-items:center; justify-content:center; }
  .sheet-close:hover { background:#eef1f5; color:var(--ink); }

  @media (max-width: 768px) {
    .drawer { width:100vw; }
    .right { flex:0 0 100vw; }
  }
  /* 结果编辑框右键菜单 */
  #ctxMenu { position:fixed; z-index:9999; background:var(--card); border:1px solid var(--bd); border-radius:6px; box-shadow:0 4px 12px rgba(0,0,0,.15); padding:4px 0; display:none; min-width:110px; }
  #ctxMenu button { display:block; width:100%; text-align:left; padding:6px 14px; border:none; background:transparent; cursor:pointer; font-size:12px; color:var(--ink); }
  #ctxMenu button:hover { background:var(--pri); color:#fff; }
  #ctxMenu hr { border:none; border-top:1px solid var(--bd); margin:4px 0; }
  /* 自动更新提示弹窗 */
  .modal-mask{ position:fixed; inset:0; background:rgba(0,0,0,.45); display:none; align-items:center; justify-content:center; z-index:1200; }
  .modal-mask.open{ display:flex; }
  .modal-card{ background:#fff; border-radius:12px; padding:22px 24px; max-width:380px; width:calc(100% - 48px); box-shadow:0 16px 48px rgba(0,0,0,.28); }
  .modal-title{ font-size:16px; font-weight:700; margin-bottom:10px; color:var(--ink); }
  .modal-body{ font-size:14px; color:var(--ink); margin-bottom:18px; line-height:1.55; }
  .modal-foot{ display:flex; justify-content:flex-end; gap:10px; }
  .switch-row{ display:inline-block; font-size:13.5px; color:var(--ink); cursor:pointer; line-height:1.4; }
  .switch-row input{ width:14px; height:14px; margin:0 6px 0 0; vertical-align:middle; }
  .switch-row span{ vertical-align:middle; line-height:1.4; }
</style>
</head>
<body>
<div class="topbar">
  <h1>墨痕 · 近代报刊转录助手</h1>
  <div class="headright">
    <span class="cfg" id="cfgLine">加载配置中…</span>
    <button class="logbtn" id="logBtn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
      日志
    </button>
    <button class="gear" id="promptBtn" aria-label="提示词" title="提示词（OCR / 结构化可随时修改，保存后即时生效）">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h10M4 17h16M17 9.5l2.5 2.5L17 14.5"/></svg>
      提示词
    </button>
    <button class="gear" id="usageBtn" aria-label="用量统计" title="用量统计（按模型 / 阶段聚合 token 消耗）">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 14a9 9 0 0 1 18 0"/><line x1="12" y1="14" x2="16" y2="9"/><circle cx="12" cy="14" r="1" fill="currentColor"/><line x1="3" y1="20" x2="21" y2="20"/></svg>
      用量统计
    </button>
    <button class="gear" id="eyeBtn" aria-label="护眼模式" title="护眼模式（柔和豆沙绿，不改动画布图片）">👁 护眼</button>
    <button class="gear" id="gearBtn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      设置
    </button>
  </div>
</div>
<div id="vpnTip">
  <div class="vpn-txt"><b>识别前请先关闭网络代理 / VPN</b>（含图书馆 WebVPN 等网关）。整版裁切图单张可达数 MB，经代理转发时容易被中途掐断，表现为「连接被远端中断 / 连接超时」；改用直连后重试即可。</div>
  <button class="vpn-x" id="vpnTipClose" title="不再显示这条提示">知道了</button>
</div>
<div class="wrap">
  <div class="left">
      <div id="toolbar" style="display:flex; align-items:center; gap:8px; margin-bottom:8px; padding:6px 8px; background:#f1f5f9; border-radius:8px; font-size:13px;">
        <span style="color:#475569; font-weight:600;">交互模式：</span>
        <button id="modeSelect" class="tb-btn" data-mode="select" style="padding:4px 12px; border:1px solid #cbd5e1; border-radius:6px; background:#2563eb; color:#fff; cursor:pointer;">框选</button>
        <button id="modePan" class="tb-btn" data-mode="pan" style="padding:4px 12px; border:1px solid #cbd5e1; border-radius:6px; background:#fff; color:#334155; cursor:pointer;">拖动(平移)</button>
      </div>
      <div id="canvasHost">
      <div id="canvasWrap">
      <canvas id="cv" width="800" height="600"></canvas>
    </div>
      <div id="zoomBar">
        <button id="zoomOut" class="sm sec" title="缩小">−</button>
        <span id="zoomPct" title="双击恢复「适应窗口」">100%</span>
        <button id="zoomIn" class="sm sec" title="放大">+</button>
        <button id="rotBtn" class="sm sec" title="顺时针旋转 90°（旋转后该版已画框会清空）">⟳</button>
      </div>
      <div id="pageBar">
        <button class="sm sec" id="prevPage" title="上一版">◀</button>
        <span id="pageNav" class="pg"><span id="pageCounter">第 0 / 0 版</span><small id="crossStatus" style="display:none">跨页 · 已载入 0 版</small></span>
        <button class="sm sec" id="nextPage" title="下一版">▶</button>
      </div>
      </div>
    <div id="hint">
      <span class="stage"><span class="flow-num">①</span>导入文件 → 抽图并归档</span>
      <span class="stage"><span class="flow-num">②</span>载入图片 → 框选</span>
      <span class="stage"><span class="flow-num">③</span>识别全部</span>
      <span class="stage"><span class="flow-num">④</span>校对 → 保存 → 来源补充（可选） → 结构化</span>
    </div>
  </div>

  <div class="right">
    <div class="card">
      <h2>① 抽图与归档</h2>
      <div id="sourceBox">
        <div class="src-list-wrap">
          <div class="src-list-head">
            <button class="fold-toggle" type="button" title="折叠 / 展开列表"></button>
            <label class="chk"><input type="checkbox" id="sourceSelAll"> <span>全选</span></label>
            <button class="src-head-btn gray" id="importSource" title="导入 PDF / 图片到 source/">导入文件</button>
            <input type="file" id="importSourceIn" multiple accept=".png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.gif,.pdf" style="display:none;">
            <button class="src-head-btn gray" id="importClipboard" title="从剪贴板粘贴截图到 source/（请先用截图工具复制图片）">粘贴截图</button>
            <button class="src-head-btn sec" id="runExtractGroup" title="从 source/ 抽图并归档">抽图并归档</button>
            <button class="src-head-btn stop" id="delSource" title="删除选中的导入文件（含派生产物）">删除</button>
            <span class="cnt" id="sourceCnt"></span>
          </div>
          <div id="sourceList" class="src-list"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>② 载入与框选</h2>
      <label>整版处理模式</label>
      <select id="mergeSel">
        <option value="single" selected>单页模式：每版独立识别 / 导出</option>
        <option value="cross">跨页模式：多版按阅读顺序合并为一篇</option>
      </select>
      <label>整版原图（可多选）</label>
        <div class="src-list-wrap">
          <div class="src-list-head">
            <button class="fold-toggle" type="button" title="折叠 / 展开列表"></button>
            <label class="chk"><input type="checkbox" id="selAll"> <span>全选</span></label>
          <button class="sm ghost src-head-btn" id="loadCropped" title="将上方勾选的整版载入为工作集">载入</button>
          <button class="sm stop src-head-btn" id="delCropped" title="删除选中的整版原图（含 output 同名产物）">删除</button>
          <button class="sm ghost src-head-btn" id="sortBtn" title="按依据与方向对整版列表排序并保存">排序</button>
          <span class="cnt" id="imgCnt"></span>
        </div>
        <div class="src-list" id="srcList"></div>
      </div>
      <div style="margin-top:8px; color:var(--mut); font-size:12px;">当前源：<span class="srcname" id="srcName">未载入</span></div>
      <label>框选标签</label>
      <select id="lblSel">
        <option value="title">title（标题）</option>
        <option value="section">section（节标题）</option>
        <option value="author">author（作者）</option>
        <option value="text" selected>text（正文）</option>
      </select>
      <div style="color:var(--mut); font-size:11.5px; margin-top:4px; line-height:1.5;">
        快捷键：<b>1</b> 标题 · <b>2</b> 正文 · <b>3</b> 作者 · <b>4</b> 节标题；每个标签只对下一个新框生效，画完自动回到 text。
      </div>
    </div>

    <div class="card">
      <h2>③ OCR识别</h2>
      <p class="sub">组 = 一篇文章：同组（同色）框合并识别/导出为一篇，留空则自动合并为一篇，组内按“标题 → 作者 → 正文”排序。只有<b>标题（title）</b>会开新组；节标题（section）是文章内部分节的小标题，算正文的一部分，不参与分篇。</p>
      <div style="margin-top:12px; border-top:1px solid var(--bd); padding-top:12px;">
        <h3 style="font-size:13px;font-weight:600;margin:0 0 8px;color:var(--ink);">已框选区域（按阅读顺序，序号 = 导出次序）</h3>
        <div id="boxList"><div style="color:var(--mut); font-size:12px;">尚未框选</div></div>
      </div>
      <div class="btns" style="margin-top:12px; align-items:center;">
        <button id="recogAll">识别全部</button>
        <button class="stop" id="clearBoxes">清空框选</button>
        <span id="ocrHint" style="font-size:13px; color:var(--ok); display:none;"></span>
      </div>
    </div>

    <div class="card">
      <h2>④ 校对与结构化</h2>
      <!-- 单框重识别结果：放在 resultList 之外，renderResults() 清空列表时不会被重建。
           只读展示，绝不写回 crossResults / allPageData / output，由用户自行复制粘贴。 -->
      <div id="rerunPanel" class="card" style="display:none; margin-bottom:10px; border-color:var(--pri);">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:6px;">
          <span id="rerunHead" style="font-size:12px; color:var(--mut);"></span>
          <button class="sm sec" id="rerunClose" title="关闭">关闭</button>
        </div>
        <div id="rerunStatus" style="font-size:12px; margin-bottom:6px; color:var(--mut); word-break:break-all;"></div>
        <textarea id="rerunText" readonly style="width:100%; min-height:120px; max-height:40vh; box-sizing:border-box; font-family:'Microsoft YaHei',monospace; font-size:12px; padding:6px; border:1px solid var(--bd); border-radius:4px; resize:vertical;" placeholder="新识别结果将显示在这里"></textarea>
        <div class="btns" style="margin-top:8px; align-items:center; gap:6px;">
          <button class="sm" id="rerunCopy">复制新文本</button>
          <button class="sm sec" id="rerunLocate">在结果中定位</button>
          <span id="rerunHint" style="font-size:11.5px; color:var(--mut); flex:1 1 auto; line-height:1.5;">结果不会自动写回，请自行粘贴到下方对应段落。</span>
        </div>
      </div>
      <div id="resultList"></div>
      <div id="ctxMenu" style="display:none;"></div>
      <div class="btns" style="margin-top:10px; align-items:center;">
        <button class="run" id="saveEdit">保存修改</button>
        <button class="stop" id="clearResults" title="清空所有已载入版的识别结果，并删除 output/ 下本轮导出的 txt/json；框选与原图保留">一键清空</button>
        <span id="saveEditHint" style="font-size:13px; color:var(--ok); display:none;"></span>
      </div>
      <!-- 来源补充：墨痕题录信息默认取自导入文件名。文件名未携带报刊名/日期/版次时，在此按版补充。同一版内多篇共享来源，不同版各自填；留空沿用原逻辑。 -->
      <details class="src-extra" style="margin-top:8px; font-size:12.5px;" open>
        <summary style="cursor:pointer; color:var(--sub);">来源补充（导入文件名未含报刊名/日期/版次时填）</summary>
        <div style="margin-top:8px; display:flex; flex-direction:column; gap:6px;">
          <!-- 跨页模式：多图合并为一篇，只需填一份来源 -->
          <div id="srcSingleWrap">
            <textarea id="srcGlobalText" rows="2" placeholder="如：大公报 1943-01-17 第2版（自由输入，自动解析）" style="width:100%; resize:vertical; box-sizing:border-box; font-family:inherit; font-size:13px; padding:6px 8px;"></textarea>
            <div style="margin-top:6px; display:flex; align-items:center; gap:8px;">
              <span style="color:var(--sub); font-size:12.5px; white-space:nowrap; flex:0 0 auto;">载体类型</span>
              <select id="srcGlobalCarrier" style="font-family:inherit; font-size:13px; padding:3px 6px; width:110px; flex:0 0 auto;">
                <option value="newspaper">报纸[N]</option>
                <option value="journal">期刊[J]</option>
              </select>
            </div>
            <p class="sub" style="margin:2px 0 0; color:var(--sub);">跨页模式：多图合并为一篇，填一份来源即可。</p>
          </div>
          <!-- 单页模式：一次性导入多版，每版来源不同，按版分别填（同一版内多篇共享该来源） -->
          <div id="srcPerPageWrap" style="display:none;">
            <p class="sub" style="margin:0 0 4px; color:var(--sub);">单页模式：每版来源不同，请分别填写（同一版内多篇共享该来源）。</p>
            <div id="perPageSrc"></div>
          </div>
        </div>
      </details>
      <div class="btns" style="margin-top:10px; align-items:center; flex-wrap:nowrap; gap:8px;">
        <label style="margin:0; display:flex; align-items:center; white-space:nowrap;">结构化模式：</label>
        <select id="postMode" style="width:auto; min-width:90px;">
          <option value="kb">知识库.md</option>
          <option value="plain" selected>纯文本.txt</option>
        </select>
        <button class="purple" id="runPost">结构化</button>
        <button class="sec" id="exportAll" title="把 output/knowledge_base 下的 .md 和 output/plain_text 下的结构化 .txt 统一复制到 output/oneclick/ 中（直接平铺，不分子文件夹、不按次分文件夹），原产物保持不变">一键导出</button>
      </div>
    </div>
  </div>
</div>

<!-- 日志底部抽屉（bottom sheet） -->
<div class="log-sheet" id="logSheet" role="dialog" aria-hidden="true">
  <div class="sheet-head">
    <h2>日志</h2>
    <div class="log-tools">
      <input id="logSearch" class="log-search" type="text" placeholder="搜索过滤…">
      <button id="logCopy" class="sm ghost" type="button">复制</button>
      <button id="logClear" class="sm ghost" type="button">清空</button>
      <label class="sub" style="margin:0; display:inline-flex; align-items:center; gap:4px;"><input id="logFollow" type="checkbox" checked style="width:auto;"> 自动滚动</label>
      <button class="sheet-close" id="logSheetClose" aria-label="关闭">✕</button>
    </div>
  </div>
  <div id="log" class="logbox"></div>
</div>

<div class="drawer-mask" id="drawerMask"></div>
<aside class="drawer" id="drawer" role="dialog" aria-hidden="true">
  <div class="drawer-head">
    <h2>设置（密钥 / 模型名）</h2>
    <button class="drawer-close" id="drawerClose" aria-label="关闭">✕</button>
  </div>
  <div class="drawer-body">
    <div class="set-group">
      <h3>OCR 视觉模型（任选一）</h3>
      <div class="row" style="margin-bottom:8px; align-items:center; gap:10px; flex-wrap:wrap;">
        <label class="rdo"><input type="radio" name="cfgProvider" value="doubao"> 豆包（推荐）</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="qwen"> 千问</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="kandian"> 看典古籍</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="baidu"> 百度智能云（快速）</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="other"> 自定义</label>
      </div>
      <div class="row">
        <div class="key-field"><label>OCR API Key</label>
          <div class="input-wrap">
            <input id="cfgApiKey" class="secret" type="text" placeholder="ARK..." autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
            <span class="eye" data-target="cfgApiKey" title="显示/隐藏"></span>
          </div></div>
        <div id="cfgModelRow"><label>模型名</label><input id="cfgModel" placeholder=""></div>
      </div>
      <label id="cfgBaseUrlLabel">OCR Base URL（可选，默认千问AI平台）</label>
      <input id="cfgBaseUrl" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1">
      <div id="cfgKandianBox" style="display:none">
        <label style="margin-top:8px">看典账号（注册用的邮箱或手机号，必填）</label>
        <input id="cfgKandianEmail" placeholder="you@example.com" autocomplete="off" autocapitalize="off" spellcheck="false">
        <label style="margin-top:8px">排版方向 det_mode</label>
        <select id="cfgKandianMode">
          <option value="auto">auto 自动判定</option>
          <option value="sp">sp 强制竖排（右起，每列自上而下）</option>
          <option value="hp">hp 强制横排</option>
        </select>
        <label style="margin-top:8px">横排字序 hp_line_words_angel（仅 v2 生效）</label>
        <select id="cfgKandianHpOrder">
          <option value="right2left">right2left 右起（民国报纸横排惯例，默认）</option>
          <option value="left2right">left2right 左起（引擎原默认）</option>
        </select>
        <label style="margin-top:8px;line-height:1.75">看典为判别式引擎：<b>不吃提示词</b>、<b>不加现代标点</b>、繁体原样输出。</label>
      </div>
      <div id="cfgBaiduBox" style="display:none">
        <label style="margin-top:8px">Secret Key（与上面的 API Key 配对，必填）</label>
        <input id="cfgBaiduSecret" placeholder="百度应用的 Secret Key" autocomplete="off" autocapitalize="off" spellcheck="false">
        <label style="margin-top:8px">接口档位</label>
        <select id="cfgBaiduModel">
          <option value="accurate_basic">accurate_basic · 高精度版（免费额度较高）</option>
          <option value="accurate">accurate · 高精度含位置版（免费额度约为前者一半）</option>
        </select>
        <label class="switch-row" style="margin-top:8px;">
          <input type="checkbox" id="cfgBaiduFallback">
          <span>当前档位额度用尽时自动切到另一档</span>
        </label>
        <label style="margin-top:8px;line-height:1.75">百度为判别式引擎：<b>不吃提示词</b>、繁体原样输出、<b>输出速度极快</b>。</label>
      </div>
    </div>
    <div class="set-group">
      <h3>后置（DeepSeek / 结构化题录）</h3>
      <div class="row">
        <div class="key-field"><label>DeepSeek API Key</label>
          <div class="input-wrap">
            <input id="cfgDsKey" class="secret" type="text" placeholder="sk-..." autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
            <span class="eye" data-target="cfgDsKey" title="显示/隐藏"></span>
          </div></div>
        <div><label>DeepSeek 模型名</label><input id="cfgDsModel" placeholder="deepseek-flash"></div>
      </div>
      <label>DeepSeek Base URL（可选，默认官方）</label>
      <input id="cfgDsUrl" placeholder="https://api.deepseek.com">
    </div>
    <div class="set-group">
      <h3>输出格式</h3>
      <div class="row" style="align-items:center; gap:10px; flex-wrap:nowrap;">
        <label style="white-space:nowrap;">引用格式</label>
        <select id="cfgCitationFormat" style="width:auto; min-width:160px;">
          <option value="gb7714">GB/T 7714-2015</option>
          <option value="history_research">《历史研究》注释规范</option>
        </select>
      </div>
      <div class="row" style="align-items:center; gap:10px; flex-wrap:nowrap; margin-top:10px;">
        <label style="white-space:nowrap;">重命名规则</label>
        <select id="cfgRenamePolicy" style="width:auto; min-width:160px;">
          <option value="auto">自动识别（推荐）</option>
          <option value="always">总是按文章标题改名</option>
          <option value="never">从不改名</option>
        </select>
      </div>
      <label class="sub" style="display:block; margin-top:6px;">自动识别：截图/相机/聊天图片等无意义文件名会按文章标题改名；你自己命名的文件保留原名。</label>
      <label class="switch-row" style="margin-top:10px;">
        <input type="checkbox" id="cfgKeepTraditional">
        <span>保留繁体（默认转简体）</span>
      </label>
    </div>
    <div class="set-group">
      <h3>数据存盘位置</h3>
      <label class="sub" style="display:block; margin-bottom:6px;">数据当前存盘于（只读，修改后重启生效）：</label>
      <div id="dataDirActual" class="data-dir-actual" style="font-size:12px; color:var(--fg); background:var(--panel2,#f3f4f6); padding:6px 8px; border-radius:6px; word-break:break-all; margin-bottom:8px; line-height:1.5;"></div>
      <div class="row" style="gap:8px; align-items:center;">
        <input id="cfgDataDir" placeholder="留空 = 恢复默认" style="flex:1;">
        <button class="sec" id="pickDataDir" type="button">浏览…</button>
      </div>
      <label class="sub" style="display:block; margin-top:6px;">修改后需<strong>重启墨痕</strong>方可生效；旧目录数据不会自动迁移，需手动复制。</label>
    </div>
    <div class="set-group">
      <h3>版本更新</h3>
      <div class="row version-row">
        <span class="sub">当前版本：<span id="curVersion">--</span></span>
        <button class="sec" id="checkUpdateBtn" type="button">检查更新</button>
      </div>
      <label class="switch-row" style="margin-top:10px;">
        <input type="checkbox" id="autoUpdateChk">
        <span>自动更新（检测到新版本时自动下载并重启墨痕）</span>
      </label>
      <div id="updateStatus" class="sub" style="margin-top:6px;"></div>
    </div>
  </div>
  <div class="drawer-foot">
    <button id="saveCfg">保存配置</button>
    <button class="ghost" id="clearCfg" type="button">清空配置</button>
    <span class="sub" id="saveHint"></span>
  </div>
</aside>

<aside class="drawer" id="usageDrawer" role="dialog" aria-labelledby="usageTitle" aria-hidden="true">
  <div class="drawer-head">
    <h2 id="usageTitle">用量统计</h2>
    <button class="drawer-close" id="usageClose" aria-label="关闭" title="关闭（ESC）">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="drawer-body">
    <div class="set-group">
      <div class="filter-row">
        <label>模型 <select id="usageModelSel"><option value="全部">全部</option></select></label>
        <label>阶段 <select id="usageStageSel">
          <option value="全部">全部</option>
          <option value="box_ocr">OCR</option>
          <option value="deepseek">结构化</option>
        </select></label>
        <button class="ghost" id="usageRefresh">刷新</button>
      </div>
      <table class="usage-table" id="usageTable">
        <thead><tr><th>模型</th><th>阶段</th><th>调用</th><th>输入</th><th>输出</th><th>总 tokens</th><th>平均耗时(s)</th></tr></thead>
        <tbody></tbody>
      </table>
      <p class="sub" id="usageEmpty" style="display:none; margin-top:10px;">当前筛选下暂无数据。</p>
    </div>
  </div>
  <div class="drawer-foot">
    <span class="sub" id="usageHint">数据来源 token_log.csv（每次 OCR / 结构化完成自动追加）</span>
  </div>
</aside>

<aside class="drawer" id="promptDrawer" role="dialog" aria-labelledby="promptTitle" aria-hidden="true">
  <div class="drawer-head">
    <h2 id="promptTitle">提示词</h2>
    <button class="drawer-close" id="promptClose" aria-label="关闭" title="关闭（ESC）">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="drawer-body">
    <div class="set-group">
      <h3>OCR 提示词<span class="reset" id="resetOcr">恢复默认</span></h3>
      <p class="sub">用于「识别全部」逐框转录。</p>
      <textarea class="prompt-area" id="prompt_ocr" spellcheck="false"></textarea>
    </div>
    <div class="set-group">
      <div class="mode-tabs">
        <label><input type="radio" name="postModeEdit" value="kb"> 知识库模式</label>
        <label><input type="radio" name="postModeEdit" value="plain" checked> 纯文本</label>
      </div>
      <div class="mode-tabs" style="margin-top:8px;">
        <label><input type="radio" name="postFmtEdit" value="gb7714" checked> GB/T 7714-2015</label>
        <label><input type="radio" name="postFmtEdit" value="history_research"> 《历史研究》</label>
      </div>
      <h3>结构化提示词<span class="reset" id="resetPost">恢复默认</span></h3>
      <p class="sub" id="postHint">知识库模式：用于「结构化」阶段题录抽取，产物为.md格式。</p>
      <textarea class="prompt-area" id="prompt_post" spellcheck="false"></textarea>
    </div>
  </div>
  <div class="drawer-foot">
    <button id="savePrompt">保存提示词</button>
    <span class="sub" id="promptHint">保存后下次识别 / 结构化即时生效</span>
  </div>
</aside>

<aside class="drawer" id="sortDrawer" role="dialog" aria-labelledby="sortTitle" aria-hidden="true">
  <div class="drawer-head">
    <h2 id="sortTitle">整版排序</h2>
    <button class="drawer-close" id="sortClose" aria-label="关闭" title="关闭（ESC）">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="drawer-body">
    <div class="set-group">
      <h3>排序依据</h3>
      <label class="rdo"><input type="radio" name="sortBy" value="default" checked> 默认顺序（按页码 / 文件名自然序重置）</label>
      <label class="rdo"><input type="radio" name="sortBy" value="name"> 文件名</label>
      <label class="rdo"><input type="radio" name="sortBy" value="mtime"> 修改时间</label>
    </div>
    <div class="set-group">
      <h3>排序方向</h3>
      <label class="rdo"><input type="radio" name="sortDir" value="asc" checked> 升序</label>
      <label class="rdo"><input type="radio" name="sortDir" value="desc"> 降序</label>
    </div>
  </div>
  <div class="drawer-foot">
    <button id="applySort" class="sec">排序</button>
    <button class="ghost" id="sortCancel" type="button">取消</button>
  </div>
</aside>

<script>
const $ = id => document.getElementById(id);
// 防 Chromium 静默换值：select 保持焦点时，滚轮滚动页面会逐项切换其选中项（无任何界面提示，
// 曾致「框选标签」被无声拨到 title）。任何 select 值变化后立即失焦，堵死这条静默通道；
// 悬停滚轮在 select 上时也先失焦，避免聚焦态下滚轮直接改值。
document.addEventListener('change', e=>{ const t=e.target; if(t && t.tagName==='SELECT'){ try{ t.blur(); }catch(_){} } }, true);
document.addEventListener('wheel', e=>{ const t=e.target; if(t && t.tagName==='SELECT'){ try{ t.blur(); }catch(_){} } }, true);
// 「框选标签」切换时在日志面板留痕（兼作探针：若再出现静默变化可从日志定位时间点）
document.getElementById('lblSel').addEventListener('change', ()=>{ try{ log('[框选标签] 已切换为 '+document.getElementById('lblSel').value); }catch(_){} });
const cv = $('cv'); const ctx = cv.getContext('2d');
let img = null, natW = 0, natH = 0, scale = 1, baseScale = 1, userZoom = 1;
const ZOOM_MIN = 0.2, ZOOM_MAX = 5.0;
let boxes = [];   // {id,label,x,y,w,h,group}
let results = {}; // key -> {title,author,body,raw}
let srcName = ""; let drawing = null, cur = null; let selectedId = null; let pendingSelectId = null;
let dragState = null; let uid = 1; let mode = 'auto'; let backendCfg = {};
// —— 翻页竞态防护（2026-11-05）——
// imgLoading: 目标版图片尚未解码完成，此刻画布上仍是上一版的图（img/natW/boxes 都还指上一版）；
// loadSeq: 翻页请求序号，onload 只在序号仍为最新时生效，快速连翻时旧响应不会再抢回状态；
// baseOf: 起框/拖动瞬间的基准快照（版名 + 图片像素尺寸），mouseup 时不一致即丢弃，杜绝「框记到错的对象上」；
// pageDims: 同会话内各版图片尺寸，用于发现「图被重抽/旋转后尺寸变了」导致的整体偏移。
let imgLoading = false, loadSeq = 0, baseOf = null; const pageDims = new Map();
// 多版识别状态：各整版框选/识别结果按页分别存放于 allPageData，
// pageOrder 记录工作集（= 阅读顺序，由「载入」批量填充）。
// mergeMode 决定整版间关系：'single' 每版独立识别/导出；'cross' 多版合并为一篇。
let allPageData = {}; let pageOrder = []; let pageList = []; let navList = []; let pageIdx = -1; let crossResults = {};
let fileMtimes = {};   // 整版原图修改时间（秒），来自 /api/list_images 的 mtimes，供排序抽屉使用
let mergeMode = 'single';
// 跨页分篇：以「标题」为唯一边界（无前端开关）。有标题开新篇，无标题并入前一篇；
// 续页题头（重复正题 / 只剩（二）标记 / （一）这类章节小标题）继续写在当前篇里。
let off = 0;

const LBL_COLOR = { title:'#2563eb', section:'#0891b2', text:'#16a34a', author:'#9333ea' };
const GROUP_COLORS = ['#dc2626','#2563eb','#16a34a','#9333ea','#d97706','#0891b2','#7c3aed','#db2777','#65a30d','#ea580c'];
function groupColor(g){ let h=0; for(let i=0;i<g.length;i++) h=(h*31+g.charCodeAt(i))%16777215; return GROUP_COLORS[h%GROUP_COLORS.length]; }
const HANDLE=9, HIT_PAD=2;

// ---------- 配置 / 状态 ----------
const IS_MAC = /Mac/i.test(navigator.platform||'') || /Macintosh|Mac OS X/i.test(navigator.userAgent||'');
let CURRENT_VERSION = '';
let AUTO_UPDATE = false;
let SKIP_VERSION = null;
const RELEASE_PAGE_URL = 'https://github.com/dabuxiaobu/mohen-newspaper-ocr/releases';
fetch('/api/config').then(r=>r.json()).then(c=>{ backendCfg=c; window.__RUNTIME_DIR=c.runtime_dir||''; CURRENT_VERSION = c.version || ''; if($('curVersion')) $('curVersion').textContent = CURRENT_VERSION || '--'; if($('dataDirActual')) $('dataDirActual').textContent = (c.data_dir || c.runtime_dir || '（未能读取当前数据目录）'); fillCfgInputs(c.values||{}); updateCfgLine(); }).catch(()=>{ $('cfgLine').textContent='配置读取失败'; });
// 启动恢复历史日志（关闭 exe 不丢失）
restoreLogs();
// 启动后自动检查新版本（点3）：内部读取自动更新开关并决定弹窗或自动更新
autoUpdateCheckOnLaunch();
// 启动时把已保存的密钥/模型回填进输入框，避免重开后看似"丢失"
// OCR 服务商切换：每个服务商各存一组凭据，切换时回填对应值
let ocrProvider = 'doubao';
const ocrStash = { qwen:{api_key:'',base_url:'',model:''}, doubao:{api_key:'',base_url:'',model:''}, kandian:{api_key:'',base_url:'',model:'',email:'',det_mode:'auto',hp_order:'right2left'}, baidu:{api_key:'',base_url:'',model:'',secret:'',fallback:'true'}, other:{api_key:'',base_url:'',model:''} };
function applyProviderFromValues(v){
  v = v || {};
  for(const p of ['qwen','doubao','kandian','baidu','other']){ const pre=p.toUpperCase();
    ocrStash[p] = { api_key:v[pre+'_API_KEY']||'', base_url:v[pre+'_BASE_URL']||'', model:v[pre+'_MODEL']||'',
                    email:v[pre+'_EMAIL']||'', det_mode:v[pre+'_DET_MODE']||'auto',
                    hp_order:v[pre+'_HP_ORDER']||'right2left',
                    secret:v[pre+'_SECRET_KEY']||'',
                    // 未写过该键（老配置）视为默认开启，与后端默认一致
                    fallback:(p==='baidu' ? (String(v.BAIDU_AUTO_FALLBACK==null?'true':v.BAIDU_AUTO_FALLBACK).toLowerCase()==='false' ? 'false' : 'true') : '') }; }
  ocrProvider = (['qwen','doubao','kandian','baidu','other'].includes(v.BOX_OCR_PROVIDER)) ? v.BOX_OCR_PROVIDER : 'doubao';
  setProviderRadio(ocrProvider);
  fillActiveProviderInputs();
  // 回填独立的结构化（DeepSeek）密钥/地址/模型，否则重启后设置面板显示为空（密钥已落盘只是未恢复显示）
  $('cfgDsKey').value = v.DEEPSEEK_API_KEY || '';
  $('cfgDsUrl').value = v.DEEPSEEK_BASE_URL || '';
  $('cfgDsModel').value = v.DEEPSEEK_MODEL || '';
}
function setProviderRadio(p){ const r=document.querySelector('input[name="cfgProvider"][value="'+p+'"]'); if(r) r.checked=true; }
function fillActiveProviderInputs(){
  const s = ocrStash[ocrProvider] || {};
  $('cfgApiKey').value = s.api_key||''; $('cfgBaseUrl').value = s.base_url||''; $('cfgModel').value = s.model||'';
  // 看典古籍专有：账号 + 排版方向，随服务商切换显隐
  const kb=$('cfgKandianBox'); if(kb) kb.style.display = (ocrProvider==='kandian') ? 'block' : 'none';
  const ke=$('cfgKandianEmail'); if(ke) ke.value = s.email||'';
  const km=$('cfgKandianMode'); if(km) km.value = s.det_mode||'auto';
  const kh=$('cfgKandianHpOrder'); if(kh) kh.value = s.hp_order||'right2left';
  // 百度智能云专有：Secret Key（与 API Key 配对）+ 接口档位下拉（替代共用「模型名」输入框）
  const bb=$('cfgBaiduBox'); if(bb) bb.style.display = (ocrProvider==='baidu') ? 'block' : 'none';
  const bs=$('cfgBaiduSecret'); if(bs) bs.value = s.secret||'';
  // 百度档位只有两档，历史值（如 general_basic）一律回落到 accurate_basic，避免下拉显示空白
  const bm=$('cfgBaiduModel'); if(bm) bm.value = (s.model==='accurate') ? 'accurate' : 'accurate_basic';
  const bf=$('cfgBaiduFallback'); if(bf) bf.checked = (String(s.fallback==null?'true':s.fallback).toLowerCase()!=='false');
  // 两处都能填同一个键会打架，选百度时把共用「模型名」整行藏起来
  const bmr=$('cfgModelRow'); if(bmr) bmr.style.display = (ocrProvider==='baidu') ? 'none' : 'block';
  const def = (ocrProvider==='qwen')
    ? { key:'sk-...', base:'https://dashscope.aliyuncs.com/compatible-mode/v1', model:'' }
    : (ocrProvider==='doubao')
    ? { key:'ark-', base:'https://ark.cn-beijing.volces.com/api/v3', model:'ep-2024xxxxxxxx-xxxxx（火山方舟接入点 ID）' }
    : (ocrProvider==='kandian')
    ? { key:'看典 API Token', base:'https://ocr.kandianguji.com', model:'v2（推荐）/ beta / default' }
    : (ocrProvider==='baidu')
    ? { key:'百度应用的 API Key', base:'https://aip.baidubce.com', model:'accurate_basic（高精度）/ accurate（含位置）/ general_basic（标准）' }
    : { key:'', base:'', model:'' };
  $('cfgApiKey').placeholder = def.key;
  $('cfgBaseUrl').placeholder = def.base;
  $('cfgModel').placeholder = def.model;
  const lbl=$('cfgBaseUrlLabel'); if(lbl) lbl.textContent = (ocrProvider==='qwen')
    ? 'OCR Base URL（可选，默认千问AI平台）'
    : (ocrProvider==='doubao')
    ? 'OCR Base URL（可选，默认火山方舟）'
    : (ocrProvider==='kandian')
    ? '看典 Base URL（可选，默认 https://ocr.kandianguji.com）'
    : (ocrProvider==='baidu')
    ? 'Base URL（可选，默认 https://aip.baidubce.com）'
    : 'OCR Base URL';
}
function stashActive(){
  const s = ocrStash[ocrProvider] || (ocrStash[ocrProvider]={});
  s.api_key=$('cfgApiKey').value.trim(); s.base_url=$('cfgBaseUrl').value.trim(); s.model=$('cfgModel').value.trim();
  const ke=$('cfgKandianEmail'); if(ke) s.email=ke.value.trim();
  const km=$('cfgKandianMode'); if(km) s.det_mode=km.value||'auto';
  const kh=$('cfgKandianHpOrder'); if(kh) s.hp_order=kh.value||'right2left';
  const bs=$('cfgBaiduSecret'); if(bs) s.secret=bs.value.trim();
  // 百度：档位与「额度用尽自动换档」存在与 BAIDU_MODEL / BAIDU_AUTO_FALLBACK 同一组键上
  if(ocrProvider==='baidu'){
    const bm=$('cfgBaiduModel'); if(bm) s.model=bm.value||'accurate_basic';
    const bf=$('cfgBaiduFallback'); if(bf) s.fallback=bf.checked?'true':'false';
  }
}
function fillCfgInputs(v){
  applyProviderFromValues(v);
  const cfSel=$('cfgCitationFormat'); if(cfSel) cfSel.value = (v.CITATION_FORMAT||'gb7714');
  const rp=$('cfgRenamePolicy'); if(rp) rp.value = (v.RENAME_POLICY||'auto');
  const kt=$('cfgKeepTraditional'); if(kt) kt.checked = ((v.KEEP_TRADITIONAL||'false').toLowerCase()==='true');
  const dd=$('cfgDataDir'); if(dd) dd.value = (v.DATA_DIR||'');
  applyEyeCare((v.EYE_CARE||'false').toLowerCase()==='true', false);
}
function applyEyeCare(on, save){
  document.body.classList.toggle('eye-care', !!on);
  const b=$('eyeBtn'); if(b){ b.classList.toggle('eyecare-on', !!on); b.textContent = on ? '👁 护眼·开' : '👁 护眼'; }
  if(save){
    fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({EYE_CARE: on?'true':'false'})}).catch(()=>{});
  }
}
const _eye=$('eyeBtn'); if(_eye) _eye.onclick=()=>applyEyeCare(!document.body.classList.contains('eye-care'), true);

// ---------- 主页顶部提示：建议关闭网络代理 / VPN（可关闭，记忆在 localStorage） ----------
// 提示条占位高度由实测回填到 .wrap，避免窗口变窄换行时撑破视口（.wrap 原为 calc(100vh - 56px)）。
function syncVpnTipH(){
  const tip=$('vpnTip'), w=document.querySelector('.wrap'); if(!tip||!w) return;
  if(tip.style.display==='none' || !tip.offsetHeight){ w.style.height=''; return; }
  w.style.height='calc(100vh - 56px - '+tip.offsetHeight+'px)';
}
function initVpnTip(){
  const tip=$('vpnTip'); if(!tip) return;
  let off=false; try{ off = localStorage.getItem('mh_vpn_tip_off')==='1'; }catch(_e){}
  if(off){ tip.style.display='none'; return; }
  const b=$('vpnTipClose');
  if(b) b.onclick=()=>{ tip.style.display='none';
    try{ localStorage.setItem('mh_vpn_tip_off','1'); }catch(_e){}
    syncVpnTipH(); };
  window.addEventListener('resize', syncVpnTipH);
  syncVpnTipH();
}
initVpnTip();
function updateCfgLine(){
  const hasKey = backendCfg && backendCfg.dry_run===false;
  const model = (backendCfg&&backendCfg.model)||'未配置';
  const host = (backendCfg&&backendCfg.base_url_host)||'ark.cn-beijing.volces.com';
  if(!hasKey) $('cfgLine').innerHTML='OCR：<span class="warn">未配置 API_KEY</span> 模型 '+model+' <b>请点「设置」填写 OCR 密钥与模型名</b>';
  else $('cfgLine').innerHTML='OCR：真实调用 · 模型 <b>'+model+'</b> @ '+host;
}
function getOverrides(){ const s=ocrStash[ocrProvider]||{}; const o={provider:ocrProvider}; if(s.base_url)o.base_url=s.base_url; if(s.api_key)o.api_key=s.api_key; if(s.model)o.model=s.model; if(s.email)o.email=s.email; if(s.det_mode)o.det_mode=s.det_mode; if(s.hp_order)o.hp_order=s.hp_order; if(s.secret)o.secret=s.secret; if(ocrProvider==='baidu')o.auto_fallback=(s.fallback==='false'?'false':'true'); return o; }

// ---------- 加载图片（canvas drawImage 直接绘制，载入即显示，无需点击） ----------
function loadDataURL(url,name,idx,seq){ const pname=(name||'').replace(/\.[^.]+$/,'');
  // 切走前先把当前版状态存回 allPageData（始终回写，避免翻页丢失框选/编辑）
  if(srcName && allPageData[srcName]){ allPageData[srcName].boxes=boxes; allPageData[srcName].results=results; }
  const im=new Image(); im.onerror=()=>{ if(seq==null||seq===loadSeq){ imgLoading=false; updatePageNav(); draw(); } log('[载入图片] 解码失败：'+(name||'')); };
  im.onload=()=>{
    if(seq!=null && seq!==loadSeq){ log('[载入] 丢弃过期图片响应：'+(name||'')+'（当前序号 '+loadSeq+'）'); return; }
    imgLoading=false;
    // 换图前取消进行中的框选/拖动：起点取旧图基准、终点取新图基准会把框拉歪
    if(drawing||dragState){ drawing=null; dragState=null; cur=null; log('[载入] 换页时有未完成的框选/拖动，已取消以免坐标错基准'); }
    img=im; natW=im.naturalWidth; natH=im.naturalHeight;
    // 同会话内尺寸变化检测（重抽图/旋转会改像素尺寸，旧框会整体偏移）——只告警不改坐标，避免把错框洗白
    { const _dim=natW+'x'+natH, _old=pageDims.get(pname);
      if(_old && _old!==_dim){ const _n=((allPageData[pname]&&allPageData[pname].boxes)||[]).length;
        log('[载入] 警告：'+pname+' 图片尺寸已变（'+_old+' → '+_dim+'），该版 '+_n+' 个框，旧框会整体偏移，请复核'); }
      pageDims.set(pname,_dim); }
    srcName=pname; const _sn=$('srcName'); if(_sn) _sn.textContent=srcName||'未命名';
    // 懒初始化该版存储；pageOrder 仅由「载入所选」批量填充，翻页（loadDataURL）绝不改动
    if(!allPageData[srcName]) allPageData[srcName]={boxes:[],results:{}};
    boxes=allPageData[srcName].boxes; results=allPageData[srcName].results;   // 引用，编辑即持久化
    selectedId=null; renderBoxList(); if(mergeMode!=='cross') renderResults(); resizeCanvas();   // 跨页模式面板内容与当前页无关，翻页不重建，避免校对文本框重置
    if(pendingSelectId!=null){ selectedId=pendingSelectId; pendingSelectId=null; renderBoxList(); draw(); }
    if(pendingFocus){ const _pf=pendingFocus; pendingFocus=null; focusBoxOnCanvas(_pf); }
    // 页码优先用调用方传入的 idx（gotoPage 已同步设过，最可靠），反推仅作兜底（均基于 navList，即当前载入的工作集）
    if(typeof idx==='number' && idx>=0 && navList[idx] && navList[idx].replace(/\.[^.]+$/,'')===srcName) pageIdx=idx;
    else pageIdx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===srcName);
    updatePageNav();
  }; im.src=url; }
function updatePageNav(){ const pc=$('pageCounter'); if(pc){ pc.textContent=pageIdx<0?'未载入（共 '+pageList.length+' 版可载入）':'第 '+(pageIdx+1)+' / '+navList.length+' 版'; }
  const cs=$('crossStatus'); if(cs){ const cross=mergeMode==='cross'; cs.style.display=cross?'block':'none'; cs.textContent=cross?('跨页 · 已载入 '+pageOrder.length+' 版'):'单页模式'; }
  renderPerPageSrc(); }
function gotoPage(i){ if(i<0||i>=navList.length) return; const name=navList[i];
  // 同步设置页码并立即刷新导航，不依赖图片 onload 反推（避免竞态/反推失败导致翻页失效）
  pageIdx=i; updatePageNav();
  const _seq=++loadSeq; imgLoading=true;   // 进入「载入中」：此间画布仍是上一版的图，禁止起框
  const _pc=$('pageCounter'); if(_pc) _pc.textContent+=' · 载入中…';   // 可见提示：否则用户在图未刷出来时落笔，会被静默忽略
  draw();   // 立即盖遮罩，避免用户把上一版画面当成本次要框的版
  fetch('/api/image?name='+encodeURIComponent(name)+'&dir=cropped_hi').then(r=>r.json()).then(j=>loadDataURL(j.data,name,i,_seq)).catch(e=>{ if(_seq===loadSeq){ imgLoading=false; updatePageNav(); draw(); } log('翻页失败：'+e); }); }
const PAD=24;   // 画布四周留白像素，便于在图片边角起框
function resizeCanvas(){ const wrap=$('canvasWrap'); const availW=Math.max(1,wrap.clientWidth-32-2*PAD); const availH=Math.max(1,wrap.clientHeight-32-2*PAD);
  if(img){ baseScale=Math.min(1,availW/natW,availH/natH); scale=baseScale*userZoom; cv.width=Math.max(1,Math.floor(natW*scale)+2*PAD); cv.height=Math.max(1,Math.floor(natH*scale)+2*PAD);
    cv.classList.remove('placeholder'); draw(); }
  else { cv.width=Math.max(1,availW+2*PAD); const h=Math.max(420,wrap.clientHeight-40); cv.height=Math.max(1,h+2*PAD); cv.classList.add('placeholder'); drawPlaceholder(); } }
window.addEventListener('resize',()=>{ userZoom=1; resizeCanvas(); draw(); updateZoomBar(); });
function setZoom(z){ userZoom=Math.max(ZOOM_MIN,Math.min(ZOOM_MAX,z)); resizeCanvas(); draw(); updateZoomBar(); }
function updateZoomBar(){ const el=$('zoomPct'); if(el)el.textContent=Math.round(userZoom*100)+'%'; }
function drawPlaceholder(){ if(img)return; ctx.clearRect(0,0,cv.width,cv.height); ctx.fillStyle=document.body.classList.contains('eye-care')?'#F2F8F1':'#f8fafc'; ctx.fillRect(0,0,cv.width,cv.height);
  ctx.strokeStyle='#cbd5e1'; ctx.setLineDash([8,6]); ctx.strokeRect(PAD,PAD,cv.width-2*PAD,cv.height-2*PAD); ctx.setLineDash([]);
  ctx.fillStyle='#94a3b8'; ctx.textAlign='center'; ctx.font='15px "Microsoft YaHei",sans-serif';
  ctx.fillStyle='#64748b'; ctx.font='13px "Microsoft YaHei",sans-serif';
  ctx.fillText('加载整版报纸原图后，即可鼠标拖拽框选文章区域',cv.width/2,cv.height/2+20); ctx.textAlign='left'; }
function globalBoxIndex(i){
  const idx=pageOrder.indexOf(srcName);
  if(idx<0) return i+1;
  let base=0;
  for(let k=0;k<idx;k++){ base += (allPageData[pageOrder[k]]&&allPageData[pageOrder[k]].boxes?allPageData[pageOrder[k]].boxes.length:0); }
  return base+i+1;
}
function draw(){ if(!img){ drawPlaceholder(); return; } ensureOwnBoxes('绘制');   // 画布永远只画「当前版自己的」框，串页先纠回来
  ctx.clearRect(0,0,cv.width,cv.height); ctx.drawImage(img,PAD,PAD,natW*scale,natH*scale);
  const cross = mergeMode==='cross';
  boxes.forEach((b,i)=>{
    // 诊断①：框记录于别的版 → 不在本版绘制。这正是「第5版显示第1版的框」的形态，留痕而不静默。
    if(b._pg && b._pg!==srcName){ if(!b._pgWarned){ b._pgWarned=1; log('[一致性] 框 #'+b.id+' 创建于「'+b._pg+'」，不属于当前版「'+srcName+'」，已不在本版绘制'); } return; }
    // 诊断②：框记录时的像素尺寸与当前版不符 → 坐标基准不一致（两版尺寸不同、scale 又不同时就会这样）
    if(b._bw && (b._bw!==natW||b._bh!==natH) && !b._dimWarned){ b._dimWarned=1;
      log('[一致性] 框 #'+b.id+' 记录于 '+b._bw+'×'+b._bh+'，当前版 '+natW+'×'+natH+'，坐标基准不符，该框可能偏位'); }
    const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; const isSel=b.id===selectedId;
    const color=(b.group?groupColor(b.group):LBL_COLOR[b.label])||'#dc2626';
    ctx.strokeStyle=color; ctx.lineWidth=isSel?3:2; ctx.strokeRect(X,Y,W,H);
    ctx.fillStyle=color; ctx.fillRect(X,Y-16,20,16); ctx.fillStyle='#fff'; ctx.font='12px sans-serif';
    ctx.textAlign='center'; ctx.fillText(String(cross?globalBoxIndex(i):(i+1)),X+10,Y-4);     ctx.textAlign='left';
    if(isSel) drawHandles(b,isSel);
  });
  if(cur){ const X=cur.x*scale+PAD,Y=cur.y*scale+PAD,W=cur.w*scale,H=cur.h*scale; ctx.strokeStyle='#f59e0b'; ctx.lineWidth=2; ctx.setLineDash([5,3]); ctx.strokeRect(X,Y,W,H); ctx.setLineDash([]); }
  // 载入遮罩：此刻画布上还是上一版的图与框，不遮住的话用户会误以为已经翻到新版并开始框选
  // （跨页模式下连续几版版面相似，肉眼更难分辨），这正是「框用错基准」的入口。
  if(imgLoading){ ctx.fillStyle='rgba(15,23,42,0.55)'; ctx.fillRect(0,0,cv.width,cv.height);
    ctx.fillStyle='#fff'; ctx.textAlign='center'; ctx.font='bold 16px "Microsoft YaHei",sans-serif';
    ctx.fillText('载入中…',cv.width/2,cv.height/2); ctx.font='12px "Microsoft YaHei",sans-serif';
    ctx.fillStyle='#cbd5e1'; ctx.fillText('上一版画面，请稍候再框选',cv.width/2,cv.height/2+22); ctx.textAlign='left'; } }
// commitCanvas 已移除：回退到 #166 同步绘制方案——绘制在图片 onload 同步任务内完成即可随该次渲染上屏，无需点击。
function drawHandles(b,isSel){ const hs=handlesOf(b); const act=dragState&&dragState.kind==='resize'&&dragState.id===b.id; ctx.fillStyle=act?'#f59e0b':'#2563eb'; ctx.strokeStyle='#fff'; ctx.lineWidth=1;
  hs.forEach(h=>{ ctx.fillRect(h.x,h.y,HANDLE,HANDLE); ctx.strokeRect(h.x,h.y,HANDLE,HANDLE); }); }
function handlesOf(b){ const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; const c=n=>n-HANDLE/2;
  return [{name:'nw',x:c(X),y:c(Y)},{name:'n',x:c(X+W/2),y:c(Y)},{name:'ne',x:c(X+W),y:c(Y)},{name:'w',x:c(X),y:c(Y+H/2)},
          {name:'e',x:c(X+W),y:c(Y+H/2)},{name:'sw',x:c(X),y:c(Y+H)},{name:'s',x:c(X+W/2),y:c(Y+H)},{name:'se',x:c(X+W),y:c(Y+H)}]; }

function toNat(e){ const r=cv.getBoundingClientRect(); const px=(e.clientX-r.left)*(cv.width/r.width)-PAD, py=(e.clientY-r.top)*(cv.height/r.height)-PAD; return [px*(natW/(cv.width-2*PAD)),py*(natH/(cv.height-2*PAD))]; }
// 按「按下鼠标那一刻的 scale」换算：scale 依赖图片尺寸（baseScale=min(1,availW/natW,availH/natH)），
// 换图后 scale 会变，同一个屏幕位置就会算出两个不同的像素值 —— 框既偏位、尺寸又正常，这是偏移的真因。
// 正常情况（s 与当前 scale 相同）完全走上面 toNat 的老路径，行为零变化。
function toNatAt(e,s){ if(s==null||Math.abs(s-scale)<1e-9) return toNat(e);
  const r=cv.getBoundingClientRect(); const k=Math.max(1e-6,s);
  const px=(e.clientX-r.left)*(cv.width/r.width)-PAD, py=(e.clientY-r.top)*(cv.height/r.height)-PAD;
  return [px/k, py/k]; }
function toCanvas(e){ const r=cv.getBoundingClientRect(); return [(e.clientX-r.left)*(cv.width/r.width),(e.clientY-r.top)*(cv.height/r.height)]; }
function hitHandle(e){ if(selectedId==null)return null; const sb=boxes.find(b=>b.id===selectedId); if(!sb)return null; const [cx,cy]=toCanvas(e); for(const h of handlesOf(sb)){
  if(cx>=h.x-HIT_PAD&&cx<=h.x+HANDLE+HIT_PAD&&cy>=h.y-HIT_PAD&&cy<=h.y+HANDLE+HIT_PAD) return {box:sb,handle:h.name}; } return null; }
function hitBox(e){ const [cx,cy]=toCanvas(e); for(let i=boxes.length-1;i>=0;i--){ const b=boxes[i];
  const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; if(cx>=X&&cx<=X+W&&cy>=Y&&cy<=Y+H) return b; } return null; }

let interactionMode = 'select';
let panState = null;
const _modeSelect = $('modeSelect'), _modePan = $('modePan');
function setInteractionMode(m){
  interactionMode = m;
  if(_modeSelect){ _modeSelect.style.background = (m==='select') ? '#2563eb' : '#fff'; _modeSelect.style.color = (m==='select') ? '#fff' : '#334155'; }
  if(_modePan){ _modePan.style.background = (m==='pan') ? '#2563eb' : '#fff'; _modePan.style.color = (m==='pan') ? '#fff' : '#334155'; }
  cv.style.cursor = (m==='pan') ? 'grab' : 'crosshair';
}
if(_modeSelect) _modeSelect.onclick = ()=>setInteractionMode('select');
if(_modePan) _modePan.onclick = ()=>setInteractionMode('pan');

cv.addEventListener('mousedown',e=>{ if(!img)return;
  // 载入中禁止起框：此刻画布仍是上一版的图，记下的坐标会落进上一版或混用两套基准
  if(imgLoading){ log('[框选] 目标版尚未载入完成（画布仍是上一版），已忽略本次操作'); return; }
  ensureOwnBoxes('起框');   // 串页兜底：boxes 若不属于当前版，先切回本版数组，再取基准快照
  baseOf={page:srcName,w:natW,h:natH,s:scale};   // 基准快照（含 scale），mouseup 时校验+换算
  clearBoxFocusHighlight();   // 回到画布操作即结束上一次的片段标记；若点到框，mouseup 会重新标新的那段
  if(interactionMode==='pan'){ panState={x:e.clientX,y:e.clientY}; cv.style.cursor='grabbing'; return; }
  if(!e.altKey){ const hh=hitHandle(e); if(hh){ const [x,y]=toNat(e);
  dragState={kind:'resize',id:hh.box.id,handle:hh.handle,startX:x,startY:y,origBox:{...hh.box},s:scale}; selectedId=hh.box.id; renderBoxList(); draw(); return; }
  const hb=hitBox(e); if(hb){ const [x,y]=toNat(e); dragState={kind:'move',id:hb.id,startX:x,startY:y,origBox:{...hb},s:scale}; selectedId=hb.id; renderBoxList(); draw(); return; } }
  // Alt+左键：跳过命中检测，强制从空白起画新框（应对排版紧凑/框体重叠场景）
  selectedId=null; renderBoxList(); draw(); const [x,y]=toNat(e); drawing={x,y,s:scale}; cur=null; });
cv.addEventListener('mousemove',e=>{ if(interactionMode==='pan' && panState){ const w=$('canvasWrap'); if(w){ w.scrollLeft-=(e.clientX-panState.x); w.scrollTop-=(e.clientY-panState.y); } panState={x:e.clientX,y:e.clientY}; return; }
  if(drawing){ const [x,y]=toNatAt(e,drawing.s);
  cur={x:Math.min(drawing.x,x),y:Math.min(drawing.y,y),w:Math.abs(x-drawing.x),h:Math.abs(y-drawing.y)}; draw(); return; }
  if(!dragState){ if(interactionMode==='pan'){ cv.style.cursor='grab'; return; } const hh=hitHandle(e),hb=hitBox(e); cv.style.cursor=hh?'resize':(hb?'move':'crosshair'); return; }
  const [x,y]=toNatAt(e,dragState.s); const dx=x-dragState.startX,dy=y-dragState.startY; const b=boxes.find(bx=>bx.id===dragState.id); if(!b)return; const o=dragState.origBox;
  if(dragState.kind==='move'){ b.x=o.x+dx; b.y=o.y+dy; } else { let nx=o.x,ny=o.y,nw=o.w,nh=o.h; const h=dragState.handle;
    if(h.includes('w')){ nx+=dx; nw-=dx; } if(h.includes('e')){ nw+=dx; } if(h.includes('n')){ ny+=dy; nh-=dy; } if(h.includes('s')){ nh+=dy; }
    if(nw<5)nw=5; if(nh<5)nh=5; b.x=nx;b.y=ny;b.w=nw;b.h=nh; } draw(); });
cv.addEventListener('mouseup',e=>{ if(interactionMode==='pan'){ panState=null; cv.style.cursor='grab'; return; }
  // 基准一致性校验：起框/拖动用的是按下那一刻的版与像素尺寸；若抬起时已换过图，坐标就是两套基准拼出来的，必须丢弃
  const _bad = !baseOf || baseOf.page!==srcName || baseOf.w!==natW || baseOf.h!==natH;
  if(_bad){ log('[框选] 基准不一致（按下时 '+(baseOf?baseOf.page+' '+baseOf.w+'x'+baseOf.h:'无')+' → 现在 '+(srcName||'无')+' '+natW+'x'+natH+'），本次操作已丢弃'); }
  if(drawing){ if(_bad){ drawing=null; cur=null; draw(); return; } const [x,y]=toNatAt(e,drawing.s); const x0=Math.min(drawing.x,x),y0=Math.min(drawing.y,y);
  const w=Math.abs(x-drawing.x),h=Math.abs(y-drawing.y); drawing=null;
  if(w>5&&h>5){ const _lb=$('lblSel').value;
    // _pg/_bw/_bh：框创建时所在版与图片像素尺寸的快照，纯诊断用（不改坐标、不做自动换算）。
    // 旧框没有这三个字段，一律视为「属于当前版、尺寸一致」，行为不变。
    const nb={id:uid++,label:_lb,x:x0,y:y0,w,h,group:'',_pg:srcName,_bw:natW,_bh:natH}; ensureOwnBoxes('新建框'); boxes.push(nb); selectedId=nb.id;
    // 标签只作用于「刚画的这一个框」：画完立即复位为 text。下拉框若粘在 title 上，
    // 后面每个新框都会被误设成标题、一版画出多个假分组；连续画多个标题按数字键 1 切换即可。
    try{ $('lblSel').value='text'; }catch(_e){}
    if(_lb!=='text'){ try{ log('[框选] 新建 #'+nb.id+' 标签 '+_lb+'（已复位为 text）'); }catch(_e){} }
    recomputeCross(); renderBoxList(); renderResults(); }
  cur=null; draw(); return; } if(dragState){
  if(_bad){ const _o0=dragState.origBox, _b0=boxes.find(x=>x.id===dragState.id); if(_b0&&_o0) Object.assign(_b0,_o0); dragState=null; cur=null; draw(); return; }
  // 单击（按下抬起间框无位移）= 放大定位 + 右侧对应结果卡片跳上来；有位移则是拖动调框，不联动
  const _ds=dragState, _o=_ds.origBox; dragState=null; draw();
  const _b=boxes.find(x=>x.id===_ds.id);
  const _moved=!_b||_b.x!==_o.x||_b.y!==_o.y||_b.w!==_o.w||_b.h!==_o.h;
  if(!_moved && _ds.kind==='move') onBoxClick(_b);
} });
// 右键点击框 / 控制点直接删除该框（无需去侧边栏点删除）
cv.addEventListener('contextmenu',e=>{
  if(!img)return;
  e.preventDefault();
  const hh=hitHandle(e); const hb=hh?hh.box:hitBox(e);
  if(hb){ deleteBoxById(hb.id); }
});
document.addEventListener('keydown',e=>{
  const tag=document.activeElement&&document.activeElement.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT') return;
  // 数字键 1~4 快速切换框选标签（title / text / author / section），只对下一个新框生效
  if(!e.ctrlKey&&!e.altKey&&!e.metaKey){
    const _km={'1':'title','2':'text','3':'author','4':'section'};
    const _kv=_km[e.key];
    if(_kv){ const s=$('lblSel'); if(s){ s.value=_kv; try{ log('[框选标签] 已切换为 '+_kv); }catch(_e){} e.preventDefault(); return; } }
  }
  if(e.key==='Delete'||e.key==='Backspace'){
    if(selectedId!=null) deleteBoxById(selectedId);
    return;
  }
  // 键盘左右键控制画布水平滚动（没有输入焦点时）
  const cw=$('canvasWrap'); if(!cw) return;
  if(e.key==='ArrowLeft'){ cw.scrollLeft-=120; e.preventDefault(); }
  else if(e.key==='ArrowRight'){ cw.scrollLeft+=120; e.preventDefault(); }
});
// 分篇判定依赖 title 框数量：框增删/改标签后必须重算跨页视图，否则判定用的是旧快照
function recomputeCross(){ if(mergeMode!=='cross') return;
  const cts=aggregateCrossTargets(); crossResults={}; for(const ct of cts){ crossResults[ct.key]=mergeCrossTarget(ct); } }
function deleteBoxById(id){ let found=false;
  for(const pname in allPageData){ const arr=allPageData[pname].boxes; const i=arr.findIndex(b=>b.id===id); if(i>=0){ arr.splice(i,1); found=true; break; } }
  if(!found){ const i=boxes.findIndex(b=>b.id===id); if(i>=0) boxes.splice(i,1); }
  if(selectedId===id)selectedId=null; recomputeCross(); renderBoxList(); renderResults(); draw(); }
// 跨页展平：按 pageOrder 阅读顺序列出各页框，连续编号；返回每项含 box / pageName / localIdx / globalIdx
function flattenBoxes(){ const list=[]; let g=0;
  for(const pname of pageOrder){ const arr=(allPageData[pname]&&allPageData[pname].boxes)||[];
    for(let i=0;i<arr.length;i++) list.push({box:arr[i], pageName:pname, localIdx:i, globalIdx:g++}); }
  return list; }
function pageArr(pg){ return (allPageData[pg]&&allPageData[pg].boxes)||boxes; }
// 归属断言：boxes 必须始终是「当前版自己的」数组。一旦两者不是同一个引用，接下来任何画框/改框都会写进别的版，
// 表现为「这一版显示的是上一版的框」（2026-11-05 用户反馈版5 复刻版1）。发现即纠正 + 留痕，不静默继续。
function ensureOwnBoxes(tag){
  if(!srcName) return false;
  const pd=allPageData[srcName]; if(!pd||!pd.boxes) return false;
  if(boxes===pd.boxes) return false;
  log('[一致性] 警告：'+(tag||'')+' 当前画布框数组不属于正在查看的版 '+srcName+'（疑似串页），已切回本版数组');
  boxes=pd.boxes; results=pd.results||{};
  return true; }
function locateBox(id){
  for(const pname of pageOrder){ const arr=(allPageData[pname]&&allPageData[pname].boxes)||[];
    const b=arr.find(x=>x.id===id);
    if(b){ if(srcName!==pname){ pendingSelectId=id; pendingFocus=b; const idx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===pname); if(idx>=0) gotoPage(idx); }
      else { selectedId=id; renderBoxList(); draw(); focusBoxOnCanvas(b); } return; } }
  selectedId=id; renderBoxList(); draw(); }

// ---------- 画布 ↔ 结果面板 双向定位（校对时上方看图、右侧看文） ----------
// 单击框即联动，不设开关：只在该框已有识别结果时触发，拖框/拉控制点/画新框都不参与，
// 且右侧只做「滚到卡片 + 给该段涂底色」，不占用选区、不打断编辑，无需用户去关。
let pendingFocus = null;      // 跨版定位时暂存的待聚焦矩形，等图片 onload 后再执行
const FOCUS_ZOOM_MAX = 3.0;   // 放大上限 3×（小标题框放到更高会失真且丢掉上下文）
// 让画布缩放并居中到指定矩形（自然坐标 r={x,y,w,h}）。上限 3×，下限保持「适应窗口」1×（只放大不缩小）。
function focusBoxOnCanvas(r){
  if(!r || !img) return;
  const wrap=$('canvasWrap'); if(!wrap) return;
  const availW=Math.max(1,wrap.clientWidth-24), availH=Math.max(1,wrap.clientHeight-24);
  const fit=Math.min(availW*0.86/Math.max(1,r.w), availH*0.86/Math.max(1,r.h));
  const z=Math.min(FOCUS_ZOOM_MAX, Math.max(1, fit/(baseScale||1)));
  if(Math.abs(z-userZoom)>0.08) setZoom(z);   // 与当前缩放相差很小则不重绘，只滚动定位
  requestAnimationFrame(()=>{
    const wr=wrap.getBoundingClientRect(), cr=cv.getBoundingClientRect();
    const cvLeft=cr.left-wr.left+wrap.scrollLeft, cvTop=cr.top-wr.top+wrap.scrollTop;
    const cx=(r.x+r.w/2)*scale+PAD, cy=(r.y+r.h/2)*scale+PAD;
    wrap.scrollLeft=cvLeft+cx-wrap.clientWidth/2;
    wrap.scrollTop =cvTop +cy-wrap.clientHeight/2;
  });
}
// 框 → 该框所属识别结果卡片的 key。单页为 page:版名:子key，跨页为 crossauto/cross/default 等，统一反查不硬编码。
function resultKeyOfBox(b){
  if(!b) return null;
  const t=getOcrTargets(boxes).find(t=>t.boxes&&t.boxes.indexOf(b)>=0); if(!t) return null;
  const ct=aggregateCrossTargets().find(ct=>ct.pageTargets&&ct.pageTargets.some(pt=>pt.pname===srcName && pt.t.key===t.key));
  return ct?ct.key:null;
}
// 卡片 key → 卡片 DOM。逐项比对 dataset 而非属性选择器，规避 key 里冒号/中文的转义问题。
function findResultCard(key){
  if(!key) return null; let hit=null;
  document.querySelectorAll('#resultList textarea[data-key]').forEach(ta=>{ if(ta.dataset.key===key) hit=ta.closest('.card'); });
  return hit;
}
// 右侧面板平滑滚动到指定卡片，并短暂高亮
function scrollResultCard(key){
  const card=findResultCard(key); if(!card) return;
  const pane=card.closest('.right'); if(!pane) return;
  const top=card.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
  pane.scrollTo({top:Math.max(0, top-12), behavior:'smooth'});
  card.classList.add('flash'); setTimeout(()=>card.classList.remove('flash'), 1400);
}
// 单击画布上的框：放大定位 + 右侧对应卡片跳上来。
// 仅当该框「已有识别结果」时才联动 —— 框选阶段没有结果，天然不受影响，不会打扰画框。
// 框级识别结果（仅内存，不落盘、不进产物）：键 = 版名|target key|框id
// 用途：点画布上的某个框时，在该篇合并后的文本里精确选中「这个框自己读出来的那段」。
// 多个框合并成一篇后，parts 里的逐框结果原本用完即弃，导致「点框 → 定位到对应文字」无从下手。
let boxPartsMap={};
// 识别失败的框登记（仅内存，不落盘）：键 = 版名|页内 target key。用途是把「没读到」与
// 「读到了但内容不对」区分开——失败时不再把错误文本伪装成正文写进 results（那会覆盖上一次
// 成功的结果，且产物里出现整段报错文字），只在此登记，由结果面板标红提示重新识别。
// 每次「识别全部」起始清空，避免跨批次残留。
let failedBoxMap={};
function failMark(pname,tkey,total,count,error){
  failedBoxMap[pname+'|'+tkey]={total:total,count:count,error:error||''}; }
function failInfoOfTarget(t){
  try{ for(const pt of (t.pageTargets||[])){ const f=failedBoxMap[pt.pname+'|'+pt.t.key]; if(f) return f; } }catch(_e){}
  return null; }
function bpKey(pname,tkey,boxId){ return pname+'|'+tkey+'|'+boxId; }
function bpPut(pname,tkey,boxId,label,txt,tl,au,bd){
  try{ boxPartsMap[bpKey(pname,tkey,boxId)]={label:label||'',text:txt||'',t:tl||'',a:au||'',b:bd||''}; }catch(_e){}
}
function bpGet(pname,tkey,boxId){ try{ return boxPartsMap[bpKey(pname,tkey,boxId)]||null; }catch(_e){ return null; } }
// 该卡片覆盖的所有框（版名 + 局部 target key + 框 id）：编辑时用来同步片段文本。
// 复用渲染时已算好的 aggregateCrossTargets 结果，避免每张卡都全量重算一遍。
function cardPartsOfTarget(t){
  const out=[];
  try{ (t.pageTargets||[]).forEach(pt=>{ (pt.t.boxes||[]).forEach(b=>out.push({pname:pt.pname, tkey:pt.t.key, boxId:b.id})); }); }catch(_e){}
  return out;
}
// 文本被编辑后，把本篇各框记住的「这个框自己读出来的那段」同步成编辑后的新文本。
// 不同步的后果：下次点这个框仍拿识别时的原文做 indexOf，用户改过的段必然找不到 → 定位失效。
// 偏移用 adjustHlSpan 推，改动在段前整体平移、段内并进来、段后不动。
function syncPartsAfterEdit(parts, ov, nv){
  if(!parts||!parts.length||ov===nv) return;
  parts.forEach(p=>{
    const rec=bpGet(p.pname,p.tkey,p.boxId); if(!rec) return;
    for(const f of ['b','t','a']){
      const s=(rec[f]||'').trim(); if(!s) continue;
      const at=ov.indexOf(s); if(at<0) continue;   // 该段被合并去重丢弃，本就不参与定位，跳过
      const sp=adjustHlSpan(ov, nv, at, s.length);
      rec[f]=nv.slice(sp.start, sp.start+sp.len);
    }
  });
}
// 在该篇的文本框里选中「这个框自己读出来的那段文字」
// 返回 true = 已定位到片段；false = 找不到（用户改过文本 / 该框片段被合并去重丢弃），退回卡片级滚动
// ===== 片段标记（不占用选区） =====
// 目标：点画布上的框后，右侧对应那段文字被「涂色」，而不是被「选中」。
// 选区是浏览器的瞬时状态，用户一点进文本框改字就没了；改为底色标记后，
// 标记与光标/选区互不干扰，校对编辑时不会再打断。
const HL_NAME='mohen-boxfocus';
let hlScrollTa=null, hlScrollFn=null;
let hlState=null;   // {ta,start,len,value}：当前片段标记的锚点，编辑时按差异平移/扩展，黄底跟着文字走
function detachHlScroll(){ if(hlScrollTa&&hlScrollFn){ try{ hlScrollTa.removeEventListener('scroll',hlScrollFn); }catch(_e){} } hlScrollTa=null; hlScrollFn=null; }
function clearBoxFocusHighlight(){
  hlState=null;
  try{ if(window.CSS&&CSS.highlights) CSS.highlights.delete(HL_NAME); }catch(_e){}
  detachHlScroll();
  const rl=$('resultList'); if(rl) rl.querySelectorAll('.hl-band').forEach(n=>n.remove());
}
// 重新落一次标记（点进文本框、选字等会让浏览器重算高亮时用来复位；不改选区、不动光标）
function hlReapply(){ const st=hlState; if(!st||!st.ta||!st.ta.isConnected) return;
  try{ paintHlRaw(st.ta, st.start, st.len); }catch(_e){} }
// 编辑后重算标记区间：用「公共前缀/后缀」定位这次改动落在哪，
// 改动在标记前 → 整体平移；在标记后 → 不变；与标记重叠 → 把改动范围并进标记。
function adjustHlSpan(ov,nv,start,len){
  const end=start+len; if(ov===nv) return {start,len};
  const minL=Math.min(ov.length,nv.length);
  let p=0; while(p<minL && ov[p]===nv[p]) p++;
  let s=0; while(s<minL-p && ov[ov.length-1-s]===nv[nv.length-1-s]) s++;
  const oldEnd=ov.length-s, delta=nv.length-ov.length;
  let ns=start, ne=end;
  if(oldEnd<=start){ ns=start+delta; ne=end+delta; }
  else if(p>=end){ /* 改动在标记之后，区间不变 */ }
  else { ns=Math.min(start,p); ne=Math.max(end+delta, ns); }
  if(ns<0) ns=0; if(ne<ns) ne=ns; if(ne>nv.length) ne=nv.length; if(ns>ne) ns=ne;
  return {start:ns, len:ne-ns};
}
// 降级：老运行时没有 createValueRange 时，用同字体度量的隐藏镜像量出该段的行矩形，
// 再在文本框上层盖半透明色带（pointer-events:none，不影响输入与选区）。
function paintHlBands(ta, at, len){
  const wrap=ta.parentElement; if(!wrap||!wrap.classList.contains('ta-wrap')) return false;
  const layer=wrap.querySelector('.hl-layer'); if(!layer) return false;
  layer.innerHTML='';
  const cs=getComputedStyle(ta);
  const padL=parseFloat(cs.paddingLeft)||0, padT=parseFloat(cs.paddingTop)||0;
  const padR=parseFloat(cs.paddingRight)||0;
  const contentW=Math.max(8, ta.clientWidth-padL-padR);
  const m=document.createElement('div');
  m.style.cssText='position:absolute;visibility:hidden;left:-99999px;top:0;box-sizing:content-box;width:'+contentW+'px;'+
    'padding:0;border:0;margin:0;font-family:'+cs.fontFamily+';font-size:'+cs.fontSize+';font-weight:'+cs.fontWeight+
    ';font-style:'+cs.fontStyle+';line-height:'+cs.lineHeight+';letter-spacing:'+cs.letterSpacing+
    ';word-spacing:'+cs.wordSpacing+';white-space:pre-wrap;overflow-wrap:break-word;word-break:'+cs.wordBreak+
    ';tab-size:'+(cs.tabSize||8)+';';
  m.textContent=ta.value;
  document.body.appendChild(m);
  try{
    const tn=m.firstChild; if(!tn) return false;
    const s=Math.max(0,Math.min(at,ta.value.length)), e2=Math.max(s,Math.min(at+len,ta.value.length));
    const rng=document.createRange(); rng.setStart(tn,s); rng.setEnd(tn,e2);
    const base=m.getBoundingClientRect();
    const rects=Array.from(rng.getClientRects()); if(!rects.length) return false;
    const inner=document.createElement('div'); inner.className='hl-inner';
    rects.forEach(r=>{ const d=document.createElement('div'); d.className='hl-band';
      d.style.left=(r.left-base.left+padL)+'px'; d.style.top=(r.top-base.top+padT)+'px';
      d.style.width=Math.max(2,r.width)+'px'; d.style.height=Math.max(2,r.height)+'px'; inner.appendChild(d); });
    layer.appendChild(inner);
    const sync=()=>{ inner.style.transform='translate(0,'+(-(ta.scrollTop||0))+'px)'; };
    sync(); hlScrollTa=ta; hlScrollFn=sync; ta.addEventListener('scroll',sync,{passive:true});
    return true;
  }catch(_e){ return false; } finally { m.remove(); }
}
// 一级：CSS Custom Highlight API + OpaqueRange（textarea.createValueRange，Chromium 152+）
// 只画标记、不动锚点：编辑跟随与点击复位都走这里，避免把 hlState 一起清掉
function paintHlRaw(ta, at, len){
  try{ if(window.CSS&&CSS.highlights) CSS.highlights.delete(HL_NAME); }catch(_e){}
  detachHlScroll();
  const rl=$('resultList'); if(rl) rl.querySelectorAll('.hl-band').forEach(n=>n.remove());
  try{
    if(ta&&typeof ta.createValueRange==='function'&&window.Highlight&&window.CSS&&CSS.highlights){
      const s=Math.max(0,Math.min(at,ta.value.length)), e2=Math.max(s,Math.min(at+len,ta.value.length));
      CSS.highlights.set(HL_NAME, new window.Highlight(ta.createValueRange(s,e2)));
      return true;
    }
  }catch(_e){}
  try{ return paintHlBands(ta, at, len); }catch(_e2){ return false; }
}
function highlightBoxRange(ta, at, len){ clearBoxFocusHighlight(); return paintHlRaw(ta, at, len); }
function selectBoxText(b){
  try{
    const t=getOcrTargets(boxes).find(t=>t.boxes&&t.boxes.indexOf(b)>=0); if(!t) return false;
    const rec=bpGet(srcName,t.key,b.id); if(!rec) return false;
    const key=resultKeyOfBox(b); if(!key) return false;
    const card=findResultCard(key); if(!card) return false;
    const ta=card.querySelector('textarea'); if(!ta) return false;
    // 候选顺序：正文 > 标题 > 署名。正文框就落在正文那段，标题框落在标题那段。
    const cand=[rec.b,rec.t,rec.a].map(s=>(s||'').trim()).filter(s=>s.length>0);
    let at=-1, len=0;
    for(const s of cand){ const i=ta.value.indexOf(s); if(i>=0){ at=i; len=s.length; break; } }
    if(at<0){ return false; }
    ta.focus({preventScroll:true});
    // 只把光标放到该段开头，不选中任何文字：选中态一改字就消失，也会和用户自己的选字打架
    try{ ta.setSelectionRange(at, at); }catch(_e){}
    // 用底色标出该框对应的那段文字（不占选区，编辑过程中仍在）
    highlightBoxRange(ta, at, len);
    // 记下锚点：之后编辑时按前后文差异把标记平移/扩展，黄底跟着文字走，不会一改就没
    hlState={ta, start:at, len, value:ta.value};
    // 文本框已自动撑高时无需内部滚动；若设了固定高度导致内部滚动，把光标估到可视区
    if(ta.scrollHeight>ta.clientHeight+8){
      const before=ta.value.slice(0,at);
      const ratio=(before.length/Math.max(1,ta.value.length));
      ta.scrollTop=Math.max(0, ratio*ta.scrollHeight - ta.clientHeight/2);
    }
    scrollResultCard(key);
    return true;
  }catch(_e){ return false; }
}
function onBoxClick(b){
  if(!b || !img) return;
  const key=resultKeyOfBox(b); if(!key) return;
  const it=resultToItem(crossResults[key]);
  if(!(it.raw||it.body||it.title||it.author)) return;
  focusBoxOnCanvas(b);
  // 优先做片段级定位（把该框对应的那段文字涂色标出），定位不到才退回卡片级滚动
  clearBoxFocusHighlight();
  if(!selectBoxText(b)) scrollResultCard(key);
}
// 反向：结果卡片上的「定位」按钮 → 画布定位到该篇（用整篇所有框的并集）。
// 目标框在别的版时先翻页，把矩形存进 pendingFocus，等图片 onload 再定位（复用 pendingSelectId 通道）。
function locateByResultKey(key){
  const ct=aggregateCrossTargets().find(x=>x.key===key); if(!ct||!ct.pageTargets||!ct.pageTargets.length) return;
  const pt=ct.pageTargets[0]; if(!allPageData[pt.pname]) return;
  const r=computeUnionBox(pt.t.boxes||[]); if(!isFinite(r.x)) return;
  const first=(pt.t.boxes||[])[0]||null;
  if(srcName!==pt.pname){
    if(first) pendingSelectId=first.id;
    pendingFocus=r;
    const idx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===pt.pname); if(idx>=0) gotoPage(idx);
    return;
  }
  if(first){ selectedId=first.id; renderBoxList(); draw(); }
  focusBoxOnCanvas(r);
}

function renderBoxList(){ const el=$('boxList');
  ensureOwnBoxes('渲染框列表');   // 先保证 boxes 属于当前版，避免把别版的框当成自己的列出来
  const cross = mergeMode==='cross';
  const view = cross ? flattenBoxes() : boxes.map((b,i)=>({box:b, pageName:srcName, localIdx:i, globalIdx:i}));
  if(!view.length){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">尚未框选</div>'; return; } el.innerHTML='';
  const grpMap=new Map(); view.forEach(v=>{ if(!grpMap.has(v.pageName)) grpMap.set(v.pageName, effectiveGroups(pageArr(v.pageName))); });
  // 跨页模式编号：必须按「成篇」全局累计，否则每版都从「自动1」重来，与下方「跨页·项 N」对不上。
  // crossNo 直接由 aggregateCrossTargets 反查（成篇即编号），因此在「续页合并」后同一篇的各版框会共用同一个编号；
  // 万一聚合抛错，退回 grpOff（按各版标题框数量累计）保证至少有全局递增的编号。单页模式两者皆空，行为不变。
  const crossNo=new Map(), grpOff=new Map();
  if(cross){ try{ aggregateCrossTargets().forEach((t,ti)=>{ (t.boxes||[]).forEach(b=>{ if(!crossNo.has(b.id)) crossNo.set(b.id,ti+1); }); }); }catch(_e){}
    let acc=0; (pageOrder.length?pageOrder:[srcName]).forEach(pn=>{ grpOff.set(pn,acc); let n=0; (pageArr(pn)||[]).forEach(b=>{ if(!b.group&&b.label==='title') n++; }); acc+=n; }); }
  view.forEach((v)=>{ const b=v.box; const d=document.createElement('div'); d.className='box'+(b.id===selectedId?' sel':'');
    const effG = b.group || grpMap.get(v.pageName).get(b.id) || 'auto0';
    const color=(b.group?groupColor(b.group):LBL_COLOR[b.label])||'#dc2626'; let groupHint;
    if(b.group){ groupHint='组:'+b.group; }
    else if(cross){ const gN=crossNo.get(b.id)||((parseInt(String(effG).replace(/^auto/,''),10)||0)+(grpOff.get(v.pageName)||0)); groupHint='自动'+gN; }
    else { groupHint='自动'+String(effG).replace(/^auto/,''); }
    const num = cross ? (v.globalIdx+1) : (v.localIdx+1);
    const pageTag = cross ? `<span class="bx-page" title="所在版">${esc(v.pageName.replace(/\.[^.]+$/,'')||'')}</span>` : '';
    d.innerHTML=`<span class="bx-num" style="background:${color}">${num}</span>${pageTag}
      <div class="bx-main">
        <select data-page="${esc(v.pageName)}" data-idx="${v.localIdx}">${['title','section','author','text'].map(l=>`<option ${l===b.label?'selected':''}>${l}</option>`).join('')}</select>
        <input class="grp" data-page="${esc(v.pageName)}" data-idx="${v.localIdx}" value="${esc(b.group||'')}" placeholder="自动分组" title="留空=按标题自动分组；填同名手动合并">
        <span class="bx-group" style="color:${color}" title="${groupHint}">${groupHint}</span>
      </div>
      <div class="bx-ops">
        <button class="mini sec" data-up="${v.localIdx}" data-page="${esc(v.pageName)}">↑ 上移</button><button class="mini sec" data-dn="${v.localIdx}" data-page="${esc(v.pageName)}">↓ 下移</button>
        <button class="mini" data-loc="${b.id}">定位</button><button class="mini sec" data-rerun="${b.id}" title="只重新识别这一框并显示新文本，不自动写回（需手动复制粘贴）">重识别</button><button class="mini stop" data-del="${b.id}">✕ 删除</button>
      </div>`; el.appendChild(d); });
  el.querySelectorAll('select').forEach(s=>s.onchange=e=>{ const pg=e.target.dataset.page, i=+e.target.dataset.idx; pageArr(pg)[i].label=e.target.value; recomputeCross(); draw(); renderResults(); renderBoxList(); });
  el.querySelectorAll('input.grp').forEach(inp=>inp.onchange=e=>{ const pg=e.target.dataset.page, i=+e.target.dataset.idx; pageArr(pg)[i].group=e.target.value.trim(); recomputeCross(); renderBoxList(); draw(); renderResults(); });
  el.querySelectorAll('[data-del]').forEach(b=>b.onclick=e=>{ deleteBoxById(+e.target.dataset.del); });
  el.querySelectorAll('[data-up]').forEach(b=>b.onclick=e=>{ const i=+e.target.dataset.up, pg=e.target.dataset.page; const arr=pageArr(pg); if(i>0){[arr[i-1],arr[i]]=[arr[i],arr[i-1]];recomputeCross();renderBoxList();draw();} });
  el.querySelectorAll('[data-dn]').forEach(b=>b.onclick=e=>{ const i=+e.target.dataset.dn, pg=e.target.dataset.page; const arr=pageArr(pg); if(i<arr.length-1){[arr[i],arr[i+1]]=[arr[i+1],arr[i]];recomputeCross();renderBoxList();draw();} });
  el.querySelectorAll('[data-loc]').forEach(b=>b.onclick=e=>{ locateBox(+e.target.dataset.loc); });
  el.querySelectorAll('[data-rerun]').forEach(b=>b.onclick=e=>{ e.stopPropagation(); rerunBox(+e.target.dataset.rerun); });
  rerunSyncButtons(); }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
// ---------- 单框重识别：只返回新文本，绝不写回 ----------
// 单页 / 跨页行为完全一致：重新识别这一个框，把新文本显示在小窗里，由用户自行复制粘贴。
// 不写 crossResults / allPageData / boxPartsMap / failedBoxMap，不触发 renderResults() /
// recomputeCross() / 导出 / 统计，避免任何一次重算冲掉用户手改。
let rerunBusy=false, rerunCtx=null;
function rerunSyncButtons(){ let v=false; try{ v=rerunBusy; }catch(_e){}
  document.querySelectorAll('[data-rerun]').forEach(b=>{ b.disabled=v; }); }
function rerunShow(head,status,text,color){
  const p=$('rerunPanel'); if(!p) return;
  const has=(text!==null && text!==undefined);
  p.style.display='block';
  $('rerunHead').textContent=head||'';
  $('rerunStatus').textContent=status||'';
  $('rerunStatus').style.color=color||'var(--mut)';
  $('rerunText').value=has?String(text):'';
  $('rerunText').style.display=has?'block':'none';
  $('rerunCopy').style.display=has?'':'none';
}
function rerunSetBusy(v){ rerunBusy=v; rerunSyncButtons(); }
$('rerunClose').onclick=()=>{ const p=$('rerunPanel'); if(p) p.style.display='none'; };
$('rerunCopy').onclick=()=>{
  const ta=$('rerunText'); if(!ta.value) return;
  ta.focus(); ta.select(); let ok=false;
  try{ ok=document.execCommand('copy'); }catch(_e){}
  if(!ok){ try{ navigator.clipboard.writeText(ta.value).catch(()=>{}); }catch(_e2){} }
  $('rerunHint').textContent='已复制，请粘贴到下方对应段落（结果不会自动写回）。';
};
$('rerunLocate').onclick=()=>{
  const c=rerunCtx; if(!c) return;
  try{
    if(c.pname===srcName && boxes.indexOf(c.box)>=0 && selectBoxText(c.box)){
      $('rerunHint').textContent='已用黄底标出该框对应的段落。'; return; }
    const ct=aggregateCrossTargets().find(t=>(t.boxes||[]).indexOf(c.box)>=0);
    if(ct){ scrollResultCard(ct.key); $('rerunHint').textContent='已滚到对应结果卡片（该框不在当前版，未切换页面以免冲掉手改）。'; return; }
  }catch(_e){}
  $('rerunHint').textContent='未找到对应结果卡片，请先点「识别全部」生成结果。';
};
async function rerunBox(boxId){
  if(rerunBusy) return;
  let pname=null, b=null;
  for(const pn of pageOrder){ const arr=(allPageData[pn]&&allPageData[pn].boxes)||[];
    const hit=arr.find(x=>String(x.id)===String(boxId)); if(hit){ pname=pn; b=hit; break; } }
  if(!pname||!b){ alert('找不到该框，请刷新框列表后重试'); return; }
  const flat=flattenBoxes().find(v=>v.box===b);
  const num=(mergeMode==='cross')?(flat?(flat.globalIdx+1):'?'):(boxes.indexOf(b)>=0?boxes.indexOf(b)+1:'?');
  let artNo=0; try{ const cts=aggregateCrossTargets();
    for(let i=0;i<cts.length;i++){ if((cts[i].boxes||[]).indexOf(b)>=0){ artNo=i+1; break; } } }catch(_e){}
  const head='框 #'+(b.id!=null?b.id:'?')+' · '+String(pname).replace(/\.[^.]+$/,'')+' · '+(b.label||'未标')+
    (mergeMode==='cross'?' · 第 '+(artNo||'?')+' 篇 / 第 '+num+' 框':' · 第 '+num+' 框');
  rerunCtx={pname:pname, box:b};
  const seq0=loadSeq, dim0=pageDims.get(pname)||'';
  rerunSetBusy(true);
  rerunShow(head,'识别中…（只返回新文本，不自动写回）',null,'var(--pri)');
  $('rerunHint').textContent='结果不会自动写回，请自行粘贴到下方对应段落。';
  log('[单框重识别] 开始：'+head);
  try{
    await refreshRunCfg();
    const j=await fetch('/api/image?name='+encodeURIComponent(pname)+'&dir=cropped_hi').then(r=>r.json()).catch(e=>({error:e.message}));
    if(j.error||!j.data) throw new Error(j.error||'取图失败（no data）');
    const im=await new Promise((res,rej)=>{ const m=new Image(); m.onload=()=>res(m); m.onerror=()=>rej(new Error('整版图解码失败')); m.src=j.data; });
    const nW=im.naturalWidth, nH=im.naturalHeight;
    // 竞态：期间翻页 / 该版重抽图改了像素尺寸 / 框被删，都丢弃结果，避免用错基准
    if(loadSeq!==seq0) throw new Error('期间已切换图片，本次结果已丢弃，请重试');
    if(dim0 && dim0!==(nW+'x'+nH)) throw new Error('该版图片尺寸已变（'+dim0+' → '+nW+'x'+nH+'），框坐标基准可能失效，请重试');
    if(!allPageData[pname] || !(allPageData[pname].boxes||[]).some(x=>String(x.id)===String(boxId))) throw new Error('该框已被删除，本次结果已丢弃');
    if(!(b.w>=8 && b.h>=8)) throw new Error('框尺寸异常 '+Math.round(b.w)+'×'+Math.round(b.h)+'px，请删除该框重画');
    const r=await ocrBox(b, im, nW, nH, {pname:pname, quiet:true});
    if(loadSeq!==seq0) throw new Error('期间已切换图片，本次结果已丢弃，请重试');
    if(!r || r.failed) throw new Error((r&&r.error)||'识别失败');
    if(r.finish_reason==='SKIPPED') throw new Error('框尺寸异常，已跳过识别，请删除该框重画');
    const txt=String(r.text||'');
    rerunShow(head,'识别完成 · 新文本 '+txt.length+' 字（未写回，需手动复制粘贴）',txt,'var(--ok)');
    log('[单框重识别] 完成：'+head+'，新文本 '+txt.length+' 字；未写回任何结果，需手动复制粘贴。');
  }catch(e){
    const msg=String((e&&e.message)||e);
    rerunShow(head,'未获得新文本：'+msg,null,'var(--err)');
    log('[单框重识别] 失败：'+head+' — '+msg+'（原结果未改动）');
  }finally{ rerunSetBusy(false); }
}

// ---------- 识别 ----------
// 组内排序优先级：标题 → 作者 → 正文。节标题(section)与正文同级（同为 2），
// 同级再按框选顺序排，因此节标题会留在它在正文中的原位置，不会被挪到段首或段尾。
function labelPriority(l){ return l==='title'?0:l==='author'?1:2; }
// 生效组计算（单一事实来源）：无 group 的框按"框选顺序"遍历，遇到一个 title 就开新组（auto1/auto2…），
// 该标题及其之后、下一个标题之前的框都归这一组；首个标题之前的框并入第一组（auto1）；整页无标题则统一 auto0（一篇）。
// 显式标 group 的框始终优先，按组各自独立成篇。
// 注意：只有 title 开新组；section（节标题）是文章内部的分节小标题，永远留在当前组里，不参与分篇。
function effectiveGroups(arr){
  const map=new Map(); const pre=[]; let idx=0, cur=null;
  (arr||[]).forEach(b=>{
    if(b.group){ map.set(b.id, b.group); return; }
    if(b.label==='title'){ idx++; cur='auto'+idx; map.set(b.id, cur); }
    else { if(!cur){ cur='auto0'; pre.push(b.id); } map.set(b.id, cur); }
  });
  if(idx>0){ pre.forEach(id=>map.set(id,'auto1')); } // 首个标题前的框并入第一组
  return map;
}
function getOcrTargets(bs){ bs=bs||boxes;
  if(!bs.length){ return []; } // 无框：禁止识别，返回空目标（绝不整版兜底）
  const eg=effectiveGroups(bs); const groups=new Map();
  bs.forEach((b,i)=>{ const g=eg.get(b.id); const isAuto=!b.group; const key=isAuto?('auto:'+g):('grp:'+g);
    if(!groups.has(key)) groups.set(key,{key,frames:[],group:isAuto?'':g,isAuto});
    groups.get(key).frames.push({b,oi:i}); });
  return Array.from(groups.values()).map(g=>{ g.frames.sort((a,c)=>(labelPriority(a.b.label)-labelPriority(c.b.label))||(a.oi-c.oi));
    const boxesOf=g.frames.map(f=>f.b);
    const label = g.isAuto ? (boxesOf.length>1 ? g.key.replace('auto:','') : boxesOf[0].label) : ('group:'+g.group);
    return {key:g.key,boxes:boxesOf,group:g.group||'',label,auto:g.isAuto?g.key.replace('auto:',''):''}; }); }

// 跨页分篇：以「标题」为唯一边界，不按版切。
//   跨页模式把多版按阅读顺序串成一条流，遇到标题框就开新篇；
//   没有标题的框（同版续文、无题头的续页）一律并入当前这一篇。
//   这与单页模式的本质区别：单页模式边界落在版内、绝不跨版合并；跨页模式允许跨版续写。
//   续页题头（「……（续）」「（二）」之类）若与当前篇标题相同或高度相似，也算续文并入当前篇，
//   否则「每版都印一遍题头」的报刊会被误判成多篇。
function countTitleBoxes(){
  let n=0;
  for(const pname of pageOrder){ const pd=allPageData[pname]; if(!pd) continue;
    for(const b of (pd.boxes||[])) if(b.label==='title') n++; }
  return n; }
// 标题规范化：去空白、去「（续）/（二）/（未完）」类标记、去尾部分隔符
function _normTitle(s){
  return (s||'')
    .replace(/[\s\u3000]/g,'')
    .replace(/[（(]\s*(?:续|續|续完|續完|未完|待续|待續|[一二三四五六七八九十百]+|\d+)\s*[)）]/g,'')
    .replace(/^(?:续|續|续完|續完|续前|續前)/,'')
    .replace(/[.。·・…—\-－_:：;；,，]+$/,'')
    .trim(); }
// 是否为「章节小标题」：以（一）/一、/1. 之类序号起头。报刊里这类是正文内部分节，
// 不是新文章的开篇题头，必须续写当前篇，否则一篇长文会被它自己的小标题切成多篇。
function _isSectionMark(s){
  const t=(s||'').replace(/^[\s\u3000]+/,'');
  return /^[（(]\s*[一二三四五六七八九十百\d]+\s*[)）]/.test(t)
      || /^[一二三四五六七八九十百]+\s*[、.．]/.test(t)
      || /^\d+\s*[、.．]\s*/.test(t); }
// 是否为同一篇的题头：完全相同 / 一方是另一方的片段（续页题头常被截短）/ 公共前缀达 80%（错字、繁简差异）
function _titleSame(a,b){
  if(!a||!b) return false;
  if(a===b) return true;
  const s=a.length<=b.length?a:b, l=a.length<=b.length?b:a;
  if(s.length>=4 && l.indexOf(s)>=0 && s.length>=Math.ceil(l.length*0.5)) return true;
  const m=Math.min(a.length,b.length); let n=0;
  while(n<m && a[n]===b[n]) n++;
  return n>=Math.max(6, Math.ceil(m*0.8)); }
// 跨页聚合：把 pageOrder 中各页的框/识别结果按阅读顺序合并成「文章」。
// 分篇边界只看标题，不按版切 —— 这是跨页模式与单页模式的根本区别（单页模式每版成篇，绝不跨版合并）：
//   ① 显式组 group → 同组跨版合成一篇（cross:group，优先级最高）；
//   ② 有标题框 → 开新篇；但「与该篇正题相同/相似的续页题头」「只剩（二）（续）这类标记的题头」
//      「以（一）/一、/1. 起头的章节小标题」都算续写当前篇，不另起；
//   ③ 无标题框 → 并入当前篇（同版续文、无题头的续页）；开头就无标题则落 __cross_default__ 兜底。
function aggregateCrossTargets(){
  if(!pageOrder.length) return getOcrTargets().map(t=>({...t,pageTargets:[{pname:srcName,t}]}));
  const groups=new Map();
  let curKey=null;    // 当前正在续写的篇（最近一次由标题开篇的文章）
  let curTitle='';    // 该篇规范化后的标题，用于续页题头比对
  for(const pname of pageOrder){
    const pd=allPageData[pname]; if(!pd) continue;
    const ts=getOcrTargets(pd.boxes);
    for(const t of ts){
      const titled=(t.boxes||[]).some(b=>b.label==='title');
      // 该组识别出的标题（识别前为空字符串；识别完成后才可用于续页题头比对）
      const hasRes=!!(pd.results && pd.results[t.key]);
      const rawTitle=titled?resultToItem(hasRes?pd.results[t.key]:null).title:'';
      const recTitle=_normTitle(rawTitle);
      let k,label,isArticle=false;
      if(mergeMode!=='cross'){
        // 单页模式：分篇边界落在版内（按 group / 标题切分成若干篇），key 必带版名，绝不跨页合并
        k='page:'+pname+':'+t.key; label=t.group?('group:'+t.group):t.label; isArticle=true;
      } else if(t.group){
        // ① 显式组最高优先级：同组跨版合成一篇
        k='cross:'+t.group; label='group:'+t.group; isArticle=true;
      } else if(titled){
        // ② 有标题 → 开新篇。以下三种算续写当前篇，不另起：
        //    a) 标题与该篇正题相同/高度相似（续页题头重复印正题）；
        //    b) 识别完成后标题只剩「（二）」「（续）」这类标记（规范化后为空）——无法命名新篇；
        //    c) 以（一）/一、/1. 起头的章节小标题——是正文分节，不是新文章题头。
        //    注意识别前 hasRes 为假，此时一律保守按新篇处理，避免尚未拿到文本就误并。
        const isCont=!!(curKey && hasRes && (
            recTitle ? ((curTitle && _titleSame(recTitle, curTitle)) || _isSectionMark(rawTitle))
                     : true));
        if(isCont){ k=curKey; label=groups.get(curKey).label; }
        else { k='crossauto:'+pname+':'+t.key; label=t.label; isArticle=true; }
      } else {
        // ③ 无标题 → 并入当前篇（同版续文 / 无题头的续页）；开头就无标题则落兜底篇
        if(curKey){ k=curKey; label=groups.get(curKey).label; }
        else { k='__cross_default__'; label='跨页·续页（开头无标题）'; }
      }
      if(!groups.has(k)) groups.set(k,{key:k,label,group:t.group||'',boxes:(t.boxes||[]).slice(),pageTargets:[]});
      else { const g=groups.get(k); if(t.boxes) g.boxes.push(...t.boxes); }
      groups.get(k).pageTargets.push({pname,t});
      if(isArticle){ curKey=k; if(recTitle) curTitle=recTitle; }
    }
  }
  return Array.from(groups.values());
}
// —— 合并后剔除正文里重复的署名 ——
// 场景：署名框被标成「正文」，它读出的「雲姑」会按正文并入 body；同时另一个正文框
// 又自己把署名读进 author 字段，于是产物里作者出现两次（作者：云姑 + 正文首行「雲姑」）。
// 判定刻意收窄：只在正文前 3 块里找、必须是单行、极短、不含句读，且与作者「同字或差 1 字」
// （差 1 字用于吸收繁简与个别 OCR 差异，如 云姑/雲姑）；一篇文章最多删一处。
function _signNorm(s){ return String(s||'').replace(/[\s\u3000]+/g,'')
  .replace(/[，。、；：:·．\.\-—－（）()「」『』《》〈〉“”"'`]/g,''); }
function _levAtMost1(a,b){ if(a===b) return true; const la=a.length, lb=b.length;
  if(Math.abs(la-lb)>1) return false;
  if(la===lb){ let d=0; for(let i=0;i<la;i++){ if(a[i]!==b[i]){ if(++d>1) return false; } } return true; }
  const s=la<lb?a:b, l=la<lb?b:a;
  for(let i=0;i<s.length;i++){ if(s[i]!==l[i]) return s.slice(i)===l.slice(i+1); } return true; }
function _sameSign(a,b){ const x=_signNorm(a), y=_signNorm(b); if(!x||!y) return false;
  if(x===y) return true; return (x.length<=12 && y.length<=12) ? _levAtMost1(x,y) : false; }
function dropDupSign(author, body){
  const bd=body||''; if(!bd) return bd;
  const auN=_signNorm(author); if(!auN || auN.length>12) return bd;  // 作者字段过长说明不是署名，放过
  const blocks=bd.split(/\n{2,}/); if(blocks.length<2) return bd;
  let removed=false; const out=[];
  blocks.forEach((blk,i)=>{ const t=(blk||'').trim();
    const hit = i<3 && !removed && t && t.indexOf('\n')<0 && _signNorm(t).length<=12 &&
                !/[。！？，；：、]/.test(t) && _sameSign(auN,t);
    if(hit){ removed=true; return; }
    out.push(blk); });
  return removed ? out.join('\n\n').replace(/^\s+|\s+$/g,'') : bd; }
function mergeCrossTarget(ct){
  let title='',author='',bodies=[];
  for(const {pname,t} of ct.pageTargets){
    const pd=allPageData[pname]; if(!pd) continue;
    const item=resultToItem(pd.results[t.key]);
    if(item.title){
      if(!title) title=item.title;               // 首个非空作为标题
      else if(!_titleSame(_normTitle(item.title), _normTitle(title))) bodies.push(item.title);
      // 后续标题：与正题相同者视为续页题头，丢弃不进正文（避免产物里重复一遍标题）；
      // 明显不同者（显式组里混了另一篇）绝不能丢，并入正文
    }
    if(item.author){
      if(!author) author=item.author;            // 首个非空作为作者
      else bodies.push(item.author);             // 后续非空：并入正文，不静默丢弃
    }
    if(item.body) bodies.push(item.body);
  }
  // 跨页拼接：若上一段末句未收尾（无句号/问号/叹号/引号收尾），说明是跨页续句，直接衔接不分段。
  // 例外：上一段是「（一）/一、/1.」起头的节标题，或极短（不足 12 字）的独立行时，另行分段——
  // 否则节标题会被后一段粘成「（一）从人的纪念到人的教训本年一月中，德国人民……」。
  // 阈值不宜放大：跨页被切断的句子也可能只有十几字，放大就会把真正的续句误断开。
  const SENT_END=/[。！？」』）】.!?\"'）]\s*$/;
  const HEADING_LIKE_MAX=12;
  let body='', lastSeg='';
  const bs=bodies.filter(Boolean);
  for(let k=0;k<bs.length;k++){
    const seg=bs[k].replace(/^\s+|\s+$/g,'');
    if(!seg) continue;
    if(!body){ body=seg; lastSeg=seg; continue; }
    const ends=SENT_END.test(lastSeg);
    const headingLike=(!ends && (_isSectionMark(lastSeg) || lastSeg.length<HEADING_LIKE_MAX));
    if(ends || headingLike) body += '\n\n' + seg;   // 上段已收尾 / 上段是短标题行 → 新段落
    else body += seg;                                // 跨页续句 → 直接衔接
    lastSeg=seg;
  }
  body = dropDupSign(author, body);   // 正文里重复出现的署名（作者已单独成字段）剔除
  const raw=`标题：${title}\n作者：${author}\n正文：${body}`.trim();
  return {title,author,body,raw};
}
function crossBaseName(){ return (pageOrder.length?pageOrder[0]:srcName).replace(/\.[^.]+$/,'')+'_跨页'; }
// 把后端返回的绝对路径转成相对运行时目录的简短显示（用于日志中「已打开…」）
function osRel(p){ if(!p) return p; try{ const base=((window.__RUNTIME_DIR)||''); if(base && p.startsWith(base)){ const rel=p.slice(base.length).replace(/^[\\/]/,''); return /^output[\\/]/.test(rel) ? rel : 'output/'+rel; } }catch(e){} return p; }

// 裁切图四周补纯白缓冲边：竖排阅读起点永远贴右边缘，模型对紧贴图像边界的文字召回率偏低，
// 常整列漏读（表现为「开头必丢一列」）。补边后贴边列离开图像边界，恢复正常识别。
// 边距自适应：min(48, max(16, 4% × 长边))；纯白无内容，不引入噪声，源框坐标不变。
function ocrPad(w,h){ return Math.min(48, Math.max(16, Math.round(0.04*Math.max(w,h)))); }
// 并发识别：同版内的框允许同时发出。上限来自配置 OCR_CONCURRENCY（缺省 2，上限 6），
// 设成 1 即退回严格串行。结果按索引写回，绝不依赖完成先后——段落顺序错乱不会报错，
// 只会在成品里静默出现前后颠倒，是最不该由并发引入的故障。
function ocrConcurrency(){
  const v=(backendCfg&&backendCfg.values&&backendCfg.values.OCR_CONCURRENCY)||'';
  const n=parseInt(v,10);
  return (isFinite(n)&&n>0)?Math.min(n,6):3; }
// 识别开始前重读一次配置：并发数改在 box_config.json 里即可生效，
// 不必重启程序、也不必刷新页面（刷新会丢掉当前框选状态）。
async function refreshRunCfg(){
  try{ const r=await fetch('/api/config'); const j=await r.json();
    if(j&&j.values){ backendCfg=backendCfg||{}; backendCfg.values=Object.assign({}, backendCfg.values||{}, j.values); }
  }catch(_e){}
}
async function runLimited(items, limit, fn){
  const list=items||[]; if(!list.length) return [];
  const n=Math.max(1, Math.min(limit||1, list.length));
  const out=new Array(list.length); let next=0;
  const worker=async()=>{ for(;;){ const i=next++; if(i>=list.length) return; out[i]=await fn(list[i], i); } };
  await Promise.all(Array.from({length:n}, worker));
  return out; }
// —— OCR 输出质量自检 ——
// 便宜的视觉模型（如千问）遇到复杂竖排/密排原文会「整段读飞」：返回空、返回一两行、
// 返回大量非汉字乱码或连续重复字符。这类失败此前静默混进结果，用户以为工具坏了。
// 判定为可疑时记入 ocrQualityFlags，识别结束后统一提示「改用豆包 Seed-2.1-turbo 重跑」。
let ocrQualityFlags=[];
function ocrQualityIssue(text, b){
  const lab=(b&&b.label)||'';
  // 只剥掉行首的「标题：/作者：/正文：」字段名，保留其后内容（不能连整行一起删）
  const raw=(text||'').replace(/^(标题|作者|正文)[:：]/gm,'').trim();
  const visible=raw.replace(/\s/g,'');
  if(!visible) return '返回空内容';
  const han=(visible.match(/[\u4e00-\u9fff]/g)||[]).length;
  const area=(b&&b.w&&b.h)?(b.w*b.h):0;
  if(/(.)\1{9,}/.test(visible)) return '出现连续重复字符，疑似模型退化输出';
  const bad=(visible.match(/[�\uFFFD□]/g)||[]).length;
  if(visible.length>=20 && bad/visible.length>0.12) return '不可辨字符占比 '+Math.round(bad/visible.length*100)+'%';
  // 标题/作者/节标题框本来就短，不做长度判断（否则正常短标题会被误报）
  if(lab==='title'||lab==='author'||lab==='section') return '';
  // 外文原文（成词、空格分界的拉丁文本）不算乱码，跳过汉字占比判断
  const latinWords=(visible.match(/[A-Za-z]{2,}/g)||[]).length;
  if(han/visible.length<0.15 && latinWords>=8) return '';
  // 大框却只读出几个字：正常密排框不会这样，判定疑似整段漏读
  if(visible.length<10 && area>150000) return '框内面积较大但仅 '+visible.length+' 字，疑似整段漏读';
  if(visible.length>=25 && han/visible.length<0.15) return '汉字占比仅 '+Math.round(han/visible.length*100)+'%，疑似乱码/串行';
  return '';
}
// 按框的显式标签，把 parseArticle 的结果收进对应字段（单框 / 多框共用，避免两处逻辑分叉）。
// 关键防御在 author 分支：模型在署名框里没写「作者：」前缀时 p.author 为空、整段正文落在
// p.body，旧写法 `parsed.author=p.author||p.body` 会把整段正文塞进作者行。作者行本身不会丢字，
// 但多框合并时「作者只取首个非空」会让其余 parts 的作者字段整段消失——这是字面意义的缺行。
// 署名必然极短，因此超过阈值或含句读的一律按正文处理。
const AUTHOR_AS_SIGN_MAX=24;
function foldParsedByLabel(b,p){
  const parsed={title:'',author:'',body:''};
  const lab=b?b.label:'';
  if(lab==='title'){ parsed.title=p.title||p.body; }
  else if(lab==='author'){
    // 候选署名：模型写了「作者：」就用该字段，没写前缀时整段落在 body，只能看长短与形态判断。
    const _au=(p.author||'').trim(), _bd=(p.body||'').trim(), _sign=_au||_bd;
    const _isSign=!!_sign && _sign.length<=AUTHOR_AS_SIGN_MAX && _sign.indexOf('\n')<0 &&
                  !/[，。；：、！？,;:!?]/.test(_sign);
    if(_isSign) parsed.author=_sign; else parsed.body=_bd||_au;   // 不像署名：整段留作正文，避免合并阶段被丢弃
  }
  else if(lab==='section'){ parsed.body=(p.body||p.title||p.author||'').replace(/^(标题|作者|正文)[：:]\s*/,''); }
  // 正文框（含历史结果）：整框内容一律作正文，不再拆出标题/作者。
  // 一个框一个字段，text 框就是正文框；模型即便仍按旧提示词给出「标题：xxx」，那也只是它把某行读成了标题，
  // 不代表这篇真有标题 —— 先按模型原始输出的行序整段取回，再剥掉字段名前缀，一个字都不丢。
  else if(lab==='text'){
    const _strip=s=>(s||'').replace(/^[ \t]*(标题|作者|正文)[：:][ \t]*\n?/gm,'').replace(/\n{3,}/g,'\n\n').trim();
    const _rawStripped=_strip(p.raw);
    parsed.body=_rawStripped || [p.title,p.author,p.body].filter(x=>x).join('\n\n');
  }
  // 未标标签的框（历史数据）：三个字段全部保留。曾只取 body —— 模型在未标标签框里输出「标题：xxx」时
  // 该行被整段丢弃，表现为「结构化后第一个框的标题被吞」。
  else { parsed.title=p.title||''; parsed.author=p.author||''; parsed.body=p.body||''; }
  return parsed;
}
// 裁切一张框图（四周补白边，避免贴边列漏读）。
function cropB64(b, im){ const P=ocrPad(b.w,b.h);
  const oc=document.createElement('canvas'); oc.width=b.w+2*P; oc.height=b.h+2*P;
  const octx=oc.getContext('2d'); octx.fillStyle='#fff'; octx.fillRect(0,0,oc.width,oc.height);
  octx.drawImage(im,b.x,b.y,b.w,b.h,P,P,b.w,b.h); return oc.toDataURL('image/png').split(',')[1]; }
async function ocrBox(b, im, nW, nH, opt){ const P=ocrPad(b.w,b.h);
  // opt.pname：单框重识别时目标框可能不属于当前显示版，日志/来源/质量标记须用该版名；
  // opt.quiet：单框重识别不写入本批质量自检标记，避免污染「识别全部」的统计与提示。
  const _pn=(opt&&opt.pname)||srcName, _quiet=!!(opt&&opt.quiet);
  // 防御：宽或高异常（0 / 负数 / 过小）时下面 canvas 会退化成纯白小图，模型必然返回空、
  // 且界面上毫无提示（表现为「这个框一个字都没识别出来」）。这类框一律不发请求。
  if(!(b.w>=8 && b.h>=8)){
    const _m='框尺寸异常 '+Math.round(b.w)+'×'+Math.round(b.h)+'px，已跳过识别（多半是拖拽时只动了一条边或坐标被截断）。请删除该框重画。';
    log('[识别] '+_m);
    if(!_quiet) ocrQualityFlags.push({page:_pn, label:(b.label||'未标标签'), issue:_m});
    return {text:'', usage:{}, finish_reason:'SKIPPED'}; }
  // 坐标越界只告警不拦截：坐标系若整体偏移，跳过会让用户彻底无法识别，先让它照常发请求
  if(nW && nH && (b.x>=nW || b.y>=nH || b.x+b.w<=0 || b.y+b.h<=0)){
    const _w='框坐标越界 @('+Math.round(b.x)+','+Math.round(b.y)+') 尺寸 '+Math.round(b.w)+'×'+Math.round(b.h)+'，超出原图 '+(nW||'?')+'×'+(nH||'?')+'，识别结果可能为空。';
    log('[识别] 警告：'+_w); }
  const b64=cropB64(b,im);
  // 请求本身可能直接抛错（后端未就绪、连接被拒、超时）。此前这类错误会冒泡到整页 try，
  // 让该版所有框一起作废；这里就地收敛成「这个框识别失败」，其余框照常继续。
  let j=null;
  const _postOcr=async()=>{ const r=await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_b64:b64,src:_pn,label:(b.label||''),overrides:getOverrides()})});
    return await r.json(); };
  try{ j=await _postOcr(); }
  catch(_e){ j={ok:false, error:'请求失败：'+String((_e&&_e.message)||_e).slice(0,180)}; }
  // 并发识别会撞上厂商 RPM/TPM 限流：这类失败退避后重试一次，不能直接判成「这个框读不出来」，
  // 否则并发只是把串行时能成功的框变成了随机的失败框。
  const _rlTxt=String((j&&j.error)||'');
  if((!j||!j.ok)&&/429|rate.?limit|RPM|TPM|concurren|too many requests|频繁|限流/i.test(_rlTxt)){
    log('[识别] 触发限流，1.5 秒后重试 1 次：'+_rlTxt.slice(0,80));
    await new Promise(_r=>setTimeout(_r,1500));
    try{ j=await _postOcr(); }
    catch(_e2){ j={ok:false, error:'请求失败（重试）：'+String((_e2&&_e2.message)||_e2).slice(0,180)}; }
  }
  if(!j || !j.ok){ const _e=(j&&j.error)||'未知错误';
    // 关键：不返回「看起来像正文」的错误文本。此前返回 '[OCR_ERROR] xxx'，被 parseArticle
    // 当正文写进 results，既覆盖掉上一次成功的结果，又在产物里留下整段报错文字（实测
    // 29 次连续失败后界面显示 8033 字 = 29 × 277，全是同一条错误提示）。
    return {ok:false, failed:true, error:_e, text:'', usage:{}, finish_reason:'FAILED'}; }
  if(j.note) log('[百度] '+j.note);   // 额度用尽自动换档等过程提示，让用户知道档位变过
  const _t=j.text||'';
  const _issue=ocrQualityIssue(_t, b);
  if(_issue && !_quiet) ocrQualityFlags.push({page:_pn, label:(b.label||'未标标签'), issue:_issue});
  // 输出被截断（finish_reason=length）：结尾少了一截但界面上完全看不出来，必须显式报出
  if(String(j.finish_reason||'')==='length'){
    const _tm='模型输出达到长度上限被截断（finish_reason=length），本框结尾可能缺失，建议拆成两个框重跑。';
    log('[识别] '+(b.label||'未标')+' 框：'+_tm);
    if(!_quiet) ocrQualityFlags.push({page:_pn, label:(b.label||'未标标签'), issue:_tm});
  }
  return {ok:true, failed:false, text:_t, usage:j.usage||{}, finish_reason:j.finish_reason||''}; }
async function recognizeOneBox(b){ return ocrBox(b, img, natW, natH); }
async function recognizeAll(){ if(!img){ alert('请先载入整版图片'); return; }
  if(!pageOrder.length){ alert('请先在卡片②勾选并「载入」整版原图。'); return; }
  // 仅识别已框选的版；未框选的版跳过（不整版兜底、不阻断其余版），与下方 getOcrTargets 的跳过逻辑一致
  const boxedPages=pageOrder.filter(p=>allPageData[p]&&allPageData[p].boxes&&allPageData[p].boxes.length);
  const noBoxPages=pageOrder.filter(p=>!boxedPages.includes(p));
  if(!boxedPages.length){ alert('没有可识别的整版：请先为至少一版框选区域再识别，无框严禁识别。'); return; }
  if(noBoxPages.length){ log('[识别全部] 以下整版未框选，已跳过（仅识别已框选的 '+boxedPages.length+' 版）：'+noBoxPages.join('、')); }
  await refreshRunCfg();   // 取最新的 OCR_CONCURRENCY（改配置后直接点识别即可，不必重启）
  setOcrHint('识别中…', true);
  ocrQualityFlags=[];   // 每次识别全部重置质量自检标记，避免跨批次累积
  boxPartsMap={};       // 同步清空框级识别结果（点框定位片段的依据）
  failedBoxMap={};      // 清空上一次的失败登记，避免旧批次标红残留
  // 标签自检：一版 title 框过多时提示。title 会开新组，误把正文框设成 title 会一次性造出多个假分组。
  try{ const _bad=[];
    for(const _p of pageOrder){ const _n=(((allPageData[_p]||{}).boxes)||[]).filter(b=>b.label==='title').length;
      if(_n>6) _bad.push((_p||'-')+'（'+_n+' 个）'); }
    if(_bad.length) log('[标签自检] 以下整版的 title 框偏多：'+_bad.join('、')+'。若非一页多篇，请到卡片③把误设的框改回 text。');
  }catch(_e){}
  // 逐版离线识别：每版重新取图并按页存回 allPageData，最后统一聚合
  let failedPages=[];
  let ocrPt=0,ocrCt=0,ocrTt=0,ocrDur=0;  // 本次识别累计 token / 耗时
  const tStart=performance.now();
  ensureOwnBoxes('识别前');   // 识别前再确认一次，避免把别版的框送去识别
  log('[识别全部] 模式='+mode+'，载入 '+pageOrder.length+' 版；各版框数：'+pageOrder.map(p=>(p+'='+(allPageData[p]?allPageData[p].boxes.length:'(无)'))).join('，'));
  for(const pname of boxedPages){
    const pd=allPageData[pname]; if(!pd){ failedPages.push(pname+'（未载入框选数据）'); continue; }
    try{
      const j=await (await fetch('/api/image?name='+encodeURIComponent(pname)+'&dir=cropped_hi')).json().catch(()=>({error:'响应不是JSON'}));
      if(j.error || !j.data){ failedPages.push(pname+'（取图失败：'+(j.error||'no data')+'）'); continue; }
      const im=await new Promise(res=>{ const m=new Image(); m.onload=()=>res(m); m.src=j.data; });
      const nW=im.naturalWidth, nH=im.naturalHeight;
      const targets=getOcrTargets(pd.boxes);
      if(!targets.length) log('[识别全部] 警告：'+pname+' 无识别目标（无框），已跳过。');
      for(const t of targets){
        if(t.key==='__whole__'){ log('[识别全部] 错误：检测到整版兜底目标，已禁止识别（无框严禁）。'); continue; }
        else if(t.boxes&&t.boxes.length===1){
          const b=t.boxes[0]; const r=await ocrBox(b,im,nW,nH);
          // 识别失败：不写 results、不覆盖上一次成功的结果，只登记失败并由结果面板标红。
          if(r.failed){ failMark(pname,t.key,1,1,r.error);
            log('[识别全部] '+pname+' 框#'+(b.id!=null?b.id:'?')+'（'+(b.label||'未标')+'）识别失败，原结果保留未覆盖：'+(r.error||''));
            continue; }
          const p=parseArticle(r.text||'');
          const parsed=foldParsedByLabel(b,p);
          bpPut(pname, t.key, b.id, b.label, r.text||'', parsed.title, parsed.author, parsed.body);
          pd.results[t.key]=parsed; if(r.usage){ ocrPt+=r.usage.prompt_tokens||0; ocrCt+=r.usage.completion_tokens||0; ocrTt+=r.usage.total_tokens||0; ocrDur+=r.usage.duration_s||0; } }
        else if(t.boxes&&t.boxes.length>1){ const parts=[]; const _fail=[];
          const _n=ocrConcurrency();
          const _rs=new Array(t.boxes.length);
          if(_n>1) log('[识别全部] '+pname+' 该篇 '+t.boxes.length+' 个框并发识别（并发数 '+_n+'）');
          const rr=await runLimited(t.boxes, _n, (b)=>ocrBox(b,im,nW,nH));
          rr.forEach((r,i)=>{ _rs[i]=r; });
          for(let _i=0;_i<t.boxes.length;_i++){ const b=t.boxes[_i]; const r=_rs[_i];
            if(!r||r.failed){ _fail.push({b:b,error:(r&&r.error)||''});
              continue; }   // 失败框不产出 part，绝不伪造内容
            const p=parseArticle(r.text||'');
            const parsed=foldParsedByLabel(b,p);
            bpPut(pname, t.key, b.id, b.label, r.text||'', parsed.title, parsed.author, parsed.body);
            parts.push(parsed); if(r.usage){ ocrPt+=r.usage.prompt_tokens||0; ocrCt+=r.usage.completion_tokens||0; ocrTt+=r.usage.total_tokens||0; ocrDur+=r.usage.duration_s||0; } }
          if(_fail.length){ failMark(pname,t.key,t.boxes.length,_fail.length,_fail[0].error);
            log('[识别全部] '+pname+' 该篇 '+t.boxes.length+' 个框中有 '+_fail.length+' 个识别失败（框#'+
              _fail.map(f=>f.b.id!=null?f.b.id:'?').join('、#')+'），失败框不产出内容，其余框照常合并：'+(_fail[0].error||'')); }
          // 全部失败时整篇不落结果，避免把空篇写进 results 顶掉上一轮的成品
          if(!parts.length){ log('[识别全部] '+pname+' 该篇全部框识别失败，保留原结果不动。'); continue; }
          // 多个框各读各的，正文框常把页面上的大标题也顺带读进 title 字段，
          // 于是「烟台纪行」这类正题会被重复读出 3~5 遍。合并口径与 mergeCrossTarget 一致：
          // 与正题相同/相似的后续标题视为「同一个标题被重复读出」，丢弃不进正文；
          // 只有真正不同的（副题、引题、另一篇的题头）才并入正文，不静默丢弃。
          //
          // 合并口径（20261103 修正三处）：
          // ① 标题：首个非空作正题，后续相似者视为重复读出而丢弃，不同者原地并入正文。
          // ② 作者：首个非空作作者，后续**不同的**一并进正文。旧写 `find(x=>x)` 只取首个非空、
          //    其余 author 直接丢弃，若某 author 框被读成整段正文，整段就此消失。
          // ③ 顺序：严格按 parts 顺序拼接。旧写把非首框标题统一前置到正文最前，
          //    段落会从原位搬到开头（实测 17 字栏头被搬到 2922 字正文的开头）。
          const _titles=parts.map(p=>p.title).filter(x=>x);
          const title=_titles[0]||'';
          const _authors=parts.map(p=>p.author).filter(x=>x);
          const author=_authors[0]||'';
          const _segs=[];
          parts.forEach((p,i)=>{
            if(i>0 && p.title && !_titleSame(_normTitle(p.title), _normTitle(title))) _segs.push(p.title);
            if(i>0 && p.author && p.author!==author) _segs.push(p.author);
            if(p.body) _segs.push(p.body);
          });
          const _bodyRaw=_segs.filter(x=>x).join('\n\n');
          const body=dropDupSign(author, _bodyRaw);   // 剔除正文里重复出现的署名
          pd.results[t.key]={title,author,body,raw:`标题：${title}\n作者：${author}\n正文：${body}`.trim()}; } }
      // 单页模式：每版识别完立即按「当前版名」导出，避免循环结束后只导出末尾 srcName 那一版（其余版白识别）
      if(mergeMode!=='cross'){
        // 先聚合已识别版到 crossResults（此时仅含截至当前已识别的版），再按当前版名导出
        const cts=aggregateCrossTargets(); crossResults={}; for(const ct of cts){ crossResults[ct.key]=mergeCrossTarget(ct); }
        srcName=pname; exportTxt(false); exportJson(false);
      }
    }catch(err){ failedPages.push(pname+'（'+ (err&&err.message||err) +'）'); log('[识别全部] 警告：'+pname+' 识别失败，已跳过，继续后续版。'); }
  }
  // 识别完成后，按阅读顺序聚合各版结果到 crossResults（单向合并视图，不污染 per-page 数据）
  const cts=aggregateCrossTargets();
  crossResults={};
  for(const ct of cts){ crossResults[ct.key]=mergeCrossTarget(ct); }
  renderResults();
  const cross = mergeMode==='cross';
  const okCount = boxedPages.length - failedPages.length;
  const wall = ((performance.now()-tStart)/1000).toFixed(2);
  const _prov=(backendCfg&&backendCfg.values&&backendCfg.values.BOX_OCR_PROVIDER)||'doubao';
  const _pre=_prov.toUpperCase();
  const cfgModel=(backendCfg&&backendCfg.values&&backendCfg.values[_pre+'_MODEL'])||'';
  log('[识别全部] 已识别 '+okCount+' / '+boxedPages.length+' 版（共载入 '+pageOrder.length+' 版，跳过未框选 '+noBoxPages.length+' 版）'+(cross?'（跨页模式：按阅读顺序、以标题为界分篇）':'（单页模式：每版独立）')+'。');
  // 分篇清单：明确「每版是否都识别到了、内容归到哪一篇」，避免只看结果面板的文本框数量而误判漏识别
  try{
    const _cts=aggregateCrossTargets();
    const _tn=countTitleBoxes();
    log('[识别全部] 分篇结果：共 '+_cts.length+' 篇'+(cross&&_cts.length?('（按标题分篇：工作集标题框 '+_tn+' 个 → 遇标题开新篇，无标题的框并入前一篇，续页题头与当前篇标题相同则续写同篇）'):''));
    _cts.forEach((ct,i)=>{
      const names=ct.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,''));
      const nbox=ct.pageTargets.reduce((s,pt)=>s+((pt.t&&pt.t.boxes)?pt.t.boxes.length:0),0);
      log('  第 '+(i+1)+' 篇 · '+(ct.label||'-')+' ← '+names.length+' 版 / '+nbox+' 框：'+names.join(' + '));
    });
    if(cross&&_cts.length===1&&boxedPages.length>1) log('[识别全部] 提示：'+boxedPages.length+' 版内容已合并成 1 篇（分篇只看标题）。若其中某版其实是另一篇文章，请给它那篇画上并标注「标题」框；也可在「自动分组」里给各篇填不同组号强制分篇。');
  }catch(_e){ log('[识别全部] 分篇清单打印失败：'+_e); }
  // 失败框汇总：失败不写结果、不进导出，但必须让用户看见「哪里没读到」，否则只会以为内容就是这么少。
  const _failBoxes=Object.keys(failedBoxMap).length;
  if(_failBoxes) log('[识别全部] 注意：'+_failBoxes+' 篇存在识别失败的框（结果面板已标红）。失败框不写入任何内容、也不进导出；请检查网络与 API 配置后重新识别这些框。');
  if(failedPages.length) setOcrHint('识别完成，'+failedPages.length+' 版失败', false);
  else if(_failBoxes) setOcrHint('识别完成，'+_failBoxes+' 篇有失败框（已标红）', false);
  else setOcrHint('识别完成 ✓（'+okCount+' 版）', true);
  const _ccN=ocrConcurrency();
  log('[识别全部] 本次 OCR 消耗'+(cfgModel?'（'+cfgModel+'）':'')+' → 输入 '+ocrPt.toLocaleString()+' + 输出 '+ocrCt.toLocaleString()+' = 总 '+ocrTt.toLocaleString()+' tokens；各框识别耗时合计 '+ocrDur.toFixed(2)+'s'+(_ccN>1?('（并发 '+_ccN+' 个框）'):'')+'，本批实际用时 '+wall+'s。');
  // 记录一次「识别全部」运行；OCR 调用次数按本次识别的「组」数计（每个 group 1 次，含未标 group 自动合并的默认组）
  if(okCount>0){ try{ const callGroups=aggregateCrossTargets(); fetch('/api/ocr_run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:cfgModel,pages:pageOrder.length,boxes:pageOrder.reduce((s,p)=>s+((allPageData[p]&&allPageData[p].boxes)?allPageData[p].boxes.length:0),0),calls:callGroups.length})}).catch(()=>{}); }catch(e){} }
  if(failedPages.length) log('[识别全部] 以下版识别失败：'+failedPages.join('、'));
  // 质量自检汇总：把本批次可疑框一次性报出，并给出模型建议
  if(ocrQualityFlags.length){
    const _p=(backendCfg&&backendCfg.values&&backendCfg.values.BOX_OCR_PROVIDER)||'doubao';
    log('[质量自检] 本次有 '+ocrQualityFlags.length+' 个框的识别结果异常，疑似识别失败：');
    ocrQualityFlags.slice(0,15).forEach(f=>log('  · '+f.page+' / '+f.label+' — '+f.issue));
    if(ocrQualityFlags.length>15) log('  · …其余 '+(ocrQualityFlags.length-15)+' 个略');
    if(_p!=='doubao') log('[质量自检] 当前识别模型不是豆包。此类整段读飞多因原文字形/版式复杂超出该模型能力边界，建议在「设置 → 识别模型」改用豆包 Seed-2.1-turbo 后重跑这些框。');
    else log('[质量自检] 若原图清晰仍大量异常，请检查框是否切到字行、或整版图分辨率过低。');
    ocrQualityFlags=[];
  }
  // 跨页模式：循环结束后统一导出合并结果；单页模式已在循环内逐版导出，此处不再重复导出
  if(cross){ exportTxt(false); exportJson(false); } }
function parseArticle(text){ const t=(text||'').replace(/\r/g,'');
  const tm=t.match(/(?:^|\n)标题[：:]\s*([\s\S]*?)(?=(?:\n作者[：:]|\n正文[：:]|$))/);
  const am=t.match(/(?:^|\n)作者[：:]\s*([\s\S]*?)(?=(?:\n正文[：:]|$))/);
  const bm=t.match(/(?:^|\n)正文[：:]\s*([\s\S]*)$/);
  let title=(tm?tm[1]:'').trim(), author=(am?am[1]:'').trim(), body=(bm?bm[1]:t).trim();
  // 防御：模型偶发把字段名（如"正文："）误写入字段值，导致"作者：正文：水文"；清理字段值开头的字段名前缀
  title = title.replace(/^(标题[：:]?|作者[：:]?|正文[：:]?)\s*/,'').trim();
  author = author.replace(/^(正文[：:]?|标题[：:]?|作者[：:]?)\s*/,'').trim();
  body = body.replace(/^正文[：:]\s*/,'').trim();
  // 防御：模型把正文首句当署名填进作者字段（如作者=正文前 50 字），清空作者
  if(author && body && (author===body || (author.length>=15 && body.startsWith(author)))){ author=''; }
  // 防御：剥离模型偶发的"注：/疑为/照录"注释括号（用户自行校对，不需要 AI 疑似说明）
  const stripNote = s => (s||'').replace(/（(?:注：|原文)[\s\S]*?）/g,'').trim();
  title = stripNote(title); author = stripNote(author); body = stripNote(body);
  return {title, author, body, raw:t.trim()}; }
function resultToItem(o){ if(!o)return{title:'',author:'',body:'',raw:''}; if(typeof o==='string')return parseArticle(o); return o; }
function itemToText(it){ if(!it)return''; if(typeof it==='string')return it.trim(); if(it.raw)return it.raw.trim(); return `标题：${it.title||''}\n作者：${it.author||''}\n正文：${it.body||''}`.trim(); }
function renderResults(){ const el=$('resultList');
  clearBoxFocusHighlight();   // 重渲染会重建 textarea，旧的片段标记必须一起清掉，否则指向已销毁的节点
  if(!img){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">载入整版图片后，在此显示可核对 / 编辑的识别结果。</div>'; return; }
  const cross = mergeMode==='cross';
  const targets=aggregateCrossTargets(); if(!targets.length){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">多篇模式需先框选区域，再点「识别全部」。</div>'; return; }
  // 重建前快照：容器滚动位置 + 各 textarea 的高度/滚动位置，避免翻页/重渲染后校对位置丢失
  const keepSc=el.scrollTop; const keep={};
  el.querySelectorAll('textarea[data-key]').forEach(ta=>{ keep[ta.dataset.key]={h:ta.style.height, st:ta.scrollTop, ex:ta.dataset.expanded||''}; });
  el.innerHTML='';
  // 全局序号映射：box 对象 -> 阅读顺序中的连续编号（与 renderBoxList 展平一致），避免多版时局部序号错乱
  const flat=flattenBoxes(); const gIdx=new Map(); flat.forEach(v=>gIdx.set(v.box, v.globalIdx+1));
  targets.forEach((t,i)=>{ const item=resultToItem(crossResults[t.key]); let title,size;
    if(cross){
      if(t.key==='__cross_whole__'){ title='跨页·整篇合并'; size=pageOrder.length+' 版合并'; }
      else if(t.group){ const pages=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); title=`跨页·组 ${t.group}（${pages}）`; size=t.pageTargets.length+' 处 / '+pageOrder.length+' 版'; }
      else if(t.key==='__cross_default__'){ title='跨页·续页（开头无标题）'; size=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); }
      else { title=`跨页·项 ${i+1}`; size=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); }
    } else if(t.key==='__whole__'){ title='（无框未识别）'; size='请框选后再识别'; }
    else if(t.boxes&&t.boxes.length>1){ const g=t.boxes[0].group; title=g?`组 ${g}`:'合并';
      const area=t.boxes.reduce((s,b)=>s+b.w*b.h,0); size=`${t.boxes.length} 个框 / 约 ${Math.round(area).toLocaleString()}px²`; }
    else { const b=t.boxes?t.boxes[0]:null; title=b?`框 ${i+1} · ${b.label}`:'单篇'; size=b?`${b.w}×${b.h}px`:''; }
    // 识别失败标记：失败不再伪造正文，因此卡片可能为空。没有显式提示的话，用户只会看到
    // 「这篇没内容」，误以为漏识别或程序坏了。标红 + 提示条把「没读到」讲清楚。
    const _fi=failInfoOfTarget(t);
    let display=item.raw;
    if(!cross && t.boxes&&t.boxes.length===1){ const b=t.boxes[0];
      if(b.label==='title')display=item.title?`标题：${item.title}`:item.raw;
      else if(b.label==='author')display=item.author?`作者：${item.author}`:item.raw;
      else if(b.label==='section')display=item.body||item.title||item.author||item.raw||'';
      else if(b.label==='text'){ const ls=[]; if(item.title)ls.push(`标题：${item.title}`); if(item.author)ls.push(`作者：${item.author}`); if(item.body)ls.push(item.body); display=ls.join('\n')||item.raw; } }
    const d=document.createElement('div'); d.className='card'+(_fi?' fail':''); d.dataset.key=t.key;
    d._parts=cardPartsOfTarget(t);   // 本篇各框的片段记录位置，编辑时同步用（挂 DOM 上，避免二次全量聚合）
    // 提示条放在 textarea 之外：不能让「识别失败」这类提示进入文本框，否则会被当正文编辑并导出。
    const _failBar=_fi?`<div class="failbar">⚠ 识别失败：${esc(_fi.count+'/'+_fi.total+' 个框未读到')} — ${esc(_fi.error||'未知错误')}${
      item.raw?'（下方为上一次成功识别的结果，本次失败未覆盖它）':'（本篇无内容，可修正后重新识别）'}</div>`:'';
    d.innerHTML=`<div class="hd"><span class="title">${title}</span><span class="meta">${size}</span><button class="exp" data-exp-key="${esc(t.key)}" title="展开：显示全文，不用框内滚动条">展开</button><button class="loc" data-loc-key="${esc(t.key)}" title="在左侧画布定位并放大到该篇">定位</button></div>${_failBar}<div class="ta-wrap"><textarea data-key="${esc(t.key)}"${_fi&&!item.raw?' placeholder="识别失败，暂无内容"':''}>${esc(display)}</textarea><div class="hl-layer"></div></div>`;
    el.appendChild(d); });
  el.querySelectorAll('[data-loc-key]').forEach(btn=>{ btn.onclick=e=>{ e.stopPropagation(); locateByResultKey(btn.dataset.locKey); }; });
  // 展开 / 收起：展开＝撑到内容全高（不再用框内滚动条），收起＝回到 CSS 的自适应高度。
  // 状态记在 textarea.dataset.expanded 上，翻页/重识别重渲染时由上方 keep 快照恢复，
  // 否则校对到一半翻页就会被收回去。
  const applyExpanded=(ta,on)=>{ const _c=ta.closest('.card'); const btn=_c?_c.querySelector('[data-exp-key]'):null;
    if(on){ ta.dataset.expanded='1'; ta.style.height='auto'; ta.style.height=(ta.scrollHeight+2)+'px';
      if(btn){ btn.textContent='收起'; btn.title='收起：回到默认高度'; } }
    else { ta.dataset.expanded=''; ta.style.height='';
      if(btn){ btn.textContent='展开'; btn.title='展开：显示全文，不用框内滚动条'; } } };
  el.querySelectorAll('textarea').forEach(ta=>{ const k=ta.dataset.key; const kb=keep[k];
    if(kb){ if(kb.ex){ applyExpanded(ta,true); } else if(kb.h) ta.style.height=kb.h; ta.scrollTop=kb.st; }
    const _card=ta.closest('.card'); const _expBtn=_card?_card.querySelector('[data-exp-key]'):null;
    if(_expBtn) _expBtn.onclick=e=>{ e.stopPropagation(); applyExpanded(ta, ta.dataset.expanded!=='1'); };
    // 该卡片覆盖的框（编辑时要把这些框记住的片段文本一起同步，否则改过的段落下次点框定位不回来）
    const cardParts=(ta.closest('.card')||{})._parts||[]; let prevVal=ta.value;
    ta.oninput=e=>{
    const nv=e.target.value, ov=prevVal; prevVal=nv;
    // 先同步各框片段文本（不依赖当前有没有片段标记，换过卡片也照样同步）
    syncPartsAfterEdit(cardParts, ov, nv);
    // 编辑时不清除标记，改为跟着这次改动平移/扩展（用户就是要边改边看原图对应位置）
    if(hlState && hlState.ta===ta){
      const sp=adjustHlSpan(hlState.value, nv, hlState.start, hlState.len);
      hlState.start=sp.start; hlState.len=sp.len; hlState.value=nv;
      try{ paintHlRaw(ta, sp.start, sp.len); }catch(_e){}
    }
    const item=resultToItem(crossResults[k]); item.raw=e.target.value;
    const p=parseArticle(e.target.value); item.title=p.title; item.author=p.author; item.body=p.body; crossResults[k]=item; };
    // 点进文本框、拖选、失焦回来等会让浏览器重算高亮，这里复位一次，保证黄底不会因「点击」而消失
    ta.addEventListener('focus', ()=>{ if(hlState && hlState.ta!==ta) clearBoxFocusHighlight(); hlReapply(); });
    ta.addEventListener('click', ()=>hlReapply());
    ta.addEventListener('mouseup', ()=>hlReapply());
    ta.addEventListener('select', ()=>hlReapply());
  });
  el.scrollTop=keepSc; }

// ---------- 导出 ----------
function computeUnionBox(bs){ let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity; bs.forEach(b=>{x0=Math.min(x0,b.x);y0=Math.min(y0,b.y);x1=Math.max(x1,b.x+b.w);y1=Math.max(y1,b.y+b.h);});
  return {x:Math.round(x0),y:Math.round(y0),w:Math.round(x1-x0),h:Math.round(y1-y0)}; }
function aggregateTargetsForExport(pageName){
  const cross = mergeMode==='cross';
  let targets=aggregateCrossTargets();
  // 单页模式：只导出指定版（pageName，缺省为当前 srcName），不把工作集其余版也写进同一个文件
  const scope = pageName || srcName;
  if(!cross && scope){ targets=targets.filter(t=>t.key.startsWith('page:'+scope+':') || t.pageTargets.every(pt=>pt.pname===scope)); }
  const rows=targets.map((t,i)=>{
    if(cross) return {order:i+1,label:t.label,group:t.group||'',box:null,text:itemToText(crossResults[t.key])};
    if(t.key==='__whole__') return {order:i+1,label:'（无框未识别）',group:'',box:null,text:'请框选后再识别'};
    const bs=t.boxes; return {order:i+1,label:t.label,group:(bs[0].group||''),box:bs.length===1?{x:Math.round(bs[0].x),y:Math.round(bs[0].y),w:Math.round(bs[0].w),h:Math.round(bs[0].h)}:computeUnionBox(bs),text:itemToText(crossResults[t.key])};
  });
  // 空篇不进导出：识别失败或尚未识别的篇，其文本只剩「标题：\\n作者：\\n正文：」骨架。
  // 此前这类空壳照样写成 txt/json 并进入结构化统计，成品里留下空条目；过滤后 order 连续重排。
  const _blankRow=r=>r.text!=='请框选后再识别' && String(r.text||'').replace(/^(标题|作者|正文)[：:]\s*/gm,'').trim()==='';
  return rows.filter(r=>!_blankRow(r)).map((r,i)=>({...r,order:i+1})); }
function currentExportName(){ return (mergeMode==='cross' && pageOrder.length>1)?crossBaseName():srcName; }
// 跨页产物归集目录：每次跨页任务一个专属文件夹（以跨页基名为子目录）；单页不建子目录
function currentOutDir(){ return (mergeMode==='cross' && pageOrder.length>1)?crossBaseName():''; }
function exportTxt(){ const en=currentExportName(); if(!en){ alert('请先载入图片（需含文件名作为出处）'); return; } const items=aggregateTargetsForExport(); if(!items.length)return;
  const od = currentOutDir();
  const body = {source_name:en, boxes:items}; if(od) body.out_dir=od;
  fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json()).then(j=>{ if(j.ok){ log('[导出 txt]\n'+j.written.map(s=>'  output/'+(od?od+'/':'')+s).join('\n')); } else log('导出失败：'+(j.error||'')); }).catch(e=>log('导出失败：'+e)); }
// 自动导出 JSON 到 output/{整版名}.json（服务端落盘，无需手动下载）
function exportJson(doLog){ const en=currentExportName(); if(!en){ if(doLog)alert('请先载入图片'); return; } const items=aggregateTargetsForExport(); if(!items.length)return;
  const od = currentOutDir();
  const body = {source_name:en, mode:mode, boxes:items}; if(od) body.out_dir=od;
  fetch('/api/export_json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json()).then(j=>{ if(j.ok&&doLog)log('[导出 JSON]\n'+j.written.map(s=>'  output/'+(od?od+'/':'')+s).join('\n')); else if(doLog)log('导出 JSON 失败：'+(j.error||'')); }).catch(e=>{ if(doLog)log('导出 JSON 失败：'+e); }); }
// 把右侧面板的编辑（活在 crossResults 中）写回 allPageData[页].results，
// 这样切页/再次「识别全部」重建 crossResults 时不会丢手动改的字。
// 跨页模式不在此回写（其结构由 mergeCrossTarget 动态拼接，单页独立成篇才需逐版持久化）。
function itemToStore(it){ if(!it) return it; if(typeof it==='string') return {title:'',author:'',body:'',raw:it}; return {title:it.title||'',author:it.author||'',body:it.body||'',raw:it.raw||itemToText(it)}; }
function flushEditsToAll(){
  if(mergeMode==='cross') return;
  for(const key in crossResults){
    const parts=String(key).split(':');
    if(parts[0]!=='page' || parts.length<3) continue;
    const pname=parts[1]; const sub=parts.slice(2).join(':');
    if(allPageData[pname]) allPageData[pname].results[sub]=itemToStore(crossResults[key]);
  }
}
// 识别全部按钮旁提示（与保存 hint 一致：常驻，下次点识别刷新）
function setOcrHint(txt, ok){
  const h=$('ocrHint'); if(!h) return;
  h.textContent=txt||'';
  h.style.color = ok? 'var(--ok)' : 'var(--err)';
  h.style.display = txt? 'inline' : 'none';
}
// 保存修改按钮旁提示（与配置保存 hint 一致：常驻，下次点保存刷新）
function setSaveHint(txt, ok){
  const h=$('saveEditHint'); if(!h) return;
  h.textContent=txt||'';
  h.style.color = ok? 'var(--ok)' : 'var(--err)';
  h.style.display = txt? 'inline' : 'none';
}
// 保存修改：把右侧当前编辑结果覆盖写回 output/ 的 txt + json
async function saveEdit(){
  flushEditsToAll();   // 单页模式：先把手动编辑落回 allPageData，避免后续翻页/重识别丢字
  const cross = mergeMode==='cross';
  if(cross){
    // 跨页模式：维持原逻辑，整批合并写成一个文件
    const en=currentExportName(); if(!en){ alert('请先载入图片'); return; }
    const items=aggregateTargetsForExport(); if(!items.length){ alert('没有可导出的识别结果'); return; }
    const od=currentOutDir();
    const body={source_name:en,mode:mode,boxes:items}; if(od) body.out_dir=od;
    try{
      const j=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
      if(!j.ok){ setSaveHint('保存失败', false); log('保存失败：'+(j.error||'')); return; }
      log('[保存修改] 已覆盖写回 txt\n'+j.written.map(s=>'  output/'+(od?od+'/':'')+s).join('\n'));
      const b2={source_name:en,mode:mode,boxes:items}; if(od)b2.out_dir=od;
      const j2=await fetch('/api/export_json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b2)}).then(r=>r.json());
      if(j2.ok) log('[保存修改] 已覆盖写回 json → '+(j2.path||'(未知路径)')); else log('保存 JSON 失败：'+(j2.error||''));
      // 引用联动：手改作者/标题/日期后本地重算引用串（不调模型），写回结构化产物。
      // 跨页·按篇拆分：逐篇调用（每篇文件名为 {en}__{篇序}），避免整轮只重算第一篇。
      try{
        for(let i=0;i<items.length;i++){
          const br=await fetch('/api/rebuild_ref',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_name:`${en}__${i+1}`,mode:mode,out_dir:od,boxes:[items[i]]})}).then(r=>r.json());
          if(br&&br.ok&&br.ref) log('[保存修改] 已按手改字段重算引用(篇'+(i+1)+')：'+br.ref);
          else if(br&&br.ok) log('[保存修改] 引用重算未返回结果(篇'+(i+1)+')，产物引用可能未更新');
          else if(br) log('[保存修改] 引用重算跳过(篇'+(i+1)+')：'+(br.error||'未结构化'));
        }
      }catch(e){}
    }catch(e){ setSaveHint('保存失败', false); log('保存失败：'+e); }
    setSaveHint('已保存 ✓', true);
    return;
  }
  // 单页模式：遍历所有已载入的版，逐版写回各自独立的 output/{版名}.json / .txt
  const pages = pageOrder.length ? pageOrder.slice() : (srcName?[srcName]:[]);
  let done=0;
  for(const pname of pages){
    const en=pname.replace(/\.[^.]+$/,'');
    const items=aggregateTargetsForExport(pname);
    if(!items.length) continue;
    const body={source_name:en, boxes:items};
    try{
      const j=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
      if(!j.ok){ log('['+en+'] 保存失败：'+(j.error||'')); continue; }
      log('['+en+'] 已覆盖写回 txt\n'+j.written.map(s=>'  output/'+s).join('\n'));
      const b2={source_name:en,mode:mode,boxes:items};
      const j2=await fetch('/api/export_json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b2)}).then(r=>r.json());
      if(j2.ok){ log('['+en+'] 已覆盖写回 json → '+(j2.path||'(未知路径)')); done++; }
      else log('['+en+'] 保存 JSON 失败：'+(j2.error||''));
      // 引用联动：单页模式手改作者/标题/日期后本地重算引用串（不调模型）
      // 该版内分了多篇（按组/标题）时，产物是 {版名}__{篇序}，必须逐篇重算；
      // 只传整版名会定位不到已拆篇的产物，引用一律「跳过」。
      try{
        const _rfBase = (items.length>1) ? `${en}__` : null;
        for(let i=0;i<items.length;i++){
          const _sn = _rfBase ? (_rfBase+(i+1)) : en;
          const br=await fetch('/api/rebuild_ref',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_name:_sn,mode:mode,out_dir:'',boxes:[items[i]]})}).then(r=>r.json());
          const _tag = _rfBase ? ('['+en+' 篇'+(i+1)+']') : ('['+en+']');
          if(br&&br.ok&&br.ref) log(_tag+' 已按手改字段重算引用：'+br.ref);
          else if(br&&br.ok) log(_tag+' 引用重算未返回结果，产物引用可能未更新');
          else if(br) log(_tag+' 引用重算跳过：'+(br.error||'未结构化'));
        }
      }catch(e){}
    }catch(e){ log('['+en+'] 保存失败：'+e); }
  }
  if(done>0) setSaveHint('已保存 ✓（'+done+' 版）', true);
  else setSaveHint('保存失败', false);
  if(done>0) log('[保存修改] 完成，共写回 '+done+' 个版的 json/txt。');
  else log('[保存修改] 没有可导出的识别结果，未写入任何文件。');
}

// ---------- 阶段 0 / 4 ----------
function refreshImageList(){
  return fetch('/api/list_images?dir=cropped_hi').then(r=>r.json()).then(j=>{
    const oldIdx=pageIdx, oldName=srcName;
    pageList=j.files||[]; fileMtimes=j.mtimes||{}; renderSrcList();
    // navList（当前载入的工作集）只保留仍存在的文件，避免删图后导航指向已消失的页
    navList = navList.filter(f=>pageList.includes(f));
    if(pageIdx>=navList.length) pageIdx = navList.length? navList.length-1 : -1;
    // 若当前页在刷新后的列表中仍然有效，则保留 pageIdx；否则按 srcName 重新定位；无 srcName 则复位
    if(oldIdx>=0 && oldIdx<pageList.length && pageList[oldIdx].replace(/\.[^.]+$/,'')===oldName){ /* 保持 */ }
    else { pageIdx=oldName?pageList.findIndex(f=>f.replace(/\.[^.]+$/,'')===oldName):-1; }
    updatePageNav();
  }).catch(()=>{});
}
function renderSrcList(){ const box=$('srcList'); if(!box) return; box.innerHTML='';
  if(!pageList.length){ if($('imgCnt')) $('imgCnt').textContent = '共 0 项'; const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.remove('collapsed'); syncSelAll(); return; }
  pageList.forEach((f,i)=>{ const lab=document.createElement('div'); lab.className='item'; lab.draggable=true; lab.innerHTML=`<input type="checkbox" class="src-chk" value="${esc(f)}"> <span class="ord">${i+1}</span> <span class="nm" title="可拖动调整顺序">${esc(f)}</span> <span class="ops"><button type="button" class="mv" data-up ${i===0?'disabled':''} title="上移">↑</button><button type="button" class="mv" data-dn ${i===pageList.length-1?'disabled':''} title="下移">↓</button></span>`; box.appendChild(lab); });
  syncSelAll();
  const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.remove('collapsed');
  if($('imgCnt')) $('imgCnt').textContent = '共 '+pageList.length+' 项'; }
function syncSelAll(){ const sa=$('selAll'); if(!sa) return; const chks=document.querySelectorAll('.src-chk');
  if(!chks.length){ sa.checked=false; sa.indeterminate=false; return; }
  const all=[...chks].every(c=>c.checked); const none=![...chks].some(c=>c.checked);
  sa.checked = all; sa.indeterminate = !all && !none; }
function flashHint(msg){ alert(msg); }
function runExtractAndGroup(){
  const btn = $('runExtractGroup');
  if(btn && btn.disabled){ return; }            // 防重：抽图中禁止再次触发（用户可能因慢而连点）
  if(btn){ btn.disabled = true; btn.textContent = '抽图中…'; }
  log('[抽图并归档] 开始…');
  fetch('/api/extract_and_group',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'auto'})}).then(r=>r.json()).then(j=>{
    (j.steps||[]).forEach(s=>log('['+s.step+'] '+(s.ok?'成功':'失败')+' (returncode='+s.returncode+')'));
    log((j.stdout||'')+(j.stderr?'\n'+j.stderr:''));
    if(j.ok){
      // 抽图归档完成，source/ 源文件已完成中转使命；只清空 source/ 根文件，不级联删除 cropped_hi/ 与 output/
      fetch('/api/clear_source',{method:'POST'}).then(r=>r.json()).then(j2=>{
        refreshImageList();
        refreshSourceList();
        // 抽图归档只是产出整版 PNG，不应自动载入工作集；清空旧工作集避免看起来像「自动载入」
        pageOrder=[]; crossResults={}; allPageData={}; navList=[]; pageIdx=-1; updatePageNav(); renderBoxList(); renderResults();
        log('[抽图并归档] 完成 ↑ source/ 已清空 '+(j2.count||0)+' 个源文件（整版原图与识别结果保留），整版列表已刷新。请勾选需要的整版后点击「载入」载入工作集。');
      }).catch(e=>{ log('清空 source/ 失败：'+e); refreshImageList(); refreshSourceList(); });
    }
    else log('失败：'+(j.error||''));
  }).catch(e=>log('抽图并归档失败：'+e))
    .finally(()=>{ if(btn){ btn.disabled=false; btn.textContent='抽图并归档'; } }); }
// 来源补充：根据当前模式与工作集，渲染「按版来源」输入行。跨页合并一篇→隐藏并改用全局框；
// 单页一次多版→每版一行（同一版内多篇共享）。按版名暂存已填值，刷新时不丢失。
let _savedSrcByPage = {};
let _savedCarrierByPage = {};
function attrEsc(s){ return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function renderPerPageSrc(){
  const wrap = document.getElementById('srcPerPageWrap');
  const single = document.getElementById('srcSingleWrap');
  if(!wrap || !single) return;
  // UI 切换只看用户选的模式；真正的跨页结构化在 runPost 里仍要求 pageOrder.length>1
  const isCross = mergeMode==='cross';
  wrap.style.display = isCross ? 'none' : 'block';
  single.style.display = isCross ? 'block' : 'none';
  if(isCross) return;  // 跨页模式不渲染每版行，保留全局框
  const box = document.getElementById('perPageSrc');
  if(!box) return;
  // 暂存已填值（按版名），避免 updatePageNav 频繁刷新导致输入丢失
  box.querySelectorAll('.pp-row').forEach(row=>{
    const idx = parseInt(row.dataset.idx, 10); const pg = pageOrder[idx]; if(!pg) return;
    _savedSrcByPage[pg] = row.querySelector('.pp-text').value;
    const cs = row.querySelector('.pp-carrier');
    _savedCarrierByPage[pg] = cs ? cs.value : 'newspaper';
  });
  if(!pageOrder.length){ box.innerHTML = '<p class="sub" style="margin:2px 0; color:var(--sub);">尚未载入工作集。载入后将按版显示来源输入框。</p>'; return; }
  // 每版一行分两层：上层「第N版 + 完整版名」，下层「来源输入框 + 载体下拉」。
  // 版名不再锁宽截断（多版同名 clipboard-* 时也能分辨）；输入框用单行 input，
  // 避免 textarea 在窄栏里被压成竖缝、露出原生滚动条与缩放角。
  let html = '';
  pageOrder.forEach((pg, idx)=>{
    const s = _savedSrcByPage[pg] || '';
    const cv = _savedCarrierByPage[pg] || 'newspaper';
    html += '<div class="pp-row" data-idx="'+idx+'" style="margin-bottom:8px;">'
          + '<div style="display:flex; align-items:center; gap:6px; margin-bottom:3px;">'
          +   '<span style="flex:0 0 auto; font-size:11px; color:var(--mut); background:#eef1f4; border:1px solid var(--bd); border-radius:4px; padding:0 5px; line-height:16px;">第'+(idx+1)+'版</span>'
          +   '<span style="flex:1 1 auto; min-width:0; font-size:11.5px; color:var(--sub); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="'+attrEsc(pg)+'">'+esc(pg)+'</span>'
          + '</div>'
          + '<div style="display:flex; align-items:center; gap:8px;">'
          +   '<input class="pp-text" type="text" placeholder="如：大公报 1943-01-17 第2版" value="'+attrEsc(s)+'" style="flex:1 1 auto; min-width:0; font-family:inherit; font-size:13px; padding:4px 6px;">'
          +   '<select class="pp-carrier" style="flex:0 0 104px; width:104px; font-family:inherit; font-size:12.5px; padding:3px 4px;">'
          +     '<option value="newspaper"'+(cv==='newspaper'?' selected':'')+'>报纸[N]</option>'
          +     '<option value="journal"'+(cv==='journal'?' selected':'')+'>期刊[J]</option>'
          +   '</select>'
          + '</div>'
          + '</div>';
  });
  box.innerHTML = html;
}
async function runPost(){
  const mode = $('postMode') ? $('postMode').value : 'plain';
  // 来源补充：区分单页/跨页。跨页合并一篇→一份全局自由文本；单页一次多版→每版各自自由文本。
  // 判定口径必须与 renderPerPageSrc 一致（只看 mergeMode）：跨页模式下每版行不渲染，
  // 若这里再用「版数>1」判定，跨页模式只载入 1 版时会错去读不存在的每版行 → 来源补充静默丢失。
  const uiCross = mergeMode==='cross';
  let globalSO = null;
  if(uiCross){
    const t = (document.getElementById('srcGlobalText')||{}).value||'';
    const c = (document.getElementById('srcGlobalCarrier')||{}).value||'newspaper';
    if(t.trim()) globalSO = {text: t.trim(), carrier: c};
  }
  const perPageSO = {};
  if(!uiCross){
    document.querySelectorAll('#perPageSrc .pp-row').forEach(row=>{
      const idx = parseInt(row.dataset.idx, 10);
      const pg = pageOrder[idx];
      if(!pg) return;
      const t = row.querySelector('.pp-text').value.trim();
      const c = (row.querySelector('.pp-carrier')||{}).value||'newspaper';
      if(t) perPageSO[pg] = {text: t, carrier: c};
    });
  }
  const top = (mode==='plain' ? 'plain_text' : 'knowledge_base');
  // 结构化路径分支维持原判定（跨页且多版 → 合并单文件夹路径）；
  // 来源补充的收集口径已在上面与 UI 对齐，单页路径用 globalSO 兜底（跨页模式只载 1 版时生效）。
  const isCross = mergeMode==='cross' && pageOrder.length>1;
  // 统一空状态守卫：未载入任何版时直接提示，不调用后端，避免生成「未命名」空文件夹
  if(!pageOrder.length){ alert('请先载入工作集（勾选整版原图并点「载入所选」），再点结构化。'); return; }
  // 自动先落盘：结构化（postprocess.py 子进程）只读取磁盘上的 raw 产物，不接触浏览器内存中的
  // crossResults；因此必须先确保 output/ 下的 .txt/.json 已写回，省去手动点「保存修改」的前置步骤。
  // 复用 saveEdit 的落盘逻辑（跨页合并单文件 / 单页逐版写回），同时 flushEditsToAll 防止手动编辑丢字。
  log('[结构化] 先自动保存当前识别结果（等同「保存修改」）…');
  await saveEdit();
  // 诊断：本轮到底分成几篇。若此处是 N 篇而结构化只出 1 篇文档，问题在导出/收拢层；
  // 若此处就是 1 篇，说明分篇判定把多篇并成了一篇（看结果面板卡片数即可确认）。
  try{
    const _nExp = aggregateTargetsForExport().length;
    const _modeTxt = (mergeMode==='cross'?'跨页模式':'单页模式');
    log('[结构化] 本轮分 '+_nExp+' 篇（'+_modeTxt+'）'+( _nExp>1 ? '，raw 产物按篇写为 {基名}__{篇序}.txt' : '，单篇' )+'。');
  }catch(e){}
  // 结构化成功后的统一清理：clearPost 只移除「本次实际结构化处理（ok 且未 skipped）」的版，
  // 其余载入但未处理的版保留在工作集与目录列表（pageList）中，可继续框选识别 / 再次结构化，
  // 避免出现「结构化后所有载入图片全部消失」的问题。cropped_hi/ 下已处理的源图仍物理删除。
  const clearPost = (cleanFiles)=>{
    // 仅移除「本次结构化实际成功产出（ok 且未 skipped）」的版；未完成的版保留在列表/工作集，可继续识别
    const cleaned = (cleanFiles && cleanFiles.length) ? cleanFiles.slice() : pageOrder.slice();  // 本次已结构化处理的整版名（cropped_hi 文件名）
    const crossRaw = (mergeMode==='cross' && pageOrder.length>1) ? crossBaseName() : '';  // 跨页 raw 中转目录名（output/{crossRaw}/）
    // pageOrder/cleaned 存去扩展名版名；pageList/navList/复选框 value 带扩展名。
    // 统一按去扩展名比较，避免格式不一致导致「已结构化的版从列表/导航移除失败、勾选态被误清」。
    const _strip = (s)=>String(s||'').replace(/\.[^.]+$/,'');
    const keepList = (arr)=>arr.filter(p=>!cleaned.some(c=>_strip(c)===_strip(p)));
    // 统一移除：只把本次已结构化处理的版（cleaned）从 列表/工作集/导航 中移除，其余原样保留。
    // 修复：原先当「处理版数 === 工作集总版数」时走 else 分支把整张 pageList/pageOrder 全部清空，
    // 导致用户「所有载入的图片全部消失」；现在无论处理了几版都只移除 cleaned，未处理的版继续保留。
    pageList=keepList(pageList); pageOrder=keepList(pageOrder); navList=keepList(navList);
    for(const p of cleaned){ const k=_strip(p); delete allPageData[k]; delete _savedSrcByPage[k]; delete _savedCarrierByPage[k]; }
    const cts=aggregateCrossTargets(); crossResults={}; for(const ct of cts){ crossResults[ct.key]=mergeCrossTarget(ct); }
    if(cleaned.some(c=>_strip(c)===_strip(srcName))){
      // 当前画布正显示已结构化的版 → 切到剩余第一版（gotoPage 会重画 画布/框/来源补充行）
      if(navList.length){ gotoPage(0); }
      else { pageIdx=-1; srcName=''; img=null; boxes=[]; results={}; const sn=$('srcName'); if(sn) sn.textContent='未载入'; renderBoxList(); renderResults(); draw(); }
    } else {
      // 当前显示的版未被结构化 → 原地保留，仅校正页码索引
      pageIdx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===srcName);
    }
    // 跨页整轮结束（所有版均被移除）时清空全局来源框；部分移除保留剩余版的来源
    if(!navList.length){ const _g=document.getElementById('srcGlobalText'); if(_g) _g.value=''; }
    updatePageNav(); renderSrcList(); renderPerPageSrc();
    // 剩余工作集版在 ② 列表保持勾选（复选框 value 带扩展名，pageOrder 不带，按去扩展名匹配），便于直接继续「识别全部」/ 再次结构化
    document.querySelectorAll('.src-chk').forEach(c=>{ c.checked = pageOrder.some(p=>p===_strip(c.value)); });
    syncSelAll();
    // 真正删除 cropped_hi/ 下本次已处理的源图：结构化产物（含整版原图副本与 OCR 结果）已落盘到 output/，
    // cropped_hi/ 里的原件成为残留，应物理删除（仅删指定文件、不级联、不碰 output/）。
    if(cleaned.length){
      fetch('/api/cleanup_cropped',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:cleaned})})
        .then(r=>r.json()).then(j=>{ if(j.ok){ if(j.removed&&j.removed.length){ log('[清理] 已物理删除 cropped_hi/ 源图 '+j.removed.length+' 个：'+j.removed.join('、')); } else { log('[警告] cropped_hi/ 中应删 '+cleaned.length+' 个源图，但后端未删到任何文件（可能扩展名不匹配或文件已不在，请核查）。'+(j.diag?'\n[清理诊断] '+j.diag.join(' | '):'')); } } else { log('[清理] cropped_hi/ 源图删除失败：'+(j.error||'')); } })
        .catch(e=>log('[清理] cropped_hi/ 源图删除失败：'+e));
    }
    // 跨页模式：删除 output/{crossRaw}/ 这个 raw 中转目录。saveEdit 自动落盘时把 raw 写到该目录，
    // _gather_round 已把内容搬进 output/{top}/{crossRaw}/（最终产物），原目录被掏空成残留空壳，应删掉。
    // 防护在后端：仅删该目录内程序产物文件（图片/.txt/.json），删空才 rmdir；含未知内容则保留不删。
    if(crossRaw){
      fetch('/api/cleanup_cross_raw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base:crossRaw})})
        .then(r=>r.json()).then(j=>{ if(j.ok){ if(j.dir_removed){ log('[清理] 已删除跨页 raw 中转目录 output/'+crossRaw+'/（最终产物在 output/'+(top==='plain_text'?'plain_text':'knowledge_base')+'/'+crossRaw+'/）。'); } else if(j.removed&&j.removed.length){ log('[清理] 已删除 output/'+crossRaw+'/ 下 '+j.removed.length+' 个残留文件（目录因含其他内容保留）。'); } } else { log('[清理] 跨页 raw 目录清理失败：'+(j.error||'')); } })
        .catch(e=>log('[清理] 跨页 raw 目录清理失败：'+e));
    }
  };
  // 仅结构化「本会话真正识别过」的版：output/ 可能残留上一轮会话的旧 OCR txt，
  // 不能因磁盘有 txt 就把未识别的版一起结构化/清理（只识别部分时其余版应原样保留）
  const recPages = pageOrder.filter(p=>allPageData[p]&&allPageData[p].results&&Object.keys(allPageData[p].results).length);
  if(!recPages.length){ log('[结构化] 没有本会话的识别结果（未识别任何版），已跳过。请先「识别全部」或逐框识别。'); return; }
  if(isCross){
    // 跨页：合并为一篇，单文件夹，打开该合并子文件夹（维持原行为）
    const outDir = crossBaseName().replace(/\.[^.]+$/,'');
    const pages = recPages.slice();
    fetch('/api/postprocess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,out_dir:outDir,pages,source_override:globalSO})})
      .then(r=>r.json()).then(j=>{ let s='[后置 阶段4 · '+(mode==='plain'?'纯文本':'知识库')+']\n'+(j.stdout||'')+(j.stderr?'\n'+j.stderr:''); if(j.ok&&j.opened_dir){ const rel=osRel(j.opened_dir); s+='\n↑ 已打开该轮文件夹：'+rel; }
        // 只把本次实际识别处理（recPages）的版交给 clearPost 移除并删除源图；未框选未识别的版保留在工作集，可继续框选识别。
        if(j.ok && !j.skipped){ clearPost(recPages.slice()); s+='\n[结构化] 已识别版已从工作集移除，未识别版保留在列表中，可继续框选识别。'; } log(s); })
      .catch(e=>log('后置失败：'+e));
    return;
  }
  // 单页模式：逐版调用 postprocess，每版独立子文件夹（output/{top}/{整版名}/）；
  // 全部完成后统一打开父目录 output/{top}（多子文件夹，不钻进某一页）
  // 注意：必须逐版串行。此前 forEach 并发全发，几十版同时调 DeepSeek 会触发限流/超时大面积失败；
  // 且失败时前端只打 j.error，而后端对「子进程退出码非 0」的返回没有 error 字段（真实原因在
  // stdout/stderr），用户只看到「失败:」空提示。现改为串行 + 失败时打印 stderr/stdout 末尾。
  const pages = recPages.slice();
  // 每版将导出几篇（=将出几份文档）：单页模式「一版内按组分多篇」时，版数与文档数不等，
  // 只报「N 版」会让人误以为只出 N 份，故此处按 aggregateTargetsForExport 的篇口径统计。
  const nArtsOf = {};
  for(const p of pages){ try{ nArtsOf[p]=aggregateTargetsForExport(p).length; }catch(_e){ nArtsOf[p]=0; } }
  let okCount=0, okArts=0, hasRealOutput=false; const okPages=[]; const failedPages=[];
  for(const pname of pages){
    const od = pname.replace(/\.[^.]+$/,'');
    try{
      const r = await fetch('/api/postprocess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,out_dir:od,pages:[pname],no_open:true,source_override:(perPageSO[pname]||globalSO||null)})});
      const j = await r.json();
      if(j.ok && !j.skipped){ okCount++; okArts += (nArtsOf[pname]||0); hasRealOutput=true; okPages.push(pname); }
      if(j.ok){
        const head=(j.stdout||'').split('\n').slice(0,2).join(' / ');
        log('[版 '+esc(pname)+'] '+(head||(j.skipped?'无产物':'成功')));
      }else{
        failedPages.push(pname);
        const errTail=String(j.error||j.stderr||j.stdout||'').trim().split('\n').filter(Boolean).slice(-3).join(' ／ ');
        log('[版 '+esc(pname)+'] 失败: '+(errTail||'原因未知（后端未返回错误信息），请复制完整日志反馈'));
      }
    }catch(e){
      failedPages.push(pname);
      const detail=(e&&e.stack)?e.stack:(''+e);
      log('[版 '+esc(pname)+'] 后置失败：'+(e&&e.message?e.message:e));
      fetch('/api/log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({line:'[探针] 版 '+pname+' postprocess fetch 失败: '+detail})}).catch(()=>{});
    }
  }
  {
    // 口径说明：版数 ≠ 文档数。单页模式一版之内用组名/标题分了多篇时，一篇出文档一份，
    // 因此这里同时报「版 / 篇 / 将出几份」，避免用户以为「1 版」就只该出 1 份。
    const _scope = (okCount===pages.length)
        ? ('本次识别 '+pages.length+' 版、共 '+okArts+' 篇')
        : ('本次识别 '+pages.length+' 版（其中可结构化 '+okCount+' 版）、共 '+okArts+' 篇');
    let s='[后置 阶段4 · '+(mode==='plain'?'纯文本':'知识库')+'] 单页模式：'+_scope
         +(okArts>0
             ? (' → 将出 '+okArts+' 份文档（逐篇一份，产物在 output/'+top+'/整版名/ 下）。')
             : ('；未能统计出可分篇数，请点「保存修改」核对实际导出的文件数后反馈。'));
    const after = ()=>{ if(hasRealOutput){ clearPost(okPages); s+='\n[结构化] 已完成版已从工作集移除，其余版保留在列表中，可继续框选识别。'; if(failedPages.length){ s+='\n[提示] 以下版未完成结构化（无产物/失败），仍保留在列表与 cropped_hi/：'+failedPages.join('、'); } } log(s); };
    // 统一打开父目录 output/{top}（多子文件夹，不钻进某一页）
    fetch('/api/open_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:top})})
      .then(r=>r.json()).then(j=>{ if(j.ok){ const rel=osRel(j.path); s+='\n↑ 已打开父文件夹：'+rel; } }).catch(()=>{})
      .finally(after);
  }
}

// ---------- 日志面板（彩色、轮询、搜索、复制、清空） ----------
function log(s){ const lines=String(s).split('\n'); const box=$('log'); const frag=document.createDocumentFragment();
  for(const m of lines){ const div=document.createElement('div'); div.className='logline lvl-info'; const msg=document.createElement('span'); msg.className='msg'; msg.textContent=m; div.appendChild(msg); div._txt=m; frag.appendChild(div); }
  box.appendChild(frag); if($('logFollow')===null||true){ box.scrollTop=box.scrollHeight; } applySearch();
  // 持久化：每行追加到后端日志文件，关闭 exe 后历史可恢复
  try{ fetch('/api/log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({line:String(s)})}).catch(()=>{}); }catch(e){} }
// 页面加载时恢复历史日志（关闭 exe 不丢失）
async function restoreLogs(){ try{ const r=await fetch('/api/logs'); const j=await r.json(); if(j.ok && j.content){ const box=$('log'); j.content.split('\n').filter(l=>l.length).forEach(m=>{ const div=document.createElement('div'); div.className='logline lvl-info'; const msg=document.createElement('span'); msg.className='msg'; msg.textContent=m; div.appendChild(msg); div._txt=m; box.appendChild(div); }); box.scrollTop=box.scrollHeight; applySearch(); } }catch(e){} }
function escLog(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function applySearch(){ const v=($('logSearch').value||'').trim().toLowerCase(); document.querySelectorAll('#log .logline').forEach(d=>{ const txt=(d._txt||'').toLowerCase();
  const msg=d.querySelector('.msg'); if(!v||txt.includes(v)){ d.style.display=''; if(msg){ if(v){ const i=txt.indexOf(v); msg.innerHTML=escLog(d._txt.slice(0,i))+'<mark style="background:#fde68a">'+escLog(d._txt.slice(i,i+v.length))+'</mark>'+escLog(d._txt.slice(i+v.length)); } else msg.textContent=d._txt; } } else d.style.display='none'; }); }
$('logSearch').addEventListener('input',applySearch);
$('logCopy').onclick=()=>{ const lines=[...document.querySelectorAll('#log .logline')].map(d=>d._txt||''); navigator.clipboard.writeText(lines.join('\n')).then(()=>{ const b=$('logCopy'); const o=b.textContent; b.textContent='已复制✓'; setTimeout(()=>b.textContent=o,1200); }); };
$('logClear').onclick=()=>{ if(confirm('确认清空日志面板？')){ $('log').innerHTML=''; off=0; } };

// ---------- 事件绑定 ----------
$('recogAll').onclick=recognizeAll;
$('zoomIn').onclick=()=>setZoom(userZoom*1.2);
$('zoomOut').onclick=()=>setZoom(userZoom/1.2);
$('zoomPct').ondblclick=()=>setZoom(1);   // 双击缩放百分比 = 恢复「适应窗口」
$('rotBtn').onclick=async()=>{
  if(!srcName){ alert('请先载入整版图片再旋转。'); return; }
  if(boxes.length && !confirm('旋转后当前整版已画的框会错位，将清空当前页的框。确定旋转？')) return;
  try{
    if(!img){ alert('当前没有可旋转的图片。'); return; }
    const _nm=srcName, _idx=pageIdx;
    // 1) 客户端即时旋转显示：画布 90° 顺时针，直接把 canvas 当绘制源，避免大图 base64 回传（秒级响应）
    const c=document.createElement('canvas'); c.width=natH; c.height=natW;
    const cx=c.getContext('2d'); cx.translate(c.width,0); cx.rotate(Math.PI/2); cx.drawImage(img,0,0);
    delete allPageData[_nm]; boxes=[]; results={};
    img=c; natW=c.width; natH=c.height; userZoom=1; resizeCanvas(); draw();
    const _sn=$('srcName'); if(_sn) _sn.textContent=_nm; renderBoxList();
    // 2) 后端旋转持久化（后台，不阻塞显示；失败再提示）
    const j=await fetch('/api/rotate_image?name='+encodeURIComponent(_nm)+'&dir=cropped_hi').then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); });
    if(!j.ok){ log('[旋转] 服务端保存失败：'+(j.error||'')); alert('旋转已显示，但保存失败：'+(j.error||'')); return; }
    log('[旋转] 已顺时针旋转 90°：'+_nm+'（已清空该版框选）');
  }catch(e){ log('[旋转] 异常：'+e); alert('旋转异常：'+e); }
};
$('canvasWrap').addEventListener('wheel',e=>{
  // 鼠标滚轮直接控制画布缩放；按住 Shift 仍可做水平滚动兜底
  if(!img)return;
  const w=$('canvasWrap');
  e.preventDefault();
  if(e.shiftKey){ w.scrollLeft+=e.deltaY; }
  else { setZoom(userZoom*(e.deltaY<0?1.1:0.9)); }
},{passive:false});
$('saveEdit').onclick=saveEdit;
// 一键清空：清掉「所有已载入版」的识别结果，并删除 output/ 下本轮导出的 raw 转录产物（txt/json）。
// 不动框选区域、不动 cropped_hi/ 整版原图、不动已生成的结构化 .md；清空后需重新点「识别全部」。
// 用 addEventListener 绑定（与 clearBoxes 同理），避免按钮 id 与同名函数/变量冲突导致点击无反应。
const _clearResultsBtn=$('clearResults');
if(_clearResultsBtn){ _clearResultsBtn.addEventListener('click', async ()=>{
  if(!pageOrder.length){ alert('尚未载入整版，没有可清空的结果。'); return; }
  const _anyRes=aggregateCrossTargets().some(t=>{
    const it=resultToItem(crossResults[t.key]); return !!(it.raw||it.body||it.title||it.author); });
  if(!_anyRes){ alert('当前没有识别结果可清空。'); return; }
  const _od=currentOutDir();                                              // 跨页且载入≥2 版时非空
  const _cb=(mergeMode==='cross'&&pageOrder.length>1)?crossBaseName():''; // 跨页导出所用基名
  if(!confirm('确认清空全部 '+pageOrder.length+' 版的识别结果？\n\n'
    + '· 右侧结果面板全部清空\n'
    + '· output/ 下本轮导出的 txt / json 一并删除\n'
    + '· 框选区域、整版原图、已生成的结构化文档不受影响\n\n'
    + '清空后需重新点「识别全部」。')) return;
  let _removed=[];
  try{
    const r=await fetch('/api/clear_results',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pages:pageOrder.slice(), out_dir:_od, cross_base:_cb})});
    const j=await r.json();
    if(j&&j.ok) _removed=j.removed||[];
    else log('[一键清空] 删除产物失败：'+((j&&j.error)||'未知原因'));
  }catch(e){ log('[一键清空] 删除产物失败：'+e.message); }
  // 清内存：各版 results 置空后，按当前框选重建 crossResults（重建出来的都是空文本）
  for(const p of pageOrder){ const k=String(p||'').replace(/\.[^.]+$/,''); if(allPageData[k]) allPageData[k].results={}; }
  crossResults={}; for(const ct of aggregateCrossTargets()) crossResults[ct.key]=mergeCrossTarget(ct);
  renderResults();
  log('[一键清空] 已清空 '+pageOrder.length+' 版的识别结果'
      + (_removed.length ? ('，删除产物 '+_removed.length+' 个：\n'+_removed.map(s=>'  output/'+s).join('\n'))
                         : '，output/ 下无对应产物文件') + '。');
}); }
// 注意：按钮 id 为 clearBoxes，与脚本中可能存在的同名函数/变量存在全局属性命名冲突，
// 用 addEventListener 绑定，避免被同名函数声明覆盖导致点击无反应。
const _clearBoxesBtn=$('clearBoxes');
if(_clearBoxesBtn){ _clearBoxesBtn.addEventListener('click',()=>{
  // 无论单页还是跨页模式，清空框只清空当前正在查看的这一版（srcName），不波及工作集其余版
  if(srcName && allPageData[srcName]){ allPageData[srcName].boxes=[]; }
  boxes=(srcName&&allPageData[srcName])?allPageData[srcName].boxes:[]; results={}; crossResults={}; selectedId=null; renderBoxList(); renderResults(); draw();
  log('[清空框选] 已清空当前这一版（'+srcName+'）的框选区域。');
}); }
$('runExtractGroup').onclick=runExtractAndGroup;
$('runPost').onclick=()=>runPost();
$('exportAll').onclick=()=>{
  // 一键导出：直接执行，不再弹 confirm，完成后自动打开总文件夹
  fetch('/api/export_all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})
    .then(r=>r.json()).then(j=>{
      if(!j.ok){
        // 两类产物都为空（如尚未结构化）：只记日志，不弹窗、不打开文件夹
        log('[一键导出] '+(j.error||'没有可导出的产物')+'（请先完成识别并结构化）');
        return;
      }
      const totalFound=j.kb_count+j.plain_count;
      const copied=(j.copied!=null)?j.copied:totalFound;
      log('[一键导出] 扫描到 '+j.kb_count+' 个 .md、'+j.plain_count+' 个 .txt，成功复制 '+copied+' / '+totalFound+' 个。已平铺到：'+j.rel);
      // 部分文件复制失败时逐条报出原因（此前静默中断，用户只看到文件夹里少了一半文件）
      if(j.failed && j.failed.length){
        log('[一键导出] 注意：有 '+j.failed.length+' 个文件复制失败（已成功 '+(j.copied||0)+' 个），以下为失败原因：');
        j.failed.slice(0,30).forEach(f=>log('  ✗ '+f.name+' — '+f.error));
        if(j.failed.length>30) log('  …其余 '+(j.failed.length-30)+' 条略（详见日志文件）');
        log('[一键导出] 常见原因：目标文件被 Word/编辑器占用、路径过长（>260 字符）、磁盘空间不足。处理后可再次点击导出。');
      }
      fetch('/api/open_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:j.dir})});
    }).catch(e=>{ alert('导出请求失败：'+e); });
};
$('prevPage').onclick=()=>gotoPage(pageIdx-1);
$('nextPage').onclick=()=>gotoPage(pageIdx+1);
$('importSource').onclick=()=>$('importSourceIn').click();
// 小文件走原来的 base64+JSON 批量通道（快、无兼容风险）；大文件必须走分块二进制直传。
// 阈值 24MB：base64 通道的浏览器峰值约为文件的 5.7 倍，24MB≈137MB，仍在安全范围；
// 超过则改用 /api/import_chunk，峰值仅单块 8MB，178MB / 2GB 都不会撑爆渲染进程。
const IMPORT_SMALL_MAX = 24*1024*1024;
const IMPORT_CHUNK = 8*1024*1024;
// 超过此体积直接拒绝并给出处置建议（抽图阶段还要整页渲染进内存，过大易失败）
const IMPORT_HARD_MAX = 2*1024*1024*1024;
async function uploadBigFile(f){
  const total=Math.ceil(f.size/IMPORT_CHUNK);
  let last=0;
  for(let i=0;i<total;i++){
    const blob=f.slice(i*IMPORT_CHUNK, Math.min(f.size,(i+1)*IMPORT_CHUNK));
    const buf=await blob.arrayBuffer();
    const j=await (await fetch('/api/import_chunk',{method:'POST',
      headers:{'Content-Type':'application/octet-stream','X-Mh-Name':encodeURIComponent(f.name),
               'X-Mh-Chunk':String(i),'X-Mh-Total':String(total),'X-Mh-Size':String(f.size)},
      body:buf})).json();
    if(!j.ok) throw new Error(j.error||('第 '+(i+1)+' 块失败'));
    last=i+1;
    // 每完成约 10% 或最后一块报一次进度，避免逐块刷屏
    if(last===total || last%Math.max(1,Math.ceil(total/10))===0){
      const pct=Math.round(last/total*100);
      log('[导入文件] '+esc(f.name)+' 分块上传 '+last+'/'+total+'（'+pct+'%）');
    }
    await new Promise(res=>setTimeout(res,0));   // 让出主线程，保持界面可响应
  }
  return last===total;
}
$('importSourceIn').onchange=async e=>{
  const files=Array.from(e.target.files||[]);
  if(!files.length)return;
  try{
    // 体积预检：先拦下必然撑爆内存 / 抽图必失败的超大文件，给出可处置的提示
    const tooBig=files.filter(f=>f.size>IMPORT_HARD_MAX);
    const okFiles=files.filter(f=>f.size<=IMPORT_HARD_MAX);
    if(tooBig.length){
      log('[导入文件] 已跳过 '+tooBig.length+' 个超大文件（单个 > 2GB）：'+tooBig.map(f=>esc(f.name)).join('、'));
      alert('以下文件超过 2GB，已跳过：\n\n'+tooBig.map(f=>'· '+f.name+'（'+(f.size/1073741824).toFixed(2)+' GB）').join('\n')+
            '\n\n单个 PDF 的合理上限约 1GB（报纸单页合订本）。超过此体量请先用 PDF 工具拆分后再导入。');
    }
    let okCount=0;
    // 大文件逐个分块上传（同一时刻内存里只有一个8MB 块）
    for(const f of okFiles){
      if(f.size>IMPORT_SMALL_MAX){
        const mb=(f.size/1048576).toFixed(1);
        log('[导入文件] '+esc(f.name)+'（'+mb+' MB）走分块上传通道，共 '+Math.ceil(f.size/IMPORT_CHUNK)+' 块…');
        try{ if(await uploadBigFile(f)){ okCount++; log('[导入文件] 已写入 source/：'+esc(f.name)+'（'+mb+' MB）'); } }
        catch(err){ log('[导入文件] 失败（'+esc(f.name)+'）：'+(err&&err.message||err)); }
        continue;
      }
      const buf=await f.arrayBuffer();
      const bytes=new Uint8Array(buf); const CH=0x8000; let bin='';
      for(let i=0;i<bytes.length;i+=CH){ bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+CH)); }
      const j=await (await fetch('/api/import_source',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({files:[{name:f.name,data:btoa(bin)}]})})).json();
      if(j.ok){ okCount+=j.count; log('[导入文件] 已写入 '+j.count+' 个文件到 source/：'+(j.written.join(', ')||'(无)')); }
      else log('[导入文件] 失败（'+esc(f.name)+'）：'+(j.error||''));
      await new Promise(res=>setTimeout(res,0));
    }
    log('[导入文件] 全部完成，共导入 '+okCount+' 个文件');
    refreshSourceList().then(fs=>{ if(fs&&fs.length){ document.querySelectorAll('.source-chk').forEach(c=>c.checked=true); syncSourceSelAll(); } });
  }catch(err){ log('导入文件失败：'+(err&&err.message||err)); }
  e.target.value='';
};
$('importClipboard').onclick=async ()=>{
  log('[粘贴截图] 正在读取剪贴板图像…');
  try{
    fetch('/api/import_clipboard',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
      .then(r=>r.json()).then(j=>{
        if(j.ok){ log('[粘贴截图] 已写入 '+j.count+' 张到 source/：'+(j.written.join(', ')||'(无)')); if(j.skipped&&j.skipped.length)log('[粘贴截图] 跳过：'+j.skipped.join(', ')); refreshSourceList().then(fs=>{ if(fs&&fs.length){ document.querySelectorAll('.source-chk').forEach(c=>c.checked=true); syncSourceSelAll(); } }); }
        else log('[粘贴截图] 失败：'+(j.error||''));
      }).catch(e=>log('粘贴截图失败：'+e));
  }catch(err){ log('粘贴截图失败：'+(err&&err.message||err)); }
};
// ---------- 文件删除机制 ----------
function resetCanvasState(){
  // 彻底复位：回到未载入的初始状态
  srcName=''; const sn=$('srcName'); if(sn) sn.textContent='未载入';
  img=null; natW=0; natH=0; pageIdx=-1;
  loadSeq++; imgLoading=false; drawing=null; dragState=null; cur=null;   // 作废在途载入，避免旧响应把画布复活
  allPageData={}; pageOrder=[]; crossResults={};
  boxes=[]; results={}; selectedId=null; pendingSelectId=null;
  updatePageNav(); renderBoxList(); renderResults(); resizeCanvas(); draw();
}
function refreshSourceList(){
  return fetch('/api/list_source').then(r=>r.json()).then(j=>{
    const box=$('sourceList'); box.innerHTML='';
    const fs=j.files||[];
    if(!fs.length){ if($('sourceCnt')) $('sourceCnt').textContent = '共 0 项'; const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.remove('collapsed'); syncSourceSelAll(); return fs; }
    fs.forEach(n=>{
      const lab=document.createElement('label'); lab.className='item'; lab.title='删除该文件及其派生产物';
      lab.innerHTML=`<input type="checkbox" class="source-chk" value="${esc(n)}"> <span>${esc(n)}</span>`;
      box.appendChild(lab);
    });
    syncSourceSelAll();
    const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.remove('collapsed');
    if($('sourceCnt')) $('sourceCnt').textContent = '共 '+fs.length+' 项';
    return fs;
  }).catch(()=>{});
}
function syncSourceSelAll(){ const sa=$('sourceSelAll'); if(!sa) return; const chks=document.querySelectorAll('.source-chk');
  if(!chks.length){ sa.checked=false; sa.indeterminate=false; return; }
  const all=[...chks].every(c=>c.checked); const none=![...chks].some(c=>c.checked);
  sa.checked = all; sa.indeterminate = !all && !none; }
function delFile(sub, name, skipConfirm=false, noRefresh=false){
  return new Promise(resolve=>{
    const extra = sub==='source' ? '、cropped_hi 抽图及 output 同名产物' : '及 output 同名产物';
    if(!skipConfirm && !confirm('确认删除「'+name+'」'+extra+'？\n此操作不可撤销。')){ resolve(false); return; }
    fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sub,name})})
      .then(r=>r.json()).then(j=>{
        if(j.ok){ let msg='[删除] 已删除 '+sub+'/'+name; if(j.cascade&&j.cascade.length) msg+='，并清理：'+j.cascade.join(', '); log(msg); if(!noRefresh){ refreshSourceList(); refreshImageList(); } resolve(true); }
        else { log('[删除] 失败：'+(j.error||'')); resolve(false); }
      }).catch(e=>{ log('删除失败：'+e); resolve(false); });
  });
}
$('delSource').onclick=async ()=>{ const chks=[...document.querySelectorAll('.source-chk:checked')]; if(!chks.length){ alert('请先在上方勾选要删除的导入文件。'); return; }
  if(!confirm('确认删除选中的 '+chks.length+' 个导入文件（含派生产物）？')) return;
  let ok=0;
  await Promise.all(chks.map(c=>delFile('source', c.value, true, true).then(r=>{ if(r) ok++; })));
  log('[删除] 已删除 '+ok+'/'+chks.length+' 个导入文件');
  await refreshSourceList(); await refreshImageList();
  // source/ 删除会级联删 cropped_hi/ 同名整版；若当前显示的整版已被级联删除，则复位画布
  if(srcName && !pageList.some(f=>f.replace(/\.[^.]+$/,'')===srcName)){
    if(navList.length){ pageIdx=0; updatePageNav(); gotoPage(0); }
    else { resetCanvasState(); log('[删除] 源文件删除导致当前整版被级联清理，已复位到初始画布状态。'); }
  }
};
$('delCropped').onclick=async ()=>{ const chks=[...document.querySelectorAll('.src-chk:checked')]; if(!chks.length){ alert('请先在上方勾选要删除的整版原图。'); return; }
  const delNames=new Set(chks.map(c=>c.value.replace(/\.[^.]+$/,'')).filter(Boolean));
  if(!confirm('确认删除选中的 '+chks.length+' 个整版原图（含 output 同名产物）？')) return;
  let ok=0;
  await Promise.all(chks.map(c=>delFile('cropped_hi', c.value, true, true).then(r=>{ if(r) ok++; })));
  log('[删除] 已删除 '+ok+'/'+chks.length+' 个整版原图');
  await refreshImageList();
  // 刷新后若当前源已不在 cropped_hi/ 中（无论是否被本次勾选删除），都要复位或切换到仍存在的页
  if(srcName && !pageList.some(f=>f.replace(/\.[^.]+$/,'')===srcName)){
    if(navList.length){ pageIdx=0; updatePageNav(); gotoPage(0); }
    else { resetCanvasState(); log('[删除] 整版列表已空，已复位到初始画布状态。'); }
  } };
refreshImageList();
refreshSourceList();
// 全选 / 取消全选
$('selAll').onclick=e=>{
  const chks=document.querySelectorAll('.src-chk');
  if(!chks.length){ // 列表为空时全选没有作用对象，阻止勾选并提示
    e.preventDefault(); e.target.checked=false; e.target.indeterminate=false;
    flashHint('当前没有可勾选的整版原图，请先抽图并归档或导入图片。');
    return;
  }
  chks.forEach(c=>c.checked=e.target.checked); syncSelAll();
};
$('selAll').onchange=e=>{ // 键盘 / 间接触发时同步子项（点空白区等）
  const chks=document.querySelectorAll('.src-chk');
  if(chks.length){ chks.forEach(c=>c.checked=e.target.checked); syncSelAll(); }
};
document.addEventListener('change', e=>{ if(e.target.classList.contains('src-chk')) syncSelAll(); if(e.target.classList.contains('source-chk')) syncSourceSelAll(); });
// 整版原图列表：↑/↓ 调整顺序（仅改前端 pageList 顺序，载入按此顺序；并持久化到 cropped_hi/.order.json）
$('srcList').addEventListener('click', e=>{
  const btn = e.target.closest('.ops .mv'); if(!btn || btn.disabled) return;
  const items = [...$('srcList').children];
  const i = items.indexOf(btn.closest('.item')); if(i<0) return;
  const j = btn.dataset.up!==undefined ? i-1 : i+1;
  if(j<0 || j>=pageList.length) return;
  const checked = new Set([...document.querySelectorAll('.src-chk')].filter(c=>c.checked).map(c=>c.value));
  const t = pageList[i]; pageList[i] = pageList[j]; pageList[j] = t;
  renderSrcList();
  document.querySelectorAll('.src-chk').forEach(c=>{ c.checked = checked.has(c.value); });
  syncSelAll();
  persistCroppedOrder();
});
// 整版列表：拖拽调整顺序（HTML5 拖放，兼容现有 ↑/↓ 与勾选；拖动后持久化到 .order.json）
let _dragSrcIdx = -1;
(function(){
  const sl = $('srcList');
  sl.addEventListener('dragstart', e=>{
    const item = e.target.closest('.item'); if(!item) return;
    _dragSrcIdx = [...sl.children].indexOf(item);
    item.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', String(_dragSrcIdx)); } catch(_) {}
  });
  sl.addEventListener('dragover', e=>{
    e.preventDefault();
    const item = e.target.closest('.item'); if(!item) return;
    const overIdx = [...sl.children].indexOf(item);
    const dragging = sl.querySelector('.dragging');
    if(overIdx<0 || _dragSrcIdx<0 || !dragging || overIdx===_dragSrcIdx) return;
    if(overIdx < _dragSrcIdx) sl.insertBefore(dragging, item);
    else sl.insertBefore(dragging, item.nextSibling);
    _dragSrcIdx = [...sl.children].indexOf(dragging);
  });
  sl.addEventListener('drop', e=>{
    e.preventDefault();
    const dragging = sl.querySelector('.dragging'); if(dragging) dragging.classList.remove('dragging');
    const checked = new Set([...sl.querySelectorAll('.src-chk')].filter(c=>c.checked).map(c=>c.value));
    pageList = [...sl.children].map(el=>el.querySelector('.src-chk').value);
    renderSrcList();
    sl.querySelectorAll('.src-chk').forEach(c=>{ c.checked = checked.has(c.value); });
    syncSelAll();
    persistCroppedOrder();
    _dragSrcIdx = -1;
    log('[排序] 已通过拖拽调整整版顺序并保存。');
  });
  sl.addEventListener('dragend', e=>{
    const dragging = sl.querySelector('.dragging'); if(dragging) dragging.classList.remove('dragging');
    _dragSrcIdx = -1;
  });
})();
async function persistCroppedOrder(){
  try{ await fetch('/api/reorder_cropped',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:pageList})}); }
  catch(e){ log('[排序] 持久化失败：'+e); }
}
// source 全选 / 取消全选
$('sourceSelAll').onclick=e=>{
  const chks=document.querySelectorAll('.source-chk');
  if(!chks.length){ e.preventDefault(); e.target.checked=false; e.target.indeterminate=false; flashHint('当前没有可勾选的导入文件。'); return; }
  chks.forEach(c=>c.checked=e.target.checked); syncSourceSelAll();
};
$('sourceSelAll').onchange=e=>{
  const chks=document.querySelectorAll('.source-chk');
  if(chks.length){ chks.forEach(c=>c.checked=e.target.checked); syncSourceSelAll(); }
};
// 列表折叠 / 展开（箭头点击切换）
document.querySelectorAll('.fold-toggle').forEach(b=>b.onclick=()=>{ const w=b.closest('.src-list-wrap'); if(w) w.classList.toggle('collapsed'); });
// 整版处理方式开关
$('mergeSel').onchange=e=>{ mergeMode=e.target.value; updatePageNav();
  // 若已识别过，切换模式后立即按新模式重算跨页视图（否则 crossResults 仍为旧模式聚合，结果面板会错乱/空白）
  if(Object.keys(crossResults).length){ const cts=aggregateCrossTargets(); crossResults={}; for(const ct of cts){ crossResults[ct.key]=mergeCrossTarget(ct); } }
  renderResults(); };
// 载入：读取勾选的整版，按勾选顺序批量载入为工作集（pageOrder）
$('loadCropped').onclick=async ()=>{
  let chks=[...document.querySelectorAll('.src-chk:checked')];
  if(!chks.length){ alert('请先在上方勾选要载入的整版原图（或用「全选」）。'); return; }
  // 先强制刷新列表，避免 pageList 陈旧（如抽图后未刷新导致只载到旧图）
  try{
    const checkedNames = new Set(chks.map(c=>c.value));
    const j=await fetch('/api/list_images?dir=cropped_hi').then(r=>r.json());
    pageList=j.files||[];
    renderSrcList();
    // 刷新会重建 DOM，按之前勾选的文件名恢复勾选状态
    document.querySelectorAll('.src-chk').forEach(c=>{ c.checked = checkedNames.has(c.value); });
    syncSelAll();
    chks=[...document.querySelectorAll('.src-chk:checked')];
  }catch(e){ /* 失败沿用现有 */ }
  if(!chks.length){ alert('cropped_hi/ 下没有可载入的 PNG，请先抽图并归档。'); return; }
  allPageData=allPageData||{}; pageOrder=[]; crossResults={};
  chks.forEach(c=>{ const pname=c.value.replace(/\.[^.]+$/,'');
    if(!allPageData[pname]) allPageData[pname]={boxes:[],results:{}}; // 已画框的版保留，不覆盖
    pageOrder.push(pname); });
  navList = chks.map(c=>c.value); // 导航（翻页）范围限定为本次勾选载入的整版，避免翻到未载入的图
  const verb = mergeMode==='cross' ? '跨页模式·按阅读顺序合并' : '单页模式·每版独立';
  log('[载入] 已载入 '+pageOrder.length+' 版（'+verb+'）：'+pageOrder.map(n=>'\n  · '+n).join('')+'\n正在显示 '+pageOrder[0]+'，可翻页查看其余各版。');
  const firstIdx = navList.findIndex(f=>f===chks[0].value);
  if(firstIdx>=0) gotoPage(firstIdx);
  else { alert('未找到首个勾选图片：'+chks[0].value); return; }
  renderBoxList(); renderResults();
  refreshSourceList();
};

// ---------- 抽屉（设置 / 用量统计 / 提示词） ----------
const mask=$('drawerMask'); const drawer=$('drawer'); const usageDrawer=$('usageDrawer'); const promptDrawer=$('promptDrawer'); const sortDrawer=$('sortDrawer');
async function openExternalUrl(url){
  try { await fetch('/api/open_url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}); }
  catch(e){ log('[打开链接] 失败：'+e); }
}
async function checkUpdate(){
  const st=$('updateStatus');
  if(!st) return;
  st.textContent='检查中…'; st.style.color='var(--mut)';
  try{
    const r=await fetch('/api/check_update',{cache:'no-store'});
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||'检查失败');
    if(j.need){
      let html='发现新版本 <b>'+escHtml(j.latest)+'</b>（当前 '+escHtml(j.current||'--')+'）';
      // 该版本没提供与本机芯片匹配的 Mac 包时（如 Intel 包缺失），不提供自动更新按钮——
      // 否则点了必然装错架构、装完打不开，用户还找不到原因。
      // macOS：仅当运行位置无法自我替换（还在 dmg 里 / 不在应用程序文件夹 / 目录不可写）
      // 或上次更新未完成时，才只给「打开发布页手动下载」；位置正常的 Mac 用户照常「立即更新」。
      if(j.manual_only){
        html+='<div style="color:var(--mut);margin-top:6px;line-height:1.7;">'
            + (j.env_hint?('<span style="color:var(--warn);">'+escHtml(j.env_hint)+'</span><br>'):'')
            + '下载时按芯片选包：Apple 芯片（M1/M2/M3…）选 <b>arm64</b>，Intel 选 <b>x86_64</b>。<br>'
            + '请下载 dmg，双击后把里面的「墨痕」拖进「应用程序」文件夹；'
            + '装好后再弹出 dmg，从「应用程序」里打开。直接在 dmg 窗口里双击运行会导致更新失败。<br>'
            + '<button class="sec" id="gotoReleaseArch" style="margin-top:8px;">打开发布页下载</button></div>';
      } else if(j.arch_warn){
        html+='<div style="color:var(--warn);margin-top:6px;line-height:1.6;">'
            + '该版本未提供适用于本机芯片（'+escHtml(j.local_arch||'未知')+'）的 Mac 安装包，已停用自动更新以免装错架构；'
            + '请 <button class="sec" id="gotoReleaseArch" style="margin:0 2px;">手动打开发布页</button> 自行选择。</div>';
      } else {
        html+=' <button class="sec" id="doUpdate" style="margin-left:8px;">立即更新</button>';
      }
      st.innerHTML=html;
      const _du=$('doUpdate'); if(_du) _du.onclick=()=>startUpdate(j);
      const _ga=$('gotoReleaseArch'); if(_ga) _ga.onclick=()=>openExternalUrl(j.page_url||RELEASE_PAGE_URL);
    } else {
      st.textContent='当前已是最新版本（'+escHtml(j.current||'--')+'）'; st.style.color='var(--ok)';
    }
  }catch(e){
    st.innerHTML='检查失败：'+escHtml((e&&e.message)||e)+' <button class="sec" id="gotoReleaseFallback">手动打开发布页</button>'; st.style.color='var(--warn)';
    $('gotoReleaseFallback').onclick=()=>openExternalUrl(RELEASE_PAGE_URL);
  }
}
async function startUpdate(j){
  // 防御：Mac 版不走程序内替换，直接引导去发布页（正常路径已不会调到这里）。
  if(j && j.manual_only){ openExternalUrl(j.page_url||RELEASE_PAGE_URL); return; }
  const st=$('updateStatus'); if(!st) return;
  st.textContent='正在下载更新包…（可能需要一会儿）'; st.style.color='var(--mut)';
  try{
    const r=await fetch('/api/update_download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({download_url:j.download_url, sha256_url:j.sha256_url, alt_download_url:j.alt_download_url||'', alt_sha256_url:j.alt_sha256_url||'', target:j.latest||''})});
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'下载失败');
    st.textContent='下载完成，正在重启以完成升级…'; st.style.color='var(--ok)';
    try{ await fetch('/api/update_apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
  }catch(e){
    st.innerHTML='下载失败：'+escHtml((e&&e.message)||e)+' <button class="sec" id="gotoReleaseFallback2">手动打开发布页</button>'; st.style.color='var(--err)';
    const fb=$('gotoReleaseFallback2'); if(fb) fb.onclick=()=>openExternalUrl(j.page_url||RELEASE_PAGE_URL);
  }
}
// 启动后自动检查更新：读开关 → 有新版则按开关决定自动更新或弹窗
async function autoUpdateCheckOnLaunch(){
  try{
    const r=await fetch('/api/auto_update'); const j=await r.json();
    AUTO_UPDATE=!!j.auto_update;
    SKIP_VERSION=j.skip_version||null;
    const c=$('autoUpdateChk'); if(c) c.checked=AUTO_UPDATE;
  }catch(e){}
  try{
    const r=await fetch('/api/check_update',{cache:'no-store'});
    const j=await r.json();
    if(!j.ok || !j.need) return;
    if(j.latest===SKIP_VERSION){ log('[更新] 已设置跳过 '+SKIP_VERSION+'，暂不提醒'); return; }
    // 当前环境不能自我替换（在 dmg 里运行 / 不在应用程序文件夹 / 上次更新未完成）：
    // 只记日志，等用户手动点「检查更新」看原因与下载入口。
    if(j.manual_only){
      log('[更新] 发现新版本 '+j.latest+'，但当前环境不适合自动更新'+(j.env_hint?('（'+j.env_hint+'）'):'')+'，请点「检查更新」手动下载');
      return;
    }
    if(AUTO_UPDATE && !j.arch_warn){ log('[更新] 已开启自动更新，正在下载新版本…'); startUpdate(j); }
    else if(AUTO_UPDATE && j.arch_warn){ log('[更新] 该版本无本机芯片（'+(j.local_arch||'未知')+'）对应包，跳过自动更新，改提示手动选包'); showUpdateModal(j); }
    else { showUpdateModal(j); }
  }catch(e){ /* 启动检查失败不打扰用户 */ }
}
// 可关闭的更新提示弹窗（点3）：询问是否立即更新
function showUpdateModal(j){
  let m=document.getElementById('updateModal');
  if(!m){
    m=document.createElement('div'); m.id='updateModal'; m.className='modal-mask';
    m.innerHTML='<div class="modal-card">'
      +'<div class="modal-title">发现新版本</div>'
      +'<div class="modal-body"><span class="um-body"></span></div>'
      +'<label class="switch-row um-skip-row" style="margin:6px 0 14px;">'
      +'<input type="checkbox" id="umSkip"><span>不再提示该版本</span></label>'
      +'<div class="modal-foot"><button class="ghost" id="umLater">稍后</button>'
      +'<button class="sec" id="umNow">立即更新</button></div></div>';
    document.body.appendChild(m);
  }
  // 架构不匹配时：文案改说明、按钮改「打开发布页」，不提供装错架构的入口
  const _bodyM=m.querySelector('.um-body'), _nowM=m.querySelector('#umNow');
  if(j.arch_warn){
    _bodyM.textContent='墨痕已发布新版本 '+ (j.latest||'') +'，但该版本未提供适用于本机芯片（'+(j.local_arch||'未知')+'）的 Mac 安装包。为避免装错架构，请到发布页手动选择对应芯片的包。';
    _nowM.textContent='打开发布页';
  } else {
    _bodyM.textContent='墨痕已发布新版本 '+ (j.latest||'') +'，是否立即更新？';
    _nowM.textContent='立即更新';
  }
  const skipBox=m.querySelector('#umSkip'); if(skipBox) skipBox.checked=false;
  m.classList.add('open');
  m.querySelector('#umLater').onclick=async ()=>{
    const skip=skipBox&&skipBox.checked;
    if(skip){
      try{
        await fetch('/api/auto_update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skip_version:j.latest})});
        SKIP_VERSION=j.latest; log('[更新] 已设置跳过 '+j.latest);
      }catch(e){ log('[更新] 保存跳过版本失败：'+e.message); }
    }
    m.classList.remove('open');
  };
  _nowM.onclick=()=>{
    m.classList.remove('open');
    if(j.arch_warn){ openExternalUrl(j.page_url||RELEASE_PAGE_URL); } else { startUpdate(j); }
  };
  m.onclick=(e)=>{ if(e.target===m) m.classList.remove('open'); };
}
function openSettingsDrawer(){ drawer.classList.add('open'); usageDrawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.add('open'); }
function openUsageDrawer(){ usageDrawer.classList.add('open'); drawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.add('open'); loadUsage(); }
function openPromptDrawer(){ loadPrompts(); promptDrawer.classList.add('open'); drawer.classList.remove('open'); usageDrawer.classList.remove('open'); mask.classList.add('open'); }
function openSortDrawer(){ sortDrawer.classList.add('open'); drawer.classList.remove('open'); usageDrawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.add('open'); }
function closeDrawers(){ drawer.classList.remove('open'); usageDrawer.classList.remove('open'); promptDrawer.classList.remove('open'); sortDrawer.classList.remove('open'); mask.classList.remove('open'); }
$('gearBtn').onclick=openSettingsDrawer; $('drawerClose').onclick=closeDrawers;
$('usageBtn').onclick=openUsageDrawer; $('usageClose').onclick=closeDrawers; mask.onclick=closeDrawers;
$('promptBtn').onclick=openPromptDrawer; $('promptClose').onclick=closeDrawers;
// 整版排序抽屉
$('sortBtn').onclick=openSortDrawer; $('sortClose').onclick=closeDrawers; $('sortCancel').onclick=closeDrawers; $('applySort').onclick=applySort;
// 自然排序比较：按数字段逐段比较，避免 p1,p10,p2 字典序乱序
function natCmp(a,b){
  const tok = s => (s.match(/(\d+)|(\D+)/g)||[]).map(t=>/^\d+$/.test(t)?[0,+t]:[1,t]);
  const A=tok(a), B=tok(b);
  for(let i=0;i<Math.min(A.length,B.length);i++){
    if(A[i][0]!==B[i][0]) return A[i][0]-B[i][0];
    if(A[i][1]<B[i][1]) return -1;
    if(A[i][1]>B[i][1]) return 1;
  }
  return A.length-B.length;
}
// 应用排序：依据（默认/文件名/修改时间）× 方向（升/降），重排 pageList 并持久化到 cropped_hi/.order.json
function applySort(){
  const by=(document.querySelector('input[name="sortBy"]:checked')||{}).value||'default';
  const desc=(document.querySelector('input[name="sortDir"]:checked')||{}).value==='desc';
  if(!pageList.length){ closeDrawers(); flashHint('整版列表为空，无可排序项。'); return; }
  const arr=pageList.slice();
  if(by==='name'){
    arr.sort(natCmp);
  } else if(by==='mtime'){
    const mt=f=>(fileMtimes[f]||0);
    arr.sort((a,b)=>mt(a)-mt(b));
  } else { // 默认顺序：重置为页码 / 文件名自然序
    arr.sort(natCmp);
  }
  if(desc) arr.reverse();
  pageList=arr; renderSrcList(); persistCroppedOrder();
  closeDrawers();
  log('[排序] 已按「'+(by==='name'?'文件名':by==='mtime'?'修改时间':'默认顺序')+'·'+(desc?'降序':'升序')+'」重排整版列表并保存。');
}
$('checkUpdateBtn').onclick=checkUpdate;
// 自动更新开关：开启时把开关状态持久化到 box_config.json
const autoChk=$('autoUpdateChk');
// Mac 版照常支持自动更新：只有在「程序不在应用程序文件夹 / 还在 dmg 里运行 / 目录不可写」或
// 「上次更新没完成」时，check_update 才返回 manual_only，届时只给「打开发布页」不给「立即更新」。
if(autoChk) autoChk.onchange=async ()=>{
  const v=autoChk.checked;
  try{ await fetch('/api/auto_update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_update:v})}); AUTO_UPDATE=v; log('[设置] 自动更新已'+(v?'开启':'关闭')); }
  catch(e){ log('[设置] 自动更新开关保存失败：'+e.message); }
};
document.addEventListener('keydown', e=>{ if(e.key==='Escape'&&(drawer.classList.contains('open')||usageDrawer.classList.contains('open')||promptDrawer.classList.contains('open')||sortDrawer.classList.contains('open'))) closeDrawers(); });

// ---------- 提示词抽屉 ----------
let revertOcr = false;
let postPrompts = { gb7714: {kb:'', plain:''}, history_research: {kb:'', plain:''} };   // 结构化提示词：引用格式 × 输出模式
let postDef = { gb7714: {kb:'', plain:''}, history_research: {kb:'', plain:''} };        // 对应内置默认（「恢复默认」回填用）
let postEditMode = 'plain';                     // 抽屉当前正在编辑的输出模式
let postEditFmt = 'gb7714';                     // 抽屉当前正在编辑的引用格式
let revertPost = { gb7714: {kb:false, plain:false}, history_research: {kb:false, plain:false} };  // 各槽是否点了「恢复默认」
async function loadPrompts(){
  try {
    const c = await (await fetch('/api/config')).json();
    $('prompt_ocr').dataset.def = c.prompt_ocr_default || '';
    postDef.gb7714.kb    = c.prompt_post_default || '';
    postDef.gb7714.plain = c.prompt_post_plain_default || '';
    postDef.history_research.kb    = c.prompt_post_history_default || '';
    postDef.history_research.plain = c.prompt_post_plain_history_default || '';
    $('prompt_ocr').value = (c.values && c.values.PROMPT_OCR && c.values.PROMPT_OCR.trim()) ? c.values.PROMPT_OCR : (c.prompt_ocr_default || '');
    postPrompts.gb7714.kb    = (c.values && c.values.PROMPT_POST             && c.values.PROMPT_POST.trim())             ? c.values.PROMPT_POST             : postDef.gb7714.kb;
    postPrompts.gb7714.plain = (c.values && c.values.PROMPT_POST_PLAIN       && c.values.PROMPT_POST_PLAIN.trim())       ? c.values.PROMPT_POST_PLAIN       : postDef.gb7714.plain;
    postPrompts.history_research.kb    = (c.values && c.values.PROMPT_POST_HISTORY       && c.values.PROMPT_POST_HISTORY.trim())       ? c.values.PROMPT_POST_HISTORY       : postDef.history_research.kb;
    postPrompts.history_research.plain = (c.values && c.values.PROMPT_POST_PLAIN_HISTORY && c.values.PROMPT_POST_PLAIN_HISTORY.trim()) ? c.values.PROMPT_POST_PLAIN_HISTORY : postDef.history_research.plain;
    postEditMode = 'plain'; postEditFmt = 'gb7714';
    const rb = document.querySelector('input[name=postModeEdit][value=plain]'); if (rb) rb.checked = true;
    const rf = document.querySelector('input[name=postFmtEdit][value=gb7714]'); if (rf) rf.checked = true;
    $('prompt_post').value = postPrompts[postEditFmt][postEditMode];
    updatePostHint();
  } catch(e){ log('[提示词] 读取配置失败：'+e); }
}
function updatePostHint(){
  const h = $('postHint');
  const fmt = (postEditFmt === 'history_research') ? '《历史研究》注释规范' : 'GB/T 7714-2015';
  const mode = (postEditMode === 'plain') ? '纯文本（.txt）' : '知识库（.md）';
  h.innerHTML = fmt + ' · ' + mode + '：用于「结构化」阶段题录抽取。';
}
// 切换抽屉内正在编辑的结构化提示词（输出模式 / 引用格式）
document.querySelectorAll('input[name=postModeEdit]').forEach(r=>{
  r.onchange = () => {
    postPrompts[postEditFmt][postEditMode] = $('prompt_post').value;   // 先保存当前编辑内容
    postEditMode = r.value;
    $('prompt_post').value = postPrompts[postEditFmt][postEditMode];
    updatePostHint();
  };
});
document.querySelectorAll('input[name=postFmtEdit]').forEach(r=>{
  r.onchange = () => {
    postPrompts[postEditFmt][postEditMode] = $('prompt_post').value;   // 先保存当前编辑内容
    postEditFmt = r.value;
    $('prompt_post').value = postPrompts[postEditFmt][postEditMode];
    updatePostHint();
  };
});
$('resetOcr').onclick  = () => { $('prompt_ocr').value  = $('prompt_ocr').dataset.def  || ''; revertOcr  = true; };
$('resetPost').onclick = () => { $('prompt_post').value = postDef[postEditFmt][postEditMode] || ''; revertPost[postEditFmt][postEditMode] = true; };
$('savePrompt').onclick = async () => {
  const hint = $('promptHint'); hint.textContent = '保存中…'; hint.style.color = 'var(--mut)';
  try {
    const cur = await (await fetch('/api/config')).json();
    // 现有磁盘配置（含密钥等环境项），只更新提示词相关键
    const merged = cur.values || {};
    if (revertOcr) merged.PROMPT_OCR = ''; else merged.PROMPT_OCR = $('prompt_ocr').value;
    if (revertPost.gb7714.kb)    merged.PROMPT_POST             = ''; else merged.PROMPT_POST             = postPrompts.gb7714.kb;
    if (revertPost.gb7714.plain) merged.PROMPT_POST_PLAIN       = ''; else merged.PROMPT_POST_PLAIN       = postPrompts.gb7714.plain;
    if (revertPost.history_research.kb)    merged.PROMPT_POST_HISTORY       = ''; else merged.PROMPT_POST_HISTORY       = postPrompts.history_research.kb;
    if (revertPost.history_research.plain) merged.PROMPT_POST_PLAIN_HISTORY = ''; else merged.PROMPT_POST_PLAIN_HISTORY = postPrompts.history_research.plain;
    revertOcr = false; revertPost = { gb7714: {kb:false, plain:false}, history_research: {kb:false, plain:false} };
    const r = await fetch('/api/config/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(merged)});
    const j = await r.json();
    if (j.ok) { hint.textContent = '已保存 ✓'; hint.style.color = 'var(--ok)'; log('[提示词] 已保存，下次识别 / 结构化即时生效'); }
    else { hint.textContent = '保存失败：'+(j.error||''); hint.style.color = 'var(--err)'; log('[提示词] 保存失败：'+(j.error||'')); }
  } catch(e){ hint.textContent = '保存失败：'+e.message; hint.style.color = 'var(--err)'; }
};

// ---------- 用量统计 ----------
const STAGE_LABEL = { box_ocr: 'OCR', deepseek: '结构化' };
function escHtml(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function loadUsage(){
  const model = $('usageModelSel').value;
  const stage = $('usageStageSel').value;
  try {
    const r = await fetch('/api/usage?model=' + encodeURIComponent(model) + '&stage=' + encodeURIComponent(stage));
    const j = await r.json();
    if (!j.ok){ $('usageHint').textContent = j.msg || '读取失败'; return; }
    const sel = $('usageModelSel');
    const cur = sel.value;
    const opts = ['<option value="全部">全部</option>'].concat((j.models||[]).map(m => `<option value="${escHtml(m)}">${escHtml(m)}</option>`));
    sel.innerHTML = opts.join('');
    if ((j.models||[]).includes(cur) || cur === '全部') sel.value = cur; else sel.value = '全部';
    const tbody = $('usageTable').querySelector('tbody');
    tbody.innerHTML = '';
    if (!j.by_model.length){
      $('usageEmpty').style.display = 'block';
    } else {
      $('usageEmpty').style.display = 'none';
      for (const row of j.by_model){
        const tr = document.createElement('tr');
        const stageLabel = STAGE_LABEL[row.stage] || row.stage;
        tr.innerHTML =
          `<td>${escHtml(row.model || '—')}</td>` +
          `<td class="dim">${escHtml(stageLabel)}</td>` +
          `<td class="num">${(row.stage==='box_ocr'?(row.ocr_calls||0):(row.calls||0)).toLocaleString()}</td>` +
          `<td class="num">${(row.prompt || 0).toLocaleString()}</td>` +
          `<td class="num">${(row.completion || 0).toLocaleString()}</td>` +
          `<td class="num">${(row.total || 0).toLocaleString()}</td>` +
          `<td class="num">${((row.stage==='box_ocr'?(row.ocr_calls||0):(row.calls||0))?row.duration_s/(row.stage==='box_ocr'?(row.ocr_calls||0):(row.calls||0)):0).toFixed(2)}</td>`;
        tbody.appendChild(tr);
      }
    }
    $('usageHint').textContent = '数据来源 token_log.csv（每次 OCR / 结构化完成自动追加）';
  } catch(e){
    $('usageHint').textContent = '网络错误：' + e.message;
  }
}
$('usageRefresh').onclick = loadUsage;
$('usageModelSel').onchange = loadUsage;
$('usageStageSel').onchange = loadUsage;

// ---------- 日志底部抽屉（bottom sheet） ----------
const logSheet=$('logSheet'); const logBtn=$('logBtn');
function openLogSheet(){ logSheet.classList.add('open'); logBtn.classList.add('active'); logBtn.setAttribute('aria-expanded','true'); }
function closeLogSheet(){ logSheet.classList.remove('open'); logBtn.classList.remove('active'); logBtn.setAttribute('aria-expanded','false'); }
function toggleLogSheet(){ logSheet.classList.contains('open')?closeLogSheet():openLogSheet(); }
$('logBtn').onclick=toggleLogSheet; $('logSheetClose').onclick=closeLogSheet;
const EYE_OPEN=`<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const EYE_CLOSED=`<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.84 9.84 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-3.72.91-3.46 3.46"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
document.querySelectorAll('.eye').forEach(e=>{ e.innerHTML=EYE_CLOSED; e.onclick=()=>{ const inp=$(e.dataset.target); const hidden=inp.classList.contains('secret'); inp.classList.toggle('secret'); e.innerHTML=hidden?EYE_OPEN:EYE_CLOSED; }; });
// OCR 服务商：输入框实时暂存 + 切换时回填对应服务商凭据
['cfgApiKey','cfgBaseUrl','cfgModel','cfgKandianEmail','cfgKandianMode','cfgKandianHpOrder','cfgBaiduSecret'].forEach(id=>{ const el=$(id); if(el) el.addEventListener('input', stashActive); });
// select 的取值变化在部分内核下不触发 input，必须补绑 change，否则下拉选择会丢失（det_mode 曾因此一直停在 auto）
['cfgKandianMode','cfgKandianHpOrder','cfgBaiduModel'].forEach(id=>{ const el=$(id); if(el) el.addEventListener('change', stashActive); });
['cfgBaiduFallback'].forEach(id=>{ const el=$(id); if(el) el.addEventListener('change', stashActive); });
document.querySelectorAll('input[name="cfgProvider"]').forEach(r=>{ r.addEventListener('change',()=>{ if(r.checked){ stashActive(); ocrProvider=r.value; fillActiveProviderInputs(); } }); });
function collectCfg(){
  stashActive();
  const d = { BOX_OCR_PROVIDER: ocrProvider };
  for(const p of ['qwen','doubao','kandian','baidu','other']){ const pre=p.toUpperCase(); const s=ocrStash[p]||{};
    d[pre+'_API_KEY']=s.api_key||''; d[pre+'_BASE_URL']=s.base_url||''; d[pre+'_MODEL']=s.model||''; }
  d.KANDIAN_EMAIL=(ocrStash.kandian&&ocrStash.kandian.email)||'';
  d.KANDIAN_DET_MODE=(ocrStash.kandian&&ocrStash.kandian.det_mode)||'auto';
  d.KANDIAN_HP_ORDER=(ocrStash.kandian&&ocrStash.kandian.hp_order)||'right2left';
  d.BAIDU_SECRET_KEY=(ocrStash.baidu&&ocrStash.baidu.secret)||'';
  d.BAIDU_AUTO_FALLBACK=(ocrStash.baidu&&ocrStash.baidu.fallback)||'true';
  d.DEEPSEEK_API_KEY=$('cfgDsKey').value.trim(); d.DEEPSEEK_BASE_URL=$('cfgDsUrl').value.trim(); d.DEEPSEEK_MODEL=$('cfgDsModel').value.trim();
  const cfSel=$('cfgCitationFormat'); if(cfSel) d.CITATION_FORMAT=cfSel.value||'gb7714';
  const rp=$('cfgRenamePolicy'); if(rp) d.RENAME_POLICY=rp.value||'auto';
  const kt=$('cfgKeepTraditional'); if(kt) d.KEEP_TRADITIONAL=kt.checked?'true':'false';
  d.EYE_CARE = document.body.classList.contains('eye-care')?'true':'false';
  const dd=$('cfgDataDir'); if(dd) d.DATA_DIR=dd.value.trim();
  return d;
}
function saveCfgToBackend(clear){ const data=collectCfg(); if(clear)for(const k in data)data[k]='';
  const hint=$('saveHint'); hint.textContent='保存中…'; hint.style.color='var(--mut)';
  fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(r=>r.json()).then(j=>{
    if(j.ok){ hint.textContent='已保存 ✓'; hint.style.color='var(--ok)'; log('[配置] 已写入：'+(j.path||'未知路径')); return fetch('/api/config').then(r=>r.json()).then(c=>{ backendCfg=c; updateCfgLine(); }); } else { hint.textContent='保存失败：'+(j.error||''); hint.style.color='var(--err)'; log('[配置] 保存失败：'+(j.error||'')+' @ '+(j.path||'')); } }).catch(e=>{ hint.textContent='保存失败：'+e.message; hint.style.color='var(--err)'; }); }
$('saveCfg').onclick=()=>saveCfgToBackend(false);
$('clearCfg').onclick=()=>{ if(confirm('确认清空所有配置？'))saveCfgToBackend(true); };
$('pickDataDir').onclick=async()=>{ try{ const j=await fetch('/api/pick_dir').then(r=>r.json()); if(j.ok && j.path){ $('cfgDataDir').value=j.path; } else if(!j.ok){ alert('无法打开文件夹选择框：'+(j.error||'')); } }catch(e){ alert('无法打开文件夹选择框：'+e.message); } };

// ---------- 结果编辑框右键菜单 ----------
let ctxTarget=null;
function setupCtxMenu(){
  const menu=$('ctxMenu'); if(!menu) return;
  menu.innerHTML=`<button data-cmd="cut">剪切</button><button data-cmd="copy">复制</button><button data-cmd="paste">粘贴</button><hr><button data-cmd="selectAll">全选</button>`;
  const hide=()=>{ menu.style.display='none'; ctxTarget=null; };
  menu.querySelectorAll('button').forEach(btn=>{ btn.onclick=(e)=>{ e.stopPropagation(); const cmd=btn.dataset.cmd; const ta=ctxTarget; if(!ta){ hide(); return; }
      try{ if(cmd==='cut'){ document.execCommand('cut'); } else if(cmd==='copy'){ document.execCommand('copy'); } else if(cmd==='paste'){ doPaste(ta); } else if(cmd==='selectAll'){ ta.select(); } }catch(err){}
      hide(); }; });
  document.addEventListener('click',hide);
  document.addEventListener('scroll',hide,true);
  window.addEventListener('blur',hide);
  const resultList=$('resultList');
  // 校对文本框字号：Ctrl+滚轮缩放（10–28px，普通滚轮不受影响），记忆到 localStorage。
  // 屏幕较小时正文 12px 偏小，用户习惯与 txt 编辑器一致的 Ctrl+滚轮缩放手势。
  (function(){
    let fs=parseInt(localStorage.getItem('mh_result_fs')||'12',10);
    if(!(fs>=10&&fs<=28)) fs=12;
    const apply=()=>document.documentElement.style.setProperty('--rs-fs', fs+'px');
    apply();
    const el=document.getElementById('resultList');
    if(!el) return;
    el.addEventListener('wheel', e=>{
      if(!e.ctrlKey) return;            // 非 Ctrl 滚轮保持原有竖向滚动
      e.preventDefault();
      const next=Math.min(28, Math.max(10, fs + (e.deltaY<0?1:-1)));
      if(next===fs) return;
      fs=next; apply();
      try{ localStorage.setItem('mh_result_fs', String(fs)); }catch(_e){}
    }, {passive:false});
  })();
  if(resultList){ resultList.addEventListener('contextmenu',e=>{ const ta=e.target.closest('textarea'); if(!ta) return; e.preventDefault(); ctxTarget=ta;
      menu.style.display='block'; const x=e.clientX, y=e.clientY; const w=menu.offsetWidth||110, h=menu.offsetHeight||120; const ww=window.innerWidth, wh=window.innerHeight;
      menu.style.left=Math.min(x, Math.max(8, ww-w-8))+'px'; menu.style.top=Math.min(y, Math.max(8, wh-h-8))+'px'; }); }
}
async function doPaste(ta){
  try{ const text=await navigator.clipboard.readText(); const start=ta.selectionStart, end=ta.selectionEnd; const before=ta.value.substring(0,start), after=ta.value.substring(end);
    ta.value=before+text+after; const pos=start+text.length; ta.selectionStart=ta.selectionEnd=pos; ta.dispatchEvent(new Event('input',{bubbles:true})); }
  catch(e){ try{ document.execCommand('paste'); }catch(_){ log('[粘贴] 当前环境不支持自动读取剪贴板，请用 Ctrl+V'); } }
}
setupCtxMenu();

resizeCanvas(); draw();
</script>
</body>
</html>"""


# ---------- pywebview 窗体（复用 modern 的骨架） ----------
ICON = os.path.join(os.path.dirname(HERE), "icon", "newspaper.ico")

_cfg_lock = threading.Lock()
_extract_lock = threading.Lock()   # 抽图并归档跨请求互斥：防止用户误以为卡顿而重复点击触发并发抽图
_extract_busy = False


def _port_listening(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as e:
        return False, e


_singleton_lock = None  # 跨进程锁文件句柄，进程存活期间持有，进程退出即由 OS 自动释放


def _show_info_box(title, msg):
    """无控制台（打包 exe / .app）环境下，给非技术用户一个可见的提示弹窗。"""
    try:
        if sys.platform == "darwin":
            # macOS：pywebview 窗体可能尚未就绪，用系统对话框最稳
            _t = str(title).replace('"', '\\"')
            _m = str(msg).replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            'display dialog "%s" with title "%s" buttons {"好"} default button 1' % (_m, _t)],
                           check=False)
            return
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(msg), str(title), 0x40)  # MB_ICONINFORMATION
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


def _acquire_lock_file(port):
    """用临时目录下的锁文件做跨进程互斥，确保单实例。
    返回 True 表示本进程成功拿到锁（即为唯一实例）；False 表示已被其他实例持有。"""
    global _singleton_lock
    try:
        import tempfile, msvcrt
    except Exception:
        return True  # 非 Windows / 拿不到 msvcrt，退化为端口检查
    try:
        lock_path = os.path.join(tempfile.gettempdir(), "manual_box_ocr_%d.lock" % int(port))
        fh = open(lock_path, "a+")
    except OSError:
        return True  # 极端情况无法建锁文件，退化为端口检查
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _singleton_lock = fh  # 持有句柄（不关闭），进程退出时由 OS 自动释放锁
        return True
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return False


def _enforce_single_instance(port):
    # 1) 端口已在服务 → 必然有实例在运行，直接提示并退出（不再静默等待 bind 失败）
    ok, _ = _port_listening("127.0.0.1", port, timeout=0.2)
    if ok:
        _show_info_box("墨痕",
                       "程序已经在运行了。\n\n请回到已经打开的窗口继续使用，不要重复双击。\n（如需重新打开，请先关闭旧窗口。）")
        os._exit(0)
    # 2) 文件锁兜底，防止“同时双击”的启动竞态
    if not _acquire_lock_file(port):
        _show_info_box("墨痕",
                       "程序已经在运行了。\n\n请回到已经打开的窗口继续使用，不要重复双击。\n（如需重新打开，请先关闭旧窗口。）")
        os._exit(0)


def _set_window_icon(title):
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        HWND, HICON, UINT = wintypes.HWND, wintypes.HICON, wintypes.UINT
        WM_SETICON, ICON_BIG = 0x0080, 1
        if not os.path.exists(ICON):
            return
        hico = user32.LoadImageW(None, ICON, 1, 0, 0, 0x10 | 0x40)
        if not hico:
            return
        my_pid = kernel32.GetCurrentProcessId()
        hits = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, ctypes.wintypes.LPARAM)
        def cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == my_pid:
                hits.append(hwnd)
            return True
        for _ in range(10):
            hits.clear()
            user32.EnumWindows(EnumProc(cb), 0)
            if hits:
                break
            time.sleep(0.3)
        for hwnd in hits:
            user32.SendMessageW(HWND(hwnd), WM_SETICON, ICON_BIG, hico)
    except Exception as e:
        print("设置窗口图标失败（不影响使用）:", e)


def main():
    # 冻结态没有独立 python 解释器，子进程只能用主 exe 当解释器。
    # 用 --run-script <path> [args...] 进入「脚本执行模式」：直接 exec 目标脚本并退出，
    # 不启动 webview（避免重开一个 exe 进程）。
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        script_path = sys.argv[2]
        sys.argv = [script_path] + sys.argv[3:]
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                code = f.read()
            exec(compile(code, script_path, "exec"), {"__name__": "__main__", "__file__": script_path})
        except Exception as e:
            import traceback
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    port = 8788
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                pass
    _ensure_blank_config()
    _migrate_cfg()
    _enforce_single_instance(port)
    server = None
    for _ in range(10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(0.3)
    if server is None:
        _show_info_box("墨痕",
                       "端口 %d 仍被占用，启动失败。\n\n请先关闭已打开的程序窗口，再重新双击。" % int(port))
        os._exit(1)
    server.daemon_threads = True

    url = f"http://127.0.0.1:{port}/"

    def stop_all():
        try:
            server.shutdown(); server.server_close()
        finally:
            os._exit(0)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    _t0 = time.time()
    while time.time() - _t0 < 10.0:
        ok, _ = _port_listening("127.0.0.1", port, timeout=0.3)
        if ok:
            break
        time.sleep(0.2)
    else:
        print(f"启动失败：HTTP 端口 {port} 在 10s 内未就绪")
        os._exit(1)

    # 默认还原位置：屏幕右侧、垂直居中（Windows 用 GetSystemMetrics，失败则随系统默认）
    win_x = win_y = None
    if sys.platform == "win32":
        try:
            _sw = ctypes.windll.user32.GetSystemMetrics(0)
            _sh = ctypes.windll.user32.GetSystemMetrics(1)
            _margin_right = 40
            win_x = max(0, _sw - 480 - _margin_right)
            win_y = max(0, (_sh - 760) // 2)
        except Exception:
            pass

    try:
        win = webview.create_window(
            "墨痕 · 近代报刊转录助手",
            url,
            width=480, height=760,
            x=win_x, y=win_y,
            min_size=(360, 220),
            background_color="#f5f6f8",
            text_select=True,
        )
    except Exception as e:
        print(f"pywebview 窗口创建失败：{e}")
        os._exit(1)
    global _MAIN_WINDOW
    _MAIN_WINDOW = win
    win.events.closed += lambda: stop_all()
    print(f"墨痕 · 近代报刊转录助手 已启动（内嵌窗口，端口 {port}；关闭窗口即退出）")
    def _on_app_ready():
        _set_window_icon("墨痕 · 近代报刊转录助手")
        try:
            win.maximize()
        except Exception:
            pass
        threading.Thread(target=_watch_topmost, args=(win,), daemon=True).start()
    try:
        webview.start(_on_app_ready)
    except Exception as _e:
        # pythonnet/.NET 的实际初始化发生在 start() 内部（guilib → winforms → import clr），
        # 不在 import webview 时。这里是该错误真正的抛出点，必须兜住并给出可操作提示。
        _startup_fail(f"webview.start 失败：{type(_e).__name__}: {_e}")


def _decl_topmost_api():
    """一次性声明 ctypes 参数/返回类型。
    关键：64 位 Python 下 ctypes 默认把句柄当 32 位 c_int 传递，会导致窗口句柄高位被截断，
    FindWindowW 返回的句柄值错误、SetWindowPos 拿到无效句柄而静默失败——正是小窗置顶一直无效的元凶。"""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.FindWindowW.restype = ctypes.c_void_p
        u.IsZoomed.argtypes = [ctypes.c_void_p]
        u.IsZoomed.restype = ctypes.c_bool
        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.IsWindow.restype = ctypes.c_bool
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = ctypes.c_bool
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
    except Exception:
        pass


def _resolve_hwnd(title):
    """在所有顶层窗口中按标题定位墨痕主窗口句柄，失败返回 None（下一轮重试）。"""
    try:
        user32 = ctypes.windll.user32
        # 1) 精确匹配窗口标题
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return int(hwnd)
        # 2) 子串兜底（标题可能带后缀，或句柄尚未就绪）
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum(h, _lp):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if title[:2] in buf.value:
                found.append(int(h))
            return True
        user32.EnumWindows(WNDENUMPROC(_enum), 0)
        if found:
            return found[0]
    except Exception:
        pass
    return None


def _watch_topmost(win):
    """根据窗口状态动态调整置顶：全屏/最大化时不置顶（可自由切换其他软件），
    还原成小窗时自动置顶（侧边栏常驻不被遮挡）。纯 Windows API，跨平台安全降级。"""
    _decl_topmost_api()
    user32 = ctypes.windll.user32
    SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    last = None
    while True:
        try:
            hwnd = _resolve_hwnd("墨痕 · 近代报刊转录助手")
            if not hwnd:
                time.sleep(0.5)
                continue
            zoomed = user32.IsZoomed(hwnd)
            # 全屏(最大化) -> 不置顶；小窗 -> 置顶
            want_top = 0 if zoomed else 1
            if want_top != last:
                user32.SetWindowPos(hwnd, HWND_TOPMOST if want_top else HWND_NOTOPMOST,
                                    0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                last = want_top
        except Exception:
            pass
        time.sleep(0.5)


if __name__ == "__main__":
    main()
