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
from openai import OpenAI, APIStatusError, APIConnectionError, RateLimitError
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


def _translate_api_error(e):
    """把 openai 抛出的模型调用异常翻成中文友好提示，便于普通用户定位。

    仅做关键词识别，匹配不到时回退原始报错，绝不吞掉信息。
    """
    status = getattr(e, "status_code", None)
    d = (getattr(e, "message", None) or str(e) or "").lower()
    if (status == 403 or "forbidden" in d or "free quota" in d or "freeter" in d
            or "free tier" in d or "allocationquota" in d or "quota" in d):
        return ("API 额度耗尽或账号处于「仅免费额度」模式：当前 API Key / 账号的免费额度已用完，"
                "请求被服务商拒绝（HTTP 403）。\n"
                "解决办法（任选其一）：\n"
                "  1. 到对应平台控制台充值 / 绑卡；\n"
                "  2. 关闭「仅使用免费额度（use free tier only）」开关；\n"
                "  3. 更换仍有额度的 API Key 或模型后重试。")
    if status == 401 or "unauthorized" in d or "invalid api key" in d or "authentication" in d:
        return ("API Key 无效或已过期：请检查设置中填写的 API Key 是否正确、是否仍有有效额度，"
                "确认后重新保存再试。")
    if status == 429 or "rate limit" in d or "too many requests" in d:
        return ("触发限流（请求过于频繁，HTTP 429）：请稍候再试；若经常触发，"
                "可降低并发或换用额度更高的账号。")
    if status in (502, 503, 504) or "bad gateway" in d or "service unavailable" in d or "timeout" in d:
        return "模型服务暂时不可用（HTTP %s）：请稍后重试；若持续，可能是服务商侧故障。" % status
    if "connection" in d or "connect" in d:
        return "无法连接模型服务（网络/地址错误）：请检查 API Base URL 与网络后重试。"
    return "模型调用失败：" + (getattr(e, "message", None) or str(e))[:400]

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

def to_simp(s, keep_traditional=False):
    """繁体转简体；keep_traditional=True 时原样返回（保留繁体）；无 OpenCC 时原样返回（依赖模型已输出简体）"""
    if keep_traditional or CC is None or not s:
        return s
    return CC.convert(s)


# 句末收束标点：行尾命中其一视为段落/条目边界，保留换行；否则视为排版断行需合并
_SENT_END = tuple("。！？…；：”』」）】》?!;:")
def merge_broken_lines(s):
    """合并框缘几何截断导致的栏内误断行（如「无出其右」后换行接「者」）。

    规则：上一行行尾不是句末标点、且中间无空行时，把下一行拼接到上一行。
    空白行（段落分隔）不跨接；不处理标题/题录等结构行（调用方只对本文字段调用）。
    """
    if not s:
        return s
    out = []
    for ln in s.split("\n"):
        t = ln.strip()
        if not t:
            out.append("")
            continue
        if out and out[-1] != "" and not out[-1].endswith(_SENT_END):
            out[-1] = out[-1] + t
        else:
            out.append(t)
    return "\n".join(out)


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
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者.题名[N].报纸名,出版日期(版次).」（无作者署名时省略「作者.」前缀）。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「题名[J].刊名,年,卷(期):页码.」。刊名取「出处：」行中的期刊名，已知为 {journal}；年从上述日期取；卷(期)从文件名或版权页提取，已知卷 {volume}、期 {issue}，无卷则只写期如 (4)；页码已知为 {pages}，著录起讫页如 :45-58。
       题名必须与上方「标题：」字段完全一致（即同一题名，不得改写、扩写或另取所谓“核心主题”），以契合史学研究对题名一致性的要求。
   - 标签：提炼 3-6 个主题词（人物、事件、组织、地点等），逗号分隔，用于知识库检索与聚合。

