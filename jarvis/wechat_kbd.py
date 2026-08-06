# -*- coding: utf-8 -*-
"""
wechat_kbd.py —— Nolan 的「键盘流通道」发文件到微信（L1.6 层）

定位：夹在 wechat_send 的 L1.5（UIA 通道，本机微信 4.1.12.26 的 MMUI 控件树
是秃的，物理走不通）与 L2（eyes.perform 视觉闭环，VLM 逐步猜坐标）之间。

第一性原理：UIA 读不到微信的控件树，但微信对「键盘」永远诚实——
Ctrl+Alt+W 唤起、Ctrl+F 搜索、Ctrl+V 粘贴、Enter 确认，全是确定性全局
键盘事件，不需要知道任何坐标。整条回路禁止坐标点击、禁止 VLM 定位；
VLM 只在两道安全闸里做「是/否」验收，不参与动作决策：

    唤起（Ctrl+Alt+W，轮询前台确认；唤起后焦点即在当前会话输入框）
      -> 安全闸 A 预检：VLM 验收「顶部标题栏是否已是 target 会话」——
         已是则跳过搜索直接粘贴（路 A）
      -> 否则 Ctrl+F 搜索 -> 文本剪贴板粘贴联系人名 -> Down 箭头步进，
         每步截图问 VLM「标题栏是否已切换为 target」（步进上限 10 次，
         否决则按 Esc 清理现场，文件绝不发送）
      -> 标题栏变成 target 即证明高亮锁定在 target 入口项上，此时按
         Enter 进会话且焦点自动落输入框（路 B），进会话后再复核一次标题栏
      -> 文件剪贴板 -> Ctrl+V -> Enter 发送
      -> 安全闸 B：VLM 验收「聊天区是否出现该文件名的文件卡片」——
         否决则诚实报「发送未确认」，绝不谎报成功

安全契约（与 uia_wechat 同款）：
  - VLM 只许答「是/否」；答案含「是」且不含「否」才算通过；
    VLM 调用任何异常一律按「验收不通过」处理（宁可误报失败，绝不谎报成功）；
  - send_file_via_keyboard 永不抛异常，一切意外归约为 ok=False 的人话。

真机事实（沿用 uia_wechat 探测结论）：
  - 微信 4.1.12.26 进程名 Weixin.exe，主窗口 class=Qt51514QWindowIcon、
    标题「微信」；托盘隐藏时 ShowWindow 唤不醒，Ctrl+Alt+W 全局热键可靠。
"""

import base64
import ctypes
import io
import os
import subprocess
import time

# 防御式导入：eyes 提供现成的 VLM 调用与全屏截屏兜底；缺席时两道安全闸
# 一律「验收不通过」，本层永远诚实失败降级，绝不放行。
try:
    import eyes as _eyes
except Exception:  # noqa: BLE001
    _eyes = None
try:
    from PIL import ImageGrab as _ImageGrab
except Exception:  # noqa: BLE001
    _ImageGrab = None

_DEFAULT_TARGET = "文件传输助手"

# 真机确认的微信主窗口特征（4.1.12.26）
_MAIN_CLASS = "Qt51514QWindowIcon"
_MAIN_TITLE = "微信"

# 虚拟键码（与 uia_wechat 同源）
_VK_CONTROL = 0x11
_VK_MENU = 0x12       # Alt
_VK_W = 0x57
_VK_F = 0x46
_VK_V = 0x56
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B
_VK_DOWN = 0x28
_KEYEVENTF_KEYUP = 0x0002

_FOREGROUND_TIMEOUT = 5.0   # 唤起后轮询前台窗口的上限（秒）
_WAKE_RETRY_INTERVAL = 2.0  # 唤起等待期间补发一次热键的间隔（秒）
_SHOT_MAX_WIDTH = 1280      # 发给 VLM 的截图最大宽度（与 eyes 同款）
_JPEG_QUALITY = 80
_MAX_DOWN_STEPS = 10        # 搜索下拉里下箭头步进的安全上限（防死循环）

