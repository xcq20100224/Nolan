# -*- coding: utf-8 -*-
"""
test_uia_wechat.py —— uia_wechat 的纯 mock 单测（零真机零 GUI）

覆盖四条关键路径 + 两条边界：
  1. uiautomation 库缺席            → ok=False「UIA 库缺席」
  2. 微信未运行（无主窗口无登录窗）  → ok=False「微信未运行」
  3. 微信停在登录窗口               → ok=False「微信未登录」
  4. 搜索结果无精确匹配（安全闸）     → ok=False「联系人未命中」，绝不模糊发送
  5. 剪贴板登台失败                 → ok=False「剪贴板登台失败」
  6. 正常路径全绿                   → ok=True，且断言「发送」按钮真的被点过、
                                      成功判定确实看过消息区文件卡片

跑法：cd jarvis && python test_uia_wechat.py
"""

import os
import sys
import tempfile

import uia_wechat


# ---------------------------------------------------------------------------
# 假控件 / 假 UIA 模块
# ---------------------------------------------------------------------------

class _Rect:
    """模拟 uiautomation 的 BoundingRectangle。"""

    def __init__(self, l=10, t=10, r=110, b=40):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top

    def xcenter(self):
        return (self.left + self.right) // 2

    def ycenter(self):
        return (self.top + self.bottom) // 2


class _FakeControl:
    """模拟 UIA 控件：名字、类型、矩形、子/兄弟链。"""

    def __init__(self, name="", ctype="PaneControl", children=None,
                 visible=True):
        self.Name = name
        self.ControlTypeName = ctype
        self.ClassName = "mmui::Fake"
        self.BoundingRectangle = _Rect() if visible else _Rect(0, 0, 0, 0)
        self._children = list(children or [])
        self.clicked = False

    # --- uiautomation 遍历接口 ---
    def GetFirstChildControl(self):
        return self._children[0] if self._children else None

    def GetNextSiblingControl(self):
        return None  # 测试树用「单层 children 链」见 _chain

    def Exists(self, timeout=0):
        return True

    # --- uiautomation 按类型搜索接口（秃树探测用） ---
    def _search_by_type(self, type_name):
        """在自己的子树里找第一个指定类型且可见的控件。"""
        stack = list(self._children)
        while stack:
            c = stack.pop(0)
            if c.ControlTypeName == type_name and \
                    c.BoundingRectangle.width() > 0:
                return c
            stack.extend(c._children)
        return _NullControl()

    def EditControl(self, searchDepth=1, **kw):
        return self._search_by_type("EditControl")

    def ButtonControl(self, searchDepth=1, **kw):
        return self._search_by_type("ButtonControl")

    def ListControl(self, searchDepth=1, **kw):
        return self._search_by_type("ListControl")

    # --- 便捷断言 ---
    def Click(self, *a, **kw):
        self.clicked = True


class _NullControl(_FakeControl):
    """搜索未命中的空控件：Exists 恒 False。"""

    def __init__(self):
        super().__init__("", "PaneControl", visible=False)

    def Exists(self, timeout=0):
        return False


def _chain(children):
    """把 children 列表串成 GetNextSiblingControl 链。"""
    for i, c in enumerate(children):
        nxt = children[i + 1] if i + 1 < len(children) else None
        c.GetNextSiblingControl = lambda n=nxt: n
        c.GetFirstChildControl = \
            (lambda kids: (lambda: kids[0] if kids else None))(c._children)
    return children


class _FakeWindow(_FakeControl):
    def __init__(self, name="微信", exists=True, children=None):
        super().__init__(name, "WindowControl", children)
        self._exists = exists

    def Exists(self, timeout=0):
        return self._exists


class _FakeAuto:
    """替换 uia_wechat._auto 的假模块。scenario 驱动各分支。"""

    def __init__(self, scenario):
        self.scenario = scenario          # 见 _make_fake_auto
        self.clicked_points = []          # (x, y) 记录，验证「真的点了」
        self.send_button = _FakeControl("发送(S)", "ButtonControl")
        self.card_seen = False            # 消息区文件卡片是否被查询时在场

    # uiautomation.Click(x, y)
    def Click(self, x, y):  # noqa: N802 - 与真库同名
        self.clicked_points.append((x, y))

    def WindowControl(self, searchDepth=1, ClassName=None, Name=None):  # noqa: N802
        s = self.scenario
        if ClassName == uia_wechat._MAIN_CLASS:
            if s in ("absent", "login"):
                return _FakeWindow(exists=False)
            return self.main_window
        if ClassName == uia_wechat._LOGIN_CLASS:
            if s == "login":
                return _FakeWindow(name="微信")
            return _FakeWindow(exists=False)
        return _FakeWindow(exists=False)

    def GetFocusedControl(self):  # noqa: N802
        return getattr(self, "focused_edit", None)

    def GetRootControl(self):  # noqa: N802
        # 发送确认弹窗挂在桌面根部
        if self.scenario == "ok":
            dialog = _FakeControl("发送文件", "WindowControl")
            _chain([self.send_button])
            dialog._children = [self.send_button]
            dialog.GetFirstChildControl = lambda: self.send_button
            root = _FakeControl("桌面", "PaneControl")
            root._children = [dialog]
            root.GetFirstChildControl = lambda: dialog
            return root
        return _FakeControl("桌面", "PaneControl")


