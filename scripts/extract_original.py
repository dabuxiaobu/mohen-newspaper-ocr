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

   例外（回退）：少数扫描件使用 pypdf 不支持的编码（如 CCITT Group 4 传真压缩，
   报 "not enough image data"），pypdf 无法枚举内嵌图。此时按 300 DPI 用 pypdfium2
   （Chrome PDFium 引擎）整页渲染回退，分辨率与原扫描一致，不影响 OCR。
   正常「图包 PDF」仍走 pypdf 原始内嵌图路径，不被此回退影响。

   另一类例外（视觉编辑）：部分 PDF 经 WPS 等工具裁剪/旋转后得到，但内嵌图像
   字节未重新编码——表现为 CropBox 小于 MediaBox 或 /Rotate≠0。此时 pypdf 抽出的
   仍是未裁/未旋转的原始大图，与「保存后看到的样子」不符。对此类页面改用
   pypdfium2 按 CropBox 可见区（自动应用旋转）渲染，DPI 取内嵌原图原生分辨率，
   保证清晰度一致、且得到编辑后的正确版式。

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
import re
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


def _src_newer(src_path: str, out_path: str):
    """源文件是否比产物新（用户改过 PDF/图片）。是则返回 True，调用方应强制重抽覆盖，

    避免「改了 PDF 重新抽仍读旧产物」的困惑。0.5 秒容差防 mtime 精度边界。
    """
    try:
        return (os.path.getmtime(src_path) - os.path.getmtime(out_path)) > 0.5
    except Exception:
        return False


# pypdfium2 懒加载（仅当 pypdf 抽不出内嵌图时回退用；正常 PDF 不触发，避免无谓的 import 开销）
_PDFIUM = None
def _get_pdfium():
    global _PDFIUM
    if _PDFIUM is None:
        import pypdfium2 as _PDFIUM
    return _PDFIUM

def render_page_pdfium(pdf_path: str, page_num: int, dpi: int = 300):
    """用 pypdfium2（Chrome PDFium 引擎）把第 page_num 页按 dpi 整页渲染为 RGB 位图。

    仅作为 pypdf 抽内嵌图失败的回退：CCITT G4 等 pypdf 不支持的编码、
    或页内压根无内嵌图的文字型 PDF。300 DPI 下分辨率与原扫描件一致，不影响 OCR。
    """
    pdfium = _get_pdfium()
    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num - 1]
        bitmap = page.render(scale=dpi / 72.0)
        return bitmap.to_pil().convert("RGB")
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _page_is_edited(page):
    """检测页面是否被视觉编辑过（裁剪/CropBox 缩小 或 旋转）。

    此类页面内嵌原图字节未变，pypdf 抽出的仍是未裁/未旋转的原始大图，
    与用户「保存后看到的样子」不符，需改用 pdfium 按可见区渲染。
    """
    try:
        mb = page.mediabox
        cb = page.cropbox
        mw, mh = float(mb.width), float(mb.height)
        cw, ch = float(cb.width), float(cb.height)
        rot = int(page.get("/Rotate", 0) or 0)
        if rot in (90, 270):
            # 旋转后物理宽高互换，比较时一并互换
            mw, mh = mh, mw
        if abs(mw - cw) > 1 or abs(mh - ch) > 1:
            return True
        return rot != 0
    except Exception:
        return False


