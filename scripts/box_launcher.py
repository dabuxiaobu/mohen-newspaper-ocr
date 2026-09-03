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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 配置落盘目录：冻结（exe）时写到 exe 所在目录，否则写到脚本同目录。
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
APP_DIR = _app_dir()
CONFIG_DIR = APP_DIR   # 配置（API Key 等）仍随 exe 目录，更新靠「覆盖解压」保留，行为不变

# 用户产物目录：脱离 exe 所在目录，固定落到「文档/墨痕数据」，与版本无关。
# 无论新版本解压到哪、几个版本并存，识别产物 output/ 都自动共享、不会随旧版本丢失。
# 首次启动会把 exe 旁遗留的旧 output/source/cropped_hi/ 及日志迁移到此处并清空原目录。
def _user_data_dir():
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

try:
    import webview  # type: ignore
except ImportError as _e:
    _msg = ("缺少 pywebview（内嵌窗口依赖）。请先安装：\n"
            "  pip install pywebview\n"
            f"\n原始错误：{_e}")
    print(_msg)
    try:
        sys.stdout = open(os.path.join(DATA_DIR, "box_launcher.log"), "a", encoding="utf-8")
        sys.stderr = sys.stdout
        print(_msg)
    except Exception:
        pass
    os._exit(1)

VERSION = "1.1.3"