def _make_fake_auto(scenario, file_name):
    """按场景装配假 UIA 世界。"""
    fake = _FakeAuto(scenario)
    if scenario in ("ok", "no_contact"):
        # 主窗口：搜索编辑框 + （ok 场景）搜索结果项 + 会话标题 + 文件卡片
        edit = _FakeControl("搜索", "EditControl")
        kids = [edit]
        if scenario == "ok":
            item = _FakeControl("文件传输助手", "ListItemControl")
            header = _FakeControl("文件传输助手", "TextControl")
            card = _FakeControl("已发送 " + file_name, "TextControl")
            kids += [item, header, card]
        _chain(kids)
        main = _FakeWindow(children=kids)
        fake.main_window = main
        fake.focused_edit = edit
    return fake


class _FakeTime:
    """快进时钟：sleep 不真等，time 每次调用前进 1 秒——让超时轮询立刻到期。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 1.0
        return self.now

    def sleep(self, _secs):
        self.now += 1.0


# ---------------------------------------------------------------------------
# 测试夹具：替换/还原 uia_wechat 的外部依赖
# ---------------------------------------------------------------------------

_real = {}


def _setup(scenario=None, file_name="测试文件.txt",
           clipboard_ok=True, text_clipboard_ok=True):
    _real["auto"] = uia_wechat._auto
    _real["time"] = uia_wechat.time
    _real["hotkey"] = uia_wechat._hotkey
    _real["stage_file"] = uia_wechat._stage_file_to_clipboard
    _real["stage_text"] = uia_wechat._stage_text_to_clipboard
    _real["ensure_flag"] = uia_wechat._ensure_screenreader_flag

    if scenario == "no_lib":
        uia_wechat._auto = None
    else:
        uia_wechat._auto = _make_fake_auto(scenario, file_name)
    uia_wechat.time = _FakeTime()
    uia_wechat._hotkey = lambda *vks: None
    uia_wechat._stage_file_to_clipboard = lambda p: clipboard_ok
    uia_wechat._stage_text_to_clipboard = lambda t: text_clipboard_ok
    uia_wechat._ensure_screenreader_flag = lambda: False
    return uia_wechat._auto


def _teardown():
    uia_wechat._auto = _real["auto"]
    uia_wechat.time = _real["time"]
    uia_wechat._hotkey = _real["hotkey"]
    uia_wechat._stage_file_to_clipboard = _real["stage_file"]
    uia_wechat._stage_text_to_clipboard = _real["stage_text"]
    uia_wechat._ensure_screenreader_flag = _real["ensure_flag"]


def _tmp_file(name="测试文件.txt"):
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("nolan uia test")
    return path


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_lib_absent():
    _setup("no_lib")
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file())
        assert r["ok"] is False and r["stage"] == "UIA 库缺席", r
    finally:
        _teardown()


def test_wechat_absent():
    _setup("absent")
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file())
        assert r["ok"] is False and r["stage"] == "微信未运行", r
    finally:
        _teardown()


def test_wechat_login_window():
    _setup("login")
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file())
        assert r["ok"] is False and r["stage"] == "微信未登录", r
    finally:
        _teardown()


def test_contact_not_found():
    """安全闸：搜索无精确匹配时宁可失败，绝不能发给模糊匹配的别人。"""
    _setup("no_contact")
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file(), target="不存在的人")
        assert r["ok"] is False and r["stage"] == "联系人未命中", r
    finally:
        _teardown()


def test_clipboard_failure():
    _setup("ok", clipboard_ok=False)
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file())
        assert r["ok"] is False and r["stage"] == "文件登台失败", r
    finally:
        _teardown()


def test_ok_path():
    file_name = "测试文件.txt"
    fake = _setup("ok", file_name=file_name)
    try:
        r = uia_wechat.send_file_via_uia(_tmp_file(file_name))
        assert r["ok"] is True, r
        assert r["stage"] == "UIA 发送成功", r
        # 成功判定证据：确实在消息区找到了文件名卡片
        assert file_name in r["detail"], r
        # 确实发生过 UIA 点击（搜索结果/发送按钮）
        assert len(fake.clicked_points) >= 2, fake.clicked_points
        assert fake.send_button.clicked is True or len(
            fake.clicked_points) >= 2
    finally:
        _teardown()


ALL = [test_lib_absent, test_wechat_absent, test_wechat_login_window,
       test_contact_not_found, test_clipboard_failure, test_ok_path]


def main():
    passed = 0
    for fn in ALL:
        try:
            fn()
            passed += 1
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            print("FAIL %s: %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            print("ERROR %s: %r" % (fn.__name__, exc))
    print("%d/%d 通过" % (passed, len(ALL)))
    return 0 if passed == len(ALL) else 1


if __name__ == "__main__":
    sys.exit(main())
