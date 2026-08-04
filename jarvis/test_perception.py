# -*- coding: utf-8 -*-
"""Gap5 屏幕状态流 · 单元测试（不碰真机：截图字节/控件清单全部内存构造）。

覆盖：
    1. 同状态 diff → changed=False（含光标微动噪声场景）
    2. 控件增删 → changed=True 且 describe_change 文本正确
    3. 像素大变（>5%）→ changed=True
    4. 窗口切换（window_sig 不同）→ changed=True + focus_shift=True
    5. should_review 两种场景 + 首步无前值
    6. 截图字节异常/为空时不抛异常（防御降级，保守判变）
    7. 性能抽查：capture_state + diff_states 全程毫秒级
"""
import io
import time

from PIL import Image, ImageDraw

import perception


# ---------------------------------------------------------------------------
# Mock 构造器：内存造截图字节与控件清单，零 IO
# ---------------------------------------------------------------------------

def _截图(光标位置=None, 大块=None) -> bytes:
    """造一张 640x360 灰底 PNG 截图字节；可叠加光标小块/内容大块。"""
    img = Image.new("L", (640, 360), 200)
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 600, 80], fill=240)   # 标题栏：让感知哈希有结构
    draw.rectangle([40, 300, 600, 340], fill=120)  # 底栏
    if 光标位置:
        x, y = 光标位置
        draw.rectangle([x, y, x + 5, y + 5], fill=0)  # 6x6 光标，物理噪声
    if 大块:
        draw.rectangle(大块, fill=30)  # 内容区大变
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _控件(*名称) -> list:
    """造 uia.dump_window_controls 格式的假控件清单。"""
    return [{"name": n, "control_type": "按钮",
             "rect": (100, 100 + 40 * i, 80, 30), "enabled": True}
            for i, n in enumerate(名称)]


_窗口 = (12345, "记事本")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_同状态不判变_含光标噪声():
    base = _截图(光标位置=(300, 200))
    moved = _截图(光标位置=(320, 210))  # 光标挪了 20px：纯噪声
    a = perception.capture_state(_窗口, base, _控件("文件", "编辑"))
    b = perception.capture_state(_窗口, moved, _控件("文件", "编辑"))
    d = perception.diff_states(a, b)
    assert not d["changed"], "光标微动不应判变：%s" % d
    assert d["pixel_change_ratio"] < perception._PIXEL_CHANGE_RATIO
    assert not d["focus_shift"]
    assert perception.describe_change(d) == "界面无实质变化"

    identical = perception.capture_state(_窗口, base, _控件("文件", "编辑"))
    d2 = perception.diff_states(a, identical)
    assert not d2["changed"] and d2["pixel_change_ratio"] == 0.0
    print("✅ 1/7 同状态（含光标微动噪声）判 changed=False")


def test_控件增删判变且文本正确():
    shot = _截图()
    a = perception.capture_state(_窗口, shot, _控件("文件", "编辑"))
    b = perception.capture_state(_窗口, shot,
                                 _控件("文件", "编辑", "确定", "取消", "应用"))
    d = perception.diff_states(a, b)
    assert d["changed"], "控件新增应判变"
    assert d["controls_added"] == ["确定", "取消", "应用"]
    assert not d["controls_removed"] and not d["focus_shift"]
    text = perception.describe_change(d)
    assert "新增 3 个控件" in text and "确定" in text and "取消" in text, text

    # 反向：控件消失同样判变
    d_rev = perception.diff_states(b, a)
    assert d_rev["changed"] and d_rev["controls_removed"] == ["确定", "取消", "应用"]
    assert "消失 3 个控件" in perception.describe_change(d_rev)
    print("✅ 2/7 控件增删判 changed=True，describe_change=%r" % text)


