# -*- coding: utf-8 -*-
"""
从指定文件夹的数据库文章 PDF / 图片，统一 autocrop 去灰/白边后输出到 cropped_hi/，供 ③ X-AnyLabeling 标注。
- PDF：用 pypdf 抽每页面积最大的内嵌原图（保留数据库原始扫描分辨率），再裁边；
- 图片（png/jpg/jpeg/webp/bmp/tif/tiff/gif）：直接打开裁边。
扫描 SRC_DIR 下的 *.pdf 与常见图片格式；输出到 DST_DIR(cropped_hi)。

⚠️ 严禁改用 fitz/PyMuPDF 的 page.get_pixmap() / render 低 DPI 路径：
   数据库导出的 PDF 是「图包 PDF」，每页只有一个图片对象，
   用 PyMuPDF render 出来的 A4 灰底图是低分辨率副本，会导致后续 OCR 退化。
   必须用 pypdf 抽 page.images 里的原始 PIL 对象，保留数据库原始扫描分辨率。

autocrop 阈值：diff > 28（与背景色 RGB max 差），margin 相对长边 0.003%
（原固定 12px 在高分辨率下偏小，按相对比例更稳）

用法：
  python extract_original.py                 # 处理 HERE/source/ 下的 *.pdf 与图片 -> cropped_hi/
  python extract_original.py --src D:/某批   # 指定别的来源文件夹
"""
import os
import sys
import time
import threading
import numpy as np
from PIL import Image
from pypdf import PdfReader

# stop_flag 用于跨进程停止信号；打包后 subprocess 跑本脚本时 _MEIPASS 未必注入
# 子进程 sys.path，import 可能失败。失败则就地用本地 Event 兜底，保证脚本不崩。
try:
    from stop_flag import STOP_EVENT
except Exception:
    STOP_EVENT = threading.Event()

# 子脚本落点须与启动器的数据目录对齐：启动器会把 RUNTIME_DIR 注入 MOHEN_DATA_DIR。
# 打包态下若仍用 sys.executable 推导目录，产物会落到 exe 目录而非「文档/墨痕数据」，
# 导致启动器下一步去 DATA_DIR 读 cropped_hi/ 时抛出 WinError 3。
_MOHEN_DATA = os.environ.get("MOHEN_DATA_DIR")
if _MOHEN_DATA and os.path.isdir(_MOHEN_DATA):
    HERE = _MOHEN_DATA
elif getattr(sys, "frozen", False):
    # 打包态（onedir）：exe 在 <应用根>/民国报纸OCR.exe，工作目录须落在应用根，
    # 否则会解析到 _internal/ 而读不到 source/、写不到 cropped_hi/。
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR_DEFAULT = os.path.join(HERE, "source")   # 默认来源：脚本同目录的 source/
DST_DIR = os.path.join(HERE, "cropped_hi")       # 输出到 cropped_hi/，供阶段 ③ 标注
LOG_PATH = os.path.join(HERE, "extract_original.log")

# 打包态（windowed exe）下子进程 sys.stdout 被 PyInstaller 丢弃，print 全部消失，
# 启动器与用户都看不到 [no image]/异常原因。因此关键事件同步落一份日志文件，
# 启动器在 0 产出时读取该文件回传前端，便于定位（如 AES 加密 PDF 缺 cryptography）。
def _elog(msg: str):
    print(msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}  # 直接放入的图片格式


def extract_images_per_page(pdf_path: str):
    """从 PDF 每页抽出面积最大的内嵌图（数据库导出一般是单图全页）。

    返回 ([(page_num, pil_image), ...], [错误描述, ...])，保留所有页，以支持跨页文章。
    单页整体失败（如 AES 加密 PDF 缺 cryptography、图像编码不支持）只跳过该页并记录错误。
    """
    reader = PdfReader(pdf_path)
    out = []
    errors = []
    for i, page in enumerate(reader.pages, 1):
        best = None
        best_area = 0
        try:
            images = list(page.images)
        except Exception as e:
            msg = f"{os.path.basename(pdf_path)} page {i}: 页面图像枚举失败: {type(e).__name__}: {e}"
            errors.append(msg)
            _elog(f"  [skip page] {msg}")
            continue
        for im in images:
            try:
                pil = im.image
            except Exception as e:
                msg = f"{os.path.basename(pdf_path)} page {i} 图 {getattr(im, 'name', '?')}: 解码失败: {type(e).__name__}: {e}"
                errors.append(msg)
                _elog(f"  [skip image] {msg}")
                continue
            if pil is None:
                continue
            area = pil.width * pil.height
            if area > best_area:
                best_area = area
                best = pil.convert("RGB")
        if best:
            out.append((i, best))
    return out, errors


