# -*- coding: utf-8 -*-
"""
perception.py —— Nolan 的「屏幕状态流」（Gap5：界面没变不重看，变了才看）

第一性原理：eyes 逐步循环里每个动作前后都截图送 VLM 复核，一张截图几万
token、一次往返 2-5 秒。但物理事实是：大多数步骤之间界面只有局部变化
甚至完全没变——点个按钮、打几个字，全屏重看是把「确认没变」这件便宜的
事用最贵的方式做。界面状态有三个廉价可算的物理指纹：窗口是谁（标题 +
尺寸）、控件树长什么样（名称 + 类型 + 相对位置）、像素分布变没变
（缩略图灰度差）。三个指纹都不变，界面就是没变——这不是猜测，是可计算
的同一性判断；只有指纹变了，才值得花一次 VLM 往返去看「变成了什么」。

设计边界（与 reliability 同源）：
  * 零 GUI 依赖：不 import pyautogui/uia/comtypes，不做任何 IO——截图字节、
    控件清单、窗口标识全部由调用方注入，纯函数可在无真机环境全 mock 回归；
  * 保守原则：任何证据缺失（截图解码失败、首步无前值）都判「变了」——
    宁可多花一次 VLM 往返，绝不在「不确定没变」时跳过复核；
  * 毫秒级：像素指纹只在 64x36 灰度缩略图上计算（2304 字节），
    控件指纹是名称 + 类型 + 8x8 位置网格的稳定哈希，全程无大图运算。

接口契约（签名一字不差）：
    capture_state(hwnd, shot_bytes, controls) -> ScreenState
        hwnd 可为 int（句柄即窗口标识）或 (hwnd, 标题) 元组（标题更稳定）；
        shot_bytes 为 JPEG/PNG 截图字节；controls 为 uia.dump_window_controls
        返回的 [{name, control_type, rect, enabled}, ...]
    diff_states(a, b) -> dict
        {changed: bool, pixel_change_ratio: 0-1,
         controls_added: [...], controls_removed: [...], focus_shift: bool}
        a 为 None（首步）时返回全变结果
    should_review(prev, curr) -> bool
        主控决策函数：True = 界面实质变化，走 VLM 复核；False = 跳过
    describe_change(d) -> str
        diff 结果翻成一句中文，供拼进 VLM prompt 或直接复用

变化判定阈值（噪声免疫的物理依据）：
  * 像素：单格灰度差 > 24 才算变（JPEG 噪声/抗锯齿抖动在 ±5 以内），
    变格占比 > 5% 才算界面变（光标 4-6px 在 64x36 缩略图上只占 1-2 格，
    占比 < 0.1%；时钟秒针同理）；
  * 控件：按 (名称, 类型) 多重集比较增删，位置量化到 8x8 网格——
    控件轻微位移不算增删，冒出一个「确定」按钮就是实质新增；
  * 窗口：window_sig 不同即焦点切换，直接判变。
"""

import hashlib
import io
import time
from dataclasses import dataclass, field

from PIL import Image

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_THUMB_W, _THUMB_H = 64, 36      # 像素指纹缩略图尺寸（2304 格，毫秒级计算的物理上限）
_PIXEL_NOISE = 24                # 单格灰度差噪声阈值：小于此值视为压缩/渲染抖动
_PIXEL_CHANGE_RATIO = 0.05       # 变格占比阈值：超过才算界面实质变化
_POS_GRID = 8                    # 控件位置量化网格（8x8）：吸收布局微抖，保留结构变化
_DEGRADED_PIXEL_SIG = 0          # 截图解码失败时的降级指纹（diff 遇之保守判变）


# ---------------------------------------------------------------------------
# 状态指纹
# ---------------------------------------------------------------------------

@dataclass
class ScreenState:
    """某个窗口某一时刻的状态指纹。

    window_sig  窗口身份指纹：标题（或句柄）+ 尺寸
    control_sig 控件清单稳定哈希：名称 + 类型 + 相对位置网格的摘要
    pixel_sig   像素感知哈希（8x8 均值哈希，64 位）；解码失败为 0
    ts          采集时刻（time.time()）
    thumb       64x36 灰度缩略图原始字节（2304B），diff 逐格比对的物理底稿
    controls    (名称, 类型) 元组，供 diff 列出增删控件的具体名单
    """
    window_sig: str = ""
    control_sig: str = ""
    pixel_sig: int = _DEGRADED_PIXEL_SIG
    ts: float = 0.0
    thumb: bytes = b""
    controls: tuple = field(default_factory=tuple)


def _window_identity(hwnd) -> str:
    """窗口身份字符串：元组取标题（句柄会随重启变化，标题更稳定），int 用句柄。"""
    if isinstance(hwnd, (tuple, list)) and len(hwnd) >= 2:
        return "title:%s" % str(hwnd[1]).strip()
    return "hwnd:%s" % hwnd


