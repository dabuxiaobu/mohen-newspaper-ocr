#!/usr/bin/env bash
# 墨痕 · macOS 源码模式启动器（等价于 Windows 的 run_box.bat）
# 用法：bash macOS/run_box.sh
set -e
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SKILL_DIR/scripts"

# 优先用技能内 venv（build_venv_mac），否则回退系统 python3
if [ -x "$SKILL_DIR/build_venv_mac/bin/python" ]; then
  PY="$SKILL_DIR/build_venv_mac/bin/python"
else
  PY=python3
fi

exec "$PY" box_launcher.py "$@"
