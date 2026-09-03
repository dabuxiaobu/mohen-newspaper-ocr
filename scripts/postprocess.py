# -*- coding: utf-8 -*-
"""
民国报纸 OCR 全链路后置：DeepSeek V4 Flash 题录结构化
=====================================================
输入：民国报纸OCR/ 各篇子目录的 OCR 转录 .txt（ocr_from_labels.py 产出）
输出：同子目录下 <篇名>_题录.md，含 标题/日期/作者/引用(GB/T 7714-2015)

一图切多篇（整版按区域框切出的 _框N 子目录）：结构化完成后自动按题录「标题」
重命名子目录为「标题-整版名」（去掉 _框N 占位），并回写 txt 首行出处；用
--no-rename 可关闭。

凭证（环境变量，沙箱无，需本机设定）：
  DEEPSEEK_API_KEY   必填
  DEEPSEEK_MODEL     必填（如火山方舟 ep-xxxx / deepseek-chat 等）
  DEEPSEEK_BASE_URL  可选，默认火山方舟 Ark（与豆包同端点）；换平台时覆盖
"""
import os
import re
import sys
import io
import json
import argparse
import csv
import time
from openai import OpenAI
from stop_flag import STOP_EVENT

# Windows 控制台默认 GBK，打印 ↳/⚠ 等符号会抛 UnicodeEncodeError 崩溃。
# 若 stdout/stderr 是 GBK 编码且无 tty（pythonw / 打包 exe / 重定向），强制改 UTF-8。
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if not sys.stderr.isatty():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 安全占位符替换：缺字段时保留 {key} 原样，避免用户自定义提示词误删占位符导致崩溃
class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

# 每次运行的 token 消耗持久化到工作区根目录 token_log.csv（doubao/deepseek 各记一行）
# 子脚本落点须与启动器的数据目录对齐：启动器会把 RUNTIME_DIR 注入 MOHEN_DATA_DIR。
# 打包态下若仍用 sys.executable 推导目录，token_log 会落到 exe 目录而非「文档/墨痕数据」，
# 与启动器（读写同一 token_log.csv 供用量面板统计）分裂，导致结构化 token 统计缺失。
_MOHEN_DATA = os.environ.get("MOHEN_DATA_DIR")
if _MOHEN_DATA and os.path.isdir(_MOHEN_DATA):
    _TOKEN_BASE = _MOHEN_DATA
elif getattr(sys, "frozen", False):
    # 打包态兜底（仅当未注入 MOHEN_DATA_DIR 时）：exe 所在目录（应用根）
    _TOKEN_BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _TOKEN_BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_LOG = os.path.join(_TOKEN_BASE, "token_log.csv")


def log_token(stage, image, model, prompt, completion, total, duration=None):
    if total is None:
        total = (prompt or 0) + (completion or 0)
    new_hdr = ["timestamp", "stage", "image", "model",
               "prompt_tokens", "completion_tokens", "total_tokens", "duration_s"]
    # 旧格式（缺 duration_s 列）自动迁移：旧数据补 duration_s=0，避免列错位
    if os.path.exists(TOKEN_LOG):
        with open(TOKEN_LOG, encoding="utf-8", newline="") as f:
            first = f.readline()
        if "duration_s" not in first:
            rows = []
            with open(TOKEN_LOG, encoding="utf-8", newline="") as f:
                r = csv.reader(f)
                old = next(r, None)
                if old:
                    for row in r:
                        if not row:
                            continue
                        while len(row) < 8:
                            row.append("0")
                        rows.append(row[:8])
            with open(TOKEN_LOG, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(new_hdr)
                w.writerows(rows)
    write_hdr = not os.path.exists(TOKEN_LOG)
    with open(TOKEN_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_hdr:
            w.writerow(new_hdr)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), stage, image, model,
                    prompt or 0, completion or 0, total or 0,
                    duration if duration is not None else 0])

