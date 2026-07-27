# -*- coding: utf-8 -*-
"""nolan_app.py / jarvis.py / bat 启动器 · 界面工程师_UI 自测脚本。

覆盖项：
    1. import nolan_app 不弹窗、不启动主循环（入口在 __main__ 守卫内）；
    2. 能创建 Tk 实例、挂接 NolanApp 组件后立刻 destroy，不报错；
    3. py_compile 全部本轮改动文件；
    4. 冒烟：pythonw 方式启动 nolan_app，3 秒内进程存活，随后 taskkill 干净结束。

存储安全：测试前后备份并还原 memory\\reminders.txt（若存在）。
"""

import os
import py_compile
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REMINDERS = os.path.join(HERE, "memory", "reminders.txt")

# pythonw 取当前解释器的同目录兄弟（python.exe ↔ pythonw.exe），
# 也可用环境变量 NOLAN_PYTHONW 显式覆盖。
PYTHONW = os.environ.get(
    "NOLAN_PYTHONW",
    os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
)

CHANGED_FILES = ["nolan_app.py", "jarvis.py", "selftest_ui.py"]


def backup_reminders() -> "str | None":
    """备份提醒存储文件，返回备份路径；文件不存在返回 None。"""
    if os.path.exists(REMINDERS):
        bak = REMINDERS + ".uitest.bak"
        shutil.copy2(REMINDERS, bak)
        return bak
    return None


def restore_reminders(bak: "str | None", original_existed: bool) -> None:
    """还原提醒存储：有备份则覆盖回去，原本不存在则删除测试产物。"""
    if bak and os.path.exists(bak):
        shutil.copy2(bak, REMINDERS)
        os.remove(bak)
    elif not original_existed and os.path.exists(REMINDERS):
        os.remove(REMINDERS)


def test_import_no_window() -> None:
    """import nolan_app 不弹窗、不进入主循环。"""
    sys.path.insert(0, HERE)
    import nolan_app  # noqa: F401
    assert callable(nolan_app.main)
    assert hasattr(nolan_app, "NolanApp")
    print("✅ 1. import nolan_app：无窗口、无主循环，契约符号齐全")


def test_tk_create_destroy() -> None:
    """创建 Tk 挂接 NolanApp 后立刻 destroy，不报错。"""
    import tkinter as tk
    import nolan_app

    root = tk.Tk()
    app = nolan_app.NolanApp(root)
    root.update()          # 让布局与首批 after 回调跑一拍
    app._on_close()        # 走正常关闭路径（守护线程标志位 + destroy）
    assert app._closed is True
    print("✅ 2. Tk 实例创建/挂接/立即销毁：无异常")


def test_py_compile() -> None:
    """py_compile 全部本轮改动文件。"""
    for name in CHANGED_FILES:
        py_compile.compile(os.path.join(HERE, name), doraise=True)
    print(f"✅ 3. py_compile 通过：{', '.join(CHANGED_FILES)}")


def test_smoke_pythonw() -> None:
    """冒烟：pythonw 启动 nolan_app，3 秒内进程存活，随后 taskkill。"""
    proc = subprocess.Popen(
        [PYTHONW, "nolan_app.py"],
        cwd=HERE,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    try:
        time.sleep(3)
        assert proc.poll() is None, f"pythonw 进程提前退出，返回码 {proc.returncode}"
        print("✅ 4. 冒烟：pythonw 启动 nolan_app，3 秒后进程仍存活")
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
        proc.wait(timeout=10)
    print("   进程已 taskkill 干净结束")


def main() -> None:
    bak = backup_reminders()
    original_existed = bak is not None
    try:
        test_import_no_window()
        test_tk_create_destroy()
        test_py_compile()
        test_smoke_pythonw()
    finally:
        restore_reminders(bak, original_existed)
    print("\n全部自测通过。")


if __name__ == "__main__":
    main()
