# -*- coding: utf-8 -*-
"""
test_ocr_fallback.py —— B3 眼睛补盲（UIA 盲区的 VLM-OCR 兜底）纯 mock 单测

不碰真机 GUI：验证四件事——
  1. 伪控件形态契约：_vlm_scan_elements 产出与 uia.dump_window_controls
     完全同构的字典 {name, control_type:"文本", rect:(x,y,w,h), enabled:True}，
     残缺项剔除、同名去重、坐标经缩放换算；
  2. 缓存命中：同一截图（内容哈希相同）只扫一次，不同截图重新扫；
  3. 合并去重：伪控件与 UIA 控件同构合并，名称重叠时 UIA 优先；
  4. 触发条件：UIA 枚举为空或 <3 个控件时启用补盲，>=3 个时零 VLM 往返。

运行：python jarvis/test_ocr_fallback.py   （仅标准库 + 本仓库模块）
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

import eyes

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond)))
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       (" | " + str(detail)) if detail else ""))


def _mock_scale():
    """坐标换算换成确定性的 x2 放大，断言只依赖纯算术，不碰真屏幕。"""
    eyes._vlm_to_screen = lambda x, y, *a, **k: (int(round(x * 2)),
                                                int(round(y * 2)))


def test_shape_contract():
    eyes._SCAN_CACHE.clear()
    _mock_scale()
    reply = ('[{"name": "播放", "x1": 10, "y1": 20, "x2": 60, "y2": 50},'
             ' {"name": "搜索框", "x1": 100, "y1": 5, "x2": 300, "y2": 35},'
             ' {"name": "", "x1": 0, "y1": 0, "x2": 9, "y2": 9},'
             ' {"name": "零面积", "x1": 5, "y1": 5, "x2": 5, "y2": 9},'
             ' {"name": "播放", "x1": 1, "y1": 1, "x2": 8, "y2": 8}]')
    eyes._ask_vlm = lambda img, text, system=None: reply
    els = eyes._vlm_scan_elements("c2hvdEE=")
    check("残缺项剔除 + 同名去重（5 项 -> 2 项）", len(els) == 2,
          str(len(els)))
    first = els[0] if els else {}
    check("伪控件形态：与 UIA 字典同构（键集合一致）",
          set(first.keys()) == {"name", "control_type", "rect", "enabled"})
    check("伪控件形态：name 取自可见文字", first.get("name") == "播放")
    check("伪控件形态：control_type 固定「文本」",
          first.get("control_type") == "文本")
    check("伪控件形态：enabled=True", first.get("enabled") is True)
    check("伪控件形态：rect=(x,y,w,h) 经缩放换算为物理像素",
          first.get("rect") == (20, 40, 100, 60), str(first.get("rect")))
    second = els[1] if len(els) > 1 else {}
    check("第二项 rect 换算", second.get("rect") == (200, 10, 400, 60),
          str(second.get("rect")))


def test_malformed_reply_safe():
    eyes._SCAN_CACHE.clear()
    _mock_scale()
    eyes._ask_vlm = lambda img, text, system=None: "这不是 JSON"
    check("非法回复返回空清单", eyes._vlm_scan_elements("c2hvdEg=") == [])
    eyes._ask_vlm = lambda img, text, system=None: '{"不是": "数组"}'
    check("非数组 JSON 返回空清单", eyes._vlm_scan_elements("c2hvdEg=") == [])

    def boom(img, text, system=None):
        raise RuntimeError("网络断开")
    eyes._ask_vlm = boom
    check("VLM 不可达返回空清单（不抛异常）",
          eyes._vlm_scan_elements("c2hvdEc=") == [])


def test_cache_hit():
    eyes._SCAN_CACHE.clear()
    _mock_scale()
    calls = {"n": 0}

    def fake_ask(img, text, system=None):
        calls["n"] += 1
        return '[{"name": "确定", "x1": 1, "y1": 1, "x2": 9, "y2": 9}]'

    eyes._ask_vlm = fake_ask
    eyes._vlm_scan_cached("c2hvdEI=")
    eyes._vlm_scan_cached("c2hvdEI=")
    check("同一截图只扫一次（缓存命中）", calls["n"] == 1, str(calls["n"]))
    eyes._vlm_scan_cached("c2hvdEM=")
    check("不同截图重新扫描", calls["n"] == 2, str(calls["n"]))


def test_merge_dedupe():
    uia = [{"name": "播放按钮", "control_type": "按钮",
            "rect": (0, 0, 10, 10), "enabled": True}]
    ocr = [{"name": "播放", "control_type": "文本",
            "rect": (0, 0, 20, 20), "enabled": True},
           {"name": "音量", "control_type": "文本",
            "rect": (30, 0, 20, 20), "enabled": True}]
    merged = eyes._merge_controls(uia, ocr)
    check("合并去重：名称重叠时 UIA 优先（「播放」被「播放按钮」覆盖）",
          [c["name"] for c in merged] == ["播放按钮", "音量"],
          [c["name"] for c in merged])
    check("空 UIA 时伪控件全保留", len(eyes._merge_controls([], ocr)) == 2)
    check("空 OCR 时 UIA 原样返回",
          eyes._merge_controls(uia, []) == uia)
    check("反向包含也去重（UIA「确定」覆盖 OCR「确定按钮」）",
          [c["name"] for c in eyes._merge_controls(
              [{"name": "确定", "control_type": "按钮",
                "rect": (0, 0, 5, 5), "enabled": True}],
              [{"name": "确定按钮", "control_type": "文本",
                "rect": (0, 0, 9, 9), "enabled": True}])] == ["确定"])


def test_trigger_condition():
    eyes._SCAN_CACHE.clear()
    _mock_scale()
    calls = {"n": 0}

    def fake_ask(img, text, system=None):
        calls["n"] += 1
        return '[{"name": "菜单", "x1": 1, "y1": 1, "x2": 9, "y2": 9}]'

    eyes._ask_vlm = fake_ask
    many = [{"name": "控件%d" % i, "control_type": "按钮",
             "rect": (i, i, 5, 5), "enabled": True} for i in range(3)]
    out = eyes._controls_with_ocr_fallback("c2hvdEQ=", many)
    check("UIA 控件 >=3 不触发补盲（零 VLM 往返）",
          calls["n"] == 0 and out is many, "calls=%d" % calls["n"])
    out = eyes._controls_with_ocr_fallback("c2hvdEU=", [])
    check("UIA 为空触发补盲并合并伪控件",
          calls["n"] == 1 and any(c["name"] == "菜单" for c in out))
    out = eyes._controls_with_ocr_fallback("c2hvdEY=", many[:2])
    check("UIA 控件 <3 同样触发（自绘界面判据）", calls["n"] == 2,
          "calls=%d" % calls["n"])
    out = eyes._controls_with_ocr_fallback("c2hvdEU=", [])
    check("补盲扫描自身也走缓存（同截图不重复扫）", calls["n"] == 2)


def test_scan_failure_no_fallback_controls():
    """补盲扫描失败（空清单）时原样返回 UIA 清单，不添乱。"""
    eyes._SCAN_CACHE.clear()

    def boom(img, text, system=None):
        raise RuntimeError("网络断开")
    eyes._ask_vlm = boom
    one = [{"name": "唯一控件", "control_type": "按钮",
            "rect": (0, 0, 5, 5), "enabled": True}]
    out = eyes._controls_with_ocr_fallback("c2hvdFo=", one)
    check("扫描失败时原样返回 UIA 清单", out == one)


if __name__ == "__main__":
    test_shape_contract()
    test_malformed_reply_safe()
    test_cache_hit()
    test_merge_dedupe()
    test_trigger_condition()
    test_scan_failure_no_fallback_controls()
    failed = [n for n, ok in _RESULTS if not ok]
    print("\n==== 共 %d 项：%d 通过，%d 失败 ===="
          % (len(_RESULTS), len(_RESULTS) - len(failed), len(failed)))
    if failed:
        for n in failed:
            print("  失败项：%s" % n)
    sys.exit(1 if failed else 0)