# 本地繁简转换：OpenCC（t2s）。安装：python -m pip install opencc-python-reimplemented（纯 Python，Windows/3.13 无 DLL 依赖；注意是 -reimplemented 不是 -reimplementation）。缺失时回退为「模型输出正文」模式。
try:
    from opencc import OpenCC
    CC = OpenCC("t2s")          # traditional -> simplified
    HAS_OPENCC = True
except Exception:
    CC = None
    HAS_OPENCC = False

def to_simp(s):
    """繁体转简体；无 OpenCC 时原样返回（依赖模型已输出简体）"""
    if CC is None or not s:
        return s
    return CC.convert(s)

# DeepSeek 官方端点；若走其他平台用 DEEPSEEK_BASE_URL 覆盖
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 后置题录处理提示词（本地 OpenCC 可用时：模型只抽短字段，正文由本地繁转简，快且省 token）
# 自适应报纸[N]与期刊[J]：由模型按「出处：」行内容判断文献类型，套对应的 GB/T 7714 著录样式。
# 占位符：{date} 报纸出版日期、{page} 报纸版次、{pubyear} 出版年、{journal} 刊名、
#         {volume} 卷、{issue} 期、{pages} 页码。缺字段时安全保留 {key}（由 _SafeDict 处理）。
SYSTEM_PROMPT_SHORT = """你负责对近代文献 OCR 转录文本抽取题录元字段（不做繁简转换，转换在本地完成）。

要求：
严格按以下字段顺序输出，字段名各占一行，不要附加任何说明、前言或结尾：
标题：<文章/篇名，尽量照原文提取>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方规则生成的 GB/T 7714 引用串>
标签：<3-6 个主题词，逗号分隔，涵盖人物/事件/组织/地点>

字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名；尽量照原文，保留原标题用字，繁简均可，转换后会统一）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空（不要写「佚名」等占位）。
   - 引用：先判断文献类型，再严格按 GB/T 7714-2015 著录（末尾句号）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「题名[N].报纸名,出版日期(版次).」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「题名[J].刊名,年,卷(期):页码.」。刊名取「出处：」行中的期刊名，已知为 {journal}；年从上述日期取；卷(期)从文件名或版权页提取，已知卷 {volume}、期 {issue}，无卷则只写期如 (4)；页码已知为 {pages}，著录起讫页如 :45-58。
       题名必须与上方「标题：」字段完全一致（即同一题名，不得改写、扩写或另取所谓“核心主题”），以契合史学研究对题名一致性的要求。
   - 标签：提炼 3-6 个主题词（人物、事件、组织、地点等），逗号分隔，用于知识库检索与聚合。

直接输出上述五个字段，不要其他内容，也不要输出正文。
"""

# 回退提示词（本地 OpenCC 缺失时）：模型承担全文繁转简与正文重写（慢、费 token）
# 同样自适应报纸/期刊；占位符同上。
SYSTEM_PROMPT_FULL = """你负责对近代文献 OCR 转录文本做后置处理。

要求：
1. 将全部内容的繁体转为简体中文；仅做繁→简字符转换，不增删改任何字词、标点与段落结构（保真优先）。
2. 严格按以下字段顺序输出，字段名各占一行，不要附加任何说明、前言或结尾：
标题：<文章/篇名，简体>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方规则生成的 GB/T 7714 引用串>
标签：<3-6 个主题词，逗号分隔，涵盖人物/事件/机构/地点>
正文：
<将原文「正文：」后的内容繁转简后的简体文本，保真连贯>
3. 字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名，简体）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空。
   - 引用：先判断文献类型，再严格按 GB/T 7714-2015 著录（末尾句号）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「题名[N].报纸名,出版日期(版次).」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「题名[J].刊名,年,卷(期):页码.」。刊名取「出处：」行中的期刊名，已知为 {journal}；年从上述日期取；卷(期)从文件名或版权页提取，已知卷 {volume}、期 {issue}，无卷则只写期如 (4)；页码已知为 {pages}，著录起讫页如 :45-58。
       题名必须与上方「标题：」字段完全一致（即同一题名，不得改写、扩写或另取所谓“核心主题”），以契合史学研究对题名一致性的要求。
   - 标签：提炼 3-6 个主题词（人物、事件、机构、地点等），逗号分隔。
   - 正文：照抄原文「正文：」后内容，仅繁转简，其余不改。
4. 直接输出上述字段，不要其他内容。
"""


