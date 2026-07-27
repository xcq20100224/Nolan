# -*- coding: utf-8 -*-
"""入口自测（入口工程师_Launcher）：jarvis.py 改名与唤醒词、--text 模式的静态验证。

只依赖 jarvis.py 本身，不触碰 ears/mouth/brain 的真实能力：
    1. import jarvis 不触发主循环；
    2. _strip_wake 唤醒词剥离行为正确；
    3. py_compile jarvis.py 通过。
"""

import py_compile
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_import_no_main_loop() -> None:
    """import jarvis 不触发主循环：在子进程中导入，应立刻干净退出。"""
    result = subprocess.run(
        [sys.executable, "-c", "import jarvis; print('IMPORT_OK')"],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"import jarvis 失败：{result.stderr}"
    assert "IMPORT_OK" in result.stdout, "import jarvis 未正常完成"
    # BANNER 只在 main() 里打印，import 时不应出现
    assert "Nolan 语音助手" not in result.stdout, "import 时疑似触发了主循环"
    print("✅ import jarvis 不触发主循环")


def test_strip_wake() -> None:
    """唤醒词剥离：Nolan（大小写不敏感）/诺兰 剥，贾维斯 不剥。"""
    sys.path.insert(0, str(HERE))
    import jarvis

    assert jarvis._strip_wake("Nolan现在几点") == "现在几点"
    assert jarvis._strip_wake("nolan，打开计算器") == "打开计算器"
    assert jarvis._strip_wake("诺兰你好") == "你好"
    assert jarvis._strip_wake("贾维斯你好") == "贾维斯你好"
    # 边界补充：大小写混合、只喊名字
    assert jarvis._strip_wake("NOLAN 今天天气") == "今天天气"
    assert jarvis._strip_wake("Nolan") == ""
    print("✅ _strip_wake 唤醒词剥离断言全部通过")


def test_py_compile() -> None:
    """py_compile jarvis.py 语法检查。"""
    py_compile.compile(str(HERE / "jarvis.py"), doraise=True)
    print("✅ py_compile jarvis.py 通过")


def test_rename_copy() -> None:
    """改名口径：用户可见文案为 Nolan，问候/道别正确。"""
    sys.path.insert(0, str(HERE))
    import jarvis

    assert jarvis.GREETING == "先生，Nolan 在线，请讲。"
    assert jarvis.FAREWELL == "先生，Nolan 已下线，再见。"
    assert "Nolan" in jarvis.BANNER and "贾维斯" not in jarvis.BANNER
    print("✅ 改名文案（BANNER/问候/道别）断言通过")


if __name__ == "__main__":
    test_import_no_main_loop()
    test_strip_wake()
    test_py_compile()
    test_rename_copy()
    print("\n🎉 selftest_entry 全部通过")
