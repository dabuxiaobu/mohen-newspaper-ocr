#!/usr/bin/env bash
# =====================================================================
# 墨痕 · macOS 构建脚本（onedir → .app → .dmg / .zip）
# ---------------------------------------------------------------------
# ★ 必须在 macOS 上运行（Windows 本机无法编译 Mac 二进制）。
# ★ 与 Windows 的 build_exe.bat 平行，互不干扰。
#
# 环境变量（均可选）：
#   MH_VERSION          版本号，默认 1.0.0（支持带/不带 v 前缀，脚本自动去 v）
#   MH_ARCH             架构后缀，默认按 uname -m 自动判断（arm64 / x86_64）
#   MACOS_SIGN_IDENTITY 若设置则对 .app 做 codesign（需本机已装对应证书）
#
# 步骤概览：
#   [1/6] 检查 python3
#   [2/6] 建独立 venv（build_venv_mac）
#   [3/6] 装依赖（pyinstaller pywebview numpy Pillow opencc ...）
#   [4/6] 用 PIL 把 icon/newspaper.ico 转 macOS 的 AppIcon.icns
#   [5/6] PyInstaller 打包 → dist/墨痕.app
#   [6/6] 生成 .dmg + .zip（绿色版）+ sha256
# =====================================================================
set -e

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SKILL_DIR"

APP_NAME="墨痕"

# 版本（去 v 前缀）
VERSION_RAW="${MH_VERSION:-1.0.0}"
VERSION="${VERSION_RAW#v}"

# 架构后缀
RAW_ARCH="${MH_ARCH:-$(uname -m)}"
case "$RAW_ARCH" in
  arm64|aarch64) ARCH_SUFFIX="arm64" ;;
  x86_64|amd64)  ARCH_SUFFIX="x86_64" ;;
  *) ARCH_SUFFIX="$RAW_ARCH" ;;
esac

MAC_DIR="$SKILL_DIR/macOS"
OUT_DIR="$SKILL_DIR/deploy/Output"
VENV="$SKILL_DIR/build_venv_mac"

echo "=== 墨痕 macOS 构建（arch=$ARCH_SUFFIX, version=$VERSION）==="

# [1/6] python3
echo "[1/6] 检查 python3"
python3 --version

# [2/6] venv（已存在则跳过创建）
echo "[2/6] 准备 venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# [3/6] 依赖
echo "[3/6] 安装依赖"
pip install -U pip >/dev/null 2>&1
pip install pyinstaller pywebview numpy Pillow opencc-python-reimplemented openai pypdf

# [4/6] .ico → .icns（PIL 原生支持 ICNS）
echo "[4/6] 生成 AppIcon.icns"
python - <<'PY'
from PIL import Image
import os
src = os.path.join("icon", "newspaper.ico")          # 相对 skill 根（脚本已 cd 到 skill 根）
dst = os.path.join("macOS", "AppIcon.icns")
im = Image.open(src)
im = im.convert("RGBA").resize((512, 512), Image.LANCZOS)   # macOS 常用大图标尺寸，保留 alpha
im.save(dst)
print("  ->", dst)
PY

# [5/6] PyInstaller
echo "[5/6] PyInstaller 打包（onedir → .app）"
pyinstaller "$MAC_DIR/box_tool_mac.spec" --noconfirm --clean

# [6/6] 打包 dmg / zip / sha256
echo "[6/6] 生成 .dmg / .zip / sha256"
mkdir -p "$OUT_DIR"

APP_PATH="dist/${APP_NAME}.app"
DMG="$OUT_DIR/${APP_NAME}-v${VERSION}-macos-${ARCH_SUFFIX}.dmg"
ZIP="$OUT_DIR/${APP_NAME}-v${VERSION}-macos-${ARCH_SUFFIX}.zip"

# 可选：代码签名（配置了 MACOS_SIGN_IDENTITY 才执行）
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  echo "  -> codesign (identity: $MACOS_SIGN_IDENTITY)"
  codesign --force --options runtime --sign "$MACOS_SIGN_IDENTITY" "$APP_PATH"
fi

# dmg
[ -f "$DMG" ] && rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG"

# zip 绿色版（保留 .app 作为顶层目录）
[ -f "$ZIP" ] && rm -f "$ZIP"
ditto -c -k --keepParent "$APP_PATH" "$ZIP"

# sha256 校验文件（文件名只保留 basename，方便朋友核对）
shasum -a 256 "$DMG" | awk -v f="$(basename "$DMG")" '{print $1"  "f}' > "$DMG.sha256.txt"
shasum -a 256 "$ZIP" | awk -v f="$(basename "$ZIP")" '{print $1"  "f}' > "$ZIP.sha256.txt"

echo ""
echo "完成 ✅"
echo "  .app : $APP_PATH"
echo "  .dmg : $DMG"
echo "  .zip : $ZIP"
echo "  sha  : $DMG.sha256.txt / $ZIP.sha256.txt"
echo ""
if [ -z "${MACOS_SIGN_IDENTITY:-}" ]; then
  echo "提示：未做代码签名，朋友首次打开需在『系统设置 → 隐私与安全性』里允许，或："
  echo "  xattr -dr com.apple.quarantine '$DMG'"
  echo "  （正式分发建议用 Apple Developer 证书 codesign + notarize）"
else
  echo "提示：已 codesign；如需免拦截/上架，建议再 notarize。"
fi
