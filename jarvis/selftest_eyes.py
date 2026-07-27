# -*- coding: utf-8 -*-
"""
selftest_eyes.py —— eyes 模块（屏幕感知 + GUI 自动化）自测脚本

八项验证，全部通过才算交付：
  1. 截屏：screenshot_b64() 非空且 base64 长度 > 10KB；
  2. 配置驱动：_vision_config() 读到 llm_config.json 的 vision_model
     （glm-4.5v）且 vision_extra_body 含 thinking disabled；
  3. VLM 连通性：真实截屏问 glm-4.5v「用一句话描述屏幕」，回复非空；
  4. 降级路径：monkeypatch 主模型调用抛超时，断言自动落到 glm-4v-flash；
  5. 坐标换算：_vlm_to_screen 单元测试（已知输入断言已知输出）；
  6. 失败报告具体化：mock VLM 持续 fail，断言报告含「第 N 步」、
     失败原因与屏幕状态描述；
  7. 目标应用缺失：mock VLM 报「屏幕上没有找到 X」，断言话术提示
     先 open_app，且第一步就返回（不做早退宽限）；
  8. 安全 E2E：开记事本 -> perform 输入「hello nolan」-> 断言成功话术
     -> taskkill 强杀记事本清理现场（E2E 会真实移动鼠标键盘，属预期）。

运行方式：cd jarvis && python selftest_eyes.py
"""

import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eyes  # noqa: E402  被测模块

# 失败话术前缀：perform 返回不以这些开头即视为成功话术
_FAIL_PREFIXES = (
    "先生，任务未能完成。",
    "先生，任务步数超出安全上限",
    "先生，检测到您将鼠标移至屏幕角落",
    "先生，我的视觉模块暂时无法连接",
)


def test_screenshot() -> None:
    """截屏契约：返回非空 str 且 base64 长度 > 10KB。"""
    shot = eyes.screenshot_b64()
    assert isinstance(shot, str) and len(shot) > 0, "截屏结果为空"
    size_kb = len(shot) / 1024
    assert len(shot) > 10 * 1024, "截图过小（%.1f KB），疑似黑屏或异常" % size_kb
    print("[PASS] 1. 截屏：base64 长度 %.1f KB（>10KB）" % size_kb)


def test_vision_config() -> None:
    """配置驱动断言：vision_model / vision_extra_body 从 llm_config.json 生效。"""
    cfg = eyes._load_llm_config()
    # 配置文件必须显式写入两个新字段（本轮升级写入）
    assert "vision_model" in cfg, "llm_config.json 缺少 vision_model 字段"
    assert "vision_extra_body" in cfg, "llm_config.json 缺少 vision_extra_body 字段"
    # 既有字段必须原样保留（不被本轮修改破坏）
    for key in ("api_key", "base_url", "model", "extra_body"):
        assert key in cfg, "llm_config.json 既有字段 %s 丢失" % key

    model, extra = eyes._vision_config()
    assert model == cfg["vision_model"], \
        "_vision_config 模型名与配置不符：%s != %s" % (model, cfg["vision_model"])
    assert model == "glm-4.5v", "vision_model 应为 glm-4.5v，实际 %s" % model
    assert isinstance(extra, dict), "vision_extra_body 应解析为 dict"
    assert extra.get("thinking", {}).get("type") == "disabled", \
        "vision_extra_body 应关闭思考模式，实际 %s" % extra

    # 缺省回落：临时把配置字段拿掉，应回落到默认 glm-4.5v
    orig_load = eyes._load_llm_config
    eyes._load_llm_config = lambda: {k: v for k, v in cfg.items()
                                     if k not in ("vision_model",
                                                  "vision_extra_body")}
    try:
        d_model, d_extra = eyes._vision_config()
    finally:
        eyes._load_llm_config = orig_load
    assert d_model == "glm-4.5v", "缺省 vision_model 应回落 glm-4.5v，实际 %s" % d_model
    assert d_extra.get("thinking", {}).get("type") == "disabled", \
        "缺省 vision_extra_body 应关闭思考模式"

    print("[PASS] 2. 配置驱动：vision_model=%s，thinking 已关闭，缺省回落正确"
          % model)


