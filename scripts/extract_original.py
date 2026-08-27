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

if getattr(sys, "frozen", False):
    # 打包态（onedir）：exe 在 <应用根>/民国报纸OCR.exe，工作目录须落在应用根，
    # 否则会解析到 _internal/ 而读不到 source/、写不到 cropped_hi/。
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR_DEFAULT = os.path.join(HERE, "source")   # 默认来源：脚本同目录的 source/
DST_DIR = os.path.join(HERE, "cropped_hi")       # 输出到 cropped_hi/，供阶段 ③ 标注
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}  # 直接放入的图片格式


def extract_images_per_page(pdf_path: str):
    """从 PDF 每页抽出面积最大的内嵌图（数据库导出一般是单图全页）。

    返回 [(page_num, pil_image), ...]，保留所有页，以支持跨页文章。
    若某页无内嵌图则跳过该页。
    """
    reader = PdfReader(pdf_path)
    out = []
    for i, page in enumerate(reader.pages, 1):
        best = None
        best_area = 0
        for im in page.images:
            try:
                pil = im.image
            except Exception as e:
                print(f"  [skip image] {os.path.basename(pdf_path)} page {i}: {e!r}")
                continue
            if pil is None:
                continue
            area = pil.width * pil.height
            if area > best_area:
                best_area = area
                best = pil.convert("RGB")
        if best:
            out.append((i, best))
    return out


def autocrop(img, margin_ratio=0.003):
    """四角采样背景色，与背景差异 > 28 的像素视为内容，按相对长边 0.3% 留 margin。"""
    arr = np.array(img.convert("RGB"))
    h, w, _ = arr.shape
    corners = np.array([arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]])
    bg = np.median(corners, axis=0)
    diff = np.abs(arr.astype(int) - bg.astype(int)).max(axis=2)
    mask = diff > 28
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return img
    margin = max(4, int(round(max(w, h) * margin_ratio)))
    x0 = max(int(xs.min()) - margin, 0)
    x1 = min(int(xs.max()) + margin, w)
    y0 = max(int(ys.min()) - margin, 0)
    y1 = min(int(ys.max()) + margin, h)
    return img.crop((x0, y0, x1, y1))


def main():
    # 允许命令行 --src 覆盖来源文件夹
    SRC_DIR = SRC_DIR_DEFAULT
    for i, a in enumerate(sys.argv):
        if a == "--src" and i + 1 < len(sys.argv):
            SRC_DIR = sys.argv[i + 1]

    os.makedirs(DST_DIR, exist_ok=True)

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
                pages = extract_images_per_page(src)
                if not pages:
                    print("[no image]", name)
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
                    cropped.save(out, optimize=True)
                    print(f"{name[:30]:32} [{kind}] page {pnum} 原图 {w0}x{h0} -> 裁切 {w1}x{h1} "
                          f"(留 {100*w1*h1/(w0*h0):.0f}%)")
            else:
                im = Image.open(src).convert("RGB")
                kind = "图片"
                w0, h0 = im.size
                cropped = autocrop(im)
                w1, h1 = cropped.size
                out = os.path.join(DST_DIR, os.path.splitext(name)[0] + ".png")
                cropped.save(out, optimize=True)
                print(f"{name[:30]:32} [{kind}] 原图 {w0}x{h0} -> 裁切 {w1}x{h1} "
                      f"(留 {100*w1*h1/(w0*h0):.0f}%)")
        except Exception as e:
            print(f"[skip] {name}: {e!r}")
    if skipped:
        print(f"[info] 已跳过 {skipped} 个已存在")


if __name__ == "__main__":
    main()