直接输出上述五个字段，不要其他内容，也不要输出正文。
"""

# 《历史研究》格式提示词：与 GB/T 7714 并列，切换引用格式时使用。著录遵循《历史研究》注释规范：
# 不标 [N]/[J] 类型标识、不标 DOI、作者后全角冒号、再引「同上」。三套对应 SHORT（抽字段）/ FULL（回退）/ PLAIN（纯文本）。
SYSTEM_PROMPT_SHORT_HISTORY = """你负责对近代文献 OCR 转录文本抽取题录元字段（不做繁简转换，转换在本地完成）。

要求：
严格按以下字段顺序输出，字段名各占一行，不要附加任何说明、前言或结尾：
标题：<文章/篇名，尽量照原文提取>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方《历史研究》规范生成的引用串>
标签：<3-6 个主题词，逗号分隔，涵盖人物/事件/组织/地点>

字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名；尽量照原文，保留原标题用字，繁简均可，转换后会统一）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空（不要写「佚名」等占位）。
   - 引用：先判断文献类型，再严格按《历史研究》注释规范著录（末尾句号，不标 [N]/[J] 类型标识、不标 DOI）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者：《篇名》，《报纸名》出版日期，第X版。」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「作者：《篇名》，《刊名》年年期。」。刊名取「出处：」行中的期刊名，已知为 {journal}；年从上述日期取；卷(期)从文件名或版权页提取，已知卷 {volume}、期 {issue}，无卷则只写期如 (4)。
       题名必须与上方「标题：」字段完全一致（即同一题名，不得改写、扩写或另取所谓“核心主题”），以契合史学研究对题名一致性的要求。
   - 标签：提炼 3-6 个主题词（人物、事件、组织、地点等），逗号分隔，用于知识库检索与聚合。

直接输出上述五个字段，不要其他内容，也不要输出正文。
"""

# 回退提示词《历史研究》版（本地 OpenCC 缺失时）：模型承担全文繁转简与正文重写（慢、费 token）
SYSTEM_PROMPT_FULL_HISTORY = """你负责对近代文献 OCR 转录文本做后置处理。

要求：
1. 将全部内容的繁体转为简体中文；仅做繁→简字符转换，不增删改任何字词、标点与段落结构（保真优先）。
2. 严格按以下字段顺序输出，字段名各占一行，不要附加任何说明、前言或结尾：
标题：<文章/篇名，简体>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方《历史研究》规范生成的引用串>
标签：<3-6 个主题词，逗号分隔，涵盖人物/事件/机构/地点>
正文：
<将原文「正文：」后的内容繁转简后的简体文本，保真连贯>
3. 字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名，简体）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空。
   - 引用：先判断文献类型，再严格按《历史研究》注释规范著录（末尾句号，不标 [N]/[J] 类型标识、不标 DOI）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者：《篇名》，《报纸名》出版日期，第X版。」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「作者：《篇名》，《刊名》年年期。」。刊名取「出处：」行中的期刊名，已知为 {journal}；年从上述日期取；卷(期)从文件名或版权页提取，已知卷 {volume}、期 {issue}，无卷则只写期如 (4)。
       题名必须与上方「标题：」字段完全一致（即同一题名，不得改写、扩写或另取所谓“核心主题”），以契合史学研究对题名一致性的要求。
   - 标签：提炼 3-6 个主题词（人物、事件、机构、地点等），逗号分隔。
   - 正文：照抄原文「正文：」后内容，仅繁转简，其余不改。
4. 直接输出上述字段，不要其他内容。
"""

# 纯文本模式《历史研究》版提示词：输出可直接复用的纯文本条目（标题/日期/作者/引用/正文），引用按《历史研究》规范。
SYSTEM_PROMPT_SHORT_PLAIN_HISTORY = """你负责对近代文献 OCR 转录文本做后置处理，输出为可直接复用的纯文本条目（不沉淀到知识库，用于日常存档与引用）。

要求：
1. 将全部内容转为简体中文（仅做繁→简字符转换，不增删改任何字词、标点与段落结构，保真优先）。
2. 严格按以下格式输出，字段名各占一行，顺序固定，不要附加任何说明、前言、代码围栏或结尾：