def test_vlm_connectivity() -> None:
    """glm-4.5v 连通性：发真实截屏问一句话描述，回复必须非空。"""
    t0 = time.time()
    shot = eyes.screenshot_b64()
    reply = eyes._ask_vlm(shot, "用一句话描述这张屏幕截图的内容。")
    cost = time.time() - t0
    assert isinstance(reply, str) and reply.strip(), "VLM 回复为空"
    print("[PASS] 3. VLM 连通（%s，%.1f s）：%s"
          % (eyes._vision_config()[0], cost, reply.strip()[:60]))


def test_fallback() -> None:
    """
    降级路径：monkeypatch _ask_vlm_once，主模型一律抛超时，
    断言 _ask_vlm 自动落到 glm-4v-flash 且调用顺序正确；
    另断言降级请求不带 vision_extra_body（旧模型兼容性）。
    """
    calls = []
    primary, primary_extra = eyes._vision_config()
    orig = eyes._ask_vlm_once

    def fake_once(image_b64, user_text, system, model, extra_body):
        calls.append((model, extra_body))
        if model == primary:
            raise eyes.httpx.TimeoutException("模拟主模型超时")
        return "降级模型回复"

    eyes._ask_vlm_once = fake_once
    try:
        reply = eyes._ask_vlm("dummy_b64", "测试降级")
    finally:
        eyes._ask_vlm_once = orig

    assert reply == "降级模型回复", "降级后应返回降级模型的回复"
    assert [c[0] for c in calls] == [primary, eyes._VLM_FALLBACK_MODEL], \
        "调用顺序应为 主模型 -> glm-4v-flash，实际 %s" % calls
    assert calls[0][1] == primary_extra, "主模型请求应带 vision_extra_body"
    assert calls[1][1] == {}, "降级请求不应带 vision_extra_body"

    # 主模型已是 flash 时不再循环降级：异常应直接上抛
    def always_fail(image_b64, user_text, system, model, extra_body):
        raise eyes.httpx.TimeoutException("模拟 flash 也超时")

    eyes._ask_vlm_once = always_fail
    orig_cfg = eyes._vision_config
    eyes._vision_config = lambda: (eyes._VLM_FALLBACK_MODEL, {})
    try:
        raised = False
        try:
            eyes._ask_vlm("dummy_b64", "测试不循环降级")
        except Exception:
            raised = True
        assert raised, "主模型已是 flash 时异常应直接上抛，不再降级"
    finally:
        eyes._ask_vlm_once = orig
        eyes._vision_config = orig_cfg

    print("[PASS] 4. 降级路径：%s 超时 -> %s 兜底成功，且不循环降级"
          % (primary, eyes._VLM_FALLBACK_MODEL))


def test_coord_mapping() -> None:
    """坐标换算单元测试：截图像素 -> 屏幕物理像素，含比例与钳制。"""
    screen_w, screen_h = eyes.pyautogui.size()  # 物理分辨率（DPI 感知后）

    # 用 1280x800 假想截图验证比例换算（不依赖真实截屏尺寸）
    shot_w, shot_h = 1280, 800
    sx = screen_w / shot_w
    sy = screen_h / shot_h

    # 中心点换算
    px, py = eyes._vlm_to_screen(640, 400, shot_w, shot_h)
    assert px == round(640 * sx) and py == round(400 * sy), \
        "中心点换算错误：(%d,%d)" % (px, py)

    # 原点不变
    assert eyes._vlm_to_screen(0, 0, shot_w, shot_h) == (0, 0), "原点应不变"

    # 越界坐标钳制到屏幕内
    px, py = eyes._vlm_to_screen(99999, -50, shot_w, shot_h)
    assert px == screen_w - 1 and py == 0, "越界钳制错误：(%d,%d)" % (px, py)

    # 默认路径（不传截图尺寸）应使用真实截图尺寸且不报错
    px, py = eyes._vlm_to_screen(100, 100)
    assert 0 <= px < screen_w and 0 <= py < screen_h, "默认换算越界"

    print("[PASS] 5. 坐标换算：比例 / 原点 / 钳制 / 默认路径均正确"
          "（屏幕 %dx%d）" % (screen_w, screen_h))