# 安全闸验收的 VLM 系统提示：只许短答，拿不准也必须二选一（含「否」即否决）
_VLM_GATE_SYSTEM = (
    "你是 Nolan 的界面验收员。我会给你一张微信窗口的截图和一个判断问题，"
    "你只根据截图中实际可见的内容回答，绝不猜测、绝不脑补。"
    "只回答一个字：是 或 否。不要输出任何其他内容。"
)


def _result(ok, stage, detail):
    """统一返回结构，stage/detail 一律人话。"""
    return {"ok": bool(ok), "stage": stage, "detail": detail}


def _sleep(seconds):
    """等待原语独立成函数：测试期可整体打桩，避免真实等待拖慢单测。"""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# 键盘注入（ctypes keybd_event，与 hands/uia_wechat 同款先例）
# ---------------------------------------------------------------------------

def _hotkey(*vks):
    """注入全局组合键：先依序按下，再逆序抬起。"""
    kb = ctypes.windll.user32.keybd_event
    for v in vks:
        kb(v, 0, 0, 0)
        time.sleep(0.06)
    for v in reversed(vks):
        kb(v, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


def _press(vk):
    """单击一个键（Enter / Esc）。"""
    _hotkey(vk)


# ---------------------------------------------------------------------------
# 窗口与进程
# ---------------------------------------------------------------------------

def _weixin_running():
    """Weixin.exe 进程是否在运行（tasklist 查询，失败按未运行处理）。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True, timeout=10)
        out = (result.stdout or b"").decode("gbk", errors="ignore")
        return "Weixin.exe" in out
    except Exception as exc:  # noqa: BLE001
        print("[wechat_kbd] 进程查询异常（按未运行处理）：%s" % exc)
        return False


def _find_wechat_hwnd():
    """win32 FindWindow 拿主窗口句柄：先按 class+标题精确找，再按 class 兜底。
    找不到返回 0（托盘隐藏的窗口用 class 也能找到句柄）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(_MAIN_CLASS, _MAIN_TITLE)
        if hwnd:
            return int(hwnd)
        return int(user32.FindWindowW(_MAIN_CLASS, None) or 0)
    except Exception as exc:  # noqa: BLE001
        print("[wechat_kbd] FindWindow 异常：%s" % exc)
        return 0


def _foreground_hwnd():
    """当前前台窗口句柄；失败返回 0。"""
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _hwnd_class(hwnd):
    """读窗口类名；失败返回空串。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(
            ctypes.c_void_p(hwnd), buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def _window_visible(hwnd):
    """窗口是否真可见（IsWindowVisible）。真机踩坑：微信藏到托盘后
    GetForegroundWindow 仍可能报它是前台，只看前台句柄会被骗。"""
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(
            ctypes.c_void_p(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def _wake_to_foreground(hwnd, timeout=_FOREGROUND_TIMEOUT):
    """
    Ctrl+Alt+W 唤起微信主窗口并轮询确认它真的到了前台且可见。
    判定：前台窗口句柄就是 hwnd（或前台窗口类名是微信主窗口类）且窗口
    IsWindowVisible。等待期间按间隔补发热键。
    注意：Ctrl+Alt+W 是「切换」键——窗口已在前台时再按会把它藏回托盘，
    所以每次按键前必须先查前台状态（真机踩坑）。
    """
    deadline = time.monotonic() + timeout
    last_wake = 0.0  # 0 表示尚未发过 hotkey
    while time.monotonic() < deadline:
        fore = _foreground_hwnd()
        fore_match = bool(fore) and \
            (fore == hwnd or _hwnd_class(fore) == _MAIN_CLASS)
        if fore_match and _window_visible(hwnd):
            return True
        if time.monotonic() - last_wake >= _WAKE_RETRY_INTERVAL:
            _hotkey(_VK_CONTROL, _VK_MENU, _VK_W)
            last_wake = time.monotonic()
        _sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# 剪贴板登台（文本 / 文件，PowerShell，与 wechat_send/uia_wechat 同款）
# ---------------------------------------------------------------------------

def _powershell_exe():
    """
    定位 powershell 可执行文件。真机踩坑：本机 System32 根目录没有
    powershell.exe（shutil.which 也找不到），只有 v1.0 完整路径可用——
    按「PATH 查找 -> 64 位完整路径 -> SysWOW64 兜底」顺序解析。
    """
    import shutil
    candidates = [
        shutil.which("powershell") or "",
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0",
                     "powershell.exe"),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "SysWOW64", "WindowsPowerShell", "v1.0",
                     "powershell.exe"),
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return "powershell"  # 实在找不到，退回裸名让系统自己碰运气


def _stage_text_to_clipboard(text):
    """把纯文本放进剪贴板——中文联系人名走粘贴比逐键注入可靠得多。"""
    try:
        quoted = "'" + text.replace("'", "''") + "'"
        result = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-Command",
             "Set-Clipboard -Value %s" % quoted],
            capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print("[wechat_kbd] 文本登台剪贴板异常：%s" % exc)
        return False


def _stage_file_to_clipboard(abs_path):
    """把文件本体（不是路径文本）放进系统剪贴板。"""
    try:
        quoted = "'" + abs_path.replace("'", "''") + "'"
        result = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-Command",
             "Set-Clipboard -LiteralPath %s" % quoted],
            capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print("[wechat_kbd] 文件登台剪贴板异常：%s" % exc)
        return False


# ---------------------------------------------------------------------------
# 截图与安全闸验收
# ---------------------------------------------------------------------------

def _shot_window_b64(hwnd, save_path=None):
    """
    截微信窗口区域，缩放到宽 <= 1280，编码 JPEG base64。
    窗口区域截取失败（最小化 / 坐标异常）时兜底为全屏截图（eyes 现成原语）。
    save_path 非空时把原图落盘，供真机验收留证。
    """
    img = None
    if _ImageGrab is not None and hwnd:
        try:
            rect = ctypes.create_string_buffer(16)  # RECT = 4 个 LONG
            if ctypes.windll.user32.GetWindowRect(
                    ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                import struct
                left, top, right, bottom = struct.unpack("4i", rect.raw)
                if right > left and bottom > top:
                    img = _ImageGrab.grab(bbox=(left, top, right, bottom))
        except Exception as exc:  # noqa: BLE001
            print("[wechat_kbd] 窗口区域截图异常（兜底全屏）：%s" % exc)
            img = None
    if img is None:
        if _eyes is None:
            return ""
        try:
            return _eyes.screenshot_b64()
        except Exception:  # noqa: BLE001
            return ""
    if save_path:
        try:
            img.convert("RGB").save(save_path, format="JPEG", quality=90)
        except Exception as exc:  # noqa: BLE001
            print("[wechat_kbd] 截图落盘失败（不影响验收）：%s" % exc)
    if img.width > _SHOT_MAX_WIDTH:
        new_h = round(img.height * _SHOT_MAX_WIDTH / img.width)
        img = img.resize((_SHOT_MAX_WIDTH, new_h), _ImageGrab.Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _vlm_yes(shot_b64, question):
    """
    安全闸验收：问 VLM 一个是非题，返回 (通过与否, VLM 原文)。
    判据：答案含「是」且不含「否」才通过；无截图、无 eyes、调用异常、
    回复为空——一律按不通过处理（宁可误报失败，绝不谎报成功）。
    """
    if not shot_b64 or _eyes is None:
        return False, "（截图或视觉模块不可用）"
    try:
        raw = _eyes._ask_vlm(shot_b64, question, system=_VLM_GATE_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        print("[wechat_kbd] 安全闸 VLM 调用异常（按不通过处理）：%s" % exc)
        return False, "（VLM 调用异常：%s）" % exc
    answer = (raw or "").strip()
    verdict = ("是" in answer) and ("否" not in answer)
    return verdict, answer


# ---------------------------------------------------------------------------
# 公开接口（集成契约，签名冻结）
# ---------------------------------------------------------------------------

def send_file_via_keyboard(abs_path, target=_DEFAULT_TARGET,
                           evidence_dir=None):
    """
    用纯键盘流把 abs_path 发到微信 target（默认文件传输助手）。
    全程确定性键盘事件，零坐标点击、零 VLM 定位；VLM 只用于两道安全闸。
    evidence_dir 非空时把两道安全闸的截图落盘留证（真机验收用）。
    返回 {"ok": bool, "stage": 人话, "detail": 人话}，永不抛异常。
    """
    try:
        # ---- 0. 前置校验 ----
        if not abs_path or not os.path.isfile(abs_path):
            return _result(False, "文件校验",
                           "文件不存在：%s" % abs_path)
        abs_path = os.path.abspath(abs_path)
        name = os.path.basename(abs_path)
        target = (target or "").strip() or _DEFAULT_TARGET

        # ---- 1. 微信进程在不在 ----
        if not _weixin_running():
            return _result(False, "微信没开",
                           "找不到 Weixin.exe 进程，微信可能没开")

        # ---- 2. 找主窗口并唤起到前台 ----
        hwnd = _find_wechat_hwnd()
        if not hwnd:
            return _result(False, "找不到微信窗口",
                           "进程在但找不到主窗口（Qt51514QWindowIcon）")
        if not _wake_to_foreground(hwnd):
            return _result(False, "唤起超时",
                           "Ctrl+Alt+W 后 %.0f 秒内微信没来到前台"
                           % _FOREGROUND_TIMEOUT)
        _sleep(0.5)  # 等窗口动画与焦点稳定

        # ---- 3. 会话定位（真机探针淬炼出的两条路） ----
        # 真机事实（6 次探针证实）：
        #   a) Ctrl+Alt+W 唤起后，键盘焦点就在当前会话的输入框里——
        #      若当前会话已是 target，可直接粘贴，根本不需要搜索；
        #   b) 搜索下拉里「搜索网络结果」建议词条排在最前且默认高亮，
        #      此时按 Enter 会打开网页搜索页而不是进会话（第 2/3 轮踩坑）；
        #   c) 按 Down 步进时高亮跟随移动，右侧面板即时预览高亮项的会话，
        #      顶部标题栏随之切换——标题栏变成 target 的那一步，就是高亮
        #      落在 target 入口项上的铁证（VLM 看不清高亮底色，但读标题栏
        #      很可靠），此刻按 Enter 进会话且焦点落在输入框（探针 5 证实）；
        #   d) 安全闸 A 通过后再按 Esc 收场会让焦点掉到会话列表上，
        #      Ctrl+V 粘贴落空（第 4 轮踩坑）——所以成功路径绝不按 Esc。
        question_a = (
            "这是电脑屏幕当前微信窗口的截图。请只根据截图中实际可见的内容"
            "判断：窗口顶部标题栏显示的当前会话名称是否恰好是「%s」，"
            "且右侧区域显示的是与该联系人的聊天内容（而不是搜索结果列表"
            "页）？只回答 是 或 否，不要输出任何其他内容。" % target)

        shot0 = _shot_window_b64(hwnd)
        already, raw0 = _vlm_yes(shot0, question_a)
        print("[wechat_kbd] 安全闸 A（预检：是否已在目标会话）：通过=%s，"
              "VLM 原文=%r" % (already, raw0))

        if not already:
            # 路 B：Ctrl+F 搜索 -> 粘贴名字 -> Down 步进直到标题栏变成 target
            _hotkey(_VK_CONTROL, _VK_F)
            _sleep(0.5)
            if not _stage_text_to_clipboard(target):
                return _result(False, "剪贴板登台失败",
                               "联系人名放不进剪贴板，没法输入搜索")
            _hotkey(_VK_CONTROL, _VK_V)
            _sleep(0.8)

            # 安全闸 A（发送前验收）：标题栏变成 target 才允许按 Enter
            pass_a = False
            raw_a = "（安全闸 A 未执行）"
            for step in range(_MAX_DOWN_STEPS + 1):
                shot_a = _shot_window_b64(hwnd, save_path=(
                    os.path.join(evidence_dir, "gate_a.jpg")
                    if evidence_dir else None))
                pass_a, raw_a = _vlm_yes(shot_a, question_a)
                print("[wechat_kbd] 安全闸 A（第 %d 次采样）：通过=%s，"
                      "VLM 原文=%r" % (step, pass_a, raw_a))
                if pass_a:
                    break
                _press(_VK_DOWN)  # 高亮下移一项，右侧面板跟随预览
                _sleep(0.4)
            if not pass_a:
                # 清理现场：Esc 退出搜索，绝不按 Enter（那会进网页搜索页）
                _press(_VK_ESCAPE)
                _sleep(0.3)
                _press(_VK_ESCAPE)
                return _result(False, "搜索未命中",
                               "安全闸 A 否决：步进 %d 次后标题栏仍未确认是"
                               "「%s」（VLM 最后答：%s）。已按 Esc 清理现场，"
                               "文件没有发送。" % (_MAX_DOWN_STEPS, target, raw_a))

            # 此刻高亮锁定在 target 入口项上：Enter 进会话且焦点落输入框
            _press(_VK_RETURN)
            _sleep(0.8)
            # 进会话复核：标题栏必须仍是 target 才许粘贴（防 VLM 预检误判）
            shot_a2 = _shot_window_b64(hwnd)
            pass_a2, raw_a2 = _vlm_yes(shot_a2, question_a)
            print("[wechat_kbd] 安全闸 A（进会话复核）：通过=%s，VLM 原文=%r"
                  % (pass_a2, raw_a2))
            if not pass_a2:
                return _result(False, "会话切换未确认",
                               "按 Enter 后标题栏未确认是「%s」（VLM 答：%s），"
                               "文件没有粘贴发送。" % (target, raw_a2))

        # ---- 4. 文件登台剪贴板 -> Ctrl+V -> Enter 发送 ----
        # 此刻焦点确定在 target 会话的输入框（路 A 唤起即焦点；路 B Enter
        # 进会话自动带焦点），可以安全粘贴。
        if not _stage_file_to_clipboard(abs_path):
            return _result(False, "剪贴板登台失败",
                           "文件放不进系统剪贴板，粘贴路线走不通")
        _hotkey(_VK_CONTROL, _VK_V)
        # 粘贴等待按文件大小缩放：大文件登台进聊天框更慢
        size_mb = os.path.getsize(abs_path) / (1024 * 1024)
        _sleep(min(1.2 + size_mb * 0.6, 10.0))
        # 「发送给 target」确认框出现时用 Enter 确认；无确认框则直接发送
        _press(_VK_RETURN)

        # ---- 6. 安全闸 B（发送后验收）：聊天区必须出现该文件名的卡片 ----
        _sleep(1.5)
        shot_b = _shot_window_b64(hwnd, save_path=(
            os.path.join(evidence_dir, "gate_b.jpg") if evidence_dir else None))
        question_b = (
            "这是电脑屏幕当前微信窗口的截图。请只根据截图中实际可见的内容"
            "判断：聊天消息区域（会话气泡列表）里是否已经出现了一个文件名"
            "为「%s」的文件卡片/文件消息？注意：文件只停留在底部输入框里"
            "不算。只回答 是 或 否，不要输出任何其他内容。" % name)
        pass_b, raw_b = _vlm_yes(shot_b, question_b)
        print("[wechat_kbd] 安全闸 B：通过=%s，VLM 原文=%r" % (pass_b, raw_b))
        if not pass_b:
            return _result(False, "发送未确认",
                           "安全闸 B 否决：聊天区未确认出现「%s」的文件卡片"
                           "（VLM 答：%s）。文件可能还留在输入框里，"
                           "建议先生看一眼微信确认现场。" % (name, raw_b))
        return _result(True, "键盘流发送成功",
                       "两道安全闸均通过：已进入「%s」会话并确认文件卡片"
                       "「%s」出现在聊天区。" % (target, name))
    except Exception as exc:  # noqa: BLE001 - 契约：永不抛异常
        print("[wechat_kbd] 未预期异常：%s" % exc)
        return _result(False, "意外异常", "键盘流通道出了点意外：%s" % exc)
