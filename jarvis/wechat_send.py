# -*- coding: utf-8 -*-
"""
wechat_send.py —— Nolan 的「发文件到微信」通道

第一性原理：大厂 AI 够不着用户的微信客户端，而 Nolan 有手（eyes/hands）。
把「文件柜里的文件发到微信」拆成一条分层降级链，每层失败降级下一层，
全部依赖注入式引用，可纯 mock 测试，零真机零 GUI：

    L0 文件校验：file_name 只许是 basename（拒绝 ../、盘符、斜杠），
       且必须在 jarvis/files/ 真实存在，否则直接人话报错；
    L1 技能快路：skills.find("在微信里把文件发给「target」") 命中
       → eyes.replay 按名重放（replay 自带终态复核，失败返回 None 降级）；
    L1.5 UIA 通道：uia_wechat.send_file_via_uia 用 Windows UI Automation
       直接操作微信控件树——不看屏幕、不猜坐标，成功判定基于 UIA 证据
       （消息区出现文件卡片），模块缺席时静默跳过；
    L1.6 键盘流通道：wechat_kbd.send_file_via_keyboard 走确定性键盘事件
       （唤起 → Ctrl+F 搜索 → 粘贴 → Enter 发送），零坐标点击，
       发送前后各有一道 VLM 是非题安全闸验收，否决即诚实降级；
    L2 视觉闭环：先把文件复制进系统剪贴板（PowerShell Set-Clipboard），
       hands.open_app 确保微信在屏幕上，再 eyes.perform 逐步闭环
       （点输入框 → Ctrl+V 粘贴文件 → 出现文件卡片 → 发送）；
    L3 诚实话术：全部失败时如实告知「没能自动完成 + 文件在文件柜里 +
       可手动拖进微信」，绝不谎称已发送。

成功固化：perform 成功后把「在微信里把文件发给「target」」+ 规范动作序列
沉淀进 skills——「」引号槽位触发 skills 的自动参数化，下次换联系人
直接命中模板走 L1 重放。规范动作序列是「经验模板」而非伪造战绩：
replay 重放时每步按名定位、终结有 VLM 复核，放不进现实的模板会
返回 None 自动降级回 L2，不存在假成功。

已知边界（诚实声明）：
  - eyes 的 VLM 感知 prompt 硬编码了「禁止向任何联系人发送消息」的安全禁令；
    发给真实联系人时 VLM 可能判 fail——默认目标「文件传输助手」是主人自己，
    不受此限。真机联调若遇此情况，需主控在 eyes 侧调整禁令措辞。
  - Ctrl+V 粘贴文件依赖桌面微信支持粘贴文件（3.x/4.x 均支持）；
    剪贴板登台失败时 perform 任务文案自动切换为「文件选择对话框输入路径」路线。
  - 本模块永不抛异常：任何意外都归约为 Nolan 人设定型话术返回。
"""

import os
import subprocess

# 防御式导入（与 hands/eyes 同款）：模块缺失时对应层级自动跳过，
# 绝不因为「眼睛/技能/手」之一缺席而整体瘫痪。
try:
    import eyes as _eyes
except Exception:  # noqa: BLE001
    _eyes = None
try:
    import skills as _skills
except Exception:  # noqa: BLE001
    _skills = None
try:
    import hands as _hands
except Exception:  # noqa: BLE001
    _hands = None
# L1.5 UIA 通道：模块缺席时静默跳到 L1.6（防御式，与上同款）
try:
    import uia_wechat as _uia_wechat
except Exception:  # noqa: BLE001
    _uia_wechat = None
# L1.6 键盘流通道：模块缺席时静默跳到 L2（防御式，与上同款）
try:
    import wechat_kbd as _wechat_kbd
except Exception:  # noqa: BLE001
    _wechat_kbd = None

_JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
# 文件柜：jarvis/files/（与 hands.SANDBOX_DIR 同一物理位置；独立计算，
# 不 import 私有常量，测试期可整体替换指向临时目录）
_FILES_DIR = os.path.join(_JARVIS_DIR, "files")

_DEFAULT_TARGET = "文件传输助手"