标题：<文章/篇名，尽量照原文提取>
日期：<YYYY-MM-DD 或 出版年 YYYY>
作者：<署名；无署名则留空>
引用：<按下方《历史研究》规范生成的引用串>
<正文：将原文「正文：」后的内容原样转录为简体中文，保真连贯，不增删改；保留原文空行分段（段落之间空一行），仅合并同一段落内、行尾无句末标点的逐行断行；□ 占位无法识别的字>

3. 字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名，繁简均可，转换后会统一）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空（不要写「佚名」等占位）。
   - 引用：先判断文献类型，再严格按《历史研究》注释规范著录（末尾句号，不标 [N]/[J] 类型标识、不标 DOI）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者：《篇名》，《报纸名》出版日期，第X版。」。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版。
       · 期刊（出处含「刊名」「卷」「期」等）：样式「作者：《篇名》，《刊名》年年期。」。刊名已知为 {journal}；年从上述日期取；卷(期)已知卷 {volume}、期 {issue}。
       题名必须与上方「标题：」字段完全一致（同一题名，不得改写、扩写或另取所谓“核心主题”）。
   - 正文：照抄原文「正文：」后内容，仅做繁→简字符转换，其余不改。

直接输出上述格式，不要其他内容。输出示例（虚构，仅作格式示范）：
标题：本市气候纪略
日期：1943-01-17
作者：本报气象组
引用：本报气象组：《本市气候纪略》，《大公报》1943年1月17日，第2版。
本埠入冬以来气温持续偏低，近日渐回暖，预计下周以晴间多云为主，风力不大。
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
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者.题名[N].报纸名,出版日期(版次).」（无作者署名时省略「作者.」前缀）。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
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
<正文：将原文「正文：」后的内容原样转录为简体中文，保真连贯，不增删改；保留原文空行分段（段落之间空一行），仅合并同一段落内、行尾无句末标点的逐行断行；□ 占位无法识别的字>

