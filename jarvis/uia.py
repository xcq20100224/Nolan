# -*- coding: utf-8 -*-
"""
uia.py —— Nolan 的「元素树眼睛」（阶段二：UIA 控件树感知）

第一性原理：VLM 看截图是「像素级猜测」，Windows 自己就知道每个按钮叫什么、
在哪里——UI Automation（UIAutomationCore.dll）把这棵控件树直接交出来。
本模块用 comtypes 直调 UIA，给 eyes 提供「可交互控件清单」，
让「点播放按钮」从视觉猜测升级为按名字吸附到真实控件中心。

线程安全（重要）：UIA 是 COM 组件，控件对象跨线程传递会炸——
  * 所有公开函数内部各自 comtypes.CoInitialize() / CoUninitialize()
    （CoInitialize 幂等，重复调用安全）；
  * 控件对象（IUIAutomationElement）绝不跨调用缓存，每次调用重新枚举；
  * 因此本模块可从任意线程调用（eyes 主循环、测试线程、HTTP 工作线程）。

接口契约：
    dump_window_controls(hwnd_or_title=None, max_items=40) -> list[dict]
        每项 {name, control_type, rect:(x,y,w,h), enabled}
    format_controls(controls) -> str        紧凑文本化，拼进 VLM prompt
    find_element(controls, keyword) -> (x, y) | None   按名称子串找中心点
    snap_to_element(controls, x, y, threshold=24) -> (x, y)  坐标吸附到控件中心

零第三方新增依赖（comtypes 已在环境内）；任何异常返回空列表/原坐标并打印
一行中文警告，绝不抛给调用方——UIA 只是精度增强，没有它截图路径照常工作。
"""

import ctypes

import comtypes
import comtypes.client

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 交互型控件类型（UIA ControlType ID -> 中文标签）：
# 只收这些类型——容器/装饰类控件（Pane/Group/Image 等）对「点哪里」没有帮助
_CONTROL_TYPE_NAMES = {
    50000: "按钮",    # Button
    50002: "复选框",  # CheckBox
    50003: "下拉框",  # ComboBox
    50004: "编辑框",  # Edit
    50005: "链接",    # Hyperlink
    50007: "列表项",  # ListItem
    50011: "菜单项",  # MenuItem
    50019: "标签页",  # TabItem
    50020: "文本",    # Text
    50030: "文档区",  # Document
}

_WALK_MAX_DEPTH = 12     # 控件树递归深度上限（深层嵌套的装饰容器不值得走）
_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


# ---------------------------------------------------------------------------
# 窗口定位
# ---------------------------------------------------------------------------

def _foreground_hwnd() -> int:
    """当前前台窗口句柄；失败返回 0。"""
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _find_hwnd_by_title(title_substr: str) -> int:
    """按标题子串（大小写不敏感）找第一个可见顶层窗口句柄；找不到返回 0。"""
    needle = (title_substr or "").strip().lower()
    if not needle:
        return 0
    hits = []
    try:
        user32 = ctypes.windll.user32

        def _on_window(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if needle in (buf.value or "").lower():
                    hits.append(hwnd)
                    return False
            except Exception:
                pass
            return True

        user32.EnumWindows(_ENUM_PROC(_on_window), 0)
    except Exception:
        return 0
    return hits[0] if hits else 0


# ---------------------------------------------------------------------------
# 控件树枚举
# ---------------------------------------------------------------------------

def _collect(walker, element, depth, out, max_items):
    """深度优先递归收集交互型控件；控件对象只在本函数栈内使用，绝不缓存。"""
    if depth > _WALK_MAX_DEPTH or len(out) >= max_items:
        return
    child = None
    try:
        child = walker.GetFirstChildElement(element)
    except Exception:
        return
    while child is not None and len(out) < max_items:
        next_sibling = None
        try:
            ct = child.CurrentControlType
            if ct in _CONTROL_TYPE_NAMES:
                name = (child.CurrentName or "").strip()
                rect = child.CurrentBoundingRectangle
                w = max(0, int(rect.right) - int(rect.left))
                h = max(0, int(rect.bottom) - int(rect.top))
                # 无名且非编辑/文档区的控件对决策没价值（空白装饰文本等）
                if name or ct in (50004, 50030):
                    if w > 0 and h > 0:
                        out.append({
                            "name": name,
                            "control_type": _CONTROL_TYPE_NAMES[ct],
                            "rect": (int(rect.left), int(rect.top), w, h),
                            "enabled": bool(child.CurrentIsEnabled),
                        })
            _collect(walker, child, depth + 1, out, max_items)
            next_sibling = walker.GetNextSiblingElement(child)
        except Exception:
            # 单个控件读取失败（已销毁/无权限）不拖垮整棵树
            try:
                next_sibling = walker.GetNextSiblingElement(child)
            except Exception:
                break
        child = next_sibling


def dump_window_controls(hwnd_or_title=None, max_items: int = 40) -> list:
    """
    枚举目标窗口（默认前台窗口；int 按句柄、str 按标题子串）的可交互控件，
    返回 [{name, control_type, rect:(x,y,w,h), enabled}, ...]。
    交互型优先（按钮/编辑框/菜单项/列表项/标签页/链接/复选框/下拉框/文档区/文本），
    按面积从大到小排序后截断到 max_items——大控件通常是主交互区。
    任何异常（窗口无 UIA 支持、COM 失败、窗口已销毁）返回空列表并打印中文警告。
    """
    if max_items <= 0:
        return []
    comtypes.CoInitialize()  # 幂等：每次调用独立初始化，控件对象不跨调用缓存
    try:
        import comtypes.gen.UIAutomationClient as UIA

        if hwnd_or_title is None:
            hwnd = _foreground_hwnd()
        elif isinstance(hwnd_or_title, int):
            hwnd = hwnd_or_title
        else:
            hwnd = _find_hwnd_by_title(str(hwnd_or_title))
        if not hwnd:
            print("[uia] 未找到目标窗口（%r），返回空控件清单" % (hwnd_or_title,))
            return []

        core = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=UIA.IUIAutomation)
        root = core.ElementFromHandle(hwnd)
        walker = core.ControlViewWalker
        out: list = []
        _collect(walker, root, 0, out, max_items)
        out.sort(key=lambda c: c["rect"][2] * c["rect"][3], reverse=True)
        return out[:max_items]
    except Exception as exc:
        print("[uia] 控件树枚举失败（%s），返回空清单，截图路径不受影响" % exc)
        return []
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 文本化 / 查找 / 吸附
# ---------------------------------------------------------------------------

