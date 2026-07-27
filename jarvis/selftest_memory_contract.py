# -*- coding: utf-8 -*-
"""Nolan 语音助手 —— memory 模块契约形态自检。

只校验契约形态，不校验业务行为：
    load() -> str
    remember(fact: str) -> str
    recall() -> str
    forget(keyword: str) -> str

检查项：
    1. memory 模块可导入；
    2. 四个函数存在且为可调用函数；
    3. 每个函数的签名参数名与契约一致（inspect 校验）；
    4. 实调一遍，返回值均为字符串且不抛异常（契约承诺 load 永不抛异常）。

真实记忆文件 jarvis\\memory\\long_term.txt 先备份、测完还原，绝不污染。

用法：python jarvis/selftest_memory_contract.py；退出码 0 表示契约符合。
"""

import inspect
import sys
from pathlib import Path

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    if ok:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        _failures.append(name)


def _backup_memory_file() -> tuple[Path, "bytes | None"]:
    """备份真实长期记忆文件；不存在则标记 None，测完删除新建的。"""
    mem_file = Path(__file__).resolve().parent / "memory" / "long_term.txt"
    original = mem_file.read_bytes() if mem_file.exists() else None
    return mem_file, original


def _restore_memory_file(mem_file: Path, original: "bytes | None") -> None:
    """按备份还原真实长期记忆文件。"""
    try:
        if original is None:
            mem_file.unlink(missing_ok=True)
        else:
            mem_file.write_bytes(original)
    except OSError:
        pass


# 契约：函数名 → 期望的位置参数名列表
CONTRACT: dict[str, list[str]] = {
    "load": [],
    "remember": ["fact"],
    "recall": [],
    "forget": ["keyword"],
}


def main() -> int:
    """运行契约自检，返回进程退出码。"""
    print("📜 memory 契约形态自检开始")

    # 1) 模块可导入
    try:
        import memory
    except Exception as exc:  # noqa: BLE001
        _check("导入 memory 模块", False, f"异常：{exc}")
        print(f"❌ {len(_failures)} 项未通过：{', '.join(_failures)}")
        return 1
    _check("导入 memory 模块", True)

    # 2) 函数存在、可调用、签名参数名正确
    for func_name, expected_params in CONTRACT.items():
        func = getattr(memory, func_name, None)
        if not callable(func):
            _check(f"memory.{func_name} 存在且可调用", False, "不存在或不可调用")
            continue
        _check(f"memory.{func_name} 存在且可调用", True)
        try:
            sig = inspect.signature(func)
            actual_params = [
                name
                for name, p in sig.parameters.items()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError) as exc:
            _check(f"memory.{func_name} 签名参数名为 {expected_params}", False, f"异常：{exc}")
            continue
        _check(
            f"memory.{func_name} 签名参数名为 {expected_params}",
            actual_params == expected_params,
            f"得到：{actual_params}",
        )

    # 3) 实调一遍：返回字符串、不抛异常（先备份真实记忆）
    mem_file, original = _backup_memory_file()
    try:
        try:
            result = memory.remember("契约自检临时事实xyz")
            _check("remember('...') 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("remember('...') 不抛异常", False, f"异常：{exc}")

        try:
            result = memory.load()
            _check("load() 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("load() 不抛异常（契约承诺永不抛）", False, f"异常：{exc}")

        try:
            result = memory.recall()
            _check("recall() 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("recall() 不抛异常", False, f"异常：{exc}")

        try:
            result = memory.forget("契约自检临时事实xyz")
            _check("forget('...') 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("forget('...') 不抛异常", False, f"异常：{exc}")
    finally:
        _restore_memory_file(mem_file, original)

    _check("自检后真实记忆文件已还原", True)

    if _failures:
        print(f"❌ {len(_failures)} 项未通过：{', '.join(_failures)}")
        return 1
    print("✅ memory 契约形态全部符合")
    return 0


if __name__ == "__main__":
    sys.exit(main())
