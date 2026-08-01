# -*- coding: utf-8 -*-
"""
selftest_hands.py —— hands.py 的模块级自测

只测本模块，不依赖 ears/mouth/brain 等其他模块。
open_url / web_search / open_app 会真实弹窗，因此用 monkeypatch
替换 os.startfile，只验证分支逻辑，不真的启动程序。

运行方式：python selftest_hands.py
"""

import os
import json

import hands

# 预期工具名（与契约一字不差）
EXPECTED_TOOLS = [
    "open_app", "open_url", "web_search", "search_web", "fetch_url",
    "read_file", "write_file", "list_files", "run_shell", "get_time",
    "media_control", "gui_control", "capture_screen", "set_web_background",
]

# 模拟 ShellExecute 可解析的 App Paths 裸命令（which 找不到但能直接启动）
BARE_RESOLVABLE = {"notepad", "calc", "mspaint"}


def make_fake_startfile(opened: list):
    """
    生成逼真的 os.startfile 替代品：真实存在的路径、URL、
    PATH 可解析或 App Paths 注册的裸命令视为成功并记录；
    其余像真实 ShellExecute 一样抛 OSError，以覆盖失败分支。
    """
    import shutil

    def fake_startfile(target):
        target = str(target)
        base = os.path.basename(target).lower()
        base_stem = base[:-4] if base.endswith(".exe") else base
        if (
            os.path.exists(target)
            or target.lower().startswith(("http://", "https://"))
            or base_stem in BARE_RESOLVABLE
            or shutil.which(target)
        ):
            opened.append(target)
            return
        raise OSError(f"模拟 ShellExecute 失败：{target}")

    return fake_startfile


def check(label: str, ok: bool):
    """打印单项测试结果并累计失败数。"""
    print(("  [通过] " if ok else "  [失败] ") + label)
    return 0 if ok else 1


