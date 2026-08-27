"""跨模块共享的「停止」事件。

- 开发态 / 打包态均复用同一个 threading.Event；
- launcher_web.py 在「停止」按钮被点击时 set()；
- 各子脚本（抽图/分组/OCR/结构化）在主循环里轮询，set 后于当前单元处理完即中止；
- 子脚本独立用 `python xxx.py` 运行时事件永远是 clear 状态，无副作用。
"""
import threading

STOP_EVENT = threading.Event()