def _decode_thumb(shot_bytes) -> tuple:
    """截图字节 -> (64x36 灰度缩略图字节, 原图宽, 原图高)；任何异常返回 (b"", 0, 0)。

    防御降级：截图管线异常不该炸掉状态流——拿不到像素证据就交出空底稿，
    由 diff 侧保守判变（交给 VLM 看），而不是在这里抛异常。
    """
    if not shot_bytes:
        return b"", 0, 0
    try:
        with Image.open(io.BytesIO(shot_bytes)) as img:
            w, h = img.size
            thumb = img.convert("L").resize((_THUMB_W, _THUMB_H), Image.BILINEAR)
            return thumb.tobytes(), w, h
    except Exception:
        return b"", 0, 0


def _ahash64(thumb: bytes) -> int:
    """64x36 缩略图再压到 8x8 算均值哈希（64 位）；空底稿返回降级指纹。"""
    if not thumb:
        return _DEGRADED_PIXEL_SIG
    try:
        small = Image.frombytes("L", (_THUMB_W, _THUMB_H), thumb).resize(
            (8, 8), Image.BILINEAR)
        pixels = list(small.tobytes())
        mean = sum(pixels) / len(pixels)
        sig = 0
        for p in pixels:
            sig = (sig << 1) | (1 if p >= mean else 0)
        return sig
    except Exception:
        return _DEGRADED_PIXEL_SIG


