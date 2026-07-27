# -*- coding: utf-8 -*-
"""hands 模块 open_app 契约形态自检（只查形态，不执行任何真实打开动作）。

校验两点：
    (a) hands.execute 签名保持契约：(name: str, args: dict) -> str；
    (b) hands.list_tools() 返回 11 个工具，且 open_app 的 description 含『VSCode』。

运行：python jarvis/selftest_openapp_contract.py  （退出码 0 表示通过）

注意：本文件不调用 hands.execute，因此不需要 monkeypatch os.startfile，
也不会触发任何应用启动，可在任何环境安全运行。
"""

import inspect
import sys

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    if ok:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        _failures.append(name)


def _test_execute_signature(hands: object) -> None:
    """(a) 校验 hands.execute 签名保持 (name: str, args: dict) -> str。"""
    print("🔍 校验 hands.execute 签名……")

    execute = getattr(hands, "execute", None)
    _check("hands.execute 存在", callable(execute))
    if not callable(execute):
        return

    try:
        sig = inspect.signature(execute)
    except (TypeError, ValueError) as exc:
        _check("hands.execute 可取签名", False, f"异常：{exc}")
        return

    params = list(sig.parameters.values())
    names = [p.name for p in params]
    _check(
        "execute 参数名为 (name, args) 且无多余必填参数",
        names[:2] == ["name", "args"] and len(params) == 2,
        f"实际签名：{sig}",
    )

    # 返回标注为 str 或未标注（契约允许隐式 str，但禁止明显改型）
    _check(
        "execute 返回标注为 str（或未标注）",
        sig.return_annotation in (str, "str", inspect.Signature.empty),
        f"实际返回标注：{sig.return_annotation!r}",
    )


def _test_list_tools(hands: object) -> None:
    """(b) 校验 list_tools() 返回 11 个工具，open_app 描述含『VSCode』。"""
    print("🔍 校验 hands.list_tools() 工具表……")

    list_tools = getattr(hands, "list_tools", None)
    _check("hands.list_tools 存在", callable(list_tools))
    if not callable(list_tools):
        return

    try:
        tools = list_tools()
    except Exception as exc:  # noqa: BLE001
        _check("list_tools() 调用不抛异常", False, f"异常：{exc}")
        return

    _check(
        "list_tools() 返回 11 个工具",
        isinstance(tools, list) and len(tools) == 11,
        f"实际数量：{len(tools) if isinstance(tools, list) else type(tools).__name__}",
    )
    if not isinstance(tools, list):
        return

    # 找到 open_app 工具条目（契约：每项为含 name/description 的 dict）
    open_app = next(
        (t for t in tools if isinstance(t, dict) and t.get("name") == "open_app"),
        None,
    )
    _check("工具表中含 open_app 条目", open_app is not None)
    if open_app is None:
        return

    description = open_app.get("description", "")
    _check(
        "open_app 的 description 含『VSCode』",
        isinstance(description, str) and "VSCode" in description,
        f"实际描述：{description!r}",
    )


def main() -> int:
    """运行全部契约自检，返回进程退出码。"""
    print("🧪 hands · open_app 契约形态自检开始")

    # hands 可能与其他模块并行开发中，导入失败单独记一条失败
    try:
        import hands
    except Exception as exc:  # noqa: BLE001
        _check("导入 hands 模块", False, f"异常：{exc}")
        print(f"❌ {len(_failures)} 项自检未通过：{', '.join(_failures)}")
        return 1
    _check("导入 hands 模块", True)

    _test_execute_signature(hands)
    _test_list_tools(hands)

    if _failures:
        print(f"❌ {len(_failures)} 项自检未通过：{', '.join(_failures)}")
        return 1
    print("✅ 全部契约自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