# 纯文本模式提示词：不沉淀到知识库，输出可直接复用的纯文本条目（标题/日期/作者/引用/正文）。
# 与「知识库模式」的区别：① 输出为 .txt 而非带 YAML frontmatter 的 _题录.md；
# ② 模型直接产出含正文的完整条目（不依赖本地 OpenCC 抽取正文）；③ 文件名加「结构化_」前缀区分 OCR 的 .txt。
# 占位符同 SYSTEM_PROMPT_SHORT（{date}/{page}/{pubyear}/{journal}/{volume}/{issue}/{pages}）。
SYSTEM_PROMPT_SHORT_PLAIN = """你负责对近代文献 OCR 转录文本做后置处理，输出为可直接复用的纯文本条目（不沉淀到知识库，用于日常存档与引用）。

要求：
1. 将全部内容转为简体中文（仅做繁→简字符转换，不增删改任何字词、标点与段落结构，保真优先）。
2. 严格按以下格式输出，字段名各占一行，顺序固定，不要附加任何说明、前言、代码围栏或结尾：

标题：<文章/篇名，尽量照原文提取>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方规则生成的 GB/T 7714 引用串>
<正文：将原文「正文：」后的内容原样转录为简体中文，保真连贯，不增删改；同栏内文字合并为一段、不逐行断；□ 占位无法识别的字>

3. 字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名，繁简均可，转换后会统一）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空（不要写「佚名」等占位）。
   - 引用：先判断文献类型，再严格按 GB/T 7714-2015 著录（末尾句号）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「题名[N].报纸名,出版日期(版次).」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「题名[J].刊名,年,卷(期):页码.」。刊名已知为 {journal}；年从上述日期取；卷(期)已知卷 {volume}、期 {issue}；页码已知为 {pages}，著录起讫页如 :45-58。
       题名必须与上方「标题：」字段完全一致（同一题名，不得改写、扩写或另取所谓“核心主题”）。
   - 正文：照抄原文「正文：」后内容，仅做繁→简字符转换，其余不改。

直接输出上述格式，不要其他内容。输出示例（虚构，仅作格式示范）：
标题：本市气候纪略
日期：1943-01-17
作者：本报气象组
引用：本市气候纪略[N].大公报,1943-01-17(2).
本埠入冬以来气温持续偏低，近日渐回暖，预计下周以晴间多云为主，风力不大。
"""