def _control_summary(controls, img_w: int, img_h: int) -> tuple:
    """控件清单 -> ((名称,类型) 元组按序去重, 稳定哈希)。

    位置量化到 8x8 相对网格：控件挪了几个像素不算结构变化，
    但移动到完全不同的屏幕区域会反映到哈希里。无尺寸的畸形控件跳过。
    """
    entries = []
    names = []
    for c in controls or []:
        try:
            name = (c.get("name") or "").strip()
            if not name:
                continue  # 无名控件对「界面变没变」没有指认价值
            ctype = c.get("control_type") or "控件"
            x, y, w, h = c["rect"]
            cx, cy = x + w / 2.0, y + h / 2.0
            if img_w > 0 and img_h > 0:
                gx = min(_POS_GRID - 1, max(0, int(cx / img_w * _POS_GRID)))
                gy = min(_POS_GRID - 1, max(0, int(cy / img_h * _POS_GRID)))
            else:  # 无截图尺寸参照：绝对坐标粗量化到 32px 格
                gx, gy = int(cx // 32), int(cy // 32)
            entries.append("%s|%s|%d,%d" % (name, ctype, gx, gy))
            names.append((name, ctype))
        except Exception:
            continue  # 单个畸形控件不拖垮整份清单
    entries.sort()
    sig = hashlib.sha1("\n".join(entries).encode("utf-8")).hexdigest()[:16]
    return tuple(names), sig


def capture_state(hwnd, shot_bytes, controls) -> ScreenState:
    """从注入的截图字节与控件清单计算状态指纹（纯函数，不做任何 IO）。

    hwnd        int 句柄，或 (hwnd, 窗口标题) 元组——标题跨重启稳定，优先注入
    shot_bytes  JPEG/PNG 截图字节；为空或损坏时降级（pixel_sig=0，thumb 空）
    controls    uia.dump_window_controls 的返回清单；为空表示 UIA 无覆盖
    """
    thumb, w, h = _decode_thumb(shot_bytes)
    names, control_sig = _control_summary(controls, w, h)
    return ScreenState(
        window_sig="%s|%dx%d" % (_window_identity(hwnd), w, h),
        control_sig=control_sig,
        pixel_sig=_ahash64(thumb),
        ts=time.time(),
        thumb=thumb,
        controls=names,
    )


# ---------------------------------------------------------------------------
# 差异计算
# ---------------------------------------------------------------------------

def _pixel_change_ratio(a: ScreenState, b: ScreenState) -> float:
    """逐格比对 64x36 灰度底稿，返回变化格占比（0-1）。

    任一侧底稿缺失（截图解码失败）返回 1.0——无法证明没变，保守判变，
    把裁决权交还给 VLM；这是「增强失效时退回默认路径」的物理表现。
    """
    if not a.thumb or not b.thumb or len(a.thumb) != len(b.thumb):
        return 1.0
    n = len(a.thumb)
    changed = 0
    for i in range(n):
        if abs(a.thumb[i] - b.thumb[i]) > _PIXEL_NOISE:
            changed += 1
    return changed / n


def _control_delta(a: ScreenState, b: ScreenState) -> tuple:
    """控件 (名称,类型) 多重集差异 -> (新增名单, 消失名单)，保序去重。"""
    from collections import Counter
    ca, cb = Counter(a.controls), Counter(b.controls)
    added, removed = [], []
    for key in cb:
        if cb[key] > ca.get(key, 0) and key[0] not in added:
            added.append(key[0])
    for key in ca:
        if ca[key] > cb.get(key, 0) and key[0] not in removed:
            removed.append(key[0])
    return added, removed


def diff_states(a, b) -> dict:
    """比较两个状态指纹，返回差异报告。

    changed             是否实质变化（窗口切换 / 像素变 >5% / 控件实质增删）
    pixel_change_ratio  像素变化格占比 0-1（底稿缺失保守给 1.0）
    controls_added      新增控件名称名单
    controls_removed    消失控件名称名单
    focus_shift         窗口身份是否切换（window_sig 不同）

    a 为 None（任务首步，无前值可比）时返回全变结果——首步必须让 VLM 看。
    """
    if b is None:
        return {"changed": False, "pixel_change_ratio": 0.0,
                "controls_added": [], "controls_removed": [],
                "focus_shift": False}
    if a is None:
        return {"changed": True, "pixel_change_ratio": 1.0,
                "controls_added": [n for n, _ in b.controls],
                "controls_removed": [], "focus_shift": True}

    focus_shift = a.window_sig != b.window_sig
    ratio = _pixel_change_ratio(a, b)
    added, removed = _control_delta(a, b)
    controls_changed = bool(added or removed)

    changed = focus_shift or ratio > _PIXEL_CHANGE_RATIO or controls_changed
    return {"changed": changed, "pixel_change_ratio": ratio,
            "controls_added": added, "controls_removed": removed,
            "focus_shift": focus_shift}


def should_review(prev, curr) -> bool:
    """主控决策：界面实质变化了才需要 VLM 复核（True），没变就跳过（False）。

    首步（prev 为 None）返回 True；任何证据缺失导致的保守判变也返回 True——
    省 VLM 往返的前提是「能证明没变」，证明不了就老老实实看。
    """
    if prev is None or curr is None:
        return True
    return diff_states(prev, curr)["changed"]


def describe_change(d: dict) -> str:
    """把 diff 报告翻成一句中文，供拼进 VLM prompt 或直接作为生效判据。"""
    if not isinstance(d, dict) or not d.get("changed"):
        return "界面无实质变化"

    parts = []
    if d.get("focus_shift"):
        parts.append("窗口/焦点已切换")
    added = d.get("controls_added") or []
    removed = d.get("controls_removed") or []
    if added:
        names = "、".join(added[:5]) + ("等" if len(added) > 5 else "")
        parts.append("新增 %d 个控件：%s" % (len(added), names))
    if removed:
        names = "、".join(removed[:5]) + ("等" if len(removed) > 5 else "")
        parts.append("消失 %d 个控件：%s" % (len(removed), names))
    ratio = d.get("pixel_change_ratio") or 0.0
    if ratio > _PIXEL_CHANGE_RATIO:
        parts.append("画面内容变化约 %d%%" % round(ratio * 100))
    return "；".join(parts) if parts else "界面有变化"


# ---------------------------------------------------------------------------
# 条件等待的稳定性判定（B1 速度战役：盲等 -> 条件等待，纯新增不改旧契约）
#
# 第一性原理：等待的唯一正当理由是「物理条件未成立」，不是「秒数没走完」。
# 以下两个纯函数把「等到什么时候算够」从时间维度翻译到证据维度：
# 调用方（eyes）按序注入采样指纹，这里只做同一性判断，不做任何 IO。
# ---------------------------------------------------------------------------

def settle_status(base, samples, required=2) -> dict:
    """
    「变化后稳定」判定（纯函数，零 IO）：
    base     动作前的基准指纹；None 表示无基准——无法证明发生过变化，保守判未稳定
    samples  动作后按时间顺序采到的指纹序列（可含 None：该帧证据缺失）
    required 变化被观察到之后，需要的连续稳定帧数
    返回 {"changed": bool, "stable_count": int, "settled": bool}：
      changed      序列中是否出现过相对 base 的实质变化
      stable_count 最后一次变化之后、与前一帧无实质变化的连续帧数
                   （None 帧会清零——证据缺失无法证明稳定，宁多等不误判稳定）
      settled      changed 且 stable_count >= required，即「变化后稳定」成立
    """
    changed = False
    stable_count = 0
    prev = base
    for s in samples:
        if s is None:
            stable_count = 0  # 证据缺失帧：无法证明稳定，计数清零
            continue
        if prev is None:
            prev = s  # 无基准时的首帧只作后续帧的比较基准，不算变化证据
            continue
        if diff_states(prev, s)["changed"]:
            if prev is base:
                changed = True  # 相对动作前基准发生实质变化
            stable_count = 0    # 帧间仍在变：稳定计数清零，基准推进到本帧
            prev = s
        else:
            stable_count += 1
    return {"changed": changed, "stable_count": stable_count,
            "settled": bool(changed and stable_count >= required)}


def first_change(base, samples) -> bool:
    """序列中是否已出现相对 base 的实质变化（纯函数，零 IO）。
    base 为 None 时保守返回 False（证明不了变化就按没变处理，继续等）。"""
    if base is None:
        return False
    for s in samples:
        if s is not None and diff_states(base, s)["changed"]:
            return True
    return False