3. 字段取值规则：
   - 标题：取文章正式题名（即引用著录所用题名，繁简均可，转换后会统一）。
   - 日期：出版日期。报纸已知为 {date}（YYYY-MM-DD）；期刊/图书优先从「出处：」行或文件名提取出版年 {pubyear}（格式化为 YYYY，有月份可写 YYYY-MM）。
   - 作者：取正文署名；无署名则留空（不要写「佚名」等占位）。
   - 引用：先判断文献类型，再严格按 GB/T 7714-2015 著录（末尾句号）：
       · 报纸（出处含「第X版」「报纸名」等）：样式「作者.题名[N].报纸名,出版日期(版次).」（无作者署名时省略「作者.」前缀）。报纸名取「出处：」行中的报纸名；出版日期已知为 {date}；版次取自文件名「第X版」，已知为第 {page} 版，著录为圆括号括起的版次，如 (4)。
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
                timing_root=None, mode="kb",
                src_name=None, src_date=None, src_page=None,
                citation_format="gb7714", keep_traditional=False):
    t0 = time.time()
    text = open(txt_path, encoding="utf-8").read()
    src = parse_source(txt_path)

    # 框缘截断导致的栏内误断行：送模型前先把 OCR 正文段合并，纯文本模式模型拿到已合并文本、输出更干净
    m_body_full = re.search(r"(正文：\s*\n?)(.*)\Z", text, re.S)
    if m_body_full:
        text = text[:m_body_full.start()] + m_body_full.group(1) + merge_broken_lines(m_body_full.group(2))

    # 来源补充覆盖：用户通过启动器在「导入文件名未携带题录信息」时手动补充。
    # 仅非空字段生效，覆盖 parse_source 从文件名抽取的结果（或空）；未填则保留原逻辑。
    if src_name:
        src["journal"] = src_name          # 期刊刊名占位符 {journal}；报纸名由启动器写入「出处：」行供模型读取
    if src_date:
        src["date"] = src_date
    if src_page:
        src["page"] = src_page

    # 提示词选择：
    #  - plain 模式（纯文本）：模型直接产出含正文的完整条目，走 对应格式 的 PLAIN 提示词；
    #  - 否则自定义覆盖优先；本地 OpenCC 可用时走「抽字段」短提示词（正文本地繁转简），
    #    缺失 OpenCC 时回退「模型输出正文」长提示词。各版均自适应报纸/期刊。
    #  - 引用格式 citation_format：gb7714（默认，GB/T 7714-2015）或 history_research（《历史研究》规范）。
    #    用户自定义 prompt_override 优先于格式切换（覆盖一切）。
    if mode == "plain":
        if prompt_override:
            # 用户自定义提示词优先（可能含正文要求），正文由模型产出，保留旧行为
            prompt = prompt_override
            use_model_body = True
        elif not HAS_OPENCC:
            # 本地无 OpenCC：正文仍由模型产出（无法本地抽正文），走完整 PLAIN 提示词
            if citation_format == "history_research":
                prompt = SYSTEM_PROMPT_SHORT_PLAIN_HISTORY
            else:
                prompt = SYSTEM_PROMPT_SHORT_PLAIN
            use_model_body = True
        else:
            # 本地有 OpenCC：正文改走本地抽取（与 kb 模式一致），模型只抽字段，
            # 分段/断行/繁简全部确定性，正文与 kb 模式逐字一致，且省约 90% 模型输出 token
            if citation_format == "history_research":
                prompt = SYSTEM_PROMPT_SHORT_HISTORY
            else:
                prompt = SYSTEM_PROMPT_SHORT
            use_model_body = False
    elif prompt_override:
        prompt = prompt_override
        use_model_body = False
    elif citation_format == "history_research":
        prompt = SYSTEM_PROMPT_SHORT_HISTORY if HAS_OPENCC else SYSTEM_PROMPT_FULL_HISTORY
        use_model_body = not HAS_OPENCC
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
        raw_body = merge_broken_lines(raw_body)   # 兜底合并框缘断行
        body = to_simp(raw_body, keep_traditional)
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

    try:
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
    except (APIStatusError, APIConnectionError, RateLimitError) as e:
        # 额度耗尽 / Key 失效等常见故障翻成中文友好提示，原异常信息附后便于排查
        raise RuntimeError("[结构化] " + _translate_api_error(e))
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
    # 注意：正文 body 在「纯文本模式」与「OpenCC 缺失回退模式」下直接取自模型输出，
    # 必须在此统一转换，否则旧报刊文模型常输出繁体（於/於）残留。
    title = to_simp(title, keep_traditional)
    author = to_simp(author, keep_traditional)
    ref = to_simp(ref, keep_traditional)
    tags = [to_simp(t, keep_traditional) for t in tags]
    body = merge_broken_lines(body)   # 纯文本模式模型输出正文兜底合并框缘断行
    body = to_simp(body, keep_traditional)

    # 题录一致性（史学研究严谨性）：title 必须与 reference 中的题名逐字一致。
    # 优先以引用串反提题名作为权威 title；引用缺失时回退模型「标题：」字段。
    ref_title = title_from_ref(ref)
    if ref_title:
        # GB/T 7714 模板为「作者.题名[N]」样式，反提会得到「作者.题名」，
        # 需在与 author 字段匹配时剥离「作者.」前缀，避免作者被写入 title 字段。
        # 仅用 author 字段精确匹配，不盲切第一个点，以免误伤题名内部含点（如「论A.B」）。
        if author and ref.startswith(author + "."):
            stripped = ref_title[len(author) + 1:].strip()
            if stripped:
                ref_title = stripped
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
        d = os.path.dirname(txt_path)
        base = os.path.splitext(os.path.basename(txt_path))[0]
        out_path = os.path.join(d, PLAIN_PREFIX + base + ".txt")
        if use_model_body:
            # OpenCC 缺失回退路径：模型输出完整条目，整段繁转简后落盘（保留旧行为）
            c = content.strip()
            c = re.sub(r"^```[a-zA-Z]*\s*", "", c)          # 去掉可能的代码围栏
            c = re.sub(r"\s*```$", "", c).strip()
            m = re.search(r"标题：", c)                       # 从首个「标题：」起，丢弃可能存在的多余前言
            if m:
                c = c[m.start():]
            c = to_simp(c, keep_traditional)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(c + "\n")
        else:
            # 本地抽取正文（OpenCC 可用）：字段来自模型、正文来自本地 OCR 原文，
            # 分段/断行/繁简全部确定性，正文与 kb 模式逐字一致。
            # 各字段已在本函数上文统一 to_simp，无需再次整体转换。
            lines = [
                f"标题：{title}",
                f"日期：{date_out}",
                f"作者：{author}" if author else "作者：",
                f"引用：{ref}" if ref else "引用：",
                "",
                body,
            ]
            c = "\n".join(lines).rstrip() + "\n"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(c)
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