def autocrop(img, margin_ratio=0.003, max_side=1024):
    """四角采样背景色，与背景差异 > 28 的像素视为内容，按相对长边 0.3% 留 margin。

    大图优化：先在长边 <= max_side 的低分辨率副本上求内容边界框（保真不变，
    裁的是原图全分辨率像素），避免对整张图建 numpy 数组导致的 O(像素) 开销。
    """
    w, h = img.size
    scale = (max_side / max(w, h)) if max(w, h) > max_side else 1.0
    if scale < 1.0:
        sw_ = max(1, int(round(w * scale)))
        sh_ = max(1, int(round(h * scale)))
        small = img.resize((sw_, sh_), Image.BILINEAR)
        arr = np.array(small.convert("RGB"))
    else:
        arr = np.array(img.convert("RGB"))
    sh, sw, _ = arr.shape
    corners = np.array([arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]])
    bg = np.median(corners, axis=0)
    diff = np.abs(arr.astype(int) - bg.astype(int)).max(axis=2)
    mask = diff > 28
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return img
    margin = max(4, int(round(max(w, h) * margin_ratio)))
    sx, sy = w / sw, h / sh
    x0 = max(int(round(xs.min() * sx)) - margin, 0)
    x1 = min(int(round(xs.max() * sx)) + margin, w)
    y0 = max(int(round(ys.min() * sy)) - margin, 0)
    y1 = min(int(round(ys.max() * sy)) + margin, h)
    return img.crop((x0, y0, x1, y1))


def main():
    global DST_DIR
    # 允许命令行 --src / --dst 覆盖默认目录；启动器已把 MOHEN_DATA_DIR 注入环境变量。
    SRC_DIR = SRC_DIR_DEFAULT
    for i, a in enumerate(sys.argv):
        if a == "--src" and i + 1 < len(sys.argv):
            SRC_DIR = sys.argv[i + 1]
        if a == "--dst" and i + 1 < len(sys.argv):
            DST_DIR = sys.argv[i + 1]

    os.makedirs(DST_DIR, exist_ok=True)
    # 每次运行重置日志文件（打包态 stdout 被丢弃，此文件是与启动器/用户对齐的关键通道）
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as _f:
            _f.write(time.strftime("[%Y-%m-%d %H:%M:%S] === 抽图开始 ===\n"))
    except Exception:
        pass

    if not os.path.isdir(SRC_DIR):
        print(f"[empty] 来源文件夹不存在：{SRC_DIR}")
        print("请新建该文件夹（或 --src 指定），把要抽图的数据库文章 PDF / 图片放进去，再点「① 抽图」。")
        sys.exit(0)

    files = sorted(f for f in os.listdir(SRC_DIR)
                   if os.path.splitext(f)[1].lower() in IMG_EXTS | {".pdf"})
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    imgs = [f for f in files if not f.lower().endswith(".pdf")]
    if not files:
        print(f"[empty] 未在 {SRC_DIR} 找到 PDF 或图片（支持 {', '.join(sorted(IMG_EXTS))}）")
        print("请把要处理的 PDF / 图片放进该文件夹，再点「① 抽图」。")
        sys.exit(0)

    print(f"来源：{SRC_DIR}（{len(pdfs)} 个 PDF + {len(imgs)} 张图片）-> 输出：{DST_DIR}（已存在则跳过）")
    _elog(f"来源：{SRC_DIR}（{len(pdfs)} 个 PDF + {len(imgs)} 张图片）")
    skipped = 0
    for name in files:
        if STOP_EVENT.is_set():
            print("!! 已请求停止，抽图中止")
            break
        src = os.path.join(SRC_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        out = os.path.join(DST_DIR, os.path.splitext(name)[0] + ".png")
        if os.path.exists(out):
            skipped += 1
            print(f"{name[:30]:32} [skip 已存在] {os.path.basename(out)}")
            continue
        try:
            if ext == ".pdf":
                pages, errs = extract_images_per_page(src)
                if not pages:
                    _elog(f"[no image] {name}"
                          + (f"（{len(errs)} 条页级错误，详见上文）" if errs else "（未发现内嵌图片，可能是文字型 PDF）"))
                    continue
                kind = "PDF"
                for pnum, im in pages:
                    out = os.path.join(DST_DIR, f"{os.path.splitext(name)[0]}_p{pnum}.png")
                    if os.path.exists(out):
                        skipped += 1
                        print(f"{name[:30]:32} [{kind}] page {pnum} [skip 已存在] {os.path.basename(out)}")
                        continue
                    w0, h0 = im.size
                    cropped = autocrop(im)
                    w1, h1 = cropped.size
                    cropped.save(out)
                    print(f"{name[:30]:32} [{kind}] page {pnum} 原图 {w0}x{h0} -> 裁切 {w1}x{h1} "
                          f"(留 {100*w1*h1/(w0*h0):.0f}%)")
            else:
                im = Image.open(src).convert("RGB")
                kind = "图片"
                w0, h0 = im.size
                cropped = autocrop(im)
                w1, h1 = cropped.size
                out = os.path.join(DST_DIR, os.path.splitext(name)[0] + ".png")
                cropped.save(out)
                print(f"{name[:30]:32} [{kind}] 原图 {w0}x{h0} -> 裁切 {w1}x{h1} "
                      f"(留 {100*w1*h1/(w0*h0):.0f}%)")
        except Exception as e:
            _elog(f"[skip] {name}: {e!r}")
    if skipped:
        print(f"[info] 已跳过 {skipped} 个已存在")


if __name__ == "__main__":
    main()
