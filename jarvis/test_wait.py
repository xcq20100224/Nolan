# -*- coding: utf-8 -*-
"""
test_wait.py —— B1 速度战役「条件等待」纯 mock 单测

不碰真机 GUI：用假截图指纹 / 假前台标题脚本验证条件等待的三条保底契约——
  1. 变化后稳定：物理条件成立立即提前走，实际等待显著低于上限；
  2. 一直不变：安静等到上限，行为与旧版固定 sleep 等价（不添乱）；
  3. 异常回退：采样抛异常时补足原固定预算，绝不引入「等不到」的新失败模式。
另覆盖 perception 新增纯函数 settle_status / first_change 的边界语义。

运行：python jarvis/test_wait.py   （仅标准库 + 本仓库模块，零真机依赖）
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 纯 mock 环境可能缺 GUI 依赖：打桩让 eyes 可导入（桩仅在真实依赖缺席时启用）
for _name in ("pyautogui", "pyperclip", "httpx"):
    try:
        __import__(_name)
    except Exception:
        _stub = types.ModuleType(_name)
        if _name == "pyautogui":
            _stub.FailSafeException = type("FailSafeException", (Exception,), {})
            _stub.FAILSAFE = True
            _stub.PAUSE = 0.0
        sys.modules[_name] = _stub

import perception
import eyes
from perception import ScreenState

_THUMB_N = 64 * 36  # 与 perception 的缩略图尺寸一致（64x36 灰度，2304 字节）


def _state(byte: int) -> ScreenState:
    """造一个假指纹：窗口身份与控件相同，仅靠像素底稿区分「变 / 不变」。"""
    return ScreenState(window_sig="title:测试窗口|100x100",
                       control_sig="sig",
                       pixel_sig=1,
                       ts=0.0,
                       thumb=bytes([byte]) * _THUMB_N,
                       controls=())


_A = _state(0)   # 基准帧
_B = _state(80)  # 变化帧（灰度差 80 > 噪声阈值 24，变格占比 100%）

_failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print("[%s] %s%s" % (tag, name, (" —— " + detail) if detail else ""))
    if not cond:
        _failures.append(name)


# ---- 场景 1：变化后稳定 -> 提前走（settle）----
_script = iter([_B, _B, _B, _B, _B])
t1, f1 = eyes._wait_screen_settled(
    _A, None, 1.0, min_wait=0.05, poll=0.02,
    sample_fn=lambda: next(_script, _B))
check("1a 变化后稳定提前走", t1 < 0.6, "上限 1.0s，实际 %.2fs" % t1)
check("1b 返回末帧供复用", f1 is not None)

# ---- 场景 2：一直不变 -> 安静等到上限（与旧固定 sleep 等价）----
t2, _ = eyes._wait_screen_settled(
    _A, None, 0.6, min_wait=0.05, poll=0.02, sample_fn=lambda: _A)
check("2 不变等到上限", t2 >= 0.55, "上限 0.6s，实际 %.2fs" % t2)

# ---- 场景 3：采样异常 -> 回退补足原固定预算 ----
def _boom():
    raise RuntimeError("假截屏爆炸")


t3, f3 = eyes._wait_screen_settled(
    _A, None, 0.4, min_wait=0.05, poll=0.02, sample_fn=_boom)
check("3 异常回退满额等待", t3 >= 0.35 and f3 is None, "上限 0.4s，实际 %.2fs" % t3)

# ---- 场景 4：无基准指纹（首步）-> 固定 sleep 保守等待 ----
t4, f4 = eyes._wait_screen_settled(None, None, 0.3, sample_fn=lambda: _B)
check("4 无基准固定等待", t4 >= 0.25 and f4 is None, "上限 0.3s，实际 %.2fs" % t4)

# ---- 场景 5：宽限等待——指纹一变即提前走 ----
_seq5 = iter([_A, _B])
e5, ch5, _ = eyes._wait_for_change(
    _A, None, 1.0, poll=0.02, sample_fn=lambda: next(_seq5, _B))
check("5 变化即走", ch5 and e5 < 0.6, "上限 1.0s，实际 %.2fs" % e5)

# ---- 场景 6：宽限等待——一直不变等到上限，不误报变化 ----
e6, ch6, _ = eyes._wait_for_change(_A, None, 0.5, poll=0.02,
                                   sample_fn=lambda: _A)
check("6 不变超时满额", (not ch6) and e6 >= 0.45, "上限 0.5s，实际 %.2fs" % e6)

# ---- 场景 7：置前后等待——前台标题确认即走；确认不了等到上限 ----
_titles = iter(["别的窗口", "别的窗口", "网易云音乐", "网易云音乐"])


class _FakeUia:
    @staticmethod
    def foreground_title():
        return next(_titles, "网易云音乐")


_old_uia = eyes._uia
eyes._uia = _FakeUia
try:
    t7 = eyes._wait_foreground("网易云", 1.0, poll=0.02)
    check("7a 前台确认即走", t7 < 0.6, "上限 1.0s，实际 %.2fs" % t7)
    t8 = eyes._wait_foreground("永不出现的窗口", 0.5, poll=0.02)
    check("7b 前台超时满额", t8 >= 0.45, "上限 0.5s，实际 %.2fs" % t8)
finally:
    eyes._uia = _old_uia

# ---- 场景 8：perception 纯函数边界语义 ----
st = perception.settle_status(_A, [_B, _B, None, _B], required=2)
check("8a None帧清零稳定计数",
      st["changed"] and st["stable_count"] == 1 and not st["settled"], str(st))
st2 = perception.settle_status(_A, [_B, _B, _B], required=2)
check("8b 变化后连续两帧稳定即 settled", st2["settled"], str(st2))
st3 = perception.settle_status(_A, [_A, _A, _A], required=2)
check("8c 无变化永不 settled", not st3["settled"] and not st3["changed"], str(st3))
st4 = perception.settle_status(None, [_B, _B], required=1)
check("8d 无基准保守不 settled", not st4["settled"], str(st4))
check("8e first_change 有/无基准",
      perception.first_change(_A, [_A, _B]) is True
      and perception.first_change(_A, [_A, _A]) is False
      and perception.first_change(None, [_B]) is False)

print()
if _failures:
    print("失败 %d 项：%s" % (len(_failures), "、".join(_failures)))
    sys.exit(1)
print("全部通过：条件等待三场景 + 纯函数边界语义均符合保底契约。")