# ---------- OCR 服务商（千问 / 豆包 自由切换） ----------
# 每个服务商独立保存一组凭据（API Key / Base URL / 模型名），切换后各自记住，
# 互不影响。BOX_OCR_PROVIDER 记录当前用哪一个。旧版仅有的 BOX_OCR_* 字段会在
# 首次启动时迁移进 QWEN_*（见 _migrate_cfg）。
OCR_PROVIDERS = {
    "qwen": {
        "label": "千问（日常）",
        "key_prefix": "QWEN",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.6-plus",
        "base_placeholder": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_placeholder": "sk-...",
        "model_placeholder": "",
    },
    "doubao": {
        "label": "豆包（疑难）",
        "key_prefix": "DOUBAO",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
        "base_placeholder": "https://ark.cn-beijing.volces.com/api/v3",
        "key_placeholder": "ark-",
        "model_placeholder": "",
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

def _query_latest_release(repo, provider):
    """查询 Gitee/GitHub 最新 release，返回 dict 或 None。"""
    import urllib.request, urllib.error, ssl, json as _json_mod
    if provider == "gitee":
        api = f"https://gitee.com/api/v5/repos/{repo}/releases/latest"
    else:
        api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "mohen-updater")
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = _json_mod.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or data.get("name") or ""
    notes = data.get("body") or ""
    page_url = data.get("html_url") or ""
    if provider == "gitee":
        page_url = f"https://gitee.com/{repo}/releases"
    assets = []
    for a in (data.get("assets") or []):
        assets.append({"name": a.get("name", ""),
                       "url": a.get("browser_download_url") or a.get("url") or ""})
    return {"tag": tag, "notes": notes, "assets": assets, "page_url": page_url}

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

def _build_update_result(current, rel, provider):
    is_mac = (sys.platform == "darwin")
    win_asset = None
    mac_asset = None
    fallback_zip = None
    for a in rel["assets"]:
        n = a["name"].lower()
        if not n.endswith(".zip"):
            continue
        if fallback_zip is None:
            fallback_zip = a
        if is_mac:
            # macOS：挑含 macos 的 zip；优先匹配当前架构（arm64/x86_64）
            if "macos" in n:
                arch = _mac_arch_keyword()
                if arch is None or arch in n:
                    mac_asset = a
                    break
        else:
            if any(k in n for k in WIN_PKG_KEYWORDS):
                win_asset = a
                break
    target = mac_asset or win_asset or fallback_zip
    # sha256 校验文件：优先「与选中包同名 + .sha256.txt」（本工具发布物的命名方式），
    # 其次兼容通用名 sha256.txt / checksums.txt 等。
    sha_asset = None
    if target:
        want = target["name"] + ".sha256.txt"
        for a in rel["assets"]:
            if a["name"] == want:
                sha_asset = a
                break
    if sha_asset is None:
        for a in rel["assets"]:
            if a["name"].lower() in ("sha256.txt", "sha256sums.txt", "checksums.txt", "checksums.sha256"):
                sha_asset = a
    need = bool(target) and bool(rel["tag"]) and rel["tag"] != current
    return {
        "ok": True, "current": current, "latest": rel["tag"],
        "notes": rel["notes"], "page_url": rel["page_url"],
        "download_url": target["url"] if target else "",
        "asset_name": target["name"] if target else "",
        "sha256_url": sha_asset["url"] if sha_asset else "",
        "need": need,
        "platform": "macos" if is_mac else "windows",
    }

def _check_update():
    current = VERSION
    if GITEE_REPO:
        r = _query_latest_release(GITEE_REPO, "gitee")
        if r and r["tag"]:
            return _build_update_result(current, r, "gitee")
    r = _query_latest_release(GITHUB_REPO, "github")
    if r and r["tag"]:
        return _build_update_result(current, r, "github")
    return {"ok": False, "error": "无法获取更新信息（Gitee/GitHub 均失败）"}

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
2. 严格忠实原文：不增删、不臆造、不擅自改写文意、不把旧用法改成现代用法。对模糊或疑似错字，按你的最佳判断直接给出你认定的字，不要加任何"疑为…""照录""（注：…）"之类的注释或说明；实在无法辨认的字才以 □ 占位。
3. 异体字、俗字、旧字形、缺笔字按你的最佳判断直接转录，不擅自替换时也不加任何注记。
4. 保留标题、副题、正文、图注、页码、书眉、版权页信息之间的层级与分隔；同一视觉块内的内容归为一段。
5. 若区域含多篇文章/多栏接续，按视觉分块分别输出并明确标注边界，不要擅自拼接为一段。
6. 不输出与转录无关的说明、寒暄或"以下是转录结果"之类前缀；直接给内容。"""

SINGLE_INSTRUCTION = """这是民国竖排报纸中的一篇文章（出处：{src}）。
请你完成该篇文章的转录：
1. 图中文字多为竖排繁体，按「从右到左逐列、每列从上到下」的顺序读取；若遇横排（左起或右起）按实际阅读方向读，严禁按图像像素行机械拼接。
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
5. **不要对任何字词加注释或疑似说明**（如"疑为…""照录""（注：…）""（原文…）"等）。遇到模糊或疑似错字，凭你的最佳判断直接给出你认定的字；实在无法辨认才以 □ 占位。
6. 若本图是该文的接续页（跨页续文，正文起首即承接上一版末句），则把**接续处上行尾与下行首连成一句完整输出**，不要在版/栏衔接处强行插入分段或空行；跨页的最终合并由系统完成，单页转录只需保证本页文字连续、不擅自断句。仅当确属新段落（上一版末已是完整句且有句号等收尾）时，才在段首正常起段。

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


def _load_cfg():
    cfg = {}
    explicit = set()
    p = os.path.join(CONFIG_DIR, "box_config.json")
    _CFG_KEYS = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                 "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                 "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                 "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                 "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN")
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
    # 兼容旧版：仅有 BOX_OCR_* 时未设置服务商，按千问处理
    cfg.setdefault("BOX_OCR_PROVIDER", "qwen")
    return cfg, explicit


def _ensure_blank_config():
    p = os.path.join(CONFIG_DIR, "box_config.json")
    if os.path.exists(p):
        return
    blank = {k: "" for k in ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                             "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                             "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                             "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                             "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN")}
    blank["BOX_OCR_PROVIDER"] = "qwen"
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(blank, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _cfg_status(cfg, explicit):
    keys = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
            "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
            "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
            "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
            "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN")
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
        data["BOX_OCR_PROVIDER"] = "qwen"
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


def do_ocr(b64, src, prompt_override=None, overrides=None, provider=None):
    import urllib.request
    import urllib.error
    import ssl
    cfg, _ = _load_cfg()
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    ov = overrides or {}
    # provider 身份优先跟随前端当前选中（overrides.provider），
    # 仅在未传时回退 BOX_OCR_PROVIDER，确保切换即时生效、不静默回退千问
    provider = ov.get("provider") or provider or cfg.get("BOX_OCR_PROVIDER", "qwen")
    if provider not in OCR_PROVIDERS:
        provider = "qwen"
    ppre = OCR_PROVIDERS[provider]["key_prefix"]
    pdef = OCR_PROVIDERS[provider]
    api_key = ov.get("api_key") or cfg.get(ppre + "_API_KEY", "")
    if not api_key:
        return ("【未配置 %s 的 API Key】当前 OCR 服务商为「%s」，但其 API Key 未填写。\n"
                "请在设置中选择该服务商、填入 %s 并保存，再执行识别。\n"
                "标题：\n作者：\n正文：（未配置密钥，未调用真实模型）"
                % (ppre, pdef["label"], ppre + "_API_KEY"))
    base_url = ov.get("base_url") or cfg.get(ppre + "_BASE_URL", "") or pdef["default_base_url"]
    model = ov.get("model") or cfg.get(ppre + "_MODEL", "") or pdef["default_model"]
    user_text = _safe_format(prompt_override or SINGLE_INSTRUCTION, src=src)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
    }
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
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        text = j["choices"][0]["message"]["content"]
        usage = j.get("usage") or {}
        duration = round(time.time() - t0, 3)
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
        }}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}")
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
                if base not in bases:
                    continue
                # 只收拢该轮的原图/ocr txt/ocr json（排除已结构化的 _题录.md 与 结构化_*.txt）
                if fn.endswith("_题录.md") or fn.startswith("结构化_"):
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
            provider = cfg.get("BOX_OCR_PROVIDER", "qwen")
            if provider not in OCR_PROVIDERS:
                provider = "qwen"
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
                "has_ds_key": bool(cfg.get("DEEPSEEK_API_KEY")),
                "config": _cfg_status(cfg, explicit),
                # 明文回填用（仅本地窗体内返回，不外传）：供前端启动填入输入框
                "values": {k: cfg.get(k, "") for k in
                           ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                            "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                            "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                            "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                            "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN")},
                # 提示词出厂默认（抽屉「恢复默认」回填；自定义留空则回落到此）
                "prompt_ocr_default": SINGLE_INSTRUCTION,
                "prompt_post_default": _read_post_default_prompt(),
                "prompt_post_plain_default": _read_post_default_prompt_plain(),
            })
        if u.path == "/api/list_images":
            qs = parse_qs(u.query)
            sub = qs.get("dir", ["cropped_hi"])[0]
            d = _img_dir(sub)
            if not os.path.isdir(d):
                return self._json({"dir": sub, "files": []})
            fs = sorted(f for f in os.listdir(d)
                        if os.path.splitext(f)[1].lower() in IMG_EXTS)
            return self._json({"dir": sub, "files": fs})
        if u.path == "/api/list_source":
            return self._list_source()
        if u.path == "/api/image":
            qs = parse_qs(u.query)
            name = qs.get("name", [""])[0]
            sub = qs.get("dir", ["cropped_hi"])[0]
            fp = os.path.join(_img_dir(sub), name)
            if not os.path.exists(fp):
                # 前端 pname 已去扩展名，自动补 .png 兜底
                cand = fp + ".png"
                if os.path.exists(cand):
                    fp = cand
                else:
                    return self._send(404, "not found")
            ext = os.path.splitext(fp)[1].lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
                    ".tif": "image/tiff", ".tiff": "image/tiff"}.get(ext, "image/png")
            b = base64.b64encode(open(fp, "rb").read()).decode()
            return self._json({"name": name, "data": f"data:{mime};base64,{b}"})
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
        return self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
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
        if u.path == "/api/ocr":
            try:
                # 前端未显式传 prompt_override 时，自动用配置里的 PROMPT_OCR 覆盖（提示词抽屉保存后即时生效）
                cfg_ocr, _ = _load_cfg()
                ov_prompt = data.get("prompt_override") or cfg_ocr.get("PROMPT_OCR") or None
                res = do_ocr(data.get("image_b64", ""), data.get("src", ""),
                             ov_prompt, data.get("overrides") or {})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            if isinstance(res, dict):
                return self._json({"ok": True, "text": res.get("text", ""), "usage": res.get("usage", {})})
            return self._json({"ok": True, "text": res, "usage": {}})
        if u.path == "/api/export":
            return self._export(data)
        if u.path == "/api/config/save":
            return self._save_cfg(data)
        if u.path in ("/api/extract", "/api/postprocess", "/api/group"):
            return self._run_script(u.path.split("/")[-1], data)
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
        if u.path == "/api/open_url":
            return self._open_url(data)
        if u.path == "/api/update_download":
            return self._update_download(data)
        if u.path == "/api/update_apply":
            return self._update_apply()
        return self._send(404, "not found")

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
        # 聚合导出：同一整版/跨页只产一个总 txt（所有框按序拼接）+ 一个总 json（框数组）。
        # 单页平铺在 output/ 根；跨页合并归集到 output/{out_dir}/，避免与按标题子目录混杂。
        safe = src
        txt_path = os.path.join(out_root, safe + ".txt")
        lines = [f"出处：{src}", ""]
        for i, b in enumerate(boxes, 1):
            lines.append(f"【框{i}】{(b.get('text') or '').strip()}")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        written = [safe + ".txt"]
        return self._json({"ok": True, "written": written, "out_root": out_root})

    def _export_json(self, data):
        # 自动导出 JSON（还原 X-AnyLabeling 习惯：框坐标 + 识别结果服务端落盘，无需手动下载）
        # 聚合导出：同一整版/跨页只产一个总 json（框数组）。单页平铺 output/ 根；
        # 跨页合并归集到 output/{out_dir}/，避免与按标题子目录混杂。
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
        arr = []
        for i, b in enumerate(boxes, 1):
            arr.append({"source": src, "mode": mode, "order": i,
                        "label": b.get("label", ""), "group": b.get("group", ""),
                        "box": b.get("box"), "text": b.get("text", "")})
        json_path = os.path.join(out_root, src + ".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
        return self._json({"ok": True, "written": [src + ".json"], "out_root": out_root, "path": json_path})

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
        # 共享 used 字典以处理 .md 与 .txt 间的同名冲突
        def _copy_flat(files, dst, used):
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
                used[name] = True
                shutil.copy2(s, os.path.join(dst, name))

        used = {}
        if kb_count > 0:
            _copy_flat(kb_files, exp_root, used)
        if plain_count > 0:
            _copy_flat(plain_files, exp_root, used)

        return self._json({
            "ok": True,
            "dir": exp_root,
            "rel": os.path.relpath(exp_root, _img_dir("")),
            "kb_count": kb_count,
            "plain_count": plain_count,
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
        return self._json({"ok": True, "written": written, "skipped": skipped,
                           "source_dir": src_dir, "count": len(written)})

    def _list_source(self):
        d = _img_dir("source")
        if not os.path.isdir(d):
            return self._json({"files": []})
        fs = sorted(os.listdir(d))
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
        for name in files:
            name = (name or "").strip()
            if not name:
                continue
            # pageOrder 里存的是去扩展名的 basename，而 cropped_hi/ 实际文件带扩展名；
            # 先按原名试删，失败且原名无扩展名时，补 .png/.jpg/.jpeg 候选再试。
            fp = _safe_delete(name, "cropped_hi")
            if not fp:
                base, ext = os.path.splitext(name)
                if not ext:
                    for e in (".png", ".jpg", ".jpeg"):
                        fp = _safe_delete(name + e, "cropped_hi")
                        if fp:
                            break
            if fp:
                removed.append(os.path.basename(fp))
        return self._json({"ok": True, "removed": removed, "count": len(removed)})

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
            # 1) 下载更新包
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "mohen-updater")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            # 2) 校验 SHA256（若 release 附带 checksum 文件）
            if sha_url:
                try:
                    sreq = urllib.request.Request(sha_url, method="GET")
                    sreq.add_header("User-Agent", "mohen-updater")
                    with urllib.request.urlopen(sreq, timeout=30, context=ctx) as sresp:
                        sha_text = sresp.read().decode("utf-8", "ignore")
                    expected = _parse_sha256(sha_text, os.path.basename(url))
                    if expected:
                        actual = _sha256_of_file(zip_path)
                        if actual.lower() != expected.lower():
                            return self._json({"ok": False, "error": "SHA256 校验失败，下载可能被篡改"})
                except Exception as _se:
                    sys.stderr.write(f"[update] sha 校验跳过：{_se}\n")
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
                # 若新包误带配置则移除，再从当前运行实例回写（保留 API Key 等）
                _cfg_pkg = os.path.join(new_app, "Contents", "MacOS", "box_config.json")
                if os.path.exists(_cfg_pkg):
                    os.remove(_cfg_pkg)
                _old_cfg = os.path.join(APP_DIR, "box_config.json")
                if os.path.exists(_old_cfg):
                    try:
                        shutil.copyfile(_old_cfg, os.path.join(new_app, "Contents", "MacOS", "box_config.json"))
                    except Exception:
                        pass
                # .app 路径：APP_DIR 在 macOS = <app>/Contents/MacOS
                app_bundle = os.path.dirname(os.path.dirname(APP_DIR))
                sh_path = os.path.join(work, "apply.sh")
                sh_content = (
                    "#!/bin/sh\n"
                    "sleep 3\n"
                    'APP_BUNDLE="$1"\n'
                    'NEW_APP="$2"\n'
                    'OLD_BAK="${APP_BUNDLE}.old"\n'
                    'pkill -f "$(basename "$APP_BUNDLE")" 2>/dev/null || true\n'
                    'if [ -e "$APP_BUNDLE" ]; then\n'
                    '  mv "$APP_BUNDLE" "$OLD_BAK" 2>/dev/null || rm -rf "$APP_BUNDLE" 2>/dev/null || true\n'
                    "fi\n"
                    'cp -R "$NEW_APP" "$APP_BUNDLE" 2>/dev/null\n'
                    'xattr -dr com.apple.quarantine "$APP_BUNDLE" 2>/dev/null || true\n'
                    'open "$APP_BUNDLE" 2>/dev/null || true\n'
                    'rm -rf "$OLD_BAK" 2>/dev/null || true\n'
                )
                with open(sh_path, "w", encoding="utf-8") as _f:
                    _f.write(sh_content)
                try:
                    os.chmod(sh_path, 0o755)
                except Exception:
                    pass
                _UPDATE_STATE = {"script": sh_path, "app_bundle": app_bundle, "new_app": new_app}
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
        if not st:
            return self._json({"ok": False, "error": "未找到已下载的更新包，请先点「立即更新」"})
        # ===== macOS：/bin/sh 启动 apply.sh 替换 .app 后退出 =====
        if sys.platform == "darwin":
            if not os.path.exists(st.get("script", "")):
                return self._json({"ok": False, "error": "未找到已下载的更新包，请先点「立即更新」"})
            try:
                subprocess.Popen(
                    ["/bin/sh", st["script"], st["app_bundle"], st["new_app"]],
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

    def _run_script(self, name, data):
        cfg, _ = _load_cfg()
        script = os.path.join(HERE, {"extract": "extract_original.py",
                                     "postprocess": "postprocess.py",
                                     "group": "group_articles.py"}[name])
        env = dict(os.environ)
        for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"):
            if cfg.get(k):
                env[k] = cfg[k]
        env.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
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
            work = os.path.join(_img_dir("output"), top, round_dir)
            # 把该轮 OCR 产物（整版原图 + .txt + .json）从 output/ 根 / 旧跨页目录收拢进 work
            moved, skipped = _gather_round(_img_dir("output"), work, pages, data.get("out_dir") or "")
            if not moved and not skipped:
                # 没有任何可结构化产物，不创建空目录、不调用 postprocess.py
                return self._json({"ok": True, "stdout": "该工作集没有可结构化的 OCR 产物（未生成 .txt / .json / 原图），未创建输出文件夹。", "skipped": True})
            # 重新结构化（如隔天重识别同一批图）：先清掉 work 里上一轮已生成的产品
            # （_题录.md / 结构化_*.txt）。否则 postprocess.py 检测到产物已存在会 skip，
            # 导致重识别后的新 OCR 无法重新生成题录，产物内容停留在旧版——one-click 导出
            # 复制到的仍是旧内容。清理后 postprocess 必重新调用模型生成最新产物。
            if os.path.isdir(work):
                for _fn in os.listdir(work):
                    if _fn.endswith("_题录.md") or _fn.startswith("结构化_"):
                        try:
                            os.remove(os.path.join(work, _fn))
                        except OSError:
                            pass
            extra = ["--root", work, "--post-mode", pmode]   # 串联断点修复：指向本工具 OCR 产物
            # 提示词抽屉保存的覆盖：kb 模式用 PROMPT_POST，plain 模式用 PROMPT_POST_PLAIN
            pp = (cfg.get("PROMPT_POST") or "").strip()
            if pp:
                extra = extra + ["--prompt-post", pp]
            ppp = (cfg.get("PROMPT_POST_PLAIN") or "").strip()
            if ppp:
                extra = extra + ["--prompt-post-plain", ppp]
        elif name == "group":
            mode = (data.get("mode") or "auto").strip() or "auto"
            extra = ["--mode", mode, "--src", _img_dir("cropped_hi"), "--dst", _img_dir("民国报纸OCR")]
        try:
            r = subprocess.run([_py(), "--run-script", script, *extra], cwd=HERE,
                               capture_output=True, text=True, env=env, timeout=900)
            res = {"ok": r.returncode == 0, "returncode": r.returncode,
                   "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}
            if name == "postprocess" and r.returncode == 0:
                # 打开文件夹策略：
                #  - no_open=True（单页逐版调用）：后端不打开，交由前端统一打开父目录 output/{top}（避免多子文件夹冲突）
                #  - open_parent=True：打开 output/{top} 父目录（多子文件夹场景）
                #  - 否则（跨页合并）：打开该轮合并子文件夹 work（维持原行为）
                if not data.get("no_open"):
                    open_target = os.path.dirname(work) if data.get("open_parent") else work
                    if _open_folder(open_target):
                        res["opened_dir"] = open_target
            return self._json(res)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)})

    def _extract_and_group(self, data):
        """抽图 + 归档：先抽 source/ -> cropped_hi/，再把整版 PNG 归档进 output/{整版名}/
        便于溯源原始图片（与 OCR 产物 {整版名}_框N/ 同处 output/ 下）。"""
        cfg, _ = _load_cfg()
        env = dict(os.environ)
        for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"):
            if cfg.get(k):
                env[k] = cfg[k]
        env.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
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
            r1 = subprocess.run([_py(), "--run-script", os.path.join(HERE, "extract_original.py"),
                                 "--src", src_dir, "--dst", _img_dir("cropped_hi")],
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
                          if f.lower().endswith(".png"))
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
            out["stdout"] += (f"\n[archive] 归档整版原图到 output/（平铺，不建子目录）：新增 {archived} 张，"
                              f"跳过已存在 {skipped} 张。\n")
            out["ok"] = True
            return self._json(out)
        except Exception as e:
            out["error"] = str(e)
            return self._json(out)

    def _save_cfg(self, data):
        allowed = ("BOX_OCR_PROVIDER", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
                   "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL",
                   "OTHER_API_KEY", "OTHER_BASE_URL", "OTHER_MODEL",
                   "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                   "PROMPT_OCR", "PROMPT_POST", "PROMPT_POST_PLAIN")
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
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family: -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; font-size:14px; }
  .wrap { display:flex; height:calc(100vh - 56px); overflow:hidden; }
  .left { flex:1 1 auto; display:flex; flex-direction:column; min-width:0; background:#eef1f5; }
  #canvasWrap { flex:1 1 auto; overflow:auto; position:relative; background:#eef1f5; }
  #zoomBar { position:sticky; top:12px; left:12px; float:left; margin:12px 0 0 12px; z-index:10;
             display:flex; align-items:center; gap:6px; background:rgba(255,255,255,0.95);
             padding:6px 10px; border-radius:8px; border:1px solid var(--bd); box-shadow:0 2px 8px rgba(0,0,0,.08); }
  #zoomBar span { font-size:12.5px; color:var(--ink); min-width:44px; text-align:center; font-weight:600; }
  #zoomBar button { min-width:auto; padding:5px 10px; }
  #pageBar { position:sticky; top:12px; float:right; margin:12px 12px 0 0; z-index:10;
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

  /* 流程提示 */
  .hint .stage { display:inline-flex; align-items:center; gap:5px; margin-right:14px;
                 white-space:nowrap; font-size:12.5px; color:var(--mut); }
  .flow-num { display:inline-flex; align-items:center; justify-content:center; width:1.5em; height:1.5em;
              border-radius:50%; background:#e8eefd; color:var(--pri); font-weight:700; font-size:11px; }
  .sub { color:var(--mut); margin:10px 0 0; font-size:12px; line-height:1.6; }
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
  #resultList .card { border:1px solid var(--bd); border-radius:6px; padding:8px; margin-bottom:8px; min-width:0; }
  #resultList .card .hd { display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:12px; color:var(--mut); margin-bottom:4px; min-width:0; }
  #resultList .card .hd .title { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #resultList .card .hd .meta { flex-shrink:0; white-space:nowrap; }
  #resultList textarea { width:100%; min-height:90px; font-family:"Microsoft YaHei",monospace; font-size:12px;
                         border:1px solid var(--bd); border-radius:4px; padding:6px; resize:vertical; }
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
  .src-list label.item { display:grid; grid-template-columns:13px 1fr; gap:6px; align-items:center; padding:3px 6px; border-radius:4px; font-size:12px; cursor:pointer; }
  .src-list label.item input { width:13px; height:13px; margin:0; }
  .src-list label.item span { word-break:break-all; line-height:1.4; }
  .src-list label.item:hover { background:#eef1f5; }
  .src-list label.item.sel { background:#eff6ff; }
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
  .key-field { display:flex; flex-direction:column; }
  .key-field .input-wrap { position:relative; }
  .key-field input { padding-right:38px !important; }
  .key-field .eye { position:absolute; right:8px; top:50%; transform:translateY(-50%);
                    cursor:pointer; user-select:none; color:var(--mut); display:flex; align-items:center; }
  .key-field .eye:hover { color:var(--pri); }
  .key-field .eye svg { display:block; }
  .key-field input::-ms-reveal { display:none; }
  .key-field input::-webkit-credentials-auto-fill-button { visibility:hidden; display:none !important; }

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
    <button class="gear" id="gearBtn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      设置
    </button>
  </div>
</div>
<div class="wrap">
  <div class="left">
      <div id="canvasWrap">
      <div id="zoomBar">
        <button id="zoomOut" class="sm sec" title="缩小">−</button>
        <span id="zoomPct">100%</span>
        <button id="zoomIn" class="sm sec" title="放大">+</button>
        <button id="zoomReset" class="sm sec" title="适应窗口">⟲</button>
      </div>
      <div id="pageBar">
        <button class="sm sec" id="prevPage" title="上一版">◀</button>
        <span id="pageNav" class="pg"><span id="pageCounter">第 0 / 0 版</span><small id="crossStatus" style="display:none">跨页 · 已载入 0 版</small></span>
        <button class="sm sec" id="nextPage" title="下一版">▶</button>
      </div>
      <canvas id="cv" width="800" height="600"></canvas>
    </div>
    <div id="hint">
      <span class="stage"><span class="flow-num">①</span>导入文件 → 抽图并归档</span>
      <span class="stage"><span class="flow-num">②</span>载入图片 → 框选</span>
      <span class="stage"><span class="flow-num">③</span>识别全部</span>
      <span class="stage"><span class="flow-num">④</span>校对 → 保存 → 结构化</span>
    </div>
  </div>

  <div class="right">
    <div class="card">
      <h2>① 抽图与归档</h2>
      <div id="sourceBox">
        <div class="src-list-wrap collapsed">
          <div class="src-list-head">
            <button class="fold-toggle" type="button" title="折叠 / 展开列表"></button>
            <label class="chk"><input type="checkbox" id="sourceSelAll"> <span>全选</span></label>
            <button class="src-head-btn gray" id="importSource" title="导入 PDF / 图片到 source/">导入文件</button>
            <input type="file" id="importSourceIn" multiple accept="image/*,.pdf" style="display:none;">
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
        <div class="src-list-wrap collapsed">
          <div class="src-list-head">
            <button class="fold-toggle" type="button" title="折叠 / 展开列表"></button>
            <label class="chk"><input type="checkbox" id="selAll"> <span>全选</span></label>
          <button class="sm ghost src-head-btn" id="loadCropped" title="将上方勾选的整版载入为工作集">载入</button>
          <button class="sm stop src-head-btn" id="delCropped" title="删除选中的整版原图（含 output 同名产物）">删除</button>
          <span class="cnt" id="imgCnt"></span>
        </div>
        <div class="src-list" id="srcList"></div>
      </div>
      <div style="margin-top:8px; color:var(--mut); font-size:12px;">当前源：<span class="srcname" id="srcName">未载入</span></div>
      <label>框选标签</label>
      <select id="lblSel">
        <option value="title">title（标题）</option>
        <option value="author">author（作者）</option>
        <option value="text" selected>text（正文）</option>
      </select>
    </div>

    <div class="card">
      <h2>③ OCR识别</h2>
      <p class="sub">组 = 一篇文章：同组（同色）框合并识别/导出为一篇，留空则自动合并为一篇，组内按“标题 → 作者 → 正文”排序。</p>
      <div style="margin-top:12px; border-top:1px solid var(--bd); padding-top:12px;">
        <h3 style="font-size:13px;font-weight:600;margin:0 0 8px;color:var(--ink);">已框选区域（按阅读顺序，序号 = 导出次序）</h3>
        <div id="boxList"><div style="color:var(--mut); font-size:12px;">尚未框选</div></div>
      </div>
      <div class="btns" style="margin-top:12px;">
        <button id="recogAll">识别全部</button>
        <button class="stop" id="clearBoxes">清空框选</button>
      </div>
    </div>

    <div class="card">
      <h2>④ 校对与结构化</h2>
      <div id="resultList"></div>
      <div id="ctxMenu" style="display:none;"></div>
      <div class="btns" style="margin-top:10px; align-items:center;">
        <button class="run" id="saveEdit">保存修改</button>
      </div>
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
      <div class="row" style="margin-bottom:8px; align-items:center; gap:10px; flex-wrap:nowrap;">
        <label class="rdo"><input type="radio" name="cfgProvider" value="qwen"> 千问（日常）</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="doubao"> 豆包（疑难）</label>
        <label class="rdo"><input type="radio" name="cfgProvider" value="other"> 其他（自定义）</label>
      </div>
      <div class="row">
        <div class="key-field"><label>OCR API Key</label>
          <div class="input-wrap">
            <input id="cfgApiKey" type="password" placeholder="ARK...">
            <span class="eye" data-target="cfgApiKey" title="显示/隐藏"></span>
          </div></div>
        <div><label>模型名</label><input id="cfgModel" placeholder=""></div>
      </div>
      <label id="cfgBaseUrlLabel">OCR Base URL（可选，默认千问AI平台）</label>
      <input id="cfgBaseUrl" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1">
    </div>
    <div class="set-group">
      <h3>后置（DeepSeek / 结构化题录）</h3>
      <div class="row">
        <div class="key-field"><label>DeepSeek API Key</label>
          <div class="input-wrap">
            <input id="cfgDsKey" type="password" placeholder="sk-...">
            <span class="eye" data-target="cfgDsKey" title="显示/隐藏"></span>
          </div></div>
        <div><label>DeepSeek 模型名</label><input id="cfgDsModel" placeholder="deepseek-v4-flash"></div>
      </div>
      <label>DeepSeek Base URL（可选，默认官方）</label>
      <input id="cfgDsUrl" placeholder="https://api.deepseek.com">
    </div>
    <div class="set-group">
      <h3>版本更新</h3>
      <div class="row" style="align-items:center; gap:12px; flex-wrap:wrap;">
        <span class="sub">当前版本：<span id="curVersion">--</span></span>
        <button class="sec" id="checkUpdateBtn" type="button">检查更新</button>
      </div>
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

<script>
const $ = id => document.getElementById(id);
const cv = $('cv'); const ctx = cv.getContext('2d');
let img = null, natW = 0, natH = 0, scale = 1, baseScale = 1, userZoom = 1;
const ZOOM_MIN = 0.2, ZOOM_MAX = 5.0;
let boxes = [];   // {id,label,x,y,w,h,group}
let results = {}; // key -> {title,author,body,raw}
let srcName = ""; let drawing = null, cur = null; let selectedId = null; let pendingSelectId = null;
let dragState = null; let uid = 1; let mode = 'auto'; let backendCfg = {};
// 多版识别状态：各整版框选/识别结果按页分别存放于 allPageData，
// pageOrder 记录工作集（= 阅读顺序，由「载入」批量填充）。
// mergeMode 决定整版间关系：'single' 每版独立识别/导出；'cross' 多版合并为一篇。
let allPageData = {}; let pageOrder = []; let pageList = []; let navList = []; let pageIdx = -1; let crossResults = {};
let mergeMode = 'single';
let off = 0;

const LBL_COLOR = { title:'#2563eb', text:'#16a34a', author:'#9333ea' };
const GROUP_COLORS = ['#dc2626','#2563eb','#16a34a','#9333ea','#d97706','#0891b2','#7c3aed','#db2777','#65a30d','#ea580c'];
function groupColor(g){ let h=0; for(let i=0;i<g.length;i++) h=(h*31+g.charCodeAt(i))%16777215; return GROUP_COLORS[h%GROUP_COLORS.length]; }
const HANDLE=9, HIT_PAD=2;

// ---------- 配置 / 状态 ----------
let CURRENT_VERSION = '';
const RELEASE_PAGE_URL = 'https://github.com/dabuxiaobu/mohen-newspaper-ocr/releases';
fetch('/api/config').then(r=>r.json()).then(c=>{ backendCfg=c; window.__RUNTIME_DIR=c.runtime_dir||''; CURRENT_VERSION = c.version || ''; if($('curVersion')) $('curVersion').textContent = CURRENT_VERSION || '--'; fillCfgInputs(c.values||{}); updateCfgLine(); }).catch(()=>{ $('cfgLine').textContent='配置读取失败'; });
// 启动恢复历史日志（关闭 exe 不丢失）
restoreLogs();
// 启动时把已保存的密钥/模型回填进输入框，避免重开后看似"丢失"
// OCR 服务商切换：每个服务商各存一组凭据，切换时回填对应值
let ocrProvider = 'qwen';
const ocrStash = { qwen:{api_key:'',base_url:'',model:''}, doubao:{api_key:'',base_url:'',model:''}, other:{api_key:'',base_url:'',model:''} };
function applyProviderFromValues(v){
  v = v || {};
  for(const p of ['qwen','doubao','other']){ const pre=p.toUpperCase();
    ocrStash[p] = { api_key:v[pre+'_API_KEY']||'', base_url:v[pre+'_BASE_URL']||'', model:v[pre+'_MODEL']||'' }; }
  ocrProvider = (['qwen','doubao','other'].includes(v.BOX_OCR_PROVIDER)) ? v.BOX_OCR_PROVIDER : 'qwen';
  setProviderRadio(ocrProvider);
  fillActiveProviderInputs();
}
function setProviderRadio(p){ const r=document.querySelector('input[name="cfgProvider"][value="'+p+'"]'); if(r) r.checked=true; }
function fillActiveProviderInputs(){
  const s = ocrStash[ocrProvider];
  $('cfgApiKey').value = s.api_key; $('cfgBaseUrl').value = s.base_url; $('cfgModel').value = s.model;
  const def = (ocrProvider==='qwen')
    ? { key:'sk-...', base:'https://dashscope.aliyuncs.com/compatible-mode/v1', model:'' }
    : (ocrProvider==='doubao')
    ? { key:'ark-', base:'https://ark.cn-beijing.volces.com/api/v3', model:'' }
    : { key:'', base:'', model:'' };
  $('cfgApiKey').placeholder = def.key;
  $('cfgBaseUrl').placeholder = def.base;
  $('cfgModel').placeholder = def.model;
  const lbl=$('cfgBaseUrlLabel'); if(lbl) lbl.textContent = (ocrProvider==='qwen')
    ? 'OCR Base URL（可选，默认千问AI平台）'
    : (ocrProvider==='doubao')
    ? 'OCR Base URL（可选，默认火山方舟）'
    : 'OCR Base URL';
}
function stashActive(){
  const s = ocrStash[ocrProvider];
  s.api_key=$('cfgApiKey').value.trim(); s.base_url=$('cfgBaseUrl').value.trim(); s.model=$('cfgModel').value.trim();
}
function fillCfgInputs(v){ applyProviderFromValues(v); }
function updateCfgLine(){
  const hasKey = backendCfg && backendCfg.dry_run===false;
  const model = (backendCfg&&backendCfg.model)||'未配置';
  const host = (backendCfg&&backendCfg.base_url_host)||'ark.cn-beijing.volces.com';
  if(!hasKey) $('cfgLine').innerHTML='OCR：<span class="warn">未配置 API_KEY</span> 模型 '+model+' <b>请点「设置」填写 OCR 密钥与模型名</b>';
  else $('cfgLine').innerHTML='OCR：真实调用 · 模型 <b>'+model+'</b> @ '+host;
}
function getOverrides(){ const s=ocrStash[ocrProvider]||{}; const o={provider:ocrProvider}; if(s.base_url)o.base_url=s.base_url; if(s.api_key)o.api_key=s.api_key; if(s.model)o.model=s.model; return o; }

// ---------- 加载图片（canvas drawImage 直接绘制，载入即显示，无需点击） ----------
function loadDataURL(url,name,idx){ const pname=(name||'').replace(/\.[^.]+$/,'');
  // 切走前先把当前版状态存回 allPageData（始终回写，避免翻页丢失框选/编辑）
  if(srcName && allPageData[srcName]){ allPageData[srcName].boxes=boxes; allPageData[srcName].results=results; }
  const im=new Image(); im.onerror=()=>{ log('[载入图片] 解码失败：'+(name||'')); };
  im.onload=()=>{
    img=im; natW=im.naturalWidth; natH=im.naturalHeight;
    srcName=pname; const _sn=$('srcName'); if(_sn) _sn.textContent=srcName||'未命名';
    // 懒初始化该版存储；pageOrder 仅由「载入所选」批量填充，翻页（loadDataURL）绝不改动
    if(!allPageData[srcName]) allPageData[srcName]={boxes:[],results:{}};
    boxes=allPageData[srcName].boxes; results=allPageData[srcName].results;   // 引用，编辑即持久化
    selectedId=null; renderBoxList(); renderResults(); resizeCanvas();   // 同步 resize + draw：随本任务渲染上屏，无需点击
    if(pendingSelectId!=null){ selectedId=pendingSelectId; pendingSelectId=null; renderBoxList(); draw(); }
    // 页码优先用调用方传入的 idx（gotoPage 已同步设过，最可靠），反推仅作兜底（均基于 navList，即当前载入的工作集）
    if(typeof idx==='number' && idx>=0 && navList[idx] && navList[idx].replace(/\.[^.]+$/,'')===srcName) pageIdx=idx;
    else pageIdx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===srcName);
    updatePageNav();
  }; im.src=url; }
function updatePageNav(){ const pc=$('pageCounter'); if(pc){ pc.textContent=pageIdx<0?'未载入（共 '+pageList.length+' 版可载入）':'第 '+(pageIdx+1)+' / '+navList.length+' 版'; }
  const cs=$('crossStatus'); if(cs){ const cross=mergeMode==='cross'; cs.style.display=cross?'block':'none'; cs.textContent=cross?('跨页 · 已载入 '+pageOrder.length+' 版'):'单页模式'; } }
function gotoPage(i){ if(i<0||i>=navList.length) return; const name=navList[i];
  // 同步设置页码并立即刷新导航，不依赖图片 onload 反推（避免竞态/反推失败导致翻页失效）
  pageIdx=i; updatePageNav();
  fetch('/api/image?name='+encodeURIComponent(name)+'&dir=cropped_hi').then(r=>r.json()).then(j=>loadDataURL(j.data,name,i)).catch(e=>log('翻页失败：'+e)); }
const PAD=24;   // 画布四周留白像素，便于在图片边角起框
function resizeCanvas(){ const wrap=$('canvasWrap'); const availW=Math.max(1,wrap.clientWidth-32-2*PAD); const availH=Math.max(1,wrap.clientHeight-32-2*PAD);
  if(img){ baseScale=Math.min(1,availW/natW,availH/natH); scale=baseScale*userZoom; cv.width=Math.max(1,Math.floor(natW*scale)+2*PAD); cv.height=Math.max(1,Math.floor(natH*scale)+2*PAD);
    cv.classList.remove('placeholder'); draw(); }
  else { cv.width=Math.max(1,availW+2*PAD); const h=Math.max(420,wrap.clientHeight-40); cv.height=Math.max(1,h+2*PAD); cv.classList.add('placeholder'); drawPlaceholder(); } }
window.addEventListener('resize',()=>{ userZoom=1; resizeCanvas(); draw(); updateZoomBar(); });
function setZoom(z){ userZoom=Math.max(ZOOM_MIN,Math.min(ZOOM_MAX,z)); resizeCanvas(); draw(); updateZoomBar(); }
function updateZoomBar(){ const el=$('zoomPct'); if(el)el.textContent=Math.round(userZoom*100)+'%'; }
function drawPlaceholder(){ if(img)return; ctx.clearRect(0,0,cv.width,cv.height); ctx.fillStyle='#f8fafc'; ctx.fillRect(0,0,cv.width,cv.height);
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
function draw(){ if(!img){ drawPlaceholder(); return; } ctx.clearRect(0,0,cv.width,cv.height); ctx.drawImage(img,PAD,PAD,natW*scale,natH*scale);
  const cross = mergeMode==='cross';
  boxes.forEach((b,i)=>{ const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; const isSel=b.id===selectedId;
    const color=(b.group?groupColor(b.group):LBL_COLOR[b.label])||'#dc2626';
    ctx.strokeStyle=color; ctx.lineWidth=isSel?3:2; ctx.strokeRect(X,Y,W,H);
    ctx.fillStyle=color; ctx.fillRect(X,Y-16,20,16); ctx.fillStyle='#fff'; ctx.font='12px sans-serif';
    ctx.textAlign='center'; ctx.fillText(String(cross?globalBoxIndex(i):(i+1)),X+10,Y-4); ctx.textAlign='left'; drawHandles(b,isSel); });
  if(cur){ const X=cur.x*scale+PAD,Y=cur.y*scale+PAD,W=cur.w*scale,H=cur.h*scale; ctx.strokeStyle='#f59e0b'; ctx.lineWidth=2; ctx.setLineDash([5,3]); ctx.strokeRect(X,Y,W,H); ctx.setLineDash([]); } }
// commitCanvas 已移除：回退到 #166 同步绘制方案——绘制在图片 onload 同步任务内完成即可随该次渲染上屏，无需点击。
function drawHandles(b,isSel){ const hs=handlesOf(b); ctx.fillStyle=isSel?'#2563eb':'rgba(37,99,235,0.45)'; ctx.strokeStyle='#fff'; ctx.lineWidth=1;
  hs.forEach(h=>{ ctx.fillRect(h.x,h.y,HANDLE,HANDLE); ctx.strokeRect(h.x,h.y,HANDLE,HANDLE); }); }
function handlesOf(b){ const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; const c=n=>n-HANDLE/2;
  return [{name:'nw',x:c(X),y:c(Y)},{name:'n',x:c(X+W/2),y:c(Y)},{name:'ne',x:c(X+W),y:c(Y)},{name:'w',x:c(X),y:c(Y+H/2)},
          {name:'e',x:c(X+W),y:c(Y+H/2)},{name:'sw',x:c(X),y:c(Y+H)},{name:'s',x:c(X+W/2),y:c(Y+H)},{name:'se',x:c(X+W),y:c(Y+H)}]; }

function toNat(e){ const r=cv.getBoundingClientRect(); const px=(e.clientX-r.left)*(cv.width/r.width)-PAD, py=(e.clientY-r.top)*(cv.height/r.height)-PAD; return [px*(natW/(cv.width-2*PAD)),py*(natH/(cv.height-2*PAD))]; }
function toCanvas(e){ const r=cv.getBoundingClientRect(); return [(e.clientX-r.left)*(cv.width/r.width),(e.clientY-r.top)*(cv.height/r.height)]; }
function hitHandle(e){ const [cx,cy]=toCanvas(e); for(let i=boxes.length-1;i>=0;i--){ const b=boxes[i]; for(const h of handlesOf(b)){
  if(cx>=h.x-HIT_PAD&&cx<=h.x+HANDLE+HIT_PAD&&cy>=h.y-HIT_PAD&&cy<=h.y+HANDLE+HIT_PAD) return {box:b,handle:h.name}; } } return null; }
function hitBox(e){ const [cx,cy]=toCanvas(e); for(let i=boxes.length-1;i>=0;i--){ const b=boxes[i];
  const X=b.x*scale+PAD,Y=b.y*scale+PAD,W=b.w*scale,H=b.h*scale; if(cx>=X&&cx<=X+W&&cy>=Y&&cy<=Y+H) return b; } return null; }

cv.addEventListener('mousedown',e=>{ if(!img)return; const hh=hitHandle(e); if(hh){ const [x,y]=toNat(e);
  dragState={kind:'resize',id:hh.box.id,handle:hh.handle,startX:x,startY:y,origBox:{...hh.box}}; selectedId=hh.box.id; renderBoxList(); draw(); return; }
  const hb=hitBox(e); if(hb){ const [x,y]=toNat(e); dragState={kind:'move',id:hb.id,startX:x,startY:y,origBox:{...hb}}; selectedId=hb.id; renderBoxList(); draw(); return; }
  selectedId=null; renderBoxList(); draw(); const [x,y]=toNat(e); drawing={x,y}; cur=null; });
cv.addEventListener('mousemove',e=>{ if(drawing){ const [x,y]=toNat(e);
  cur={x:Math.min(drawing.x,x),y:Math.min(drawing.y,y),w:Math.abs(x-drawing.x),h:Math.abs(y-drawing.y)}; draw(); return; }
  if(!dragState){ const hh=hitHandle(e),hb=hitBox(e); cv.style.cursor=hh?'resize':(hb?'move':'crosshair'); return; }
  const [x,y]=toNat(e); const dx=x-dragState.startX,dy=y-dragState.startY; const b=boxes.find(bx=>bx.id===dragState.id); if(!b)return; const o=dragState.origBox;
  if(dragState.kind==='move'){ b.x=o.x+dx; b.y=o.y+dy; } else { let nx=o.x,ny=o.y,nw=o.w,nh=o.h; const h=dragState.handle;
    if(h.includes('w')){ nx+=dx; nw-=dx; } if(h.includes('e')){ nw+=dx; } if(h.includes('n')){ ny+=dy; nh-=dy; } if(h.includes('s')){ nh+=dy; }
    if(nw<5)nw=5; if(nh<5)nh=5; b.x=nx;b.y=ny;b.w=nw;b.h=nh; } draw(); });
cv.addEventListener('mouseup',e=>{ if(drawing){ const [x,y]=toNat(e); const x0=Math.min(drawing.x,x),y0=Math.min(drawing.y,y);
  const w=Math.abs(x-drawing.x),h=Math.abs(y-drawing.y); drawing=null;
  if(w>5&&h>5){ const nb={id:uid++,label:$('lblSel').value,x:x0,y:y0,w,h,group:''}; boxes.push(nb); selectedId=nb.id; renderBoxList(); renderResults(); }
  cur=null; draw(); return; } if(dragState){ dragState=null; draw(); } });
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
  if(e.key==='Delete'||e.key==='Backspace'){
    if(selectedId!=null) deleteBoxById(selectedId);
    return;
  }
  // 键盘左右键控制画布水平滚动（没有输入焦点时）
  const cw=$('canvasWrap'); if(!cw) return;
  if(e.key==='ArrowLeft'){ cw.scrollLeft-=120; e.preventDefault(); }
  else if(e.key==='ArrowRight'){ cw.scrollLeft+=120; e.preventDefault(); }
});
function deleteBoxById(id){ let found=false;
  for(const pname in allPageData){ const arr=allPageData[pname].boxes; const i=arr.findIndex(b=>b.id===id); if(i>=0){ arr.splice(i,1); found=true; break; } }
  if(!found){ const i=boxes.findIndex(b=>b.id===id); if(i>=0) boxes.splice(i,1); }
  if(selectedId===id)selectedId=null; renderBoxList(); renderResults(); draw(); }
// 跨页展平：按 pageOrder 阅读顺序列出各页框，连续编号；返回每项含 box / pageName / localIdx / globalIdx
function flattenBoxes(){ const list=[]; let g=0;
  for(const pname of pageOrder){ const arr=(allPageData[pname]&&allPageData[pname].boxes)||[];
    for(let i=0;i<arr.length;i++) list.push({box:arr[i], pageName:pname, localIdx:i, globalIdx:g++}); }
  return list; }
function pageArr(pg){ return (allPageData[pg]&&allPageData[pg].boxes)||boxes; }
function locateBox(id){ for(const pname of pageOrder){ const arr=(allPageData[pname]&&allPageData[pname].boxes)||[];
    if(arr.some(b=>b.id===id)){ if(srcName!==pname){ pendingSelectId=id; const idx=navList.findIndex(f=>f.replace(/\.[^.]+$/,'')===pname); if(idx>=0) gotoPage(idx); } else { selectedId=id; renderBoxList(); draw(); } return; } }
  selectedId=id; renderBoxList(); draw(); }

function renderBoxList(){ const el=$('boxList');
  const cross = mergeMode==='cross';
  const view = cross ? flattenBoxes() : boxes.map((b,i)=>({box:b, pageName:srcName, localIdx:i, globalIdx:i}));
  if(!view.length){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">尚未框选</div>'; return; } el.innerHTML='';
  view.forEach((v)=>{ const b=v.box; const d=document.createElement('div'); d.className='box'+(b.id===selectedId?' sel':'');
    const color=(b.group?groupColor(b.group):LBL_COLOR[b.label])||'#dc2626'; const groupHint=b.group?('组:'+b.group):'无组';
    const num = cross ? (v.globalIdx+1) : (v.localIdx+1);
    const pageTag = cross ? `<span class="bx-page" title="所在版">${esc(v.pageName.replace(/\.[^.]+$/,'')||'')}</span>` : '';
    d.innerHTML=`<span class="bx-num" style="background:${color}">${num}</span>${pageTag}
      <div class="bx-main">
        <select data-page="${esc(v.pageName)}" data-idx="${v.localIdx}">${['title','author','text'].map(l=>`<option ${l===b.label?'selected':''}>${l}</option>`).join('')}</select>
        <input class="grp" data-page="${esc(v.pageName)}" data-idx="${v.localIdx}" value="${esc(b.group||'')}" placeholder="组" title="同一篇文章的多个框填相同组名，将合并识别/导出为一篇">
        <span class="bx-group" style="color:${color}" title="${groupHint}">${groupHint}</span>
      </div>
      <div class="bx-ops">
        <button class="mini sec" data-up="${v.localIdx}" data-page="${esc(v.pageName)}">↑ 上移</button><button class="mini sec" data-dn="${v.localIdx}" data-page="${esc(v.pageName)}">↓ 下移</button>
        <button class="mini" data-loc="${b.id}">定位</button><button class="mini stop" data-del="${b.id}">✕ 删除</button>
      </div>`; el.appendChild(d); });
  el.querySelectorAll('select').forEach(s=>s.onchange=e=>{ const pg=e.target.dataset.page, i=+e.target.dataset.idx; pageArr(pg)[i].label=e.target.value; draw(); renderResults(); });
  el.querySelectorAll('input.grp').forEach(inp=>inp.onchange=e=>{ const pg=e.target.dataset.page, i=+e.target.dataset.idx; pageArr(pg)[i].group=e.target.value.trim(); renderBoxList(); draw(); renderResults(); });
  el.querySelectorAll('[data-del]').forEach(b=>b.onclick=e=>{ deleteBoxById(+e.target.dataset.del); });
  el.querySelectorAll('[data-up]').forEach(b=>b.onclick=e=>{ const i=+e.target.dataset.up, pg=e.target.dataset.page; const arr=pageArr(pg); if(i>0){[arr[i-1],arr[i]]=[arr[i],arr[i-1]];renderBoxList();draw();} });
  el.querySelectorAll('[data-dn]').forEach(b=>b.onclick=e=>{ const i=+e.target.dataset.dn, pg=e.target.dataset.page; const arr=pageArr(pg); if(i<arr.length-1){[arr[i],arr[i+1]]=[arr[i+1],arr[i]];renderBoxList();draw();} });
  el.querySelectorAll('[data-loc]').forEach(b=>b.onclick=e=>{ locateBox(+e.target.dataset.loc); }); }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ---------- 识别 ----------
function labelPriority(l){ return l==='title'?0:l==='author'?1:2; }
// 无 group 的框默认按框选顺序合并成一篇（key 统一为 grp:__default__）；显式标 group 的框按组各自独立成篇。
function makeGroupKey(b){ return b.group?'grp:'+b.group:'grp:__default__'; }
function getOcrTargets(bs){ bs=bs||boxes;
  if(!bs.length){ return []; } // 无框：禁止识别，返回空目标（绝不整版兜底）
  const groups=new Map(); bs.forEach((b,i)=>{ const k=makeGroupKey(b); if(!groups.has(k))groups.set(k,{key:k,frames:[],group:b.group||''}); groups.get(k).frames.push({b,oi:i}); });
  return Array.from(groups.values()).map(g=>{ g.frames.sort((a,c)=>(labelPriority(a.b.label)-labelPriority(c.b.label))||(a.oi-c.oi));
    const boxesOf=g.frames.map(f=>f.b);
    const label = g.group ? ('group:'+g.group) : (boxesOf.length>1 ? ('合并 '+boxesOf.length+' 框') : boxesOf[0].label);
    return {key:g.key,boxes:boxesOf,group:g.group||'',label}; }); }

// 跨页聚合：把 pageOrder 中各页的框/识别结果按阅读顺序合并。
// 同 group 跨页合成一篇；无组框按框选顺序合并成一篇（单页模式不跨页）。
function aggregateCrossTargets(){
  if(!pageOrder.length) return getOcrTargets().map(t=>({...t,pageTargets:[{pname:srcName,t}]}));
  const groups=new Map();
  for(const pname of pageOrder){
    const pd=allPageData[pname]; if(!pd) continue;
    const ts=getOcrTargets(pd.boxes);
    for(const t of ts){
      let k,label;
      if(mergeMode!=='cross'){
        // 单页模式：每版每框强制独立成篇，key 必带版名，绝不跨页合并文本
        k='page:'+pname+':'+t.key; label=t.group?('group:'+t.group):t.label;
      } else { k=t.group?'cross:'+t.group:'__cross_default__'; label=t.group?('group:'+t.group):('合并 '+pageOrder.length+' 版'); }
      if(!groups.has(k)) groups.set(k,{key:k,label,group:t.group||'',boxes:(t.boxes||[]).slice(),pageTargets:[]});
      else { const g=groups.get(k); if(t.boxes) g.boxes.push(...t.boxes); }
      groups.get(k).pageTargets.push({pname,t});
    }
  }
  return Array.from(groups.values());
}
function mergeCrossTarget(ct){
  let title='',author='',bodies=[];
  for(const {pname,t} of ct.pageTargets){
    const pd=allPageData[pname]; if(!pd) continue;
    const item=resultToItem(pd.results[t.key]);
    if(item.title && !title) title=item.title;
    if(item.author && !author) author=item.author;
    if(item.body) bodies.push(item.body);
  }
  // 跨页拼接：若上一段末句未收尾（无句号/问号/叹号/引号收尾），说明是跨页续句，直接衔接不分段
  const SENT_END=/[。！？」』）】.!?\"'）]\s*$/;
  let body='';
  const bs=bodies.filter(Boolean);
  for(let k=0;k<bs.length;k++){
    const seg=bs[k].replace(/^\s+|\s+$/g,'');
    if(k===0){ body=seg; continue; }
    if(SENT_END.test(body)) body += '\n\n' + seg;   // 上段已收尾 → 新段落
    else body += seg;                                // 跨页续句 → 直接衔接
  }
  const raw=`标题：${title}\n作者：${author}\n正文：${body}`.trim();
  return {title,author,body,raw};
}
function crossBaseName(){ return (pageOrder.length?pageOrder[0]:srcName).replace(/\.[^.]+$/,'')+'_跨页'; }
// 把后端返回的绝对路径转成相对运行时目录的简短显示（用于日志中「已打开…」）
function osRel(p){ if(!p) return p; try{ const base=((window.__RUNTIME_DIR)||''); if(base && p.startsWith(base)) return 'output/'+p.slice(base.length).replace(/^[\\/]/,''); }catch(e){} return p; }

async function ocrBox(b, im, nW, nH){ const oc=document.createElement('canvas'); oc.width=b.w; oc.height=b.h;
  oc.getContext('2d').drawImage(im,b.x,b.y,b.w,b.h,0,0,b.w,b.h); const b64=oc.toDataURL('image/png').split(',')[1];
  const r=await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_b64:b64,src:srcName,overrides:getOverrides()})});
  const j=await r.json(); if(!j.ok) return {text:'[OCR_ERROR] '+(j.error||''), usage:{}}; return {text:j.text||'', usage:j.usage||{}}; }
