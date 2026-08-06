# -*- coding: utf-8 -*-
"""
uia_wechat.py —— Nolan 的「UIA 通道」发文件到微信（L1.5 层）

定位：夹在 wechat_send 的 L1（技能库重放）与 L2（视觉猜坐标）之间。
不看屏幕、不猜坐标：所有目标控件都来自 Windows UI Automation 控件树，
坐标只取 UIA 返回的 BoundingRectangle 中心（控件自报家门，非视觉猜测）。

关键事实（真机探测结论，见 uia_wechat_dump*.txt）：
  - 微信 4.1.12.26 进程名 Weixin.exe，主窗口 class=Qt51514QWindowIcon、
    标题「微信」；登录窗 class=mmui::LoginWindow。
  - 微信的 MMUI 自绘界面默认不向 UIA 暴露控件树（树是秃的，只剩
    MMUIRenderSubWindowHW 空壳）。Windows 的「屏幕阅读器在场」标志
    （SPI_SETSCREENREADER）为真时，MMUI 才会装配完整无障碍树——
    本模块启动时会确保该标志已置位；若微信启动早于标志置位导致树仍是
    秃的，诚实返回「需要重启微信」，绝不瞎点。
  - 主窗口隐藏到托盘时 rect 为 0x0，ShowWindow 唤不醒；
    Ctrl+Alt+W 全局热键可靠唤起（真机验证）。

安全契约：
  - target 必须在搜索结果里精确命中（Name 全等），宁可失败也不模糊发送；
  - 成功判定必须基于 UIA 证据（消息区出现该文件名的卡片），
    不许「点了就报成功」；
  - send_file_via_uia 永不抛异常，一切意外归约为 ok=False 的人话。
"""

import ctypes
import os
import subprocess
import time

# 防御式导入：uiautomation 缺席时本层整体静默跳过（wechat_send 判 None）
try:
    import uiautomation as _auto
except Exception:  # noqa: BLE001
    _auto = None

_DEFAULT_TARGET = "文件传输助手"

# SPI 标志：Windows「屏幕阅读器在场」——MMUI 无障碍树的总开关
_SPI_GETSCREENREADER = 0x0046
_SPI_SETSCREENREADER = 0x0047
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02

# 虚拟键码
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_W = 0x57
_VK_F = 0x46
_VK_V = 0x56
_KEYEVENTF_KEYUP = 0x0002

# 真机确认的窗口 class（微信 4.1.12.26）
_MAIN_CLASS = "Qt51514QWindowIcon"
_LOGIN_CLASS = "mmui::LoginWindow"
_MAIN_TITLE = "微信"


def _result(ok, stage, detail):
    """统一返回结构，stage/detail 一律人话。"""
    return {"ok": bool(ok), "stage": stage, "detail": detail}


# ---------------------------------------------------------------------------
# 系统级辅助
# ---------------------------------------------------------------------------

def _screenreader_flag_on():
    """读取 Windows「屏幕阅读器在场」标志（MMUI 无障碍树的总开关）。"""
    try:
        val = ctypes.c_bool(False)
        ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETSCREENREADER, 0, ctypes.byref(val), 0)
        return bool(val.value)
    except Exception:  # noqa: BLE001
        return False


def _ensure_screenreader_flag():
    """
    确保「屏幕阅读器在场」标志置位（持久化到注册表并广播）。
    返回 True=本次新置位（已在运行的微信可能还是秃树，需重启才生效），
    False=原本就已置位。任何异常按「未新置位」处理，宁保守不误报。
    """
    try:
        if _screenreader_flag_on():
            return False
        ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETSCREENREADER, True, None,
            _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE)
        print("[uia_wechat] 已置位屏幕阅读器标志（无障碍树总开关）")
        return True
    except Exception as exc:  # noqa: BLE001
        print("[uia_wechat] 置位屏幕阅读器标志失败：%s" % exc)
        return False