class _MockVLM:
    """
    mock 视觉模型：按 user_text 分流——
    屏幕描述补问返回固定描述，动作决策返回固定 fail JSON。
    同时把截屏替换为哑串，让整个 perform 闭环不依赖真实屏幕与网络。
    """

    def __init__(self, fail_thought: str, screen_desc: str):
        self.fail_thought = fail_thought
        self.screen_desc = screen_desc
        self.action_calls = 0

    def __enter__(self):
        self._orig_ask = eyes._ask_vlm
        self._orig_shot = eyes.screenshot_b64

        def fake_ask(image_b64, user_text, system=None):
            if "描述" in user_text:
                return self.screen_desc
            self.action_calls += 1
            return '{"action": "fail", "thought": "%s"}' % self.fail_thought

        eyes._ask_vlm = fake_ask
        eyes.screenshot_b64 = lambda: "dummy_b64"
        return self

    def __exit__(self, *_exc):
        eyes._ask_vlm = self._orig_ask
        eyes.screenshot_b64 = self._orig_shot
        return False


def test_fail_report() -> None:
    """
    失败报告具体化：mock VLM 持续 fail（非应用缺失类），
    前两次触发零动作早退宽限，第 3 步正式报告；
    断言报告含「第 3 步」、VLM 失败原因、当前屏幕状态描述。
    """
    with _MockVLM("未找到「我喜欢」入口",
                  "网易云音乐窗口，停留在发现音乐页") as mock:
        result = eyes.perform("在网易云音乐里播放我喜欢列表第一首歌", max_steps=3)

    print("[INFO] perform 失败报告：%s" % result)
    assert result.startswith("先生，任务未能完成。"), \
        "失败报告应保留契约前缀：%s" % result
    assert "第 3 步" in result, "失败报告应指明失败步骤：%s" % result
    assert "未找到「我喜欢」入口" in result, "失败报告应含 VLM 判断：%s" % result
    assert "网易云音乐窗口" in result, "失败报告应含屏幕状态描述：%s" % result
    assert mock.action_calls == 3, "应走满 3 步（2 次宽限 + 1 次正式失败）"
    print("[PASS] 6. 失败报告具体化：步骤 / 原因 / 屏幕状态齐备")


def test_app_not_found() -> None:
    """
    目标应用缺失：mock VLM 报「屏幕上没有找到网易云音乐」，
    断言第一步即返回（不做早退宽限），且话术明确提示先 open_app。
    """
    with _MockVLM("屏幕上没有找到网易云音乐", "只有桌面壁纸") as mock:
        result = eyes.perform("在网易云音乐里播放我喜欢列表第一首歌",
                              max_steps=12)

    print("[INFO] 应用缺失话术：%s" % result)
    assert result.startswith("先生，任务未能完成。"), \
        "应用缺失报告应保留契约前缀：%s" % result
    assert "屏幕上没有找到网易云音乐" in result, "应点名缺失的应用：%s" % result
    assert "open_app" in result, "应提示先用 open_app 打开：%s" % result
    assert mock.action_calls == 1, "应用缺失应立即报告，不做早退宽限"
    print("[PASS] 7. 目标应用缺失：首步即报，话术含 open_app 提示")