async function recognizeOneBox(b){ return ocrBox(b, img, natW, natH); }
async function recognizeAll(){ if(!img){ alert('请先载入整版图片'); return; }
  if(!pageOrder.length){ alert('请先在卡片②勾选并「载入」整版原图。'); return; }
  // 硬拦截：所有载入版都必须有框，无框严禁识别（不整版兜底）
  const noBoxPages=pageOrder.filter(p=>!(allPageData[p]&&allPageData[p].boxes&&allPageData[p].boxes.length));
  if(noBoxPages.length){ alert('存在未框选的整版：'+noBoxPages.join('、')+'。\n请先为每版框选区域再识别，无框严禁识别。'); return; }
  // 逐版离线识别：每版重新取图并按页存回 allPageData，最后统一聚合
  let failedPages=[];
  let ocrPt=0,ocrCt=0,ocrTt=0,ocrDur=0;  // 本次识别累计 token / 耗时
  const tStart=performance.now();
  log('[识别全部] 模式='+mode+'，载入 '+pageOrder.length+' 版；各版框数：'+pageOrder.map(p=>(p+'='+(allPageData[p]?allPageData[p].boxes.length:'(无)'))).join('，'));
  for(const pname of pageOrder){
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
          // 单框：整框内容严格归到框的显式标签字段。先用 parseArticle 拆出模型输出的标题/作者/正文，
          // 再按框 label 只取对应字段；无对应字段时退取 body（模型未写前缀时整段即内容）。
          const p=parseArticle(r.text||'');
          const parsed={title:'',author:'',body:''};
          if(b.label==='title') parsed.title=p.title||p.body;
          else if(b.label==='author') parsed.author=p.author||p.body;
          else parsed.body=p.body;
          pd.results[t.key]=parsed; if(r.usage){ ocrPt+=r.usage.prompt_tokens||0; ocrCt+=r.usage.completion_tokens||0; ocrTt+=r.usage.total_tokens||0; ocrDur+=r.usage.duration_s||0; } }
        else if(t.boxes&&t.boxes.length>1){ const parts=[]; for(const b of t.boxes){ const r=await ocrBox(b,im,nW,nH);
          // 同单框：先用 parseArticle 拆分模型输出，再按框 label 只取对应字段，避免内容串位。
          const p=parseArticle(r.text||'');
          const parsed={title:'',author:'',body:''};
          if(b.label==='title') parsed.title=p.title||p.body;
          else if(b.label==='author') parsed.author=p.author||p.body;
          else parsed.body=p.body;
          parts.push(parsed); if(r.usage){ ocrPt+=r.usage.prompt_tokens||0; ocrCt+=r.usage.completion_tokens||0; ocrTt+=r.usage.total_tokens||0; ocrDur+=r.usage.duration_s||0; } }
          const title=parts.map(p=>p.title).find(x=>x)||''; const author=parts.map(p=>p.author).find(x=>x)||''; const body=parts.map(p=>p.body).filter(x=>x).join('\n\n');
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
  const okCount = pageOrder.length - failedPages.length;
  const wall = ((performance.now()-tStart)/1000).toFixed(2);
  const _prov=(backendCfg&&backendCfg.values&&backendCfg.values.BOX_OCR_PROVIDER)||'qwen';
  const _pre=_prov.toUpperCase();
  const cfgModel=(backendCfg&&backendCfg.values&&backendCfg.values[_pre+'_MODEL'])||'';
  log('[识别全部] 已识别 '+okCount+' / '+pageOrder.length+' 版'+(cross?'（跨页模式：按阅读顺序合并为一篇）':'（单页模式：每版独立）')+'。');
  log('[识别全部] 本次 OCR 消耗'+(cfgModel?'（'+cfgModel+'）':'')+' → 输入 '+ocrPt.toLocaleString()+' + 输出 '+ocrCt.toLocaleString()+' = 总 '+ocrTt.toLocaleString()+' tokens；OCR 接口耗时 '+ocrDur.toFixed(2)+'s，总耗时 '+wall+'s。');
  // 记录一次「识别全部」运行；OCR 调用次数按本次识别的「组」数计（每个 group 1 次，含未标 group 自动合并的默认组）
  if(okCount>0){ try{ const callGroups=aggregateCrossTargets(); fetch('/api/ocr_run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:cfgModel,pages:pageOrder.length,boxes:pageOrder.reduce((s,p)=>s+((allPageData[p]&&allPageData[p].boxes)?allPageData[p].boxes.length:0),0),calls:callGroups.length})}).catch(()=>{}); }catch(e){} }
  if(failedPages.length) log('[识别全部] 以下版识别失败：'+failedPages.join('、'));
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
  if(!img){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">载入整版图片后，在此显示可核对 / 编辑的识别结果。</div>'; return; }
  const cross = mergeMode==='cross';
  const targets=aggregateCrossTargets(); if(!targets.length){ el.innerHTML='<div style="color:var(--mut); font-size:12px;">多篇模式需先框选区域，再点「识别全部」。</div>'; return; }
  el.innerHTML='';
  // 全局序号映射：box 对象 -> 阅读顺序中的连续编号（与 renderBoxList 展平一致），避免多版时局部序号错乱
  const flat=flattenBoxes(); const gIdx=new Map(); flat.forEach(v=>gIdx.set(v.box, v.globalIdx+1));
  targets.forEach((t,i)=>{ const item=resultToItem(crossResults[t.key]); let title,size;
    if(cross){
      if(t.key==='__cross_whole__'){ title='跨页·整篇合并'; size=pageOrder.length+' 版合并'; }
      else if(t.group){ const pages=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); title=`跨页·组 ${t.group}（${pages}）`; size=t.pageTargets.length+' 处 / '+pageOrder.length+' 版'; }
      else if(t.key==='__cross_default__'){ title='跨页·合并 '+pageOrder.length+' 版'; size=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); }
      else { title=`跨页·项 ${i+1} · ${t.label}`; size=t.pageTargets.map(pt=>pt.pname.replace(/\.[^.]+$/,'')).join('、'); }
    } else if(t.key==='__whole__'){ title='（无框未识别）'; size='请框选后再识别'; }
    else if(t.boxes&&t.boxes.length>1){ const g=t.boxes[0].group; title=g?`组 ${g}`:'合并';
      const area=t.boxes.reduce((s,b)=>s+b.w*b.h,0); size=`${t.boxes.length} 个框 / 约 ${Math.round(area).toLocaleString()}px²`; }
    else { const b=t.boxes?t.boxes[0]:null; title=b?`框 ${i+1} · ${b.label}`:'单篇'; size=b?`${b.w}×${b.h}px`:''; }
    let display=item.raw;
    if(!cross && t.boxes&&t.boxes.length===1){ const b=t.boxes[0];
      if(b.label==='title')display=item.title?`标题：${item.title}`:item.raw;
      else if(b.label==='author')display=item.author?`作者：${item.author}`:item.raw;
      else if(b.label==='text'){ const ls=[]; if(item.title)ls.push(`标题：${item.title}`); if(item.author)ls.push(`作者：${item.author}`); if(item.body)ls.push(item.body); display=ls.join('\n')||item.raw; } }
    const d=document.createElement('div'); d.className='card'; d.innerHTML=`<div class="hd"><span class="title">${title}</span><span class="meta">${size}</span></div><textarea data-key="${esc(t.key)}">${esc(display)}</textarea>`; el.appendChild(d); });
  el.querySelectorAll('textarea').forEach(ta=>ta.oninput=e=>{ const k=e.target.dataset.key; const item=resultToItem(crossResults[k]); item.raw=e.target.value;
    const p=parseArticle(e.target.value); item.title=p.title; item.author=p.author; item.body=p.body; crossResults[k]=item; }); }

// ---------- 导出 ----------
function computeUnionBox(bs){ let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity; bs.forEach(b=>{x0=Math.min(x0,b.x);y0=Math.min(y0,b.y);x1=Math.max(x1,b.x+b.w);y1=Math.max(y1,b.y+b.h);});
  return {x:Math.round(x0),y:Math.round(y0),w:Math.round(x1-x0),h:Math.round(y1-y0)}; }
function aggregateTargetsForExport(pageName){
  const cross = mergeMode==='cross';
  let targets=aggregateCrossTargets();
  // 单页模式：只导出指定版（pageName，缺省为当前 srcName），不把工作集其余版也写进同一个文件
  const scope = pageName || srcName;
  if(!cross && scope){ targets=targets.filter(t=>t.key.startsWith('page:'+scope+':') || t.pageTargets.every(pt=>pt.pname===scope)); }
  return targets.map((t,i)=>{
    if(cross) return {order:i+1,label:t.label,group:t.group||'',box:null,text:itemToText(crossResults[t.key])};
    if(t.key==='__whole__') return {order:i+1,label:'（无框未识别）',group:'',box:null,text:'请框选后再识别'};
    const bs=t.boxes; return {order:i+1,label:t.label,group:(bs[0].group||''),box:bs.length===1?{x:Math.round(bs[0].x),y:Math.round(bs[0].y),w:Math.round(bs[0].w),h:Math.round(bs[0].h)}:computeUnionBox(bs),text:itemToText(crossResults[t.key])};
  }); }
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
      if(!j.ok){ log('保存失败：'+(j.error||'')); return; }
      log('[保存修改] 已覆盖写回 txt\n'+j.written.map(s=>'  output/'+(od?od+'/':'')+s).join('\n'));
      const b2={source_name:en,mode:mode,boxes:items}; if(od)b2.out_dir=od;
      const j2=await fetch('/api/export_json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b2)}).then(r=>r.json());
      if(j2.ok) log('[保存修改] 已覆盖写回 json → '+(j2.path||'(未知路径)')); else log('保存 JSON 失败：'+(j2.error||''));
    }catch(e){ log('保存失败：'+e); }
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
    }catch(e){ log('['+en+'] 保存失败：'+e); }
  }
  if(done>0) log('[保存修改] 完成，共写回 '+done+' 个版的 json/txt。');
  else log('[保存修改] 没有可导出的识别结果，未写入任何文件。');
}