# —— 引用本地重算：手改作者/标题/日期后，按当前格式确定性重算引用串并写回结构化产物（不调模型）——
def _cn_date(s):
    """YYYY-MM-DD → YYYY年M月D日（历史研究格式用）；非标准原样返回。"""
    if not s:
        return ""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    return s


def _extract_ocr_meta(ocr_text, name):
    """从 OCR 源 txt 抽手改字段：标题/作者（字段行），报纸名/日期/版次（出处：行）。

    注意：用 [^\\n]* 而非 .+?，避免 \\s 吞换行导致空字段（如 kb 模式「作者：」行为空）
    把下一行正文误捕为字段值。
    """
    title0 = ""; author0 = ""
    m = re.search(r"标题：[ \t]*([^\n]*)", ocr_text)
    if m and m.group(1).strip():
        title0 = m.group(1).strip()
    m = re.search(r"作者：[ \t]*([^\n]*)", ocr_text)
    if m and m.group(1).strip():
        author0 = m.group(1).strip()
    np0 = ""; dt0 = ""; ed0 = ""
    mc = re.search(r"出处：[ \t]*([^\n]*)", ocr_text)
    if mc:
        line = mc.group(1).strip()
        # 报纸名优先取《》包裹部分（kb 模式出处行把题名也写在同一行，需用《》剥离）
        nm = re.search(r"[《「]([^》」]+)[》」]", line)
        np0 = nm.group(1).strip() if nm else line.split("，")[0].split(",")[0].strip()
        dm = re.search(r"(\d{4})[-年./](\d{1,2})[-月./](\d{1,2})", line)
        if dm:
            dt0 = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        em = re.search(r"第\s*(\d+)\s*版", line)
        if em:
            ed0 = em.group(1)
    return title0, author0, np0, dt0, ed0


def _extract_journal_meta(ref, fmt):
    """从期刊引用串反提 刊名/年/卷/期/页码（plain 模式期刊字段未单独存盘时用）。

    返回 dict: {journal, year, volume, issue, pages}；解析不到的字段为空串。
    gb7714 期刊：作者.题名[J].刊名,年,卷(期):页码.
    《历史研究》期刊：作者：《篇名》，《刊名》年期，页码。
    """
    res = {"journal": "", "year": "", "volume": "", "issue": "", "pages": ""}
    if not ref:
        return res
    if fmt == "history_research":
        all_md = re.findall(r"《([^》]+)》", ref)
        if len(all_md) >= 2:
            res["journal"] = all_md[1]          # 第二个《》为刊名（第一个是篇名）
        elif all_md:
            res["journal"] = all_md[0]
        my = re.search(r"(\d{4})年", ref)
        if my:
            res["year"] = my.group(1)
        mv = re.search(r"年([^，。]+?)，", ref)   # 年期片段
        if mv:
            seg = mv.group(1)
            vm = re.search(r"(\d+)\s*卷", seg)
            im = re.search(r"(\d+)\s*期", seg)
            if vm:
                res["volume"] = vm.group(1)
            if im:
                res["issue"] = im.group(1)
        mp = re.search(r"[，。]第?\s*([\d\-—]+)\s*页", ref)
        if mp:
            res["pages"] = mp.group(1)
    else:
        mj = re.search(r"\[J\]\.\s*([^,]+),", ref)
        if mj:
            res["journal"] = mj.group(1).strip()
        my = re.search(r",\s*(\d{4}),", ref)
        if my:
            res["year"] = my.group(1)
        mv = re.search(r",\s*\d{4},\s*([^:]+?):", ref)   # 卷(期) 片段
        if mv:
            seg = mv.group(1).strip()
            vm = re.search(r"(\d+)\s*\(", seg)
            im = re.search(r"\(\s*(\d+)\s*\)", seg)
            if vm:
                res["volume"] = vm.group(1)
            if im:
                res["issue"] = im.group(1)
            elif re.search(r"^\s*(\d+)\s*$", seg):
                res["issue"] = seg.strip()        # 无卷仅期，如 (4) 已含括号被 im 命中；此处兜底纯数字
        mp = re.search(r":\s*([\d\-—]+)\.", ref)
        if mp:
            res["pages"] = mp.group(1)
    return res


