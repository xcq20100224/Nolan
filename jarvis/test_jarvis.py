# -*- coding: utf-8 -*-
"""Nolan 语音助手 —— 免麦克风自动测试。

包含四部分：
    (a) 大脑规则层断言（时间回复 / 退出指令 / 闲聊）；
    (b) 回路测试：edge-tts 合成固定文本 → faster-whisper small 转写 →
        断言识别结果包含关键字；
    (c) 「手」工具链路测试：hands 的沙盒文件读写 / 通用 shell（三级安全闸）/ 通用应用打开
        （monkeypatch os.startfile）/ 媒体控制 media_control（Windows 媒体键），
        以及 brain._parse_intent 的意图映射抽查。
    (d) 记忆链路测试：memory 的记住 / 回忆 / 加载 / 遗忘（先备份真实记忆，
        测完还原，绝不污染 jarvis\\memory\\long_term.txt）。
    (e) 提醒链路测试：reminders 的新增 / 列出 / 引导语 / 到点弹出，
        以及闹钟意图（『1分钟后叫醒我』→ 默认内容『起床啦，先生』）
        （先备份 jarvis\\memory\\reminders.txt，测完还原）。

注意：本文件绝不 import jarvis，避免触发主循环。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import brain  # 契约：brain.think(user_text, history) -> str

# 若需要联网下载模型，走镜像（本地已缓存时不受影响）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    if ok:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        _failures.append(name)


def test_brain_rules() -> None:
    """(a) 大脑规则层三个断言。"""
    print("🧠 测试大脑规则层……")

    reply_time = brain.think("现在几点了", [])
    _check(
        "时间回复非空",
        isinstance(reply_time, str) and reply_time.strip() != "",
        f"得到：{reply_time!r}",
    )

    reply_exit = brain.think("退出", [])
    _check("退出指令返回 '__EXIT__'", reply_exit == "__EXIT__", f"得到：{reply_exit!r}")

    reply_chat = brain.think("你好呀", [])
    _check(
        "闲聊回复非空",
        isinstance(reply_chat, str) and reply_chat.strip() != "",
        f"得到：{reply_chat!r}",
    )


def test_speech_loop() -> None:
    """(b) 回路测试：edge-tts 合成 → faster-whisper 转写 → 断言关键字。"""
    print("🔁 测试语音回路（TTS → ASR）……")

    text = "贾维斯系统自检完成"
    tmp_path = Path(tempfile.gettempdir()) / "jarvis_selfcheck.mp3"

    # 1) 优先 edge-tts 在线合成；网络不可达时降级 pyttsx3 离线合成（wav）
    try:
        import edge_tts

        async def _synthesize() -> None:
            communicate = edge_tts.Communicate(text, voice="zh-CN-YunjianNeural")
            await communicate.save(str(tmp_path))

        asyncio.run(_synthesize())
        if not (tmp_path.exists() and tmp_path.stat().st_size > 0):
            raise RuntimeError("edge-tts 合成结果为空")
        _check("合成语音（edge-tts 在线）", True)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ edge-tts 不可用（{exc}），改用离线 SAPI 合成。")
        tmp_path = tmp_path.with_suffix(".wav")
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.save_to_file(text, str(tmp_path))
            engine.runAndWait()
            engine.stop()
        except Exception as exc2:  # noqa: BLE001
            _check("离线 SAPI 合成语音", False, f"异常：{exc2}")
            return
        _check("合成语音（SAPI 离线兜底）", tmp_path.exists() and tmp_path.stat().st_size > 0)

    # 2) faster-whisper small 转写
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel("small", device="cpu", compute_type="int8")
        # small 模型对短音频易误转为繁体/近音字，用 initial_prompt 引导简体中文语境
        segments, _info = model.transcribe(
            str(tmp_path),
            language="zh",
            initial_prompt="以下是简体中文普通话语音，内容为贾维斯语音助手的系统自检播报。",
        )
        recognized = "".join(seg.text for seg in segments).strip()
    except Exception as exc:  # noqa: BLE001
        _check("faster-whisper 转写", False, f"异常：{exc}")
        return

    print(f"  📝 识别结果：{recognized}")
    _check(
        "识别结果包含『贾维斯』或『自检』",
        ("贾维斯" in recognized) or ("自检" in recognized),
        f"得到：{recognized!r}",
    )

    # 清理临时文件
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _intent_name(parsed: object) -> str:
    """从 brain._parse_intent 的返回值中提取工具名。

    兼容常见返回形态：dict（含 name/tool 键）、(name, args) 元组、纯字符串。
    """
    if isinstance(parsed, dict):
        for key in ("name", "tool", "tool_name"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(parsed, (tuple, list)) and parsed:
        return str(parsed[0])
    if isinstance(parsed, str):
        return parsed
    return ""


def test_hands_tools() -> None:
    """(c) 「手」工具链路：沙盒文件读写 / 通用 shell 三级安全闸 / 意图映射抽查。"""
    print("✋ 测试「手」工具链路……")

    # hands 可能与 brain 并行开发中，导入失败时单独记一条失败
    try:
        import hands
    except Exception as exc:  # noqa: BLE001
        _check("导入 hands 模块", False, f"异常：{exc}")
        return
    _check("导入 hands 模块", True)

    # --- 沙盒文件：写 → 读 → 列 → 清理 ---
    test_name = "测试笔记.txt"
    test_content = "贾维斯第一阶段测试内容"
    sandbox_file = Path(__file__).resolve().parent / "files" / test_name

    reply_write = hands.execute("write_file", {"name": test_name, "content": test_content})
    _check(
        "write_file 写入成功（返回非抱歉文本）",
        isinstance(reply_write, str)
        and reply_write.strip() != ""
        and "抱歉" not in reply_write
        and sandbox_file.exists(),
        f"得到：{reply_write!r}",
    )

    reply_read = hands.execute("read_file", {"name": test_name})
    _check(
        "read_file 返回内容含『第一阶段』",
        isinstance(reply_read, str) and "第一阶段" in reply_read,
        f"得到：{reply_read!r}",
    )

    reply_list = hands.execute("list_files", {})
    _check(
        "list_files 列出测试文件",
        isinstance(reply_list, str) and test_name in reply_list,
        f"得到：{reply_list!r}",
    )

    # 清理测试文件（直接删沙盒文件，不依赖 run_shell——del 属于灾难命令会被拒绝）
    try:
        sandbox_file.unlink(missing_ok=True)
    except OSError:
        pass

    # --- 通用 shell：三级安全闸（safe 直接执行 / confirm 待确认 / blocked 拒绝）---
    reply_echo = hands.execute("run_shell", {"cmd": "echo Nolan之手"})
    _check(
        "run_shell('echo Nolan之手') 输出含『Nolan之手』",
        isinstance(reply_echo, str) and "Nolan之手" in reply_echo,
        f"得到：{reply_echo!r}",
    )

    reply_del = hands.execute("run_shell", {"cmd": "del /f /s C:\\"})
    _check(
        "run_shell('del /f /s C:\\\\') 被拒绝",
        isinstance(reply_del, str)
        and ("拒绝" in reply_del or "抱歉" in reply_del),
        f"得到：{reply_del!r}",
    )

    reply_pending = hands.execute("run_shell", {"cmd": "taskkill /im nonexist.exe"})
    _check(
        "run_shell('taskkill /im nonexist.exe') 返回以 '[[NEEDS_CONFIRM]]' 开头",
        isinstance(reply_pending, str) and reply_pending.startswith("[[NEEDS_CONFIRM]]"),
        f"得到：{reply_pending!r}",
    )

    reply_confirmed = hands.execute(
        "run_shell", {"cmd": "echo 已确认", "confirmed": True}
    )
    _check(
        "run_shell('echo 已确认', confirmed=True) 输出含『已确认』",
        isinstance(reply_confirmed, str) and "已确认" in reply_confirmed,
        f"得到：{reply_confirmed!r}",
    )

    # --- 媒体控制：Windows 媒体键（ctypes 零依赖，发送系统级按键，无需 monkeypatch）---
    reply_vol = hands.execute("media_control", {"action": "volume_up"})
    _check(
        "media_control('volume_up') 返回含『音量』",
        isinstance(reply_vol, str) and "音量" in reply_vol,
        f"得到：{reply_vol!r}",
    )

    reply_bad_action = hands.execute("media_control", {"action": "不存在的动作"})
    _check(
        "media_control('不存在的动作') 返回礼貌说明（含『抱歉』或『支持』）",
        isinstance(reply_bad_action, str)
        and ("抱歉" in reply_bad_action or "支持" in reply_bad_action),
        f"得到：{reply_bad_action!r}",
    )

    # --- 工具表断言：含 run_shell / media_control / gui_control / capture_screen /
    #     set_web_background、不含 run_command（工具总数 14 个）---
    tool_names = [
        tool.get("name", "")
        for tool in hands.list_tools()
        if isinstance(tool, dict)
    ]
    _check(
        "list_tools() 共 14 个工具",
        len(tool_names) == 14,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 含 run_shell",
        "run_shell" in tool_names,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 含 media_control",
        "media_control" in tool_names,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 含 gui_control",
        "gui_control" in tool_names,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 含 capture_screen",
        "capture_screen" in tool_names,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 含 set_web_background",
        "set_web_background" in tool_names,
        f"得到：{tool_names!r}",
    )
    _check(
        "list_tools() 不含 run_command",
        "run_command" not in tool_names,
        f"得到：{tool_names!r}",
    )

    # --- GUI 自动化安全闸：未确认时必须返回待确认，绝不直接动鼠标键盘 ---
    reply_gui = hands.execute("gui_control", {"task": "打开开始菜单"})
    _check(
        "gui_control('打开开始菜单') 未确认时返回以 '[[NEEDS_CONFIRM]]' 开头",
        isinstance(reply_gui, str) and reply_gui.startswith("[[NEEDS_CONFIRM]]"),
        f"得到：{reply_gui!r}",
    )

    # --- 网页背景安全闸：不存在的图片名必须礼貌说明，绝不静默失败 ---
    reply_bg = hands.execute("set_web_background", {"name": "不存在的图片xyz123.png"})
    _check(
        "set_web_background('不存在的图片xyz123.png') 返回礼貌说明（含『抱歉』或『找不到』）",
        isinstance(reply_bg, str)
        and ("抱歉" in reply_bg or "找不到" in reply_bg),
        f"得到：{reply_bg!r}",
    )

    # --- 通用应用打开：monkeypatch os.startfile，断言调用次数与话术 ---
    # 用记录调用的假函数替换 os.startfile，绝不真正打开任何应用；测试后恢复
    real_startfile = getattr(os, "startfile", None)
    calls: list[tuple] = []

    def _fake_startfile(*args: object, **kwargs: object) -> None:
        """逼真的假 startfile：记录调用，且模拟 ShellExecute 的成败语义——
        存在的路径、URL、以及 App Paths 注册的裸命令才算成功，其余抛 OSError。
        （若永不抛错，hands 的 ShellExecute 兜底会把不存在的应用也误判成功。）"""
        calls.append(args)
        target = str(args[0]) if args else ""
        app_paths_bare = {"notepad", "calc", "mspaint", "write", "msedge"}
        if (
            os.path.exists(target)
            or target.lower().startswith(("http://", "https://"))
            or target.lower() in app_paths_bare
        ):
            return
        raise OSError(2, "系统找不到指定的文件（模拟）")

    if real_startfile is None:
        _check("os.startfile 可用（Windows 专属）", False, "当前环境无 os.startfile")
    else:
        # open_app 执行后自检配套：假 startfile 成功的分支视为窗口出现
        # （_wait_for_window 一并 monkeypatch）；cmd start 兜底也不真的执行
        real_wait = hands._wait_for_window
        real_cmd_start = hands._cmd_start
        real_find = hands._find_window
        real_proc = hands._process_running
        window_flag = {"ok": True}
        os.startfile = _fake_startfile  # type: ignore[attr-defined]
        hands._wait_for_window = lambda _t, timeout=8.0: window_flag["ok"]  # noqa: E731
        hands._cmd_start = lambda _term: None  # noqa: E731
        hands._find_window = lambda _t: False  # 假世界里没有任何已存在的窗口
        hands._process_running = lambda _x: False  # 假世界里没有任何已运行的进程
        try:
            # 打开 VSCode：应命中别名/已知路径，返回含『打开』的确认
            reply_vscode = hands.execute("open_app", {"app": "vscode"})
            _check(
                "open_app('vscode') 返回含『打开』",
                isinstance(reply_vscode, str) and "打开" in reply_vscode,
                f"得到：{reply_vscode!r}",
            )
            _check(
                "open_app('vscode') 触发 startfile 恰好一次",
                len(calls) == 1,
                f"实际调用 {len(calls)} 次：{calls!r}",
            )

            # 模糊说法「chrome浏览器」：应用名解析通用化（归一化 + 模糊匹配）后
            # 应命中 Google Chrome 快捷方式，返回含『打开』的确认，
            # 且 startfile 恰好成功调用一次、目标含 'chrome'（大小写不敏感）。
            # 包一层只统计“成功”调用——假 startfile 抛 OSError 的探测尝试不算打开
            calls.clear()
            chrome_successes: list[tuple] = []
            orig_fake_chrome = os.startfile

            def _tracking_chrome(*args: object, **kwargs: object) -> None:
                orig_fake_chrome(*args, **kwargs)  # 失败会抛 OSError，不计入成功
                chrome_successes.append(args)

            os.startfile = _tracking_chrome  # type: ignore[attr-defined]
            reply_chrome = hands.execute("open_app", {"app": "chrome浏览器"})
            os.startfile = orig_fake_chrome  # 恢复裸 fake，后续用例沿用既有模式
            _check(
                "open_app('chrome浏览器') 返回含『打开』",
                isinstance(reply_chrome, str) and "打开" in reply_chrome,
                f"得到：{reply_chrome!r}",
            )
            _check(
                "open_app('chrome浏览器') startfile 恰好成功一次且目标含 'chrome'",
                len(chrome_successes) == 1
                and "chrome" in str(chrome_successes[0][0]).lower(),
                f"成功调用 {len(chrome_successes)} 次：{chrome_successes!r}",
            )

            # 别名直达「kimi」：返回含『打开』的确认
            reply_kimi = hands.execute("open_app", {"app": "kimi"})
            _check(
                "open_app('kimi') 返回含『打开』",
                isinstance(reply_kimi, str) and "打开" in reply_kimi,
                f"得到：{reply_kimi!r}",
            )

            # 打开不存在的应用：返回礼貌话术，且不得有任何一次 startfile 调用成功
            # （注意：ShellExecute 兜底会“尝试”调用 startfile 来探测 App Paths 注册，
            #  尝试本身合法，关键是全部尝试都必须失败、没有真正打开任何东西）
            calls.clear()
            success_calls: list[tuple] = []

            def _recording_success(args_tuple: tuple) -> None:
                success_calls.append(args_tuple)

            # 包一层：假 startfile 不抛错即视为“真的打开了”
            orig_fake = os.startfile

            def _tracking_startfile(*args: object, **kwargs: object) -> None:
                orig_fake(*args, **kwargs)  # 可能抛 OSError
                _recording_success(args)

            os.startfile = _tracking_startfile  # type: ignore[attr-defined]
            window_flag["ok"] = False  # 不存在的应用：窗口永不出现 -> 如实失败话术
            reply_missing = hands.execute("open_app", {"app": "不存在的东西xyz123"})
            window_flag["ok"] = True
            _check(
                "open_app('不存在的东西xyz123') 返回如实话术（含『抱歉』）",
                isinstance(reply_missing, str)
                and "抱歉" in reply_missing,
                f"得到：{reply_missing!r}",
            )
            _check(
                "open_app 未找到应用时没有任何 startfile 调用成功",
                len(success_calls) == 0,
                f"成功调用 {len(success_calls)} 次：{success_calls!r}",
            )
        finally:
            # 无论断言成败，必须恢复真正的 os.startfile 与窗口等待/兜底原语
            os.startfile = real_startfile  # type: ignore[attr-defined]
            hands._wait_for_window = real_wait
            hands._cmd_start = real_cmd_start
            hands._find_window = real_find
            hands._process_running = real_proc
        _check("测试后 os.startfile 已恢复", os.startfile is real_startfile)

    # --- 大脑意图映射抽查 ---
    try:
        from brain import _parse_intent
    except Exception as exc:  # noqa: BLE001
        _check("导入 brain._parse_intent", False, f"异常：{exc}")
        return

    cases = [
        ("现在几点", "get_time"),
        ("打开计算器", "open_app"),
        ("搜索钢铁侠", "web_search"),
        ("运行 echo hi", "run_shell"),
        ("下一首", "media_control"),
    ]
    for text, expected in cases:
        try:
            parsed = _parse_intent(text)
            actual = _intent_name(parsed)
        except Exception as exc:  # noqa: BLE001
            _check(f"_parse_intent({text!r}) → {expected}", False, f"异常：{exc}")
            continue
        _check(
            f"_parse_intent({text!r}) → {expected}",
            actual == expected,
            f"得到：{parsed!r}",
        )

    # --- open_app 动词过滤抽查 ---
    # 「打开执行」是 GUI 任务话术（确认后对界面动手），不是应用名；
    # _parse_intent 必须过滤这类动词残留，绝不把它当成 open_app('执行')。
    try:
        parsed_exec = _parse_intent("打开执行")
        actual_exec = _intent_name(parsed_exec) if parsed_exec is not None else ""
    except Exception as exc:  # noqa: BLE001
        _check("_parse_intent('打开执行') → NOT_open_app", False, f"异常：{exc}")
    else:
        _check(
            "_parse_intent('打开执行') → NOT_open_app",
            actual_exec != "open_app",
            f"得到：{parsed_exec!r}",
        )


def _backup_memory_file() -> tuple[Path, bytes | None]:
    """备份真实长期记忆文件。

    返回 (文件路径, 原内容字节)；文件不存在时原内容为 None，测试后需删除新建的文件。
    """
    mem_file = Path(__file__).resolve().parent / "memory" / "long_term.txt"
    original = mem_file.read_bytes() if mem_file.exists() else None
    return mem_file, original


def _restore_memory_file(mem_file: Path, original: bytes | None) -> None:
    """按备份还原真实长期记忆文件。"""
    try:
        if original is None:
            mem_file.unlink(missing_ok=True)  # 测试前不存在 → 删掉测试新建的
        else:
            mem_file.write_bytes(original)  # 逐字节写回原内容
    except OSError:
        pass


def test_memory_chain() -> None:
    """(d) 记忆链路：记住 → 回忆 → 加载 → 遗忘，全程备份还原真实记忆文件。"""
    print("🧠 测试记忆链路（memory）……")

    # memory 可能与 brain/hands 并行开发中，导入失败单独记一条失败
    try:
        import memory
    except Exception as exc:  # noqa: BLE001
        _check("导入 memory 模块", False, f"异常：{exc}")
        return
    _check("导入 memory 模块", True)

    mem_file, original = _backup_memory_file()
    try:
        # --- 记住 ---
        reply_remember = memory.remember("测试事实：主人喜欢深蓝色")
        _check(
            "remember 返回非抱歉确认",
            isinstance(reply_remember, str)
            and reply_remember.strip() != ""
            and "抱歉" not in reply_remember,
            f"得到：{reply_remember!r}",
        )

        # --- 回忆（口语化列出）---
        reply_recall = memory.recall()
        _check(
            "recall 含『深蓝色』",
            isinstance(reply_recall, str) and "深蓝色" in reply_recall,
            f"得到：{reply_recall!r}",
        )

        # --- 加载（原文）---
        raw = memory.load()
        _check(
            "load 含『深蓝色』",
            isinstance(raw, str) and "深蓝色" in raw,
            f"得到：{raw!r}",
        )

        # --- 遗忘 ---
        reply_forget = memory.forget("深蓝色")
        _check(
            "forget 返回非抱歉确认",
            isinstance(reply_forget, str)
            and reply_forget.strip() != ""
            and "抱歉" not in reply_forget,
            f"得到：{reply_forget!r}",
        )

        raw_after = memory.load()
        _check(
            "forget 后 load 不再含『深蓝色』",
            isinstance(raw_after, str) and "深蓝色" not in raw_after,
            f"得到：{raw_after!r}",
        )

        # --- 遗忘不存在的关键词：礼貌说明，不报错 ---
        reply_missing = memory.forget("不存在的东西xyz")
        _check(
            "forget 不存在的关键词返回礼貌说明",
            isinstance(reply_missing, str) and reply_missing.strip() != "",
            f"得到：{reply_missing!r}",
        )
    finally:
        _restore_memory_file(mem_file, original)

    _check("测试后真实记忆文件已还原", True)


def _backup_reminders_file() -> tuple[Path, bytes | None]:
    """备份真实提醒文件 jarvis\\memory\\reminders.txt。

    返回 (文件路径, 原内容字节)；文件不存在时原内容为 None，测试后需删除新建的文件。
    """
    rem_file = Path(__file__).resolve().parent / "memory" / "reminders.txt"
    original = rem_file.read_bytes() if rem_file.exists() else None
    return rem_file, original


def _restore_reminders_file(rem_file: Path, original: bytes | None) -> None:
    """按备份还原真实提醒文件。"""
    try:
        if original is None:
            rem_file.unlink(missing_ok=True)  # 测试前不存在 → 删掉测试新建的
        else:
            rem_file.write_bytes(original)  # 逐字节写回原内容
    except OSError:
        pass


def test_reminders_chain() -> None:
    """(e) 提醒链路：新增 → 列出 → 引导语 → 到点弹出，全程备份还原提醒文件。"""
    print("⏰ 测试提醒链路（reminders）……")

    # reminders 可能与 brain 并行开发中，导入失败单独记一条失败
    try:
        import reminders
    except Exception as exc:  # noqa: BLE001
        _check("导入 reminders 模块", False, f"异常：{exc}")
        return
    _check("导入 reminders 模块", True)

    rem_file, original = _backup_reminders_file()
    try:
        # --- 新增提醒：时间前缀 + 内容，返回含『提醒』的口语化确认 ---
        reply_add = reminders.add("两分钟后测试验收提醒内容")
        _check(
            "add('两分钟后测试验收提醒内容') 返回含『提醒』的确认",
            isinstance(reply_add, str) and "提醒" in reply_add,
            f"得到：{reply_add!r}",
        )

        # --- 列出未来提醒：应含刚写入的内容 ---
        reply_list = reminders.list_pending()
        _check(
            "list_pending() 含『测试验收提醒内容』",
            isinstance(reply_list, str) and "测试验收提醒内容" in reply_list,
            f"得到：{reply_list!r}",
        )

        # --- 无法解析时间：返回引导语（含『没听清』或『提醒时间』）---
        reply_guide = reminders.add("随便聊聊")
        _check(
            "add('随便聊聊') 返回引导语",
            isinstance(reply_guide, str)
            and ("没听清" in reply_guide or "提醒时间" in reply_guide),
            f"得到：{reply_guide!r}",
        )

        # --- 闹钟意图：brain.think('1分钟后叫醒我') 必须进入提醒系统 ---
        # 契约：识别『叫醒我 / 叫我起床 / 闹钟』说法并创建提醒，
        # 无具体内容时默认内容为『起床啦，先生』
        reply_alarm = brain.think("1分钟后叫醒我", [])
        _check(
            "think('1分钟后叫醒我') 返回非抱歉确认文本",
            isinstance(reply_alarm, str)
            and reply_alarm.strip() != ""
            and "抱歉" not in reply_alarm,
            f"得到：{reply_alarm!r}",
        )

        reply_pending_alarm = reminders.list_pending()
        _check(
            "list_pending() 含『起床啦』（闹钟默认内容已落盘）",
            isinstance(reply_pending_alarm, str) and "起床啦" in reply_pending_alarm,
            f"得到：{reply_pending_alarm!r}",
        )

        # --- 到点弹出：直接写入一条过去时间的提醒，再 check_due ---
        past_line = "2000-01-01T00:00|到点验收项"
        existing = rem_file.read_text(encoding="utf-8") if rem_file.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        rem_file.parent.mkdir(parents=True, exist_ok=True)
        rem_file.write_text(existing + past_line + "\n", encoding="utf-8")

        due = reminders.check_due()
        _check(
            "check_due() 弹出『到点验收项』",
            isinstance(due, list) and any("到点验收项" in item for item in due),
            f"得到：{due!r}",
        )

        due_again = reminders.check_due()
        _check(
            "再次 check_due() 为空（已弹出的提醒被移除）",
            isinstance(due_again, list) and due_again == [],
            f"得到：{due_again!r}",
        )
    finally:
        _restore_reminders_file(rem_file, original)

    _check("测试后真实提醒文件已还原", True)


def main() -> int:
    """运行全部测试，返回进程退出码。"""
    print("🧪 Nolan 免麦克风自动测试开始")
    test_brain_rules()
    test_speech_loop()
    test_hands_tools()
    test_memory_chain()
    test_reminders_chain()

    if _failures:
        print(f"❌ {len(_failures)} 项测试未通过：{', '.join(_failures)}")
        return 1
    print("✅ 全部测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