def main():
    fails = 0
    print("== hands.py 自测开始 ==")

    # ---- 1. list_tools：数量、名字、字段结构 ----
    print("[1] list_tools 契约检查")
    tools = hands.list_tools()
    names = [t["name"] for t in tools]
    fails += check("返回 14 个工具", len(tools) == 14)
    fails += check("工具名与契约完全一致", names == EXPECTED_TOOLS)
    fails += check(
        "每项都有 name/description/args 字段且类型正确",
        all(
            isinstance(t["name"], str)
            and isinstance(t["description"], str)
            and isinstance(t["args"], dict)
            for t in tools
        ),
    )

    # ---- 2. 文件三件套：写 -> 读 -> 列 -> 清理 ----
    print("[2] 文件三件套")
    test_name = "selftest_tmp.txt"
    test_content = "贾维斯文件读写自测内容"
    r = hands.execute("write_file", {"name": test_name, "content": test_content})
    fails += check("write_file 返回确认语且含文件名", test_name in r)
    r = hands.execute("read_file", {"name": test_name})
    fails += check("read_file 读回内容一致", test_content in r)
    r = hands.execute("list_files", {})
    fails += check("list_files 包含该文件", test_name in r)
    # 清理测试文件
    os.remove(os.path.join(hands.SANDBOX_DIR, test_name))
    r = hands.execute("read_file", {"name": test_name})
    fails += check("清理后 read_file 提示文件不存在", "找不到" in r)

    # ---- 3. 路径穿越攻击必须被拦截 ----
    print("[3] 路径穿越防护")
    r = hands.execute("read_file", {"name": "..\\brain.py"})
    fails += check("read_file('..\\\\brain.py') 读不到真实内容", "import" not in r and "找不到" in r)
    r = hands.execute("write_file", {"name": "..\\evil.txt", "content": "x"})
    fails += check(
        "write_file('..\\\\evil.txt') 不写到沙盒外",
        not os.path.exists(os.path.join(os.path.dirname(hands.SANDBOX_DIR), "evil.txt")),
    )
    # 它应被剥成沙盒内的 evil.txt，顺手清理
    inner = os.path.join(hands.SANDBOX_DIR, "evil.txt")
    if os.path.exists(inner):
        os.remove(inner)

    # ---- 4. run_shell：三级安全闸 + 通用执行 ----
    print("[4] run_shell 三级安全闸与通用执行")
    # list_tools 层面：run_command 已移除、run_shell 顶上（共 14 个工具，见 [1]）
    fails += check("run_command 不在 list_tools", "run_command" not in names)
    fails += check("run_shell 在 list_tools", "run_shell" in names)
    # shell_risk 三级判定
    fails += check("shell_risk('dir') == 'safe'", hands.shell_risk("dir") == "safe")
    fails += check(
        "shell_risk('del /f /s C:\\\\') == 'blocked'",
        hands.shell_risk("del /f /s C:\\") == "blocked",
    )
    fails += check(
        "shell_risk('taskkill /im notepad.exe') == 'confirm'",
        hands.shell_risk("taskkill /im notepad.exe") == "confirm",
    )
    fails += check(
        "shell_risk('format c:') == 'blocked'",
        hands.shell_risk("format c:") == "blocked",
    )
    # safe 命令直接执行
    r = hands.execute("run_shell", {"cmd": "echo Nolan之手"})
    fails += check("run_shell('echo Nolan之手') 输出含「Nolan之手」", "Nolan之手" in r)
    # blocked 命令返回拒绝话术且未执行（直接返回，不经过 subprocess）
    r = hands.execute("run_shell", {"cmd": "del /f /s C:\\"})
    fails += check(
        "run_shell('del /f /s C:\\\\') 返回拒绝话术",
        "我必须拒绝执行" in r and "抱歉" in r,
    )
    # confirm 命令未确认时返回 [[NEEDS_CONFIRM]] 开头
    r = hands.execute("run_shell", {"cmd": "taskkill /im x.exe"})
    fails += check(
        "run_shell('taskkill /im x.exe') 返回 [[NEEDS_CONFIRM]] 开头",
        r.startswith("[[NEEDS_CONFIRM]]"),
    )
    # confirmed=true 强制执行
    r = hands.execute("run_shell", {"cmd": "echo 已确认执行", "confirmed": True})
    fails += check("confirmed=true 的 echo 执行成功", "已确认执行" in r)

    # ---- 4.5 run_shell GUI 拉起非阻塞化 ----
    print("[4.5] run_shell GUI 拉起检测与非阻塞改写")
    # 纯函数检测逻辑（不真实启动任何程序）
    fails += check(
        "GUI 检测：'notepad' 判为 GUI 拉起",
        hands._looks_like_gui_launch("notepad")[0],
    )
    fails += check(
        "GUI 检测：'notepad.exe' 判为 GUI 拉起",
        hands._looks_like_gui_launch("notepad.exe")[0],
    )
    fails += check(
        "GUI 检测：'python --version' 在 CLI 白名单，不判 GUI",
        not hands._looks_like_gui_launch("python --version")[0],
    )
    fails += check(
        "GUI 检测：无法解析的命令保持原路径（不判 GUI）",
        not hands._looks_like_gui_launch("不存在的命令xyz123")[0],
    )
    fails += check(
        "GUI 检测：'taskkill /im x.exe' 在 CLI 白名单，不判 GUI",
        not hands._looks_like_gui_launch("taskkill /im x.exe")[0],
    )
    # 改写行为：monkeypatch subprocess.run，验证命令被改写为 start "" 且立即返回话术
    real_run = hands.subprocess.run
    run_calls = []

    def fake_run(*a, **kw):
        run_calls.append((a, kw))

        class _R:  # 最小化 subprocess 结果替身
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    hands.subprocess.run = fake_run
    try:
        r = hands.execute("run_shell", {"cmd": "notepad"})
        fails += check(
            "run_shell('notepad') 改写为 start 拉起并返回「已启动」话术",
            "已启动" in r
            and bool(run_calls)
            and str(run_calls[0][0][0]).startswith('start "" notepad'),
        )
    finally:
        hands.subprocess.run = real_run  # 恢复

    # ---- 5. get_time 非空 ----
    print("[5] get_time")
    r = hands.execute("get_time", {})
    fails += check("get_time 返回非空口语化表述", isinstance(r, str) and len(r) > 0 and "先生" in r)

    # ---- 6. 会弹窗的三个工具：monkeypatch os.startfile，只测分支逻辑 ----
    print("[6] open_app / open_url / web_search 分支逻辑（不真实启动）")
    opened = []
    real_startfile = os.startfile
    real_wait = hands._wait_for_window
    real_cmd_start = hands._cmd_start
    real_find = hands._find_window
    real_proc = hands._process_running
    os.startfile = make_fake_startfile(opened)  # monkeypatch（逼真版）
    hands._cmd_start = lambda _term: None  # cmd start 兜底也不真的执行
    hands._find_window = lambda _t: False  # 假世界里没有任何已存在的窗口
    hands._process_running = lambda _x: False  # 假世界里没有任何已运行的进程
    try:
        # open_app 执行后自检：假 startfile 成功的分支 -> 视为窗口出现
        hands._wait_for_window = lambda _t, timeout=8.0: True  # noqa: E731
        r = hands.execute("open_app", {"app": "记事本"})
        fails += check(
            "open_app('记事本') 最终打开 notepad",
            bool(opened)
            and os.path.basename(opened[-1]).lower().startswith("notepad")
            and "记事本" in r,
        )
        # 未知应用：各级启动全失败 -> 窗口永不出现 -> 如实失败话术
        hands._wait_for_window = lambda _t, timeout=8.0: False  # noqa: E731
        r = hands.execute("open_app", {"app": "注册表编辑器"})
        fails += check("open_app 未知应用礼貌拒绝", "抱歉" in r)
        hands._wait_for_window = lambda _t, timeout=8.0: True  # noqa: E731
        r = hands.execute("open_url", {"url": "https://example.com"})
        fails += check("open_url 打开原网址", opened[-1:] == ["https://example.com"])
        r = hands.execute("open_url", {"url": "example.com"})
        fails += check("open_url 自动补 https", opened[-1:] == ["https://example.com"])
        r = hands.execute("web_search", {"query": "贾维斯 语音助手"})
        fails += check(
            "web_search 拼必应搜索 URL",
            opened[-1].startswith("https://www.bing.com/search?q=")
            and "贾维斯" not in opened[-1]  # 中文必须被 URL 编码
            and "%E8%B4%BE" in opened[-1],
        )
    finally:
        os.startfile = real_startfile  # 恢复
        hands._wait_for_window = real_wait
        hands._cmd_start = real_cmd_start
        hands._find_window = real_find
        hands._process_running = real_proc

    # ---- 7. 未知工具与异常兜底 ----
    print("[7] 兜底行为")
    r = hands.execute("no_such_tool", {})
    fails += check("未知工具返回错误说明而不抛异常", "抱歉" in r)
    r = hands.execute("write_file", {"name": "x.txt"})  # 缺 content 参数
    fails += check("参数缺失返回提示而不抛异常", "抱歉" in r)
    # 清理 x.txt 若意外被写入
    xp = os.path.join(hands.SANDBOX_DIR, "x.txt")
    if os.path.exists(xp):
        os.remove(xp)

    # ---- 8. open_app 通用化：vscode 别名、大小写、失败话术、真实探测 ----
    print("[8] open_app 通用化改造")
    opened = []
    real_startfile = os.startfile
    real_wait = hands._wait_for_window
    real_cmd_start = hands._cmd_start
    real_find = hands._find_window
    real_proc = hands._process_running
    os.startfile = make_fake_startfile(opened)  # monkeypatch（逼真版）
    hands._cmd_start = lambda _term: None  # cmd start 兜底也不真的执行
    hands._find_window = lambda _t: False  # 假世界里没有任何已存在的窗口
    hands._process_running = lambda _x: False  # 假世界里没有任何已运行的进程
    try:
        hands._wait_for_window = lambda _t, timeout=8.0: True  # noqa: E731 - 假启动成功即视为窗口出现
        for alias in ("vscode", "VSCode"):
            opened.clear()
            r = hands.execute("open_app", {"app": alias})
            target = opened[-1] if opened else ""
            # 目标必须是 Code.exe 或与 Visual Studio Code 相关的 .lnk
            ok_target = (
                os.path.basename(target).lower() == "code.exe"
                or (target.lower().endswith(".lnk") and "visual studio code" in target.lower())
            )
            fails += check(
                f"open_app('{alias}') 能解析并『打开』VSCode",
                bool(opened) and ok_target and "已经打开" in r,
            )
        opened.clear()
        # 不存在的应用：窗口永不出现，走如实失败话术（语义变化：不再说「没有找到」，
        # 而是「已尝试启动但没有看到它的窗口」——启动可能成功但窗口被拦截/托盘化）
        hands._wait_for_window = lambda _t, timeout=8.0: False  # noqa: E731
        r = hands.execute("open_app", {"app": "不存在的东西xyz123"})
        fails += check(
            "open_app('不存在的东西xyz123') 返回「已尝试启动但没有看到窗口」的如实话术",
            "已尝试启动" in r and "没有看到它的窗口" in r and "抱歉" in r and not opened,
        )
        # 画图：which 与开始菜单都找不到时，走 ShellExecute 兜底直接 startfile
        hands._wait_for_window = lambda _t, timeout=8.0: True  # noqa: E731
        opened.clear()
        r = hands.execute("open_app", {"app": "画图"})
        fails += check(
            "open_app('画图') 兜底直接启动 mspaint",
            opened[-1:] == ["mspaint"] and "已经打开" in r,
        )
    finally:
        os.startfile = real_startfile  # 恢复
        hands._wait_for_window = real_wait
        hands._cmd_start = real_cmd_start
        hands._find_window = real_find
        hands._process_running = real_proc

    # 快捷方式挑选：卸载类垫底，完全同名优先（纯函数，与机器状态无关）
    pick = hands._pick_best_lnk(
        [
            r"C:\menu\卸载微信.lnk",
            r"C:\menu\微信小程序.lnk",
            r"C:\menu\微信.lnk",
        ],
        "微信",
    )
    fails += check("开始菜单 lnk 挑选：完全同名优先", pick == r"C:\menu\微信.lnk")
    pick = hands._pick_best_lnk(
        [r"C:\menu\卸载微信小程序.lnk", r"C:\menu\微信小程序.lnk"],
        "微信",
    )
    fails += check("开始菜单 lnk 挑选：卸载类垫底", pick == r"C:\menu\微信小程序.lnk")
    fails += check("开始菜单 lnk 挑选：空候选返回 None", hands._pick_best_lnk([], "x") is None)

    # 真实探测：本机已装 VSCode，展开后的路径必须存在
    vscode_user_path = os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"
    )
    fails += check(
        "真实探测：%LOCALAPPDATA% 下的 Code.exe 存在",
        os.path.isfile(vscode_user_path),
    )

    # ---- 9. 应用解析通用化：归一化 + 模糊匹配 ----
    print("[9] 应用解析通用化（归一化 + 双向子串模糊匹配）")

    # _normalize_term 纯函数单测：后缀词剥离与独立 app 字样
    fails += check(
        "_normalize_term('chrome浏览器') == 'chrome'",
        hands._normalize_term("chrome浏览器") == "chrome",
    )
    fails += check(
        "_normalize_term('微信电脑版') == '微信'",
        hands._normalize_term("微信电脑版") == "微信",
    )
    fails += check(
        "_normalize_term('Chrome App') == 'chrome'",
        hands._normalize_term("Chrome App") == "chrome",
    )
    fails += check(
        "_normalize_term('浏览器') 不剥空（本名保留）",
        hands._normalize_term("浏览器") == "浏览器",
    )

    # 端到端：逼真 fake_startfile 下六种口语叫法都必须解析出目标
    opened = []
    real_startfile = os.startfile
    real_wait = hands._wait_for_window
    real_cmd_start = hands._cmd_start
    real_find = hands._find_window
    real_proc = hands._process_running
    os.startfile = make_fake_startfile(opened)  # monkeypatch（逼真版）
    hands._cmd_start = lambda _term: None  # cmd start 兜底也不真的执行
    hands._find_window = lambda _t: False  # 假世界里没有任何已存在的窗口
    hands._process_running = lambda _x: False  # 假世界里没有任何已运行的进程
    try:
        # (输入, 目标文件名中必须出现的关键词)
        hands._wait_for_window = lambda _t, timeout=8.0: True  # noqa: E731 - 假启动成功即视为窗口出现
        universal_cases = [
            ("chrome浏览器", "chrome"),
            ("edge浏览器", "edge"),
            ("谷歌浏览器", "chrome"),
            ("微信电脑版", "微信"),
            ("kimi", "kimi"),
            ("Visual Studio Code", "code"),
        ]
        for spoken, keyword in universal_cases:
            opened.clear()
            r = hands.execute("open_app", {"app": spoken})
            target = os.path.basename(opened[-1]).lower() if opened else ""
            fails += check(
                f"open_app('{spoken}') 解析出目标（含 '{keyword}'）",
                bool(opened) and keyword in target and "已经打开" in r,
            )
        # 找不到的应用：如实失败话术 + 无任何成功调用（窗口永不出现）
        hands._wait_for_window = lambda _t, timeout=8.0: False  # noqa: E731
        opened.clear()
        r = hands.execute("open_app", {"app": "不存在的xyz123"})
        fails += check(
            "open_app('不存在的xyz123') 返回「已尝试启动但没有看到窗口」话术且无成功调用",
            "已尝试启动" in r and "没有看到它的窗口" in r and "抱歉" in r and not opened,
        )
    finally:
        os.startfile = real_startfile  # 恢复
        hands._wait_for_window = real_wait
        hands._cmd_start = real_cmd_start
        hands._find_window = real_find
        hands._process_running = real_proc

    # ---- 9.5 别名表扩充：常用应用别名解析（_open_app 的别名解析即 _resolve_alias）----
    print("[9.5] 别名表扩充（网易云音乐 / cloudmusic / qq音乐 等）")

    # 别名 -> 预期统一搜索词（覆盖任务要求的三个，并抽查长短别名共存场景）
    alias_cases = [
        ("网易云音乐", "网易云音乐"),
        ("cloudmusic", "网易云音乐"),
        ("qq音乐", "QQ音乐"),
        ("网易云", "网易云音乐"),
        ("b站", "哔哩哔哩"),
        ("ppt", "PowerPoint"),
        ("文档", "Word"),
        ("steam", "Steam"),
    ]
    for spoken, expected_term in alias_cases:
        fails += check(
            f"_resolve_alias('{spoken}') == '{expected_term}'",
            hands._resolve_alias(spoken) == expected_term,
        )

    # 长键优先：长短别名共存时长键必须先命中
    fails += check(
        "_extract_app_hint('在网易云音乐中，进入我喜欢') 命中长键『网易云音乐』",
        hands._extract_app_hint("在网易云音乐中，进入我喜欢") == "网易云音乐",
    )
    fails += check(
        "_extract_app_hint('在qq音乐中点一首歌') 长键『qq音乐』先于短键『qq』",
        hands._extract_app_hint("在qq音乐中点一首歌") == "QQ音乐",
    )
    fails += check(
        "_extract_app_hint('在ppt里加一页') 命中别名表键『ppt』",
        hands._extract_app_hint("在ppt里加一页") == "PowerPoint",
    )

    # ---- 10. media_control：Windows 媒体键 ----
    print("[10] media_control 媒体控制")
    # 合法动作：真实发送一次音量加媒体键（对系统无害），应返回含「音量」的确认
    r = hands.execute("media_control", {"action": "volume_up"})
    fails += check(
        "media_control('volume_up') 返回含「音量」的确认",
        "音量" in r and "先生" in r,
    )
    # 未知动作：礼貌说明，不抛异常
    r = hands.execute("media_control", {"action": "爆炸"})
    fails += check(
        "media_control('爆炸') 返回礼貌说明",
        "抱歉" in r and "做不到" in r,
    )
    # 空 action 同样兜底
    r = hands.execute("media_control", {"action": ""})
    fails += check("media_control('') 返回礼貌说明", "抱歉" in r)

    # ---- 11. gui_control：待确认安全闸 + eyes 防御降级 ----
    print("[11] gui_control 界面自动化")
    fails += check("gui_control 在 list_tools", "gui_control" in names)

    # 探针式假眼睛：记录是否被调用、返回固定口语化结果
    class _FakeEyes:
        def __init__(self):
            self.calls = []

        def perform(self, task, max_steps=8):
            self.calls.append((task, max_steps))
            return f"好的先生，界面操作已完成：{task}。"

    real_eyes = hands._eyes
    fake_eyes = _FakeEyes()
    hands._eyes = fake_eyes
    try:
        # 未确认：必须返回 [[NEEDS_CONFIRM]] 开头，且绝不触碰眼睛模块
        r = hands.execute("gui_control", {"task": "测试"})
        fails += check(
            "gui_control 未确认返回 [[NEEDS_CONFIRM]] 开头",
            isinstance(r, str) and r.startswith("[[NEEDS_CONFIRM]]"),
        )
        fails += check("未确认时不调用 eyes.perform", fake_eyes.calls == [])
        # 确认后：委托 eyes.perform 执行并原样返回其结果
        r = hands.execute("gui_control", {"task": "点选列表中的歌曲", "confirmed": True})
        fails += check(
            "confirmed=True 调用 eyes.perform 并返回其结果",
            fake_eyes.calls == [("点选列表中的歌曲", 8)] and "界面操作已完成" in r,
        )
        # 空任务兜底
        r = hands.execute("gui_control", {"task": ""})
        fails += check("gui_control 空任务礼貌提示", "抱歉" in r)
    finally:
        hands._eyes = real_eyes  # 恢复

    # eyes 不可用（并行编写的模块尚未就绪/导入失败）：确认后也须礼貌降级
    hands._eyes = None
    try:
        r = hands.execute("gui_control", {"task": "测试", "confirmed": True})
        fails += check(
            "eyes 不可用时 confirmed 也返回礼貌降级话术",
            "抱歉" in r and "不可用" in r,
        )
    finally:
        hands._eyes = real_eyes  # 恢复

    # ---- 12. capture_screen / set_web_background ----
    print("[12] capture_screen 与 set_web_background")
    fails += check("capture_screen 在 list_tools", "capture_screen" in names)
    fails += check("set_web_background 在 list_tools", "set_web_background" in names)

    # 状态文件备份：先生若已设过背景，测试后必须原样还原，绝不弄丢用户数据
    state_path = os.path.join(hands.SANDBOX_DIR, "web_background.json")
    state_backup = None
    if os.path.isfile(state_path):
        with open(state_path, "rb") as f:
            state_backup = f.read()

    # 在沙盒 captures 子目录造一张测试 png（合法 PNG，模拟 capture_screen 产物）
    from PIL import Image
    cap_dir = os.path.join(hands.SANDBOX_DIR, "captures")
    os.makedirs(cap_dir, exist_ok=True)
    test_png = "selftest_bg.png"
    Image.new("RGB", (8, 8), (200, 120, 60)).save(
        os.path.join(cap_dir, test_png), format="PNG")
    try:
        # 存在的图片：返回确认话术，且状态文件内容精确等于契约
        r = hands.execute("set_web_background", {"name": test_png})
        state_obj = None
        if os.path.isfile(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state_obj = json.load(f)
        fails += check(
            "set_web_background 存在的图片返回确认且状态文件内容正确",
            "聊天背景已更换" in r
            and state_obj == {"image": "captures/" + test_png},
        )
        # 沙盒根目录的同名文件也应能找到（查找范围：files\ 及 captures\）
        root_png = "selftest_bg_root.png"
        Image.new("RGB", (8, 8), (60, 120, 200)).save(
            os.path.join(hands.SANDBOX_DIR, root_png), format="PNG")
        try:
            r = hands.execute("set_web_background", {"name": root_png})
            with open(state_path, "r", encoding="utf-8") as f:
                state_obj = json.load(f)
            fails += check(
                "set_web_background 也能找到沙盒根目录的图片",
                "聊天背景已更换" in r and state_obj == {"image": root_png},
            )
        finally:
            os.remove(os.path.join(hands.SANDBOX_DIR, root_png))
        # 不存在的图片：礼貌说明，不写状态
        r = hands.execute("set_web_background", {"name": "不存在的图片xyz.png"})
        fails += check(
            "set_web_background 不存在的图片返回礼貌说明",
            "抱歉" in r and "找不到" in r,
        )
        # 路径穿越：basename 防护剥掉 ../，按沙盒内文件名查找（找不到）
        r = hands.execute("set_web_background", {"name": "..\\..\\secret.png"})
        fails += check(
            "set_web_background 路径穿越被剥为 basename 且找不到",
            "抱歉" in r and "找不到" in r,
        )
        # capture_screen 只测眼睛不可用时的降级（真实截屏依赖 VLM，不在自测范围）
        real_eyes = hands._eyes
        hands._eyes = None
        try:
            r = hands.execute("capture_screen", {"description": "测试元素"})
            fails += check(
                "capture_screen 眼睛不可用时礼貌降级",
                "抱歉" in r and "不可用" in r,
            )
        finally:
            hands._eyes = real_eyes  # 恢复
        # 空描述兜底
        r = hands.execute("capture_screen", {"description": ""})
        fails += check("capture_screen 空描述礼貌提示", "抱歉" in r)
    finally:
        # 清理：删测试 png；状态文件有备份则还原，无备份（测试前不存在）则删除
        tp = os.path.join(cap_dir, test_png)
        if os.path.exists(tp):
            os.remove(tp)
        if state_backup is not None:
            with open(state_path, "wb") as f:
                f.write(state_backup)
        elif os.path.exists(state_path):
            os.remove(state_path)

    # ---- 12.5 search_web：真实联网网页搜索 ----
    print("[12.5] search_web 网页搜索（真实联网调用）")
    fails += check("search_web 在 list_tools", "search_web" in names)
    # 真实调用：必应搜索页应能解析出编号结果列表
    r = hands.execute("search_web", {"query": "人工智能"})
    fails += check(
        "search_web('人工智能') 返回含『一、』且长度 > 100 的结果文本",
        isinstance(r, str) and "一、" in r and len(r) > 100,
    )
    # 冷门词：必应大概率无结果或结果无关，只要求不崩溃、返回字符串
    r = hands.execute("search_web", {"query": "asdfqwerzxcv不存在的东西xyz"})
    fails += check(
        "search_web 冷门词不崩溃（返回非空字符串，不抛异常）",
        isinstance(r, str) and len(r) > 0,
    )
    # 空关键词兜底
    r = hands.execute("search_web", {"query": ""})
    fails += check("search_web 空关键词礼貌提示", "抱歉" in r)

    # ---- 13. SmartGui 前导：_extract_app_hint / _find_window / 自动开路 ----
    print("[13] gui_control 自动开路前导（SmartGui）")

    # 13.1 应用名提取：纯函数，不碰系统状态
    hint = hands._extract_app_hint("在网易云音乐中，进入我喜欢")
    fails += check(
        "_extract_app_hint('在网易云音乐中，进入我喜欢') 返回网易云音乐相关词",
        hint is not None and "网易云音乐" in hint,
    )
    fails += check(
        "_extract_app_hint('把这句话写下来') 返回 None（不做前导）",
        hands._extract_app_hint("把这句话写下来") is None,
    )
    fails += check(
        "_extract_app_hint 优先命中别名表键（'打开记事本写点东西' -> notepad）",
        hands._extract_app_hint("打开记事本写点东西") == "notepad",
    )
    fails += check(
        "_extract_app_hint('打开网易云音乐，播放我喜欢') 正则提取",
        hands._extract_app_hint("打开网易云音乐，播放我喜欢") == "网易云音乐",
    )

    # 13.2 _find_window 真实窗口检测（真开记事本再 taskkill）——真实开窗口的
    # E2E 默认跳过：自动化回归不允许真实弹窗。需要人工验证时设
    # NOLAN_E2E_WINDOWS=1 再运行本段。
    if os.environ.get("NOLAN_E2E_WINDOWS") == "1":
        import subprocess as _sp
        import time as _time
        try:
            os.startfile("notepad")
            _time.sleep(2)
            # 中文系统标题为「无标题 - 记事本」，英文系统为「Untitled - Notepad」，两种都认
            found = hands._find_window("记事本") or hands._find_window("notepad")
            fails += check("_find_window 能检测到真实存在的记事本窗口", found)
            fails += check(
                "_find_window 对不存在的窗口标题返回 False",
                not hands._find_window("绝对不存在的窗口标题xyz123"),
            )
        finally:
            _sp.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
    else:
        print("  [跳过] 13.2 真实窗口 E2E（真开记事本）；设 NOLAN_E2E_WINDOWS=1 可开启")
        # 非真实窗口的纯语义检查照常进行：对不存在的标题必须返回 False
        fails += check(
            "_find_window 对不存在的窗口标题返回 False",
            not hands._find_window("绝对不存在的窗口标题xyz123"),
        )

    # 13.3 前导逻辑 monkeypatch：窗口缺失 -> 调解析链打开 -> 等待出现 -> 置前
    class _FakeEyes2:
        """探针式假眼睛：记录调用、返回固定口语化结果。"""

        def __init__(self):
            self.calls = []

        def perform(self, task, max_steps=8):
            self.calls.append(task)
            return "好的先生，界面操作已完成。"

    real_eyes = hands._eyes
    real_find = hands._find_window
    real_open = hands._open_app
    real_front = hands._bring_window_front
    opened_apps = []
    front_calls = []
    find_calls = {"n": 0}

    def fake_find(_word):
        # 第一次（初始检测）缺失，第二次（轮询）出现 -> 只睡 1 秒就走完前导
        find_calls["n"] += 1
        return find_calls["n"] > 1

    def fake_open(app):
        opened_apps.append(app)
        return "好的先生，已经打开。"

    def fake_front(word):
        front_calls.append(word)
        return True

    hands._eyes = _FakeEyes2()
    hands._find_window = fake_find
    hands._open_app = fake_open
    hands._bring_window_front = fake_front
    try:
        hands.execute(
            "gui_control",
            {"task": "在网易云音乐中，进入我喜欢", "confirmed": True},
        )
        fails += check(
            "窗口缺失时前导调用应用解析链打开目标应用",
            opened_apps == ["网易云音乐"],
        )
        fails += check("窗口出现后尝试置前", front_calls == ["网易云音乐"])
        fails += check(
            "前导完成后仍委托 eyes.perform 执行",
            hands._eyes.calls == ["在网易云音乐中，进入我喜欢"],
        )
    finally:
        hands._eyes = real_eyes
        hands._find_window = real_find
        hands._open_app = real_open
        hands._bring_window_front = real_front

    # 13.4 等待超时仍继续执行（等待上限临时调为 0，避免真的等 12 秒）
    real_max = hands._WINDOW_WAIT_MAX_SECONDS
    hands._eyes = _FakeEyes2()
    hands._find_window = lambda _w: False  # noqa: E731 - 窗口始终不出现
    hands._open_app = fake_open
    hands._bring_window_front = fake_front
    hands._WINDOW_WAIT_MAX_SECONDS = 0
    try:
        opened_apps.clear()
        front_calls.clear()
        r = hands.execute(
            "gui_control",
            {"task": "在网易云音乐中，进入我喜欢", "confirmed": True},
        )
        fails += check(
            "等待超时仍继续交给眼睛模块执行",
            hands._eyes.calls == ["在网易云音乐中，进入我喜欢"] and "界面操作已完成" in r,
        )
        fails += check("超时路径打开了应用但不调用置前", opened_apps == ["网易云音乐"] and front_calls == [])
    finally:
        hands._WINDOW_WAIT_MAX_SECONDS = real_max
        hands._eyes = real_eyes
        hands._find_window = real_find
        hands._open_app = real_open
        hands._bring_window_front = real_front

    # 13.5 无应用词的任务不做前导（不应触碰解析链与窗口检测）
    hands._eyes = _FakeEyes2()
    hands._find_window = fake_find
    hands._open_app = fake_open
    try:
        opened_apps.clear()
        find_calls["n"] = 0
        hands.execute("gui_control", {"task": "点选列表中的歌曲", "confirmed": True})
        fails += check(
            "无应用词任务不做前导（不检测窗口、不开应用）",
            opened_apps == [] and find_calls["n"] == 0,
        )
    finally:
        hands._eyes = real_eyes
        hands._find_window = real_find
        hands._open_app = real_open

    print("== 自测结束：%s ==" % ("全部通过" if fails == 0 else f"{fails} 项失败"))
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