def parse_source(txt_path):
    """从文件名（=篇名，含 报纸名+日期+第X版 / 刊名+年卷期）提取题录线索。

    同时提取报纸与期刊两类线索（报纸文件里期刊字段自然为空、反之亦然），
    缺字段留空字符串，调用方据此填充提示词占位符（缺失占位符安全保留原样）。
    文献类型由模型按「出处：」行判断，无需在此区分。
    """
    base = os.path.splitext(os.path.basename(txt_path))[0]
    out = {}

    # 报纸线索：日期（月/日可为 1-2 位，兼容 「1949年7月31日」与「1949年07月31日」）+ 版次
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})", base)
    out["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    p = re.search(r"第(\d+)版", base)
    out["page"] = p.group(1) if p else ""

    # 期刊线索：出版年（兼容报纸文件名里的年，但期刊通常不写「年X月」）、卷(期)、页码
    y = re.search(r"(\d{4})", base)
    out["pubyear"] = y.group(1) if y else ""
    vol = re.search(r"(?:第\s*)?(\d+)(?:\s*卷)", base)
    iss = re.search(r"(?:第\s*)?(\d+)(?:\s*[期])", base)
    out["volume"] = vol.group(1) if vol else ""
    out["issue"] = iss.group(1) if iss else ""
    pg = re.search(r"(\d{1,4})\s*[-—]\s*(\d{1,4})\b", base)
    out["pages"] = (f"{pg.group(1)}-{pg.group(2)}" if pg else "")
    out["journal"] = ""  # 刊名优先从 OCR 出处行取，文件名难稳定提取，留空由模型补
    return out


# —— 一图切多篇：结构化后按题录标题重命名子目录（去掉 _框N 占位）并回写出处 ——
RENAME_PAT = re.compile(r"^(.*)_框\d+$")     # 匹配「整版名_框N」
BANNED_CHARS = set('/\\:*?"<>|\r\n\t')        # Windows 文件名禁用字符
MAX_NAME_LEN = 80                             # 标题片段最大长度

# 纯文本模式：结构化输出 txt 的前缀，用于与同目录 OCR 的 <名>.txt 区分
PLAIN_PREFIX = "结构化_"


def clean_title(t):
    """清洗模型抽出的标题，使其可安全用作文件名片段。"""
    t = (t or "").strip()
    t = "".join(ch for ch in t if ch not in BANNED_CHARS)
    t = re.sub(r"\s+", " ", t).strip(" .。·")
    return t[:MAX_NAME_LEN]


def title_from_ref(ref):
    """从 GB/T 7714 引用串提取题名（[N]/[J] 之前的部分），作为题录 title 的唯一权威来源，
    确保 frontmatter 的 title 与 reference 中的题名逐字一致（史学研究严谨性要求）。

    引用串缺失或格式异常时返回空串，调用方回退到模型抽出的「标题：」字段。
    """
    if not ref:
        return ""
    m = re.match(r"^\s*(.+?)\s*\[(?:\w+)\]\.", ref)
    if not m:
        return ""
    t = m.group(1).strip()
    # 仅剥离最外层包裹引号（保留题名内部的引号，如事件名「七一」）
    OPEN, CLOSE = "\"“", "\"”"
    if t and t[0] in OPEN and t[-1] in CLOSE and OPEN.index(t[0]) == CLOSE.index(t[-1]):
        t = t[1:-1].strip()
    return t


def rename_by_title(txt_path, md_path, title):
    """
    若 txt 所在子目录名形如「整版名_框N」，则按题录标题重命名为「标题-整版名」，
    同步重命名目录内同名前缀文件（png/json/txt/_题录.md），并把 txt / md 里的
    旧目录名替换为新目录名（出处回写）。标题为空或目录名不含 _框N 则跳过。
    返回 (new_dir, renamed, msg)。
    """
    d = os.path.dirname(txt_path)
    base = os.path.basename(d)
    m = RENAME_PAT.match(base)
    if not m:
        return d, False, "目录名不含 _框N，跳过重命名"
    stem = clean_title(title)
    if not stem:
        return d, False, "标题为空，保留 _框N 占位"
    new_name = f"{stem}-{m.group(1)}"
    parent = os.path.dirname(d)
    nd = os.path.join(parent, new_name)
    i = 2
    while os.path.exists(nd):                 # 同名冲突加序号兜底
        nd = os.path.join(parent, f"{new_name}({i})")
        i += 1
    # 同步重命名目录内以旧基名开头的文件（png / json / txt / _题录.md）
    for fn in os.listdir(d):
        if fn.startswith(base + "_") or fn.startswith(base + "."):
            os.rename(os.path.join(d, fn), os.path.join(d, new_name + fn[len(base):]))
    os.rename(d, nd)
    # 出处回写：txt 首行「出处：{旧名}」与 md 内旧名引用 → 新名（幂等，无匹配则不动）
    new_txt = os.path.join(nd, os.path.basename(txt_path).replace(base, new_name, 1))
    new_md = os.path.join(nd, os.path.basename(md_path).replace(base, new_name, 1))
    for p in (new_txt, new_md):
        if os.path.exists(p):
            s = open(p, encoding="utf-8").read()
            ns = s.replace(base, new_name)
            if ns != s:
                open(p, "w", encoding="utf-8").write(ns)
    return nd, True, f"按标题重命名 → {new_name}"


def _read_ocr_time(root, name):
    """读某篇的 OCR 秒数（来自 <root>/.timing.json）；缺失返回 0。"""
    try:
        p = os.path.join(root, ".timing.json")
        if os.path.exists(p):
            data = json.load(open(p, encoding="utf-8"))
            return float(data.get(name, {}).get("ocr", 0) or 0)
    except Exception:
        pass
    return 0.0


def _pop_ocr_time(root, name):
    """结构化完成后，从计时文件移除该篇条目（避免陈旧数据残留）。"""
    try:
        p = os.path.join(root, ".timing.json")
        if os.path.exists(p):
            data = json.load(open(p, encoding="utf-8"))
            data.pop(name, None)
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def postprocess(txt_path, client, model, prompt_override=None, rename=True,
                timing_root=None, mode="kb"):
    t0 = time.time()
    text = open(txt_path, encoding="utf-8").read()
    src = parse_source(txt_path)

    # 提示词选择：
    #  - plain 模式（纯文本）：模型直接产出含正文的完整条目，走 SYSTEM_PROMPT_SHORT_PLAIN；
    #  - 否则自定义覆盖优先；本地 OpenCC 可用时走「抽字段」短提示词（正文本地繁转简），
    #    缺失 OpenCC 时回退「模型输出正文」长提示词。后两版均自适应报纸[N]/期刊[J]。
    if mode == "plain":
        prompt = prompt_override or SYSTEM_PROMPT_SHORT_PLAIN
        use_model_body = True
    elif prompt_override:
        prompt = prompt_override
        use_model_body = False
    elif HAS_OPENCC:
        prompt = SYSTEM_PROMPT_SHORT
        use_model_body = False
    else:
        prompt = SYSTEM_PROMPT_FULL
        use_model_body = True

    # 本地 OpenCC 可用 / 自定义提示词：正文直接从 OCR 原文抽取并繁转简，模型只抽字段
    if not use_model_body:
        m_body = re.search(r"正文：\s*\n?(.*)\Z", text, re.S)
        raw_body = m_body.group(1).strip() if m_body else ""
        body = to_simp(raw_body)
    else:
        body = ""

    # 填充占位符：覆盖所有类型可能出现的键，缺字段安全保留 {key} 原样
    fmt = dict(src)
    for k in ("date", "page", "publisher", "pubyear", "journal", "volume", "issue", "pages"):
        fmt.setdefault(k, "")
    fmt_fallback = {
        "date": "（请见转录文本出处行）",
        "page": "（请见文件名第X版）",
        "publisher": "（请见版权页/出处行）",
        "pubyear": "（请见文件名出版年）",
        "journal": "（请见出处行刊名）",
        "volume": "（请见文件名卷次）",
        "issue": "（请见文件名期次）",
        "pages": "（请见文件名页码）",
    }

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt.format_map(
                _SafeDict(**{k: (fmt.get(k) or fmt_fallback.get(k, "")) for k in fmt})),
            },
            {"role": "user", "content": text},
        ],
        # DeepSeek 官方端点关闭思考模式：字段为 thinking.type=disabled（非 Ark 私有的 enable_thinking）。
        # 不设置则默认开启且 effort=high，结构化会跑完整思维链（耗时 30s+）。
        extra_body={"thinking": {"type": "disabled"}},
        timeout=60,
    )
    content = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)

    # 解析模型输出字段（SHORT 模式无正文，FULL 模式有正文）
    m_title = re.search(r"^标题：\s*(.+)$", content, re.M)
    # 日期宽松匹配：YYYY / YYYY-MM / YYYY-MM-DD，兼容图书期刊只给年
    m_date = re.search(r"^日期：\s*(\d{4}(?:-\d{2}(?:-\d{2})?)?)", content, re.M)
    m_author = re.search(r"^作者：(.*)$", content, re.M)
    m_ref = re.search(r"^引用：\s*(.+)$", content, re.M)
    m_tags = re.search(r"^标签：\s*(.+)$", content, re.M)

    title = (m_title.group(1).strip() if m_title else
             os.path.splitext(os.path.basename(txt_path))[0])
    date_out = m_date.group(1) if m_date else src.get("date", "")
    author = m_author.group(1).strip() if m_author else ""
    ref = m_ref.group(1).strip() if m_ref else ""
    tags = [t.strip() for t in m_tags.group(1).split(",") if t.strip()] if m_tags else []

    if use_model_body:
        m_body2 = re.search(r"正文：\s*\n(.*)\Z", content, re.S)
        body = m_body2.group(1).strip() if m_body2 else ""

    # 统一繁转简（本地 OpenCC；缺失时依赖模型已输出的简体）
    title = to_simp(title)
    author = to_simp(author)
    ref = to_simp(ref)
    tags = [to_simp(t) for t in tags]

    # 题录一致性（史学研究严谨性）：title 必须与 reference 中的题名逐字一致。
    # 优先以引用串反提题名作为权威 title；引用缺失时回退模型「标题：」字段。
    ref_title = title_from_ref(ref)
    if ref_title:
        title = ref_title

    # 从引用字符串反提载体名（报纸=报纸名 / 期刊=刊名），用于 frontmatter 检索
    carrier = ""
    if ref:
        # 报纸：题名[N].报纸名,  期刊：题名[J].刊名,
        mk = re.search(r"\[(?:N|J)\]\.\s*([^,]+),", ref)
        if mk:
            carrier = mk.group(1).strip()

    tok = ""
    if usage:
        dur = round(time.time() - t0, 2)
        tok = f" token => prompt={usage.prompt_tokens} completion={usage.completion_tokens} total={usage.total_tokens} duration={dur}s"
        log_token("deepseek", os.path.splitext(os.path.basename(txt_path))[0], model,
                  usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, dur)

    if mode == "plain":
        # 纯文本模式：模型已输出完整条目（标题/日期/作者/引用/正文），
        # 直接落盘为「结构化_<名>.txt」，与同目录 OCR 的 <名>.txt 以前缀区分。
        c = content.strip()
        c = re.sub(r"^```[a-zA-Z]*\s*", "", c)          # 去掉可能的代码围栏
        c = re.sub(r"\s*```$", "", c).strip()
        m = re.search(r"标题：", c)                       # 从首个「标题：」起，丢弃可能存在的多余前言
        if m:
            c = c[m.start():]
        d = os.path.dirname(txt_path)
        base = os.path.splitext(os.path.basename(txt_path))[0]
        out_path = os.path.join(d, PLAIN_PREFIX + base + ".txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(c + "\n")
        print(f"done(plain): {os.path.basename(out_path)}{tok}")
    else:
        # 组装带 YAML frontmatter 的结构化纯文本 md（无图/json，归 Obsidian 知识库）
        # 自适应报纸/期刊：字段统一预留，空的留空；type 按引用串 [N]/[J] 自动判定。
        if "[J]" in ref:
            type_tag = "journal_ocr"
        else:
            type_tag = "newspaper_ocr"   # 默认报纸（无 [J] 即按报纸处理）
        yf = [
            "---",
            f'title: "{title}"',
            f'newspaper: "{carrier}"' if (type_tag == "newspaper_ocr" and carrier) else 'newspaper: ""',
            f'journal: "{carrier}"' if (type_tag == "journal_ocr" and carrier) else 'journal: ""',
            f"date: {date_out}",
            f"edition: {src.get('page', '')}" if (type_tag == "newspaper_ocr" and src.get("page")) else 'edition: ""',
            f'volume: "{src.get("volume", "")}"' if (type_tag == "journal_ocr" and src.get("volume")) else 'volume: ""',
            f'issue: "{src.get("issue", "")}"' if (type_tag == "journal_ocr" and src.get("issue")) else 'issue: ""',
            f'pages: "{src.get("pages", "")}"' if (type_tag == "journal_ocr" and src.get("pages")) else 'pages: ""',
            f'author: "{author}"' if author else 'author: ""',
            f'reference: "{ref}"',
            "tags: [" + ", ".join(tags) + "]",
            f"type: {type_tag}",
            "---",
            "",
            f"# {title}",
            "",
            "## 正文",
            "",
            body,
        ]
        out_path = os.path.splitext(txt_path)[0] + "_题录.md"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(yf).rstrip() + "\n")
        # 一图切多篇：按题录标题重命名（去 _框N 占位），并回写 txt/md 出处
        if rename:
            _nd, _rn, _msg = rename_by_title(txt_path, out_path, title)
            if _rn:
                print(f"  ↳ {_msg}")
        print(f"done: {os.path.basename(out_path)}{tok}")

    # —— 每篇总耗时计时：OCR 秒数（来自 <root>/.timing.json）+ 本篇结构化秒数 ——
    if timing_root:
        _name = os.path.splitext(os.path.basename(txt_path))[0]
        _struct = round(time.time() - t0, 2)
        _ocr = _read_ocr_time(timing_root, _name)
        _total = round(_ocr + _struct, 2)
        print(f"[计时] {_name} 本篇总耗时 {_total}s（OCR {_ocr}s + 结构化 {_struct}s）")
        _pop_ocr_time(timing_root, _name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="./民国报纸OCR", help="含各篇子目录 OCR txt 的大文件夹（递归查找）")
    ap.add_argument("--single", default=None, help="只处理指定篇（填 txt 文件名，含或不含 .txt 均可）")
    ap.add_argument("--post-mode", default="kb", choices=("kb", "plain"),
                    help="结构化模式：kb=知识库（_题录.md）；plain=纯文本（结构化_<名>.txt）")
    ap.add_argument("--prompt-post", default=None,
                    help="覆盖内置知识库题录提示词（kb 模式）；留空用内置默认")
    ap.add_argument("--prompt-post-plain", default=None,
                    help="覆盖内置纯文本提示词（plain 模式）；留空用内置默认")
    ap.add_argument("--no-rename", action="store_true",
                    help="关闭「按题录标题重命名 _框N 子目录 + 回写出处」（仅 kb 模式生效）")
    args = ap.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    if not HAS_OPENCC:
        print("⚠ 未检测到 opencc，已回退为「模型输出正文」模式，速度较慢、较费 token。")
        print("  请在本机执行：python -m pip install opencc-python-reimplemented  （用跑脚本的同个 python；注意 -reimplemented）")

    client = OpenAI(base_url=BASE_URL, api_key=api_key, timeout=60)

    # 递归收集待处理 OCR txt；排除已生成的「结构化_<名>.txt」（前置前缀）与隐藏文件、
    # _题录.md，避免纯文本模式产物被二次处理。
    def _collect(name=None):
        out = []
        for dp, _, fns in os.walk(args.root):
            for fn in fns:
                if not fn.endswith(".txt"):
                    continue
                if fn.startswith(".") or fn.startswith(PLAIN_PREFIX):
                    continue
                if fn.endswith("_题录.md"):
                    continue
                if name and os.path.splitext(fn)[0] != name:
                    continue
                out.append(os.path.join(dp, fn))
        return out

    if args.single:
        tps = _collect(os.path.splitext(args.single)[0])
        if not tps:
            sys.exit(f"未找到 txt：{args.single}")
    else:
        tps = _collect()
        tps.sort()

    for tp in tps:
        if STOP_EVENT.is_set():
            print("!! 已请求停止，结构化中止")
            break
        if args.post_mode == "plain":
            plain_path = os.path.join(os.path.dirname(tp),
                                     PLAIN_PREFIX + os.path.splitext(os.path.basename(tp))[0] + ".txt")
            if os.path.exists(plain_path):
                print(f"skip (已结构化): {os.path.basename(plain_path)}")
                continue
            postprocess(tp, client, model, prompt_override=args.prompt_post_plain,
                        rename=False, timing_root=args.root, mode="plain")
        else:
            md_path = os.path.splitext(tp)[0] + "_题录.md"
            if os.path.exists(md_path):
                print(f"skip (已后置): {os.path.basename(md_path)}")
                continue
            postprocess(tp, client, model, prompt_override=args.prompt_post,
                        rename=not args.no_rename, timing_root=args.root, mode="kb")


if __name__ == "__main__":
    main()