# eyes.perform 的失败话术指纹（与 eyes.py 内部话术逐字对应）：
# 命中任意一条即判定闭环未成功；done 成功的返回是 VLM 的自由汇报文本。
_FAIL_MARKERS = (
    "先生，任务未能完成",            # _MSG_FAIL_PREFIX
    "先生，检测到您将鼠标移至屏幕角落",  # _MSG_FAILSAFE
    "先生，我的视觉模块暂时无法连接",    # _MSG_VLM_DOWN
    "先生，任务步数超出安全上限",        # _over_limit_report 起手
)
_FAILSAFE_MARKER = "鼠标移至屏幕角落"

# 微信「目标缺失」信号（eyes prompt 第 10 条约定 fail 话术以
# 「屏幕上没有找到<应用名>」开头；登录二维码界面也会被 VLM 如实描述出来）
_WECHAT_MISSING_MARKERS = ("没有找到微信", "微信登录", "登录微信", "未登录")


# ---------------------------------------------------------------------------
# L0：文件校验与路径安全
# ---------------------------------------------------------------------------

def _resolve_file(file_name: str) -> tuple:
    """
    把 file_name 解析为文件柜内的绝对路径。
    安全闸：只接受纯 basename——任何 ../、盘符、正/反斜杠一律拒绝；
    文件必须真实存在于 jarvis/files/。
    返回 (绝对路径, None) 或 (None, 人话报错)。
    """
    name = (file_name or "").strip()
    if (not name or name in (".", "..")
            or "/" in name or "\\" in name or ":" in name):
        return None, ("抱歉先生，这个文件名我不能处理："
                      "请只给我文件柜里的纯文件名，不要带路径。")
    path = os.path.join(_FILES_DIR, name)
    if not os.path.isfile(path):
        return None, ("抱歉先生，文件柜里找不到「%s」这个文件，"
                      "没法发去微信。" % name)
    return path, None


# ---------------------------------------------------------------------------
# 剪贴板登台：把「文件」（不是文本）放进系统剪贴板
# ---------------------------------------------------------------------------

def _stage_file_to_clipboard(abs_path: str) -> bool:
    """
    用 PowerShell Set-Clipboard -LiteralPath 把文件本体放进剪贴板——
    这是「在聊天窗 Ctrl+V 粘贴文件」路线的物理前提（eyes 的 type 动作
    只能放文本，放不了文件）。登台失败返回 False，调用方据此把
    perform 的任务文案切换为「文件选择对话框输入路径」备选路线。
    任何异常吞掉返回 False——登台是加速器，不是新的故障点。
    """
    try:
        quoted = "'" + abs_path.replace("'", "''") + "'"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Set-Clipboard -LiteralPath %s" % quoted],
            capture_output=True, timeout=10,
        )
        ok = result.returncode == 0
        print("[wechat_send] 文件登台剪贴板%s：%s"
              % ("成功" if ok else "失败", abs_path))
        return ok
    except Exception as exc:  # noqa: BLE001
        print("[wechat_send] 文件登台剪贴板异常（按未登台处理）：%s" % exc)
        return False


# ---------------------------------------------------------------------------
# 任务文案与技能模板
# ---------------------------------------------------------------------------

def _skill_task(target: str) -> str:
    """技能的规范化任务名：「」引号让 skills 的自动参数化把联系人名
    抽成槽位，下次换联系人直接命中同一条模板。"""
    return "在微信里把文件发给「%s」" % target


def _canonical_steps(target: str) -> list:
    """
    成功固化的规范动作序列（经验模板）：
    搜索 → 输入联系人名 → 回车进会话 → Ctrl+V 粘贴文件 → 回车确认发送。
    Ctrl+V 这步物理上依赖 send_file 在重放前已把文件重新登台进剪贴板。
    record 时 skills 会把与 target 同文的 type 文本占位化为 {槽位}。
    """
    return [
        {"action": "key", "keys": "ctrl+f", "text": ""},   # 唤起微信搜索
        {"action": "type", "text": target, "keys": ""},    # 输入联系人名
        {"action": "key", "keys": "enter", "text": ""},    # 进入会话
        {"action": "key", "keys": "ctrl+v", "text": ""},   # 粘贴文件卡片
        {"action": "key", "keys": "enter", "text": ""},    # 确认发送
    ]