def _foreground_title(user32) -> str:
    """返回当前前台窗口标题（空串表示获取失败）。"""
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _activate_notepad(user32) -> bool:
    """
    兜底前台化：枚举所有窗口找到记事本主窗口，恢复并置前台。
    非交互式会话里新窗口偶发不抢前台，需要主动激活。
    """
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            t = buf.value
            if "Notepad" in t or "记事本" in t:
                found.append(hwnd)
        return True

    user32.EnumWindows(_enum, 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE：从最小化/隐藏恢复
    user32.SetForegroundWindow(hwnd)
    return True


def test_notepad_e2e() -> None:
    """
    安全 E2E：真实开记事本、真实动鼠标键盘输入文字。
    perform 返回成功话术（不以失败前缀开头）即通过；结束后强杀记事本。

    确定性处理（两道）：
      1. Win11 记事本会话恢复会让旧内容重现，干扰 VLM 判断，
         因此每次以「打开一个全新空文件」的方式启动（新标签页必为活动页）；
      2. 启动后不仅等进程，还轮询 Windows 前台窗口标题确认记事本真正
         来到前台；偶发不抢前台时用 EnumWindows + SetForegroundWindow 兜底。
    """
    print("[INFO] 8. E2E：即将打开记事本并真实操作鼠标键盘，请勿触碰……")
    # 清洁起点：先杀残留记事本进程，避免新文件只开成既有隐藏窗口的标签页
    subprocess.run(["taskkill", "/im", "notepad.exe", "/f"], capture_output=True)
    time.sleep(1)
    # 在 jarvis 目录下造一个唯一的空文件，让记事本以它为活动标签页打开
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_selftest_eyes_%d.txt" % int(time.time()))
    with open(tmp, "w", encoding="utf-8"):
        pass
    subprocess.Popen(["notepad.exe", tmp])  # 新文件标签页必为活动页，避开会话恢复干扰

    # 确定性等待：轮询前台窗口标题，直到记事本真正来到前台
    user32 = ctypes.windll.user32
    base = os.path.basename(tmp)
    foreground = False
    for _ in range(20):  # 最多等 20 秒
        title = _foreground_title(user32)
        if "记事本" in title or "Notepad" in title or base in title:
            foreground = True
            break
        time.sleep(1)
    if not foreground:
        # 兜底：主动把记事本窗口置前台，再等最多 5 秒确认
        print("[INFO] 记事本未自动前台，尝试主动激活……")
        if _activate_notepad(user32):
            for _ in range(5):
                title = _foreground_title(user32)
                if "记事本" in title or "Notepad" in title or base in title:
                    foreground = True
                    break
                time.sleep(1)
    print("[INFO] 记事本前台确认：%s" % ("是" if foreground else "否"))
    assert foreground, "环境异常：记事本窗口未能前台化，无法进行 E2E"
    time.sleep(1)  # 前台后再等界面渲染稳定
    try:
        result = eyes.perform("在记事本中输入 hello nolan，然后报告完成",
                              max_steps=6)
        print("[INFO] perform 返回：%s" % result)
        for prefix in _FAIL_PREFIXES:
            assert not result.startswith(prefix), "E2E 返回失败话术：%s" % result
        print("[PASS] 8. E2E：记事本输入任务返回成功话术")
    finally:
        # 无论成败都清理现场：强杀所有记事本进程 + 删除临时文件
        subprocess.run(["taskkill", "/im", "notepad.exe", "/f"],
                       capture_output=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("[INFO] 记事本与临时文件已清理")


def main() -> int:
    print("=" * 60)
    print("eyes 模块自测（视觉模型升级：配置驱动 + 降级 + 具体化失败报告）")
    print("=" * 60)
    test_screenshot()
    test_vision_config()
    test_vlm_connectivity()
    test_fallback()
    test_coord_mapping()
    test_fail_report()
    test_app_not_found()
    test_notepad_e2e()
    print("=" * 60)
    print("全部自测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