def _read_plain_struct(path):
    """读 plain 结构化产物（结构化_<名>.txt）头部字段，作兜底。"""
    t = open(path, encoding="utf-8").read()
    d = {}
    for key in ("标题", "作者", "日期", "引用"):
        m = re.search(r"^" + key + r"：\s*(.*)$", t, re.M)
        d[key] = m.group(1).strip() if m else ""
    ref = d.get("引用", "")
    mm = re.search(r"\[N\]\.\s*([^,]+),", ref)
    d["newspaper"] = mm.group(1).strip() if mm else ""
    mm = re.search(r"\((\d+)\)", ref)
    d["edition"] = mm.group(1) if mm else ""
    return d


def _read_kb_struct(path):
    """读 kb 结构化产物（<名>_题录.md）frontmatter 标量字段，作兜底。"""
    t = open(path, encoding="utf-8").read()
    d = {}
    for key in ("title", "author", "date", "edition", "newspaper",
                "journal", "volume", "issue", "pages", "type", "reference"):
        m = re.search(rf"^{key}:\s*(.*)$", t, re.M)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            d[key] = v
        else:
            d[key] = ""
    return d


def build_ref(title, author, carrier_type, carrier_name, date, edition,
              volume, issue, pages, fmt, kt):
    """按格式确定性拼引用串。字段任一为空则省略对应部分；题为空返回空串。

    carrier_type: 'newspaper' / 'journal'；carrier_name: 报名 / 刊名。
    期刊字段 volume/issue/pages 仅期刊模式使用（gb7714 套 [J]，历史研究不标类型）。
    """
    title = to_simp(title or "", kt)
    author = to_simp(author or "", kt)
    carrier_name = to_simp(carrier_name or "", kt)
    if not title:
        return ""
    if carrier_type == "journal":
        return _build_journal_ref(title, author, carrier_name, date,
                                 volume, issue, pages, fmt, kt)
    # 报纸
    if fmt == "history_research":
        au = (author + "：") if author else ""
        dt = _cn_date(date)
        ed = ("，第" + str(edition) + "版") if edition else ""
        return f"{au}《{title}》，《{carrier_name}》{dt}{ed}。"
    au = (author + ".") if author else ""
    ed = ("(" + str(edition) + ")") if edition else ""
    return f"{au}{title}[N].{carrier_name},{date}{ed}."


def _build_journal_ref(title, author, journal, date, volume, issue, pages, fmt, kt):
    """期刊引用：gb7714 套「作者.题名[J].刊名,年,卷(期):页码.」；
    《历史研究》套「作者：《篇名》，《刊名》年期。」（不标 [J] 类型）。"""
    year = (date or "")[:4]
    if fmt == "history_research":
        au = (author + "：") if author else ""
        yvi = year
        if volume and issue:
            yvi = f"{year}年{volume}卷{issue}期"
        elif issue:
            yvi = f"{year}年{issue}期"
        elif volume:
            yvi = f"{year}年{volume}卷"
        pg = ("，第" + pages + "页") if pages else ""
        return f"{au}《{title}》，《{journal}》{yvi}{pg}。"
    au = (author + ".") if author else ""
    vi = ""
    if volume and issue:
        vi = f"{volume}({issue})"
    elif issue:
        vi = f"({issue})"
    elif volume:
        vi = f"{volume}"
    pg = (":" + pages) if pages else ""
    return f"{au}{title}[J].{journal},{year},{vi}{pg}."