def test_像素大变判变():
    a = perception.capture_state(_窗口, _截图(), _控件("文件"))
    # 300x200 内容区填深色：占全图 26%，远超 5% 阈值
    b = perception.capture_state(_窗口, _截图(大块=[100, 100, 400, 300]), _控件("文件"))
    d = perception.diff_states(a, b)
    assert d["changed"], "像素大变应判变"
    assert d["pixel_change_ratio"] > perception._PIXEL_CHANGE_RATIO
    assert not d["controls_added"] and not d["controls_removed"]
    assert "%" in perception.describe_change(d)
    print("✅ 3/7 像素大变 %.0f%% 判 changed=True" % (d["pixel_change_ratio"] * 100))


def test_窗口切换判变且焦点转移():
    shot = _截图()
    a = perception.capture_state(_窗口, shot, _控件("文件"))
    b = perception.capture_state((67890, "浏览器"), shot, _控件("文件"))
    d = perception.diff_states(a, b)
    assert d["changed"] and d["focus_shift"], "窗口切换应判变且 focus_shift=True"
    assert "窗口/焦点已切换" in perception.describe_change(d)

    # 同标题同句柄但尺寸变（最大化/还原）：也是窗口级变化
    c = perception.capture_state(_窗口, _截图(), _控件("文件"))
    img_small = Image.new("L", (320, 180), 200)
    buf = io.BytesIO()
    img_small.save(buf, format="PNG")
    e = perception.capture_state(_窗口, buf.getvalue(), _控件("文件"))
    d2 = perception.diff_states(c, e)
    assert d2["focus_shift"], "窗口尺寸变化应反映到 window_sig"
    print("✅ 4/7 窗口切换/尺寸变化判 changed=True + focus_shift=True")


def test_should_review_两种场景():
    shot = _截图()
    a = perception.capture_state(_窗口, shot, _控件("文件"))
    same = perception.capture_state(_窗口, shot, _控件("文件"))
    assert perception.should_review(a, same) is False, "没变不应复核"

    changed = perception.capture_state(_窗口, _截图(大块=[100, 100, 400, 300]),
                                       _控件("文件"))
    assert perception.should_review(a, changed) is True, "变了必须复核"
    assert perception.should_review(None, a) is True, "首步无前值必须复核"
    print("✅ 5/7 should_review：没变 False / 变了 True / 首步 True")


def test_异常截图防御降级():
    # 空字节 / 垃圾字节：capture_state 与 diff 全程不抛异常
    a = perception.capture_state(_窗口, b"", _控件("文件"))
    assert a.pixel_sig == perception._DEGRADED_PIXEL_SIG and not a.thumb
    b = perception.capture_state(_窗口, "不是图片".encode("utf-8"), _控件("文件"))
    d = perception.diff_states(a, b)
    assert d["changed"], "像素证据缺失应保守判变（交还 VLM）"
    assert d["pixel_change_ratio"] == 1.0
    assert perception.should_review(a, b) is True

    # 畸形控件清单（缺 rect / None）也不抛
    c = perception.capture_state(_窗口, _截图(), [{"name": "孤儿"}, None, {}])
    assert c.control_sig or True  # 不抛即过
    # b 为 None 的极端输入：判无变化（防御）
    assert perception.diff_states(a, None)["changed"] is False
    print("✅ 6/7 异常截图/畸形控件不抛异常，防御降级保守判变")


def test_性能毫秒级():
    shot = _截图()
    controls = _控件(*["控件%d" % i for i in range(40)])  # 满配 40 控件
    t0 = time.perf_counter()
    a = perception.capture_state(_窗口, shot, controls)
    b = perception.capture_state(_窗口, _截图(光标位置=(10, 10)), controls)
    perception.diff_states(a, b)
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 200, "两次指纹 + 一次 diff 耗时 %.1fms，超限" % ms
    print("✅ 7/7 性能：2 次 capture + 1 次 diff 共 %.1fms（毫秒级）" % ms)


if __name__ == "__main__":
    test_同状态不判变_含光标噪声()
    test_控件增删判变且文本正确()
    test_像素大变判变()
    test_窗口切换判变且焦点转移()
    test_should_review_两种场景()
    test_异常截图防御降级()
    test_性能毫秒级()
    print("\n🎉 Gap5 屏幕状态流单元测试 7/7 全过")