// ---------- 阶段 0 / 4 ----------
function refreshImageList(){
  return fetch('/api/list_images?dir=cropped_hi').then(r=>r.json()).then(j=>{
    const oldIdx=pageIdx, oldName=srcName;
    pageList=j.files||[]; renderSrcList();
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
  pageList.forEach(f=>{ const lab=document.createElement('label'); lab.className='item'; lab.innerHTML=`<input type="checkbox" class="src-chk" value="${esc(f)}"> <span>${esc(f)}</span>`; box.appendChild(lab); });
  syncSelAll();
  const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.toggle('collapsed', pageList.length>5);
  if($('imgCnt')) $('imgCnt').textContent = '共 '+pageList.length+' 项'; }
function syncSelAll(){ const sa=$('selAll'); if(!sa) return; const chks=document.querySelectorAll('.src-chk');
  if(!chks.length){ sa.checked=false; sa.indeterminate=false; return; }
  const all=[...chks].every(c=>c.checked); const none=![...chks].some(c=>c.checked);
  sa.checked = all; sa.indeterminate = !all && !none; }
function flashHint(msg){ alert(msg); }
function runExtractAndGroup(){
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
  }).catch(e=>log('抽图并归档失败：'+e)); }
async function runPost(){
  const mode = $('postMode') ? $('postMode').value : 'plain';
  const isCross = mergeMode==='cross' && pageOrder.length>1;
  const top = (mode==='plain' ? 'plain_text' : 'knowledge_base');
  // 统一空状态守卫：未载入任何版时直接提示，不调用后端，避免生成「未命名」空文件夹
  if(!pageOrder.length){ alert('请先载入工作集（勾选整版原图并点「载入所选」），再点结构化。'); return; }
  // 自动先落盘：结构化（postprocess.py 子进程）只读取磁盘上的 raw 产物，不接触浏览器内存中的
  // crossResults；因此必须先确保 output/ 下的 .txt/.json 已写回，省去手动点「保存修改」的前置步骤。
  // 复用 saveEdit 的落盘逻辑（跨页合并单文件 / 单页逐版写回），同时 flushEditsToAll 防止手动编辑丢字。
  log('[结构化] 先自动保存当前识别结果（等同「保存修改」）…');
  await saveEdit();
  // 结构化成功后的统一清理：清空当前工作集与勾选状态，避免重复处理
  // clearPost 统一清空「已载入工作集 + 画布 + 勾选状态 + 整版原图目录列表（pageList）」。
  // 单页与跨页模式均清空 pageList：结构化完成后整轮工作结束，目录列表一并归零，避免出现「残留文件可重新打开」。
  const clearPost = ()=>{
    const cleaned = pageOrder.slice();  // 本次已结构化处理的整版名（cropped_hi 文件名）
    const crossRaw = (mergeMode==='cross' && pageOrder.length>1) ? crossBaseName() : '';  // 跨页 raw 中转目录名（output/{crossRaw}/）
    pageOrder=[]; crossResults={}; allPageData={}; navList=[]; pageList=[]; pageIdx=-1; srcName=''; img=null; boxes=[]; results={};
    // 同步复位「当前源」显示，避免 DOM 残留旧文件名
    const sn=$('srcName'); if(sn) sn.textContent='未载入';
    updatePageNav(); renderSrcList(); renderBoxList(); renderResults(); draw();
    const chks=document.querySelectorAll('.src-chk'); chks.forEach(c=>c.checked=false); syncSelAll();
    // 真正删除 cropped_hi/ 下本次已处理的源图：结构化产物（含整版原图副本与 OCR 结果）已落盘到 output/，
    // cropped_hi/ 里的原件成为残留，应物理删除（仅删指定文件、不级联、不碰 output/）。
    if(cleaned.length){
      fetch('/api/cleanup_cropped',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:cleaned})})
        .then(r=>r.json()).then(j=>{ if(j.ok){ if(j.removed&&j.removed.length){ log('[清理] 已物理删除 cropped_hi/ 源图 '+j.removed.length+' 个：'+j.removed.join('、')); } else { log('[警告] cropped_hi/ 中应删 '+cleaned.length+' 个源图，但后端未删到任何文件（可能扩展名不匹配或文件已不在，请核查）。'); } } else { log('[清理] cropped_hi/ 源图删除失败：'+(j.error||'')); } })
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
  if(isCross){
    // 跨页：合并为一篇，单文件夹，打开该合并子文件夹（维持原行为）
    const outDir = crossBaseName().replace(/\.[^.]+$/,'');
    const pages = pageOrder.slice();
    fetch('/api/postprocess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,out_dir:outDir,pages})})
      .then(r=>r.json()).then(j=>{ let s='[后置 阶段4 · '+(mode==='plain'?'纯文本':'知识库')+']\n'+(j.stdout||'')+(j.stderr?'\n'+j.stderr:''); if(j.ok&&j.opened_dir){ const rel=osRel(j.opened_dir); s+='\n↑ 已打开该轮文件夹：'+rel; }
        if(j.ok && !j.skipped){ clearPost(); s+='\n[结构化] 已清空当前工作集与勾选状态。'; } log(s); })
      .catch(e=>log('后置失败：'+e));
    return;
  }
  // 单页模式：逐版调用 postprocess，每版独立子文件夹（output/{top}/{整版名}/）；
  // 全部完成后统一打开父目录 output/{top}（多子文件夹，不钻进某一页）
  const pages = pageOrder.slice();
  let done=0, okCount=0, hasRealOutput=false; const logs=[];
  pages.forEach(pname=>{
    const od = pname.replace(/\.[^.]+$/,'');
    fetch('/api/postprocess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,out_dir:od,pages:[pname],no_open:true})})
      .then(r=>r.json()).then(j=>{ if(j.ok && !j.skipped){ okCount++; hasRealOutput=true; }
        const head=(j.stdout||'').split('\n').slice(0,2).join(' / '); logs.push('[版 '+esc(pname)+'] '+(head||(j.ok?(j.skipped?'无产物':'成功'):'失败'))); })
      .catch(e=>logs.push('[版 '+esc(pname)+'] 后置失败：'+e))
      .finally(()=>{ done++; if(done===pages.length){
        let s='[后置 阶段4 · '+(mode==='plain'?'纯文本':'知识库')+'] 单页模式：已逐版检查 '+pages.length+' 版，其中可结构化 '+okCount+' 版，每版独立子文件夹（output/'+top+'/整版名/）。';
        if(logs.length) s+='\n'+logs.join('\n');
        const after = ()=>{ if(hasRealOutput){ clearPost(); s+='\n[结构化] 已清空当前工作集与勾选状态。'; } log(s); };
        // 统一打开父目录 output/{top}（多子文件夹，不钻进某一页）
        fetch('/api/open_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:top})})
          .then(r=>r.json()).then(j=>{ if(j.ok){ const rel=osRel(j.path); s+='\n↑ 已打开父文件夹：'+rel; } }).catch(()=>{})
          .finally(after);
      } });
  });
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
$('zoomReset').onclick=()=>setZoom(1);
$('canvasWrap').addEventListener('wheel',e=>{
  // 鼠标滚轮直接控制画布缩放；按住 Shift 仍可做水平滚动兜底
  if(!img)return;
  const w=$('canvasWrap');
  e.preventDefault();
  if(e.shiftKey){ w.scrollLeft+=e.deltaY; }
  else { setZoom(userZoom*(e.deltaY<0?1.1:0.9)); }
},{passive:false});
$('saveEdit').onclick=saveEdit;
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
      const kbPart = j.kb_count>0 ? ('已复制 '+j.kb_count+' 个 .md') : '无 .md';
      const plainPart = j.plain_count>0 ? ('已复制 '+j.plain_count+' 个 .txt') : '无 .txt';
      log('[一键导出] '+kbPart+'；'+plainPart+'。已平铺到：'+j.rel);
      fetch('/api/open_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:j.dir})});
    }).catch(e=>{ alert('导出请求失败：'+e); });
};
$('prevPage').onclick=()=>gotoPage(pageIdx-1);
$('nextPage').onclick=()=>gotoPage(pageIdx+1);
$('importSource').onclick=()=>$('importSourceIn').click();
$('importSourceIn').onchange=async e=>{
  const files=Array.from(e.target.files||[]);
  if(!files.length)return;
  try{
    const payload=[];
    for(const f of files){
      const buf=await f.arrayBuffer();
      const bytes=new Uint8Array(buf); const CH=0x8000; let bin='';
      for(let i=0;i<bytes.length;i+=CH){ bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+CH)); }
      payload.push({name:f.name,data:btoa(bin)});
    }
    log('[导入文件] 正在写入 '+files.length+' 个文件…');
    fetch('/api/import_source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:payload})})
      .then(r=>r.json()).then(j=>{
        if(j.ok){ log('[导入文件] 已写入 '+j.count+' 个文件到 source/：'+(j.written.join(', ')||'(无)')); if(j.skipped&&j.skipped.length)log('[导入文件] 跳过：'+j.skipped.join(', ')); refreshSourceList().then(fs=>{ if(fs&&fs.length){ document.querySelectorAll('.source-chk').forEach(c=>c.checked=true); syncSourceSelAll(); } }); }
        else log('[导入文件] 失败：'+(j.error||''));
      }).catch(e=>log('导入文件失败：'+e));
  }catch(err){ log('导入文件失败：'+(err&&err.message||err)); }
  e.target.value='';
};
// ---------- 文件删除机制 ----------
function resetCanvasState(){
  // 彻底复位：回到未载入的初始状态
  srcName=''; const sn=$('srcName'); if(sn) sn.textContent='未载入';
  img=null; natW=0; natH=0; pageIdx=-1;
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
    const wrap=box.closest('.src-list-wrap'); if(wrap) wrap.classList.toggle('collapsed', fs.length>5);
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
const mask=$('drawerMask'); const drawer=$('drawer'); const usageDrawer=$('usageDrawer'); const promptDrawer=$('promptDrawer');
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
      html+=' <button class="sec" id="doUpdate" style="margin-left:8px;">立即更新</button>';
      st.innerHTML=html;
      $('doUpdate').onclick=()=>startUpdate(j);
    } else {
      st.textContent='当前已是最新版本（'+escHtml(j.current||'--')+'）'; st.style.color='var(--ok)';
    }
  }catch(e){
    st.innerHTML='检查失败：'+escHtml((e&&e.message)||e)+' <button class="sec" id="gotoReleaseFallback">手动打开发布页</button>'; st.style.color='var(--warn)';
    $('gotoReleaseFallback').onclick=()=>openExternalUrl(RELEASE_PAGE_URL);
  }
}
async function startUpdate(j){
  const st=$('updateStatus'); if(!st) return;
  st.textContent='正在下载更新包…（可能需要一会儿）'; st.style.color='var(--mut)';
  try{
    const r=await fetch('/api/update_download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({download_url:j.download_url, sha256_url:j.sha256_url})});
    const d=await r.json();
    if(!d.ok) throw new Error(d.error||'下载失败');
    st.textContent='下载完成，正在重启以完成升级…'; st.style.color='var(--ok)';
    try{ await fetch('/api/update_apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }catch(e){}
  }catch(e){
    st.innerHTML='下载失败：'+escHtml((e&&e.message)||e)+' <button class="sec" id="gotoReleaseFallback2">手动打开发布页</button>'; st.style.color='var(--err)';
    const fb=$('gotoReleaseFallback2'); if(fb) fb.onclick=()=>openExternalUrl(j.page_url||RELEASE_PAGE_URL);
  }
}
function openSettingsDrawer(){ drawer.classList.add('open'); usageDrawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.add('open'); }
function openUsageDrawer(){ usageDrawer.classList.add('open'); drawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.add('open'); loadUsage(); }
function openPromptDrawer(){ loadPrompts(); promptDrawer.classList.add('open'); drawer.classList.remove('open'); usageDrawer.classList.remove('open'); mask.classList.add('open'); }
function closeDrawers(){ drawer.classList.remove('open'); usageDrawer.classList.remove('open'); promptDrawer.classList.remove('open'); mask.classList.remove('open'); }
$('gearBtn').onclick=openSettingsDrawer; $('drawerClose').onclick=closeDrawers;
$('usageBtn').onclick=openUsageDrawer; $('usageClose').onclick=closeDrawers; mask.onclick=closeDrawers;
$('promptBtn').onclick=openPromptDrawer; $('promptClose').onclick=closeDrawers;
$('checkUpdateBtn').onclick=checkUpdate;
document.addEventListener('keydown', e=>{ if(e.key==='Escape'&&(drawer.classList.contains('open')||usageDrawer.classList.contains('open')||promptDrawer.classList.contains('open'))) closeDrawers(); });

// ---------- 提示词抽屉 ----------
let revertOcr = false;
let postPrompts = { kb: '', plain: '' };        // 两套结构化提示词（知识库 / 纯文本）
let postDef = { kb: '', plain: '' };            // 对应内置默认（「恢复默认」回填用）
let postEditMode = 'kb';                        // 抽屉当前正在编辑的结构化模式
let revertPost = { kb: false, plain: false };   // 各模式是否点了「恢复默认」
async function loadPrompts(){
  try {
    const c = await (await fetch('/api/config')).json();
    $('prompt_ocr').dataset.def = c.prompt_ocr_default || '';
    postDef.kb    = c.prompt_post_default || '';
    postDef.plain = c.prompt_post_plain_default || '';
    $('prompt_ocr').value = (c.values && c.values.PROMPT_OCR && c.values.PROMPT_OCR.trim()) ? c.values.PROMPT_OCR : (c.prompt_ocr_default || '');
    postPrompts.kb    = (c.values && c.values.PROMPT_POST       && c.values.PROMPT_POST.trim())       ? c.values.PROMPT_POST       : postDef.kb;
    postPrompts.plain = (c.values && c.values.PROMPT_POST_PLAIN && c.values.PROMPT_POST_PLAIN.trim()) ? c.values.PROMPT_POST_PLAIN : postDef.plain;
    postEditMode = 'plain';
    const rb = document.querySelector('input[name=postModeEdit][value=plain]'); if (rb) rb.checked = true;
    $('prompt_post').value = postPrompts.plain;
    updatePostHint();
  } catch(e){ log('[提示词] 读取配置失败：'+e); }
}
function updatePostHint(){
  const h = $('postHint');
  if (postEditMode === 'plain'){
    h.innerHTML = '纯文本模式：用于「结构化」阶段题录抽取，产物为.txt格式。';
  } else {
    h.innerHTML = '知识库模式：用于「结构化」阶段题录抽取，产物为.md格式。';
  }
}
// 切换抽屉内正在编辑的结构化提示词（知识库 / 纯文本）
document.querySelectorAll('input[name=postModeEdit]').forEach(r=>{
  r.onchange = () => {
    postPrompts[postEditMode] = $('prompt_post').value;   // 先保存当前编辑内容
    postEditMode = r.value;
    $('prompt_post').value = postPrompts[postEditMode];
    updatePostHint();
  };
});
$('resetOcr').onclick  = () => { $('prompt_ocr').value  = $('prompt_ocr').dataset.def  || ''; revertOcr  = true; };
$('resetPost').onclick = () => { $('prompt_post').value = postDef[postEditMode] || ''; revertPost[postEditMode] = true; };
$('savePrompt').onclick = async () => {
  const hint = $('promptHint'); hint.textContent = '保存中…'; hint.style.color = 'var(--mut)';
  try {
    const cur = await (await fetch('/api/config')).json();
    // 现有磁盘配置（含密钥等环境项），只更新提示词相关键
    const merged = cur.values || {};
    if (revertOcr) merged.PROMPT_OCR = ''; else merged.PROMPT_OCR = $('prompt_ocr').value;
    if (revertPost.kb)    merged.PROMPT_POST       = ''; else merged.PROMPT_POST       = postPrompts.kb;
    if (revertPost.plain) merged.PROMPT_POST_PLAIN = ''; else merged.PROMPT_POST_PLAIN = postPrompts.plain;
    revertOcr = false; revertPost.kb = false; revertPost.plain = false;
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
document.querySelectorAll('.eye').forEach(e=>{ e.innerHTML=EYE_CLOSED; e.onclick=()=>{ const inp=$(e.dataset.target); const show=inp.type==='password'; inp.type=show?'text':'password'; e.innerHTML=show?EYE_OPEN:EYE_CLOSED; }; });
// OCR 服务商：输入框实时暂存 + 切换时回填对应服务商凭据
['cfgApiKey','cfgBaseUrl','cfgModel'].forEach(id=>{ const el=$(id); if(el) el.addEventListener('input', stashActive); });
document.querySelectorAll('input[name="cfgProvider"]').forEach(r=>{ r.addEventListener('change',()=>{ if(r.checked){ stashActive(); ocrProvider=r.value; fillActiveProviderInputs(); } }); });
function collectCfg(){
  stashActive();
  const d = { BOX_OCR_PROVIDER: ocrProvider };
  for(const p of ['qwen','doubao','other']){ const pre=p.toUpperCase(); const s=ocrStash[p];
    d[pre+'_API_KEY']=s.api_key; d[pre+'_BASE_URL']=s.base_url; d[pre+'_MODEL']=s.model; }
  d.DEEPSEEK_API_KEY=$('cfgDsKey').value.trim(); d.DEEPSEEK_BASE_URL=$('cfgDsUrl').value.trim(); d.DEEPSEEK_MODEL=$('cfgDsModel').value.trim();
  return d;
}
function saveCfgToBackend(clear){ const data=collectCfg(); if(clear)for(const k in data)data[k]='';
  const hint=$('saveHint'); hint.textContent='保存中…'; hint.style.color='var(--mut)';
  fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(r=>r.json()).then(j=>{
    if(j.ok){ hint.textContent='已保存 ✓'; hint.style.color='var(--ok)'; log('[配置] 已写入：'+(j.path||'未知路径')); return fetch('/api/config').then(r=>r.json()).then(c=>{ backendCfg=c; updateCfgLine(); }); } else { hint.textContent='保存失败：'+(j.error||''); hint.style.color='var(--err)'; log('[配置] 保存失败：'+(j.error||'')+' @ '+(j.path||'')); } }).catch(e=>{ hint.textContent='保存失败：'+e.message; hint.style.color='var(--err)'; }); }
$('saveCfg').onclick=()=>saveCfgToBackend(false);
$('clearCfg').onclick=()=>{ if(confirm('确认清空所有配置？'))saveCfgToBackend(true); };

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

IDLE_TIMEOUT = 30 * 60  # 空闲 30 分钟自动退出
LAST_ACTIVE = {"t": time.time()}
_cfg_lock = threading.Lock()


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

    def idle_watchdog():
        while True:
            time.sleep(60)
            if time.time() - LAST_ACTIVE["t"] > IDLE_TIMEOUT:
                print(f"空闲超过 {IDLE_TIMEOUT // 60} 分钟，自动退出")
                stop_all()

    threading.Thread(target=idle_watchdog, daemon=True).start()

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

    try:
        win = webview.create_window(
            "墨痕 · 近代报刊转录助手",
            url,
            width=1280, height=800,
            min_size=(1000, 700),
            background_color="#f5f6f8",
            maximized=True,
            text_select=True,
        )
    except Exception as e:
        print(f"pywebview 窗口创建失败：{e}")
        os._exit(1)
    win.events.closed += lambda: stop_all()
    print(f"墨痕 · 近代报刊转录助手 已启动（内嵌窗口，端口 {port}；关闭窗口即退出）")
    webview.start(lambda: _set_window_icon("墨痕 · 近代报刊转录助手"))


if __name__ == "__main__":
    main()
