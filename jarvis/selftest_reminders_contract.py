# -*- coding: utf-8 -*-
"""Nolan 语音助手 —— reminders 模块契约形态自检。

只校验契约形态，不校验业务行为：
    add(raw: str) -> str
    list_pending() -> str
    check_due() -> list[str]
    parse_time(text: str) -> datetime | None

检查项：
    1. reminders 模块可导入；
    2. 四个函数存在且为可调用函数；
    3. 每个函数的签名参数名与契约一致（inspect 校验）；
    4. 实调一遍做返回类型抽查（add/list_pending 返回 str、check_due 返回 list、
       parse_time 返回 datetime 或 None），全程不抛异常。

真实提醒文件 jarvis\\memory\\reminders.txt 先备份、测完还原，绝不污染。

用法：python jarvis/selftest_reminders_contract.py；退出码 0 表示契约符合。
"""

import inspect
import sys
from datetime import datetime
from pathlib import Path

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    if ok:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        _failures.append(name)


def _backup_reminders_file() -> tuple[Path, "bytes | None"]:
    """备份真实提醒文件；不存在则标记 None，测完删除新建的。"""
    rem_file = Path(__file__).resolve().parent / "memory" / "reminders.txt"
    original = rem_file.read_bytes() if rem_file.exists() else None
    return rem_file, original


def _restore_reminders_file(rem_file: Path, original: "bytes | None") -> None:
    """按备份还原真实提醒文件。"""
    try:
        if original is None:
            rem_file.unlink(missing_ok=True)
        else:
            rem_file.write_bytes(original)
    except OSError:
        pass


# 契约：函数名 → 期望的位置参数名列表
CONTRACT: dict[str, list[str]] = {
    "add": ["raw"],
    "list_pending": [],
    "check_due": [],
    "parse_time": ["text"],
}


def main() -> int:
    """运行契约自检，返回进程退出码。"""
    print("📜 reminders 契约形态自检开始")

    # 1) 模块可导入
    try:
        import reminders
    except Exception as exc:  # noqa: BLE001
        _check("导入 reminders 模块", False, f"异常：{exc}")
        print(f"❌ {len(_failures)} 项未通过：{', '.join(_failures)}")
        return 1
    _check("导入 reminders 模块", True)

    # 2) 函数存在、可调用、签名参数名正确
    for func_name, expected_params in CONTRACT.items():
        func = getattr(reminders, func_name, None)
        if not callable(func):
            _check(f"reminders.{func_name} 存在且可调用", False, "不存在或不可调用")
            continue
        _check(f"reminders.{func_name} 存在且可调用", True)
        try:
            sig = inspect.signature(func)
            actual_params = [
                name
                for name, p in sig.parameters.items()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError) as exc:
            _check(f"reminders.{func_name} 签名参数名为 {expected_params}", False, f"异常：{exc}")
            continue
        _check(
            f"reminders.{func_name} 签名参数名为 {expected_params}",
            actual_params == expected_params,
            f"得到：{actual_params}",
        )

    # 3) 实调一遍做返回类型抽查（先备份真实提醒文件）
    rem_file, original = _backup_reminders_file()
    try:
        try:
            result = reminders.add("一分钟后契约自检临时提醒xyz")
            _check("add('...') 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("add('...') 不抛异常", False, f"异常：{exc}")

        try:
            result = reminders.list_pending()
            _check("list_pending() 返回 str", isinstance(result, str), f"得到：{type(result).__name__}")
        except Exception as exc:  # noqa: BLE001
            _check("list_pending() 不抛异常", False, f"异常：{exc}")

        try:
            result = reminders.check_due()
            _check("check_due() 返回 list", isinstance(result, list), f"得到：{type(result).__name__}")
            if isinstance(result, list):
                _check(
                    "check_due() 返回 list[str]",
                    all(isinstance(item, str) for item in result),
                    f"得到：{[type(item).__name__ for item in result]}",
                )
        except Exception as exc:  # noqa: BLE001
            _check("check_due() 不抛异常", False, f"异常：{exc}")

        try:
            result = reminders.parse_time("十分钟后")
            _check(
                "parse_time('十分钟后') 返回 datetime 或 None",
                result is None or isinstance(result, datetime),
                f"得到：{type(result).__name__}",
            )
        except Exception as exc:  # noqa: BLE001
            _check("parse_time('十分钟后') 不抛异常", False, f"异常：{exc}")
    finally:
        _restore_reminders_file(rem_file, original)

    _check("自检后真实提醒文件已还原", True)

    if _failures:
        print(f"❌ {len(_failures)} 项未通过：{', '.join(_failures)}")
        return 1
    print("✅ reminders 契约形态全部符合")
    return 0


if __name__ == "__main__":
    sys.exit(main())