def _native_dpi(page, base_img):
    """按内嵌原图像素与 MediaBox 物理尺寸推算原生 DPI（考虑旋转），供 pdfium 等比渲染可见区。"""
    try:
        mb = page.mediabox
        mw, mh = float(mb.width), float(mb.height)
        rot = int(page.get("/Rotate", 0) or 0)
        if rot in (90, 270):
            mw, mh = mh, mw
        if mw > 0:
            return max(72, int(round((base_img.width / mw) * 72.0)))
    except Exception:
        pass
    return 300


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
        enum_err = None
        try:
            images = list(page.images)
        except Exception as e:
            enum_err = f"{os.path.basename(pdf_path)} page {i}: 页面图像枚举失败: {type(e).__name__}: {e}"
            _elog(f"  [warn] {enum_err}（将尝试 pdfium 整页渲染回退）")
        if not enum_err:
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
        # pypdf 抽不到内嵌图（枚举失败 或 页内无图）→ 用 pypdfium2 整页渲染回退
        if best is None:
            try:
                best = render_page_pdfium(pdf_path, i, dpi=300)
                if best is not None:
                    if enum_err:
                        _elog(f"  [pdfium 回退] {os.path.basename(pdf_path)} page {i}: 内嵌图枚举失败，改用整页渲染（{best.width}x{best.height}）")
                    else:
                        _elog(f"  [pdfium 回退] {os.path.basename(pdf_path)} page {i}: 页内无内嵌图，改用整页渲染（{best.width}x{best.height}）")
            except Exception as e:
                if enum_err:
                    errors.append(enum_err)
                    _elog(f"  [skip page] {enum_err}")
                else:
                    msg = f"{os.path.basename(pdf_path)} page {i}: 无内嵌图且 pdfium 渲染失败: {type(e).__name__}: {e}"
                    errors.append(msg)
                    _elog(f"  [skip page] {msg}")
                continue
        # 页面被裁剪/旋转（WPS 等编辑后内嵌图未变）→ 内嵌原图不是「保存后的样子」，
        # 必须按 CropBox 可见区（pdfium 自动应用旋转）渲染，DPI 取内嵌原图原生分辨率。
        if _page_is_edited(page):
            try:
                dpi = _native_dpi(page, best) if best is not None else 300
                best = render_page_pdfium(pdf_path, i, dpi=dpi)
                if best is not None:
                    _elog(f"  [可见区渲染] {os.path.basename(pdf_path)} page {i}: 页面被裁剪/旋转，按原图分辨率渲染可见区（{best.width}x{best.height}）")
            except Exception as e:
                if enum_err:
                    errors.append(enum_err)
                    _elog(f"  [skip page] {enum_err}")
                else:
                    msg = f"{os.path.basename(pdf_path)} page {i}: 页面被裁剪/旋转，可见区渲染失败: {type(e).__name__}: {e}"
                    errors.append(msg)
                    _elog(f"  [skip page] {msg}")
                continue
        else:
            # 未编辑：走 pypdf 内嵌原图；抽不到（枚举失败/页内无图）→ pdfium 整页渲染回退
            if best is None:
                try:
                    best = render_page_pdfium(pdf_path, i, dpi=300)
                    if best is not None:
                        if enum_err:
                            _elog(f"  [pdfium 回退] {os.path.basename(pdf_path)} page {i}: 内嵌图枚举失败，改用整页渲染（{best.width}x{best.height}）")
                        else:
                            _elog(f"  [pdfium 回退] {os.path.basename(pdf_path)} page {i}: 页内无内嵌图，改用整页渲染（{best.width}x{best.height}）")
                except Exception as e:
                    if enum_err:
                        errors.append(enum_err)
                        _elog(f"  [skip page] {enum_err}")
                    else:
                        msg = f"{os.path.basename(pdf_path)} page {i}: 无内嵌图且 pdfium 渲染失败: {type(e).__name__}: {e}"
                        errors.append(msg)
                        _elog(f"  [skip page] {msg}")
                    continue
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
    # 维护抽图产物顺序索引（按导入/抽图先后），供前端翻页按序展示
    _ch_order_path = os.path.join(DST_DIR, ".order.json")
    _ch_order = []
    _ch_order_broken = False
    if os.path.isfile(_ch_order_path):
        try:
            _ch_order = json.load(open(_ch_order_path, encoding="utf-8")).get("order", [])
        except Exception:
            _ch_order = []
            _ch_order_broken = True

    # 文件名自然排序 key：`xx_p123.png` 按页码数字比较，避免 p1,p10,p2 字典序乱序
    def _natk(f):
        m = re.search(r"_p(\d+)", f)
        return (0, int(m.group(1)), f) if m else (1, 0, f)

    # 若无顺序索引（旧数据首次重抽 / 索引损坏），按自然排序垫底，避免旧文件顺序丢失
    if not _ch_order:
        _ch_order = sorted((f for f in os.listdir(DST_DIR)
                            if os.path.splitext(f)[1].lower() in IMG_EXTS), key=_natk)
    # 每次运行重置日志文件（打包态 stdout 被丢弃，此文件是与启动器/用户对齐的关键通道）
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as _f:
            _f.write(time.strftime("[%Y-%m-%d %H:%M:%S] === 抽图开始 ===\n"))
    except Exception:
        pass
    if _ch_order_broken:
        _elog("[warn] 既有 .order.json 解析失败（空文件或损坏），本次按自然排序垫底并重建")

    if not os.path.isdir(SRC_DIR):
        print(f"[empty] 来源文件夹不存在：{SRC_DIR}")
        print("请新建该文件夹（或 --src 指定），把要抽图的数据库文章 PDF / 图片放进去，再点「① 抽图」。")
        sys.exit(0)

    _cands = [f for f in os.listdir(SRC_DIR)
              if os.path.splitext(f)[1].lower() in IMG_EXTS | {".pdf"}]
    # 优先按导入顺序索引（.import_order.json）排序，使抽图/翻页顺序 = 导入先后
    _io = os.path.join(SRC_DIR, ".import_order.json")
    _import_order = []
    if os.path.isfile(_io):
        try:
            _import_order = json.load(open(_io, encoding="utf-8")).get("order", [])
        except Exception:
            _import_order = []
    files = sorted(_cands, key=lambda f: (0, _import_order.index(f)) if f in _import_order else (1, f))
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
        if os.path.exists(out) and not _src_newer(src, out):
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
                    if os.path.exists(out) and not _src_newer(src, out):
                        skipped += 1
                        print(f"{name[:30]:32} [{kind}] page {pnum} [skip 已存在] {os.path.basename(out)}")
                        continue
                    w0, h0 = im.size
                    cropped = autocrop(im)
                    w1, h1 = cropped.size
                    cropped.save(out)
                    _bn = os.path.basename(out)
                    if _bn not in _ch_order:
                        _ch_order.append(_bn)
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
                _bn = os.path.basename(out)
                if _bn not in _ch_order:
                    _ch_order.append(_bn)
                print(f"{name[:30]:32} [{kind}] 原图 {w0}x{h0} -> 裁切 {w1}x{h1} "
                      f"(留 {100*w1*h1/(w0*h0):.0f}%)")
        except Exception as e:
            _elog(f"[skip] {name}: {e!r}")
    # 写回抽图产物顺序索引（仅追加本次新写出，已存在的保留原序）
    # 原子写：先写临时文件再 os.replace，避免写中断留下空/半截索引（曾致列表永久字典序乱序）
    try:
        _tmp = _ch_order_path + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            json.dump({"order": _ch_order}, _f, ensure_ascii=False, indent=2)
        os.replace(_tmp, _ch_order_path)
    except Exception:
        pass
    if skipped:
        print(f"[info] 已跳过 {skipped} 个已存在")


if __name__ == "__main__":
    main()