def _center(control: dict) -> tuple:
    x, y, w, h = control["rect"]
    return (x + w // 2, y + h // 2)


def format_controls(controls: list) -> str:
    """
    紧凑文本化控件清单：「按钮「播放」@(960,540)；编辑框「搜索」@(300,80)」。
    坐标为矩形中心；禁用的控件标注（禁用）。空清单返回空串。
    """
    parts = []
    for c in controls or []:
        cx, cy = _center(c)
        label = c.get("name") or "（无名）"
        suffix = "" if c.get("enabled", True) else "（禁用）"
        parts.append("%s「%s」@(%d,%d)%s" % (c.get("control_type", "控件"),
                                            label, cx, cy, suffix))
    return "；".join(parts)


def find_element(controls: list, keyword: str) -> tuple | None:
    """按名称子串（大小写不敏感）找第一个匹配控件的中心点；无匹配返回 None。"""
    needle = (keyword or "").strip().lower()
    if not needle:
        return None
    for c in controls or []:
        if needle in (c.get("name") or "").lower():
            return _center(c)
    return None


def snap_to_element(controls: list, x: float, y: float, threshold: float = 24) -> tuple:
    """
    坐标吸附：(x,y) 落在某控件矩形内、或距某控件中心 ≤ threshold 像素时，
    返回该控件中心（多个命中取面积最小者——最精确的那个）；
    否则原样返回 (x, y)。controls 为空时原样返回，行为与无 UIA 完全一致。
    """
    best = None
    best_area = None
    for c in controls or []:
        rx, ry, w, h = c["rect"]
        cx, cy = rx + w / 2, ry + h / 2
        inside = rx <= x <= rx + w and ry <= y <= ry + h
        near = (cx - x) ** 2 + (cy - y) ** 2 <= threshold ** 2
        if inside or near:
            area = w * h
            if best is None or area < best_area:
                best = (int(cx), int(cy))
                best_area = area
    return best if best is not None else (x, y)


# ---------------------------------------------------------------------------
# 模块自测（真机验证）：打开记事本，dump 前 10 个控件
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess
    import time

    print("[uia] 自测：打开记事本，枚举控件……")
    subprocess.Popen(["notepad.exe"])
    hwnd = 0
    for _ in range(15):  # 最多等 15 秒窗口出现
        time.sleep(1)
        hwnd = _find_hwnd_by_title("记事本") or _find_hwnd_by_title("Notepad")
        if hwnd:
            break
    try:
        if not hwnd:
            print("[uia] 未等到记事本窗口，自测失败")
            raise SystemExit(1)
        controls = dump_window_controls(hwnd)
        print("[uia] 共枚举到 %d 个交互控件，前 10 个：" % len(controls))
        for c in controls[:10]:
            cx, cy = _center(c)
            print("  %s「%s」@(%d,%d) 矩形=%s enabled=%s"
                  % (c["control_type"], c["name"] or "（无名）",
                     cx, cy, c["rect"], c["enabled"]))
        print("[uia] 文本化样例：%s" % format_controls(controls[:5]))
        raise SystemExit(0 if controls else 1)
    finally:
        subprocess.run(["taskkill", "/f", "/im", "notepad.exe"],
                       capture_output=True)
        print("[uia] 记事本已清理")