def _build_task(abs_path: str, target: str, staged: bool) -> str:
    """
    L2 视觉闭环的任务文案：目标（搜索并进会话、发文件）、可行路线
    （粘贴优先 / 文件按钮备选）、以及明确的完成判据（出现文件卡片）。
    """
    parts = [
        "在微信中搜索并打开与「%s」的会话（可以按 Ctrl+F 或点击搜索框，"
        "输入名字后回车进入会话）" % target,
        "然后把文件 %s 发送给对方" % abs_path,
    ]
    if staged:
        parts.append("系统已经把这个文件复制到剪贴板了，你只需点击会话的"
                     "输入框，按 Ctrl+V 粘贴，出现文件卡片后点击发送按钮")
    else:
        parts.append("可以点击输入框旁的「+」或文件按钮，在文件选择对话框的"
                     "文件名栏输入这个完整路径后回车打开，再点击发送")
    parts.append("发送成功以聊天窗口里出现该文件的卡片为准")
    return "，".join(parts) + "。"


# ---------------------------------------------------------------------------
# 结果判读
# ---------------------------------------------------------------------------

def _is_failsafe(result: str) -> bool:
    return _FAILSAFE_MARKER in (result or "")


def _is_failure(result: str) -> bool:
    """perform 返回值是否等于「闭环未成功」：命中任一失败话术指纹。"""
    text = result or ""
    return any(marker in text for marker in _FAIL_MARKERS)


def _looks_like_wechat_missing(result: str) -> bool:
    """从 perform 的失败报告中识别「微信未安装/未登录」的目标缺失信号。"""
    text = result or ""
    return any(marker in text for marker in _WECHAT_MISSING_MARKERS)


def _open_wechat() -> None:
    """L2 前导：确保微信窗口在屏幕上（open_app 幂等：已在则不重复拉起）。
    结果只打印不判读——屏幕上到底有没有微信，由 perform 的目标缺失
    信号给出权威答案。"""
    if _hands is None:
        return
    try:
        print("[wechat_send] 前导打开微信：%s"
              % _hands.execute("open_app", {"app": "微信"}))
    except Exception as exc:  # noqa: BLE001
        print("[wechat_send] 打开微信前导异常（交给视觉闭环判定）：%s" % exc)


# ---------------------------------------------------------------------------
# 公开 API（集成契约，签名冻结）
# ---------------------------------------------------------------------------