def _hotkey(*vks):
    """注入全局组合键（不依赖前台焦点），用于 Ctrl+Alt+W / Ctrl+F / Ctrl+V。"""
    kb = ctypes.windll.user32.keybd_event
    for v in vks:
        kb(v, 0, 0, 0)
        time.sleep(0.06)
    for v in reversed(vks):
        kb(v, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


def _stage_file_to_clipboard(abs_path):
    """
    把文件本体放进系统剪贴板（PowerShell Set-Clipboard -LiteralPath），
    与 wechat_send 的登台逻辑同款，独立实现避免横向耦合。
    """
    try:
        quoted = "'" + abs_path.replace("'", "''") + "'"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Set-Clipboard -LiteralPath %s" % quoted],
            capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print("[uia_wechat] 文件登台剪贴板异常：%s" % exc)
        return False


def _stage_text_to_clipboard(text):
    """把纯文本放进剪贴板——中文联系人名走粘贴比逐键注入可靠得多。"""
    try:
        quoted = "'" + text.replace("'", "''") + "'"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Set-Clipboard -Value %s" % quoted],
            capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print("[uia_wechat] 文本登台剪贴板异常：%s" % exc)
        return False


# ---------------------------------------------------------------------------
# 控件树访问
# ---------------------------------------------------------------------------

def _find_main_window():
    """
    定位微信主窗口。返回 (状态, window)：
      ("ok", window)            主窗口在（可能托盘隐藏，交由唤起步骤处理）
      ("login", None)           只有登录窗——微信未登录
      ("absent", None)          微信进程/窗口都不在
    注意：托盘隐藏的微信主窗口是 SW_HIDE 状态，不进 UIA 桌面子树，
    WindowControl 搜不到——必须先用 win32 FindWindow 拿句柄（真机踩坑），
    再用 ControlFromHandle 包成 UIA 控件。
    """
    if _auto is None:
        return "absent", None
    win = _auto.WindowControl(
        searchDepth=1, ClassName=_MAIN_CLASS, Name=_MAIN_TITLE)
    if win.Exists(2):
        return "ok", win
    # 托盘隐藏兜底：win32 句柄 → UIA 控件
    try:
        import win32gui
        hwnd = win32gui.FindWindow(_MAIN_CLASS, _MAIN_TITLE)
        if hwnd:
            ctrl = _auto.ControlFromHandle(hwnd)
            if ctrl is not None:
                return "ok", ctrl
    except Exception as exc:  # noqa: BLE001
        print("[uia_wechat] win32 兜底查找异常：%s" % exc)
    login = _auto.WindowControl(searchDepth=1, ClassName=_LOGIN_CLASS)
    if login.Exists(1):
        return "login", None
    return "absent", None


def _window_really_visible(win):
    """窗口是否真在屏幕上：优先 win32 IsWindowVisible（UIA 的 rect 对
    SW_HIDE 窗口可能撒谎——真机见过隐藏窗口仍报完整 rect）。"""
    try:
        import win32gui
        hwnd = win.NativeWindowHandle
        if hwnd:
            return bool(win32gui.IsWindowVisible(hwnd)) and \
                not win32gui.IsIconic(hwnd)
    except Exception:  # noqa: BLE001
        pass
    try:
        rect = win.BoundingRectangle
        return rect.width() > 0 and rect.height() > 0
    except Exception:  # noqa: BLE001
        return False


def _ensure_window_visible(win):
    """
    托盘隐藏（SW_HIDE）的微信主窗口 ShowWindow 唤不醒；
    Ctrl+Alt+W 全局热键真机验证可靠。返回 True=窗口可见。
    """
    if _window_really_visible(win):
        return True
    _hotkey(_VK_CONTROL, _VK_MENU, _VK_W)
    time.sleep(2.0)
    return _window_really_visible(win)


def _tree_is_populated(win):
    """秃树检测：可见主窗口下若连一个 Edit/Button/List 后代都没有，
    说明 MMUI 无障碍树未装配（微信启动早于屏幕阅读器标志置位）。"""
    try:
        probe = win.EditControl(searchDepth=25)
        if probe.Exists(2):
            return True
        probe = win.ButtonControl(searchDepth=25)
        if probe.Exists(2):
            return True
        probe = win.ListControl(searchDepth=25)
        if probe.Exists(1):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _walk(win, max_depth=25, max_children=400):
    """递归遍历控件树，yield (depth, control)。异常即截断该分支。"""
    stack = [(win, 0)]
    while stack:
        ctrl, depth = stack.pop()
        yield depth, ctrl
        if depth >= max_depth:
            continue
        try:
            child = ctrl.GetFirstChildControl()
        except Exception:  # noqa: BLE001
            continue
        n = 0
        while child and n < max_children:
            stack.append((child, depth + 1))
            try:
                child = child.GetNextSiblingControl()
            except Exception:  # noqa: BLE001
                break
            n += 1


def _find_exact(win, name, control_type_names=None, timeout=6.0):
    """
    在控件树里找 Name 与 name 全等的控件（精确匹配，安全闸）。
    control_type_names 限定控件类型名集合（如 {"ListItemControl"}）。
    轮询等待搜索结果渲染，超时返回 None。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _depth, ctrl in _walk(win):
            try:
                if ctrl.Name != name:
                    continue
                if control_type_names and \
                        ctrl.ControlTypeName not in control_type_names:
                    continue
                rect = ctrl.BoundingRectangle
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                return ctrl
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.4)
    return None


def _find_contains(win, needle, control_type_names=None):
    """在控件树里找 Name 包含 needle 的可见控件（用于文件卡片证据）。"""
    for _depth, ctrl in _walk(win):
        try:
            if needle not in (ctrl.Name or ""):
                continue
            if control_type_names and \
                    ctrl.ControlTypeName not in control_type_names:
                continue
            rect = ctrl.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            return ctrl
        except Exception:  # noqa: BLE001
            continue
    return None


def _click_control(ctrl):
    """点击控件 UIA 自报矩形中心（坐标来自控件树，不是视觉猜测）。"""
    rect = ctrl.BoundingRectangle
    _auto.Click(rect.xcenter(), rect.ycenter())


def _find_send_dialog_button():
    """
    发送确认弹窗（「发送给 文件传输助手」）是独立顶层窗口，
    遍历桌面找其中的「发送」按钮。真机校准后锚定 selector。
    """
    root = _auto.GetRootControl()
    top = root.GetFirstChildControl()
    while top:
        try:
            cls = top.ClassName or ""
            if "mmui" in cls or "Qt" in cls or "Weixin" in cls:
                for _d, ctrl in _walk(top, max_depth=15):
                    try:
                        if ctrl.ControlTypeName == "ButtonControl" and \
                                (ctrl.Name or "").startswith("发送"):
                            return ctrl
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        try:
            top = top.GetNextSiblingControl()
        except Exception:  # noqa: BLE001
            break
    return None


# ---------------------------------------------------------------------------
# 公开接口（集成契约，签名冻结）
# ---------------------------------------------------------------------------

def send_file_via_uia(abs_path, target=_DEFAULT_TARGET):
    """
    用 UIA 通道把 abs_path 发到微信 target（默认文件传输助手）。
    返回 {"ok": bool, "stage": 人话, "detail": 人话}，永不抛异常。
    """
    try:
        # ---- 0. 环境与前置 ----
        if _auto is None:
            return _result(False, "UIA 库缺席",
                           "uiautomation 库没装上，UIA 通道不可用")
        if not abs_path or not os.path.isfile(abs_path):
            return _result(False, "文件校验",
                           "文件不存在：%s" % abs_path)
        abs_path = os.path.abspath(abs_path)
        name = os.path.basename(abs_path)
        target = (target or "").strip() or _DEFAULT_TARGET

        # 屏幕阅读器标志是 MMUI 无障碍树总开关；本次新置位时，
        # 已在运行的微信可能仍是秃树（秃树在后面检测并诚实报告）
        _ensure_screenreader_flag()

        # ---- 1. 连接/激活微信主窗口 ----
        state, win = _find_main_window()
        if state == "login":
            return _result(False, "微信未登录",
                           "微信停在登录窗口，需要先生确认登录")
        if state != "ok":
            return _result(False, "微信未运行",
                           "找不到微信主窗口，微信可能没开")
        if not _ensure_window_visible(win):
            return _result(False, "窗口唤起失败",
                           "微信主窗口唤不醒（Ctrl+Alt+W 无效）")
        if not _tree_is_populated(win):
            return _result(False, "控件树未装配",
                           "微信主窗口的无障碍控件树是秃的——真机实测微信 "
                           "4.1.12 的 MMUI 自绘主界面即使开启系统屏幕阅读器"
                           "标志也不向 UIA 暴露内容控件，此版本 UIA 通道走不通")

        # ---- 2. 打开搜索（Ctrl+F，焦点自动进搜索框） ----
        _hotkey(_VK_CONTROL, _VK_F)
        time.sleep(1.0)
        search_edit = None
        try:
            focused = _auto.GetFocusedControl()
            if focused is not None and \
                    focused.ControlTypeName == "EditControl":
                search_edit = focused
        except Exception:  # noqa: BLE001
            pass
        if search_edit is None:
            # 兜底：树里找搜索框（校准自真机 dump 的 EditControl）
            for _d, ctrl in _walk(win):
                try:
                    if ctrl.ControlTypeName == "EditControl":
                        rect = ctrl.BoundingRectangle
                        if rect.width() > 0 and rect.height() > 0:
                            search_edit = ctrl
                            break
                except Exception:  # noqa: BLE001
                    continue
        if search_edit is None:
            return _result(False, "搜索框未找到",
                           "Ctrl+F 后找不到搜索输入框控件")

        # ---- 3. 输入 target 并精确点中搜索结果 ----
        _click_control(search_edit)
        time.sleep(0.3)
        if not _stage_text_to_clipboard(target):
            return _result(False, "剪贴板登台失败",
                           "联系人名放不进剪贴板，没法输入搜索")
        _hotkey(_VK_CONTROL, _VK_V)
        time.sleep(0.8)

        # 安全闸：只接受 Name 全等的列表项/文本，绝不模糊匹配
        hit = _find_exact(win, target,
                          control_type_names={"ListItemControl",
                                              "TextControl"},
                          timeout=6.0)
        if hit is None:
            return _result(False, "联系人未命中",
                           "搜索结果里没有精确叫「%s」的项，宁可不发" % target)
        _click_control(hit)
        time.sleep(1.5)

        # 会话确认：顶部标题区应出现 target 字样（真机校准锚点）
        header = _find_exact(
            win, target, control_type_names={"TextControl"}, timeout=4.0)
        if header is None:
            return _result(False, "会话切换未确认",
                           "点了搜索结果但聊天标题没变成「%s」，不发" % target)

        # ---- 4. 文件登台剪贴板 + Ctrl+V ----
        if not _stage_file_to_clipboard(abs_path):
            return _result(False, "文件登台失败",
                           "文件放不进系统剪贴板，粘贴路线走不通")
        # 消息输入框：会话区底部的 EditControl
        input_edit = None
        try:
            win_rect = win.BoundingRectangle
            for _d, ctrl in _walk(win):
                try:
                    if ctrl.ControlTypeName != "EditControl":
                        continue
                    rect = ctrl.BoundingRectangle
                    if rect.width() > 0 and \
                            rect.top > win_rect.bottom - 400:
                        input_edit = ctrl
                        break
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        if input_edit is not None:
            _click_control(input_edit)
            time.sleep(0.3)
        _hotkey(_VK_CONTROL, _VK_V)
        time.sleep(1.5)

        # ---- 5. 确认发送弹窗（「发送给 target」→ 点发送） ----
        send_btn = _find_send_dialog_button()
        if send_btn is not None:
            _click_control(send_btn)
            time.sleep(1.5)

        # ---- 6. UIA 证据判定：消息区出现该文件名的卡片才算成功 ----
        deadline = time.time() + 8.0
        card = None
        while time.time() < deadline:
            card = _find_contains(win, name)
            if card is not None:
                break
            time.sleep(0.5)
        if card is None:
            return _result(False, "发送证据缺失",
                           "操作都执行了，但消息区没出现「%s」的文件卡片，"
                           "按未成功处理" % name)
        return _result(True, "UIA 发送成功",
                       "消息区已出现「%s」的文件卡片" % name)
    except Exception as exc:  # noqa: BLE001 - 契约：永不抛异常
        print("[uia_wechat] 未预期异常：%s" % exc)
        return _result(False, "意外异常", "UIA 通道出了点意外：%s" % exc)