def _write_plain_ref(path, title, author, date, ref):
    t = open(path, encoding="utf-8").read()
    t = re.sub(r"^标题：.*$", "标题：" + title, t, flags=re.M)
    t = re.sub(r"^作者：.*$", "作者：" + author, t, flags=re.M)
    t = re.sub(r"^日期：.*$", "日期：" + date, t, flags=re.M)
    t = re.sub(r"^引用：.*$", "引用：" + ref, t, flags=re.M)
    open(path, "w", encoding="utf-8").write(t)


def _write_kb_ref(path, title, author, date, edition, newspaper, ref):
    t = open(path, encoding="utf-8").read()
    for key, val in (("reference", ref), ("title", title), ("author", author),
                     ("date", date), ("edition", edition), ("newspaper", newspaper)):
        v = '""' if not val else ('"' + val.replace('"', "'") + '"')
        t = re.sub(rf"^{key}:\s*.*$", f"{key}: {v}", t, flags=re.M)
    open(path, "w", encoding="utf-8").write(t)


def rebuild_ref(txt_path, fmt="gb7714", kt=False):
    """手改作者/标题/日期后本地重算引用串并写回结构化产物（不调模型）。

    返回 JSON 友好的 dict：{"ok": True, "ref": ..., "mode": ..., "path": ...}
    或 {"ok": False, "error": ...}。结构化产物可能不在 txt_path 同目录
    （单页模式 OCR 平铺于 output/ 根，结构化产物在 plain_text/<名>/），
    故以 txt_path 所在目录为根递归搜索匹配 source_name 的产物。
    """
    if not os.path.isfile(txt_path):
        return {"ok": False, "error": "未找到 OCR 文本：" + os.path.basename(txt_path)}
    name = os.path.splitext(os.path.basename(txt_path))[0]
    ocr_text = open(txt_path, encoding="utf-8").read()
    title0, author0, np0, dt0, ed0 = _extract_ocr_meta(ocr_text, name)

    out_root = os.path.dirname(txt_path)
    plain_path = None
    kb_path = None
    for dp, _, fns in os.walk(out_root):
        for fn in fns:
            if fn == ("结构化_" + name + ".txt"):
                plain_path = os.path.join(dp, fn)
            elif fn == (name + "_题录.md"):
                kb_path = os.path.join(dp, fn)
    if plain_path and not kb_path:
        mode = "plain"
    elif kb_path and not plain_path:
        mode = "kb"
    elif plain_path and kb_path:
        mode = "plain"
    else:
        return {"ok": False, "error": "未结构化（找不到 结构化_%s.txt 或 %s_题录.md）" % (name, name)}

    if mode == "plain":
        struct = _read_plain_struct(plain_path)
    else:
        struct = _read_kb_struct(kb_path)

    ps = parse_source(name)
    date = ps["date"] or dt0 or struct.get("日期", struct.get("date", ""))
    edition = ps["page"] or ed0 or struct.get("edition", "")
    ref0 = struct.get("reference", struct.get("引用", ""))
    # 载体类型判定（模型原输出已正确，重算只换题名/作者，类型保留）：
    #   1) 优先 kb frontmatter 的 type 字段（权威）；
    #   2) 否则从引用串特征判定：gb7714 看 [J]/[N]；《历史研究》无类型标识，期刊含「期」、报纸含「版」。
    ctype = struct.get("type", "")
    if ctype == "journal_ocr":
        carrier_type = "journal"
    elif ctype == "newspaper_ocr":
        carrier_type = "newspaper"
    elif "[J]" in ref0:
        carrier_type = "journal"
    elif "[N]" in ref0:
        carrier_type = "newspaper"
    elif "期" in ref0 and "版" not in ref0:
        carrier_type = "journal"
    elif "版" in ref0:
        carrier_type = "newspaper"
    else:
        carrier_type = "newspaper"   # 默认报纸
    if carrier_type == "journal":
        # 期刊字段
        if mode == "kb":
            journal = struct.get("journal", "")
            volume = struct.get("volume", "")
            issue = struct.get("issue", "")
            pages = struct.get("pages", "")
        else:
            # plain 模式期刊字段未单独存盘，从现有引用串解析
            jm = _extract_journal_meta(ref0, fmt)
            journal = jm["journal"] or np0
            volume = jm["volume"]
            issue = jm["issue"]
            pages = jm["pages"]
            if not date:
                date = jm["year"]
        carrier_name = journal or np0
        newspaper_for_write = ""   # 期刊不改写 newspaper 字段（frontmatter 用 journal）
    else:
        carrier_name = np0 or struct.get("newspaper", "")
        journal = volume = issue = pages = ""
        newspaper_for_write = carrier_name
        # 版次兜底：frontmatter 为空时，从现有引用串的 (X) 提取，避免丢失原引用里的版次
        if not edition:
            rm = re.search(r"\((\d+)\)", ref0)
            if rm:
                edition = rm.group(1)
    title = title0 or struct.get("标题", struct.get("title", ""))
    author = author0 or struct.get("作者", struct.get("author", ""))

    ref = build_ref(title, author, carrier_type, carrier_name, date, edition,
                   volume, issue, pages, fmt, kt)
    if mode == "plain":
        _write_plain_ref(plain_path, title, author, date, ref)
        target = plain_path
    else:
        _write_kb_ref(kb_path, title, author, date, edition, newspaper_for_write, ref)
        target = kb_path
    return {"ok": True, "ref": ref, "mode": mode, "path": target}


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
    # 来源补充：用户在文件名未携带题录信息时手动补充（载体名称/出版日期/版次）。
    # 仅在非空时覆盖 parse_source 从文件名抽取的结果，不填则完全走原逻辑（向后兼容）。
    ap.add_argument("--src-name", default=None,
                    help="来源补充·载体名称（报纸名/刊名）；覆盖 journal 占位符，并在启动器已把名称写入「出处：」行时供模型读取")
    ap.add_argument("--src-date", default=None,
                    help="来源补充·出版日期 YYYY-MM-DD；覆盖 date 占位符")
    ap.add_argument("--src-page", default=None,
                    help="来源补充·版次（数字即可）；覆盖 page 占位符（即 frontmatter 的 edition）")
    ap.add_argument("--citation-format", default="gb7714", choices=("gb7714", "history_research"),
                    help="引用格式：gb7714=GB/T 7714-2015（默认）；history_research=《历史研究》注释规范")
    ap.add_argument("--keep-traditional", action="store_true",
                    help="保留繁体（不做本地繁→简转换），默认关闭（即输出简体）")
    # 引用本地重算：手改作者/标题/日期后，按当前格式确定性重算引用串并写回结构化产物（不调模型）
    ap.add_argument("--rebuild-ref", default=None,
                    help="指定 OCR txt 路径，本地重算其引用串并写回结构化产物后退出")
    args = ap.parse_args()

    if args.rebuild_ref:
        # 本地计算，无需 DEEPSEEK_API_KEY；直接复用 rebuild_ref 并打印 JSON 结果
        r = rebuild_ref(args.rebuild_ref, args.citation_format, args.keep_traditional)
        print(json.dumps(r, ensure_ascii=False))
        return

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
                        rename=False, timing_root=args.root, mode="plain",
                        src_name=args.src_name, src_date=args.src_date, src_page=args.src_page,
                        citation_format=args.citation_format, keep_traditional=args.keep_traditional)
        else:
            md_path = os.path.splitext(tp)[0] + "_题录.md"
            if os.path.exists(md_path):
                print(f"skip (已后置): {os.path.basename(md_path)}")
                continue
            postprocess(tp, client, model, prompt_override=args.prompt_post,
                        rename=not args.no_rename, timing_root=args.root, mode="kb",
                        src_name=args.src_name, src_date=args.src_date, src_page=args.src_page,
                        citation_format=args.citation_format, keep_traditional=args.keep_traditional)


if __name__ == "__main__":
    main()