def send_file(file_name: str, target: str = _DEFAULT_TARGET) -> str:
    """
    把 jarvis/files/ 下的文件通过微信发给 target（默认文件传输助手）。
    file_name 只许是 basename；返回 Nolan 人设定型话术，供 brain 直接播报。
    永不抛异常，绝不谎称已发送。
    """
    try:
        # ---- L0：文件校验（路径安全闸） ----
        abs_path, err = _resolve_file(file_name)
        if err:
            return err
        name = os.path.basename(abs_path)
        target = (target or "").strip() or _DEFAULT_TARGET

        # ---- L1：技能快路 ----
        if _skills is not None and _eyes is not None:
            try:
                hit = _skills.find(_skill_task(target))
            except Exception:  # noqa: BLE001
                hit = None
            if hit:
                skill_task, steps = hit
                # 重放序列里有 Ctrl+V：先把文件重新登台进剪贴板
                _stage_file_to_clipboard(abs_path)
                try:
                    result = _eyes.replay(skill_task, steps,
                                          target_hint="微信")
                except Exception:  # noqa: BLE001
                    result = None
                if isinstance(result, str):
                    if _is_failsafe(result):
                        return result + "文件还在文件柜里（%s）。" % name
                    # replay 自带终态复核，返回字符串即复核通过
                    return ("好的先生，文件「%s」已经通过微信发给%s了。"
                            % (name, target))
                # None：重放放不进现实，降级 L1.5 UIA 通道

        # ---- L1.5：UIA 通道（不看屏幕、不猜坐标，直接操作控件树） ----
        if _uia_wechat is not None:
            try:
                uia = _uia_wechat.send_file_via_uia(abs_path, target)
            except Exception:  # noqa: BLE001
                uia = None
            if isinstance(uia, dict):
                print("[wechat_send] L1.5 UIA 通道：ok=%s stage=%s detail=%s"
                      % (uia.get("ok"), uia.get("stage"), uia.get("detail")))
                if uia.get("ok"):
                    # 成功固化：与 L2 成功同款沉淀，下次走 L1 快路
                    if _skills is not None:
                        try:
                            _skills.record(_skill_task(target),
                                           _canonical_steps(target))
                        except Exception:  # noqa: BLE001
                            pass
                    return ("好的先生，文件「%s」已经通过微信发给%s了。"
                            % (name, target))
                # ok=False：诚实降级 L2 视觉闭环（话术留作 L3 现场补充）
                _l15_detail = uia.get("detail") or ""
            else:
                _l15_detail = ""
        else:
            _l15_detail = ""

        # ---- L1.6：键盘流通道（确定性键盘事件 + VLM 两道安全闸验收） ----
        if _wechat_kbd is not None:
            try:
                kbd = _wechat_kbd.send_file_via_keyboard(abs_path, target)
            except Exception:  # noqa: BLE001
                kbd = None
            if isinstance(kbd, dict):
                print("[wechat_send] L1.6 键盘流通道：ok=%s stage=%s detail=%s"
                      % (kbd.get("ok"), kbd.get("stage"), kbd.get("detail")))
                if kbd.get("ok"):
                    # 成功固化：与 L1.5/L2 成功同款沉淀，下次走 L1 快路
                    if _skills is not None:
                        try:
                            _skills.record(_skill_task(target),
                                           _canonical_steps(target))
                        except Exception:  # noqa: BLE001
                            pass
                    return ("好的先生，文件「%s」已经通过微信发给%s了。"
                            % (name, target))
                # ok=False：诚实降级 L2 视觉闭环（话术留作 L3 现场补充）
                _l16_detail = kbd.get("detail") or ""
            else:
                _l16_detail = ""
        else:
            _l16_detail = ""

        # ---- L2：视觉闭环 ----
        if _eyes is None:
            msg = ("先生，我的视觉模块现在帮不上忙，微信发送这步没能自动"
                   "完成，文件在文件柜里（%s），您可以手动把它拖进微信。"
                   % name)
            if _l15_detail:
                msg += "（UIA 通道的情况：%s）" % _l15_detail
            if _l16_detail:
                msg += "（键盘流通道的情况：%s）" % _l16_detail
            return msg
        staged = _stage_file_to_clipboard(abs_path)
        _open_wechat()
        try:
            result = _eyes.perform(_build_task(abs_path, target, staged),
                                   target_hint="微信")
        except Exception as exc:  # noqa: BLE001
            print("[wechat_send] 视觉闭环异常：%s" % exc)
            result = ""
        if _is_failsafe(result):
            return result + "文件还在文件柜里（%s）。" % name
        if _looks_like_wechat_missing(result):
            return ("先生，我在屏幕上没有找到可用的微信窗口——微信可能还没"
                    "安装，或者还没登录，请先登录微信后再让我发。文件在文件"
                    "柜里（%s），您也可以登录后手动把它拖进微信。" % name)
        if not result or _is_failure(result):
            # ---- L3：诚实话术，绝不谎称已发送 ----
            msg = ("先生，微信发送这步没能自动完成，文件在文件柜里（%s），"
                   "您可以手动把它拖进微信。" % name)
            if _l15_detail:
                msg += "（UIA 通道的情况：%s）" % _l15_detail
            if _l16_detail:
                msg += "（键盘流通道的情况：%s）" % _l16_detail
            if result:
                msg += "当时的情况：%s" % result
            return msg

        # ---- 成功固化：沉淀参数化技能，下次同类任务走 L1 快路 ----
        if _skills is not None:
            try:
                _skills.record(_skill_task(target), _canonical_steps(target))
            except Exception:  # noqa: BLE001
                pass
        return "好的先生，文件「%s」已经通过微信发给%s了。" % (name, target)
    except Exception as exc:  # noqa: BLE001 - 契约：永不抛异常
        print("[wechat_send] 未预期异常：%s" % exc)
        return ("抱歉先生，发文件到微信时出了点意外（%s），文件应该还在"
                "文件柜里，您可以手动发。" % exc)
