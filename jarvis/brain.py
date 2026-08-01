# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 大脑模块（brain.py）· 阶段五
职责：接收用户说的一句话，返回回复文本；可通过 hands 执行真实动作，
阶段三起接入长期记忆（记住 / 回忆 / 忘掉），并将记忆注入大模型人设；
阶段四起接入主动提醒（提醒我 / 我的提醒）；
阶段五起人格可靠化（正式、精确、简练，先确认后执行，如实汇报，承认无知），
并接入通用执行工具 run_shell 与「待确认」状态机（confirm 级命令须先生亲口确认）；
阶段六起接入媒体控制 media_control（规则意图直接接管暂停/切歌/音量/静音，
不劫持「播放某首歌」类指令），并在工具协议中声明能力边界，杜绝谎称完成；
阶段七起接入屏幕感知与界面自动化 gui_control（截屏 + 视觉模型 + 鼠标键盘，
复用待确认状态机，先生亲口确认后才接管鼠标键盘）；
阶段八起大脑通用化：Agent 循环（多步工具链 ≤4 轮，工具结果回灌大模型继续推理），
人设从「固定清单」升级为「任何现代 AI 能做的事都尽力完成」；
阶段九起可靠性升级：open_app 动词过滤（「打开执行」类误提取不再映射 open_app），
工具协议新增任务前导（gui_control 之前先确认目标应用已打开，未打开先用 open_app + 等待加载）；
阶段十起前导内置：gui_control 自己确保目标应用已打开并置前（检测窗口 → 缺失自动打开 → 等待出现），
工具协议不再要求 LLM 先用 open_app 开路（「先打开应用」刻在代码里，而非只写在 prompt 里建议）。

决策流程（按序）：
  1. 空输入 -> 提示重说；2. 退出意图 -> '__EXIT__'；
  3. 待确认状态机（run_shell / gui_control）：确认 / 取消 / 新指令覆盖；
  4. 记忆意图分支：记住/回忆/忘掉直接调用 memory，结果文本零包装直接返回；
  5. 提醒意图分支：提醒我/闹钟叫醒/提醒列表直接调用 reminders，结果文本零包装直接返回；
  6. 规则意图解析 _parse_intent：命中即经 _execute_tool 执行，结果文本零包装直接返回；
  7. 大模型层 _think_via_llm：system prompt 附长期记忆与工具协议，
     Agent 循环——回复若为工具 JSON 则经 _execute_tool 执行，
     结果（截断 800 字）以「[工具结果] ...」回灌后再次请求，
     最多 4 轮工具调用，直至大模型返回普通文本作为最终回复；
  8. 规则闲聊兜底（正式承认尚未接入大模型）。

接口契约（签名一字不差）：def think(user_text: str, history: list[dict]) -> str
约定：退出意图返回 '__EXIT__'；其余分支返回纯文本，不做 print 刷屏。
"""

import os
import re
import json
import random
import time

import httpx

try:
    import hands  # 阶段二「手」模块（由并行工程师编写，按契约调用）
except ImportError:  # pragma: no cover - hands 未就绪时大脑仍可降级运行
    hands = None

try:
    import memory  # 阶段三「长期记忆」模块（由并行工程师编写，按契约调用）
except ImportError:  # pragma: no cover - memory 未就绪时大脑仍可降级运行
    memory = None

try:
    import reminders  # 阶段四「主动提醒」模块（由并行工程师编写，按契约调用）
except ImportError:  # pragma: no cover - reminders 未就绪时大脑仍可降级运行
    reminders = None

# == 常量与配置 ==

EXIT_SIGNAL = "__EXIT__"          # 退出约定字符串，主循环据此结束
_API_TIMEOUT = 60.0               # 大模型请求超时（秒）；glm-5.2 偶发慢响应，放宽到 60
_HISTORY_TURNS = 10               # 带入大模型的最近对话轮数（拉长上下文，提升连贯性）

# Nolan 人设（中文 system prompt）· 阶段五：人格可靠化
# 设计原点：「贾维斯式可靠管家」——正式、得体、可靠，绝不搞笑
_SYSTEM_PROMPT = (
    "你是 Nolan，先生的私人 AI 管家。你的言行以可靠为第一准则，"
    "风格参考《是大臣》中汉弗莱的得体恭敬与贾维斯的干练：正式、精确、简练，"
    "回答两三句以内；执行类请求先用一句话复述你将做什么，"
    "完成后如实汇报结果（成功、失败及原因）；不确定的事直接承认，绝不编造；"
    "不讲笑话、不用网络流行语、不卖萌、不用表情符号。"
    "你的回复会被语音播报。称呼用户为「先生」。"
    "你的能力不设限于固定清单：写作、翻译、总结、计划、编程、分析、计算、查资料，"
    "凡是现代 AI 能做的事你都应尽力完成；复杂任务拆解为多步工具调用，"
    "每步执行后会收到结果，再继续下一步；完成所有步骤后用一两句话向先生汇报整体结果。"
)

# 退出触发词（命中即返回 __EXIT__）
_EXIT_PATTERNS = ["再见", "退出", "拜拜", "关机睡觉", "去睡觉", "结束对话"]

# 规则层“闲聊”人格化回复模板（{text} 为用户原话）
# 阶段五：与人设一致的可靠风格——正式承认尚未接入大模型，不插科打诨
_FALLBACK_REPLIES = [
    "先生，我听到您说的是「{text}」。如实向您汇报：我目前尚未接入大模型，"
    "暂时只能作有限回应，无法就此给出可靠的答复。",
    "先生，关于「{text}」，我必须如实说明：大模型尚未接通，"
    "我此刻不具备深入回答的能力，不能为您编造答案。",
    "先生，您说的「{text}」我已收到。坦率地讲，我目前尚未接入大模型，"
    "对此事没有可靠的见解，这一点我不应隐瞒。",
]

# == 规则意图解析：文本 -> (工具名, 参数) 或 None ==

_URL_RE = re.compile(r"https?://\S+")
_FILE_RE = re.compile(r"[^\s，。！？；：「」『』“”]+\.[A-Za-z0-9]+")
_LOCAL_APPS = ("记事本", "计算器", "画图")
_TIME_KEYS = ("几点", "时间", "日期", "几号", "星期", "礼拜")
_SEARCH_KEYS = ("搜索", "搜一下", "查一下", "百度一下")
_READ_KEYS = ("读取", "看看文件", "读一下", "读")
_WRITE_KEYS = ("写文件", "记录", "记下来")
_LIST_KEYS = ("列出", "有哪些文件")
_RUN_KEYS = ("运行", "执行")

# 「打开 X」通用提取的黑名单：提取结果若是纯动词/无意义词（如「打开执行」
# 提取出「执行」），说明用户并非要打开某个应用，不映射 open_app，
# 返回 None 交给 LLM 判断，避免把「打开执行」这类话误当应用名去瞎找程序
_OPEN_APP_BLACKLIST = ("执行", "运行", "操作", "工作", "一下", "帮我", "你", "它")

# 复合任务连接词：出现即说明一句话里有多个意图，规则层（单意图分发）让位给 LLM Agent 循环
_COMPOSITE_MARKERS = ("然后", "接着", "并且", "再把", "顺便", "之后", "同时", "还要")

# 动作触发词分组：一句话命中两个及以上不同组，即多意图复合任务
# （如「搜一下今天的新闻，总结三条写到日报.txt」——搜索组+写文件组，
#  虽无连接词也是复合任务，不能让单意图规则劫持）
_ACTION_GROUPS = (
    ("搜索", "搜一下", "查一下", "百度"),                      # 搜索组
    ("写到", "写进", "写入", "记到", "保存到", "写下来"),      # 写文件组
    ("总结", "概括", "归纳"),                                  # 总结组
    ("打开",),                                                # 打开组
    ("抓取", "看看网页", "网页内容"),                          # 抓取组
    ("运行", "执行"),                                          # 命令组
)


def _is_composite(text: str) -> bool:
    """判断一句话是否为多意图复合任务（连接词 或 命中多个动作触发组）。"""
    if any(marker in text for marker in _COMPOSITE_MARKERS):
        return True
    # 写文件句式 + 时间词（如「把今天的日期写到 x.txt」）：写入内容需要
    # 运行时动态求值，规则层只会被时间规则劫持或写死字面量，交给 LLM Agent 循环
    if any(w in text for w in ("写到", "写进", "写入", "记到", "保存到")) and any(
        k in text for k in _TIME_KEYS
    ):
        return True
    hits = sum(1 for group in _ACTION_GROUPS if any(word in text for word in group))
    return hits >= 2


def _strip_triggers(text: str, keys: tuple) -> str:
    """去掉意图触发词，得到剩余的关键词原文。"""
    rest = text
    for k in keys:
        rest = rest.replace(k, "")
    return rest.strip(" ，。！？：:的了下帮我请把")


# == 记忆意图：记住 / 回忆 / 忘掉（在规则工具意图之前判定） ==

_RECALL_KEYS = ("你记得", "记忆里", "记住了什么", "你了解我")
_FORGET_KEYS = ("忘掉", "别记")


def _handle_memory_intent(text: str) -> str | None:
    """
    记忆意图分支：命中则直接调用 memory 并返回其口语化结果；未命中返回 None。
    memory 未就绪（None）时不拦截，放行给后续规则与大模型层。
    注意判定顺序：先「回忆」后「记住」，避免「你记住了什么」被误当存储。
    """
    if memory is None:
        return None
    try:
        # 回忆：你记得 / 记忆里 / 记住了什么 / 你了解我
        if any(k in text for k in _RECALL_KEYS):
            return memory.recall()
        # 存储：含「记住」，取其后的内容作为记忆原文
        if "记住" in text:
            fact = text.split("记住", 1)[1].strip(" ，。！？：:帮我请把")
            if not fact:
                return "好的先生，请问您要我记住什么？"
            return memory.remember(fact)
        # 遗忘：忘掉 / 别记，取剩余部分作为关键词
        if any(k in text for k in _FORGET_KEYS):
            kw = _strip_triggers(text, _FORGET_KEYS)
            if not kw:
                return "好的先生，请问要忘掉哪方面的记忆？"
            return memory.forget(kw)
    except Exception:  # pragma: no cover - 记忆模块异常时大脑不崩，降级放行
        return None
    return None


# == 提醒意图：提醒我 / 闹钟叫醒 / 我的提醒（在记忆意图之后、规则工具意图之前判定） ==

_REMIND_LIST_KEYS = ("我的提醒", "有什么提醒", "提醒列表", "待办提醒")

# 闹钟/叫醒触发词（按特异性从长到短匹配，先匹配者优先）
_ALARM_KEYS = ("叫醒我", "叫我起床", "叫我起来", "闹钟", "叫我")

# 「叫我」的排除项：问句类不命中闹钟意图（如「你叫我什么」「你叫我问谁」）
_ALARM_EXCLUDE = ("叫我什么", "叫我问")

# 闹钟式提醒的默认内容：解析不出具体事项时按「叫醒」处理
_ALARM_DEFAULT_CONTENT = "起床啦，先生"


def _extract_alarm_raw(text: str) -> str | None:
    """
    闹钟/叫醒意图解析：命中触发词则返回交给 reminders 的原文（raw），未命中返回 None。
    规则：取触发词之前的时间部分 + 之后的事项部分拼接为 raw
    （如「1分钟后叫醒我」->「1分钟后」；触发词在句首时自然取到其后部分）。
    「叫我」先排除「叫我什么」「叫我问」类问句。
    """
    for key in _ALARM_KEYS:
        if key == "叫我" and any(ex in text for ex in _ALARM_EXCLUDE):
            continue  # 问句类不命中，继续检查其他触发词
        idx = text.find(key)
        if idx < 0:
            continue
        before = text[:idx]
        after = text[idx + len(key):]
        raw = (before + after).strip(" ，。！？：:帮我请把")
        return raw
    return None


def _handle_reminder_intent(text: str) -> str | None:
    """
    提醒意图分支：命中则直接调用 reminders 并返回其口语化结果；未命中返回 None。
    reminders 未就绪（None）时不拦截，放行给后续规则与大模型层。
    注意判定顺序：先「提醒我」与「闹钟/叫醒」（新增）后「提醒列表」（查询），
    避免「提醒我看看我的提醒」这类句子被误当查询。
    """
    if reminders is None:
        return None
    try:
        # 新增提醒：含「提醒我」，把「提醒我」从句中剔除后整句交给 reminders
        # 解析时间与内容——兼容「提醒我 1 分钟后喝水」（时间在后）与
        # 「10 秒后提醒我 测试弹出」（时间在前）两种语序
        if "提醒我" in text:
            raw = text.replace("提醒我", "", 1).strip(" ，。！？：:帮我请把")
            if not raw:
                return "好的先生，请问要我在什么时候提醒您什么？"
            return reminders.add(raw)
        # 闹钟/叫醒：叫醒我 / 叫我起床 / 叫我起来 / 闹钟 / 叫我（排除问句）
        alarm_raw = _extract_alarm_raw(text)
        if alarm_raw is not None:
            if not alarm_raw:
                return ("好的先生，请问要我在什么时候叫醒您？"
                        "比如：十分钟后叫醒我，或者明天早上七点半叫我起床。")
            # 解析出的提醒内容为空时补上默认内容，保证 reminders.add 直接落库
            try:
                _when, content = reminders._extract_time_prefix(alarm_raw)
            except Exception:
                content = ""
            if not content:
                alarm_raw = alarm_raw + _ALARM_DEFAULT_CONTENT
            return reminders.add(alarm_raw)
        # 查询提醒：我的提醒 / 有什么提醒 / 提醒列表 / 待办提醒
        if any(k in text for k in _REMIND_LIST_KEYS):
            return reminders.list_pending()
    except Exception:  # pragma: no cover - 提醒模块异常时大脑不崩，降级放行
        return None
    return None


def _parse_write_file(text: str) -> tuple | None:
    """解析写文件两种句式，解析不出返回 None。"""
    # 句式一：把 xxx 写到/记到 yyy
    m = re.search(r"把(.+?)(?:写到|记到)\s*([^\s，。！？]+)", text)
    if m:
        return ("write_file", {"name": m.group(2), "content": m.group(1).strip()})
    # 句式二：写文件 yyy 内容 xxx
    m = re.search(r"写文件\s*([^\s，。！？]+)\s*内容[:：]?\s*(.+)", text)
    if m:
        return ("write_file", {"name": m.group(1), "content": m.group(2).strip()})
    return None


def _parse_intent(text: str) -> tuple | None:
    """
    规则意图解析：返回 (工具名, 参数dict)；无法识别返回 None。
    只解析不执行，执行交给 hands.execute。
    """
    # 时间 / 日期 / 星期
    if any(k in text for k in _TIME_KEYS):
        return ("get_time", {})

    # 媒体控制（播放/暂停/切歌/音量/静音）
    # 注意：「播放」单独出现不在此映射——「播放某首歌」类指令交给大模型判断，
    # 规则层只接管不含具体曲目的控制类指令，避免劫持。
    if "暂停" in text or "继续播放" in text:
        return ("media_control", {"action": "play_pause"})
    if "下一首" in text or "切歌" in text:
        return ("media_control", {"action": "next"})
    if "上一首" in text:
        return ("media_control", {"action": "previous"})
    if "音量" in text:
        if any(k in text for k in ("大", "高", "加")):
            return ("media_control", {"action": "volume_up"})
        if any(k in text for k in ("小", "低", "减")):
            return ("media_control", {"action": "volume_down"})
    if "静音" in text:
        return ("media_control", {"action": "mute"})

    url_match = _URL_RE.search(text)

    # 打开本地应用
    if "打开" in text and not url_match:
        for app in _LOCAL_APPS:
            if app in text:
                return ("open_app", {"app": app})
        # 通用兜底：「打开 X」按应用名交给 hands 通用解析（别名/PATH/安装路径/开始菜单）
        idx = text.find("打开")
        app_name = text[idx + 2 :]
        # 先整体移除无意义填充词（含「一下」），再 strip 标点，
        # 避免逐字符 strip 把「打开一下」削成无意义的「一」去误开应用
        for filler in ("本机电脑中的", "电脑中的", "本机的", "电脑里的", "电脑中的", "电脑", "一下"):
            app_name = app_name.replace(filler, "")
        app_name = app_name.strip(" ，。！？：:帮我请把")
        # 动词过滤：提取出的「应用名」是纯动词/无意义词时不映射 open_app，
        # 放行给后续规则与 LLM 处理，避免「打开执行」被当成应用名
        if app_name and app_name not in _OPEN_APP_BLACKLIST:
            return ("open_app", {"app": app_name})

    # 打开网址 / 抓取网页
    if url_match:
        url = url_match.group(0).rstrip("。，！？；")
        if "打开" in text:
            return ("open_url", {"url": url})
        if any(k in text for k in ("看看", "抓取", "内容", "总结")):
            return ("fetch_url", {"url": url})

    # 搜索
    if any(k in text for k in _SEARCH_KEYS):
        query = _strip_triggers(text, _SEARCH_KEYS)
        if query:
            return ("web_search", {"query": query})

    # 读文件（提取带扩展名的文件名）
    if any(k in text for k in _READ_KEYS) and "写" not in text:
        m = _FILE_RE.search(text)
        if m:
            return ("read_file", {"name": m.group(0)})

    # 写文件（触发词，或“把…写到/记到…”句式）
    if any(k in text for k in _WRITE_KEYS) or ("把" in text and ("写到" in text or "记到" in text)):
        result = _parse_write_file(text)
        if result:
            return result

    # 列出文件
    if any(k in text for k in _LIST_KEYS) and "文件" in text:
        return ("list_files", {})

    # 运行命令（提取命令原文）· 阶段五：改走通用执行工具 run_shell
    # （三级安全闸由 hands 把关：safe 直接执行 / confirm 待确认 / blocked 拒绝）
    for k in _RUN_KEYS:
        idx = text.find(k)
        if idx >= 0:
            cmd = text[idx + len(k):].strip(" ，。！？：:一下帮我请")
            if cmd:
                return ("run_shell", {"cmd": cmd})
    return None


# == 待确认状态机（阶段五引入 run_shell；阶段六 gui_control 复用同一状态机） ==
# 安全闸是可靠性的组成部分：confirm 级命令与界面自动化操作，
# 都必须等先生亲口确认才可执行。
_pending_shell: dict | None = None  # 存 {'tool': 工具名, 'args': 原始参数}；None 表示当前无待确认事项

_CONFIRM_KEYS = ("确认", "执行吧", "好的", "是的")   # 先生亲口确认
_CANCEL_KEYS = ("取消", "算了", "别执行")            # 先生明确放弃


def _handle_pending_shell(text: str) -> str | None:
    """
    待确认状态机：存在待确认事项时，根据用户输入决定确认 / 取消 / 覆盖。
    确认时用原工具名 + 原参数并附加 confirmed=True 重放执行
    （run_shell 与 gui_control 走同一条路径）。
    返回回复文本表示已处理；返回 None 表示无待确认事项（或已被新指令覆盖），
    交给后续正常流程。
    """
    global _pending_shell
    if _pending_shell is None:
        return None
    tool = _pending_shell["tool"]
    args = dict(_pending_shell["args"])
    # 先判取消后判确认，避免歧义句误入确认分支
    if any(k in text for k in _CANCEL_KEYS):
        _pending_shell = None
        return "好的先生，已取消该操作。"
    if any(k in text for k in _CONFIRM_KEYS):
        _pending_shell = None
        if hands is None:
            return "抱歉先生，执行模块当前不可用，该操作未能执行。"
        args["confirmed"] = True
        return hands.execute(tool, args)
    # 其它新指令：覆盖旧 pending，继续正常流程
    _pending_shell = None
    return None


def _execute_tool(tool: str, args: dict) -> str:
    """
    统一工具执行入口（规则路径与 LLM 路径共用）。
    hands 返回以 '[[NEEDS_CONFIRM]]' 开头的文本时，将工具与参数存入待确认状态机，
    并向先生如实复述风险、请求确认；其余结果文本零包装直接返回。
    """
    global _pending_shell
    args = args or {}
    result = hands.execute(tool, args)
    # 失败自动换路（gui_control 专用）：眼睛报告「目标应用缺失」时，
    # 自动提取应用名 -> open_app 打开（其内置窗口等待）-> 原任务重放一次，
    # 返回第二次的结果。视觉模块断连 / 安全中止 / 步数超限一律不重试。
    if (
        tool == "gui_control"
        and isinstance(result, str)
        and "请先让我用 open_app 打开它" in result
    ):
        hint = None
        try:
            hint = hands._extract_app_hint(str(args.get("task", "")))
        except Exception:  # noqa: BLE001 - 提取失败按无 hint 处理
            hint = None
        if hint:
            print(f"[brain] gui_control 报告目标应用缺失，自动打开「{hint}」后重放一次……")
            hands.execute("open_app", {"app": hint})
            retry_args = dict(args)
            retry_args["confirmed"] = True
            return hands.execute("gui_control", retry_args)
    if isinstance(result, str) and result.startswith("[[NEEDS_CONFIRM]]"):
        _pending_shell = {"tool": tool, "args": dict(args)}
        if tool == "run_shell":
            cmd = str(args.get("cmd", ""))
            return f"先生，这条命令有一定风险：「{cmd}」。您确认执行吗？"
        if tool == "gui_control":
            task = str(args.get("task", ""))
            return (
                f"先生，完成「{task}」需要我接管您的鼠标和键盘，"
                "通过截屏观察界面后进行点击、输入等操作。"
                "操作期间请尽量不要碰鼠标键盘，紧急中止可把鼠标甩到屏幕角落。"
                "您确认执行吗？"
            )
        return result
    return result


# == 大模型层：OpenAI 兼容聊天接口 + 工具协议 ==

_MEMORY_LIMIT = 2000  # 注入 system prompt 的长期记忆最大字符数（超出截断）


def _build_system_prompt() -> str:
    """人设 + 长期记忆 + 工具协议段落：告诉 LLM 它是谁、记得什么、会用什么。"""
    prompt = _SYSTEM_PROMPT
    # 长期记忆注入：记忆模块未就绪或记忆为空时不加这一节
    if memory is not None:
        try:
            mem_text = memory.load().strip()
        except Exception:  # pragma: no cover - 记忆读取异常视为无记忆
            mem_text = ""
        if mem_text:
            prompt += "\n以下是你对主人的长期记忆：\n" + mem_text[:_MEMORY_LIMIT]
    if hands is None:
        return prompt
    tool_lines = "\n".join(
        f"- {t['name']}（参数：{json.dumps(t.get('args', {}), ensure_ascii=False)}）：{t['description']}"
        for t in hands.list_tools()
    )
    prompt += (
        "\n你现在拥有一双「手」，可以替主人执行以下工具：\n"
        f"{tool_lines}\n"
        "当主人要求执行动作时，你的整个回复只能是一个 JSON 对象，"
        "格式严格为 {\"tool\": \"工具名\", \"args\": {...}}，args 的键名必须与上面列出的参数名完全一致，"
        "除此以外不要输出任何字符。"
        "示例——主人说「把今天很开心写到日记.txt」，你只回复："
        "{\"tool\": \"write_file\", \"args\": {\"name\": \"日记.txt\", \"content\": \"今天很开心\"}}\n"
        "多步任务示例——主人说「把网易云音乐我喜欢列表第一首歌的封面设为聊天背景」："
        "你先调用 open_app 或 gui_control 打开并操作到目标界面，"
        "再调用 capture_screen 截取封面，最后调用 set_web_background 设为背景，"
        "每步只输出一个工具 JSON，拿到结果后再决定下一步。\n"
        "注意：只有当主人明确要求执行某个动作时才输出工具 JSON；"
        "闲聊、观点、知识问答一律正常口语回复，绝不调用工具。"
        "复杂任务拆解为多步：每次只输出一个工具 JSON，执行后你会收到一条"
        "以「[工具结果]」开头的消息，据此继续下一步；全部完成后用正常口语向先生汇报整体结果。"
        "遇到需要组合多个工具的现实任务（例如「把网易云音乐我喜欢列表第一首歌的封面设为聊天背景」），"
        "用 Agent 循环拆解串联：先用 open_app 或 gui_control 打开并操作到目标界面，"
        "再用 capture_screen 截取所需界面元素，最后用 set_web_background 设置；"
        "每一步拿到结果再决定下一步；做不到某一步时如实说明卡在哪一步。\n"
        "示例——主人说「播放我喜欢列表里的第一首歌」，你只回复："
        "{\"tool\": \"gui_control\", \"args\": {\"task\": \"在网易云音乐里点选我喜欢列表中的第一首歌并播放\"}}\n"
        "调用 gui_control 时只传 task，不要传 confirmed；系统会自动向主人请求确认，"
        "主人确认后才真正执行。"
        "任务前导已由 gui_control 内置：gui_control 会自己确保目标应用已经打开并切到前台"
        "（自动检测窗口、缺失时自动打开并等待），你只需在 task 里清楚描述界面内要做的操作，"
        "例如「在网易云音乐中，进入我喜欢列表，点击第一首歌的播放按钮」。"
        "打开任何应用程序一律用 open_app（args 的 app 直接传应用名原文，如 VSCode、微信），"
        "不要用 run_shell 启动程序。"
        "当专用工具无法完成主人的需求时，用 run_shell 自行构造 Windows cmd 或 "
        "PowerShell 命令执行，就像主人自己动手操作电脑一样；"
        "优先使用专用工具；命令必须非破坏、可读性好。"
        "研究、新闻、资料查询类任务必须用 search_web（它返回结果文本供你阅读），"
        "拿到结果后自己总结，需要保存再调 write_file；"
        "web_search 只是把浏览器打开给主人看，对你完成任务没有帮助。"
        "能力边界：软件界面内的操作（点击按钮、点选列表项等）用 gui_control 工具"
        "（会自动截屏看界面再操作鼠标键盘，执行前系统会向主人请求确认）；"
        "run_shell 用于命令行能完成的事；媒体播放控制优先用 media_control；"
        "做不到的事如实说明并给出可替代方案，禁止谎称完成；"
        "命令执行后没有输出时，要说明命令已执行但无法确认目标是否达成，并给出验证或替代办法。"
    )
    return prompt


def _load_llm_config() -> dict:
    """读取大脑配置：环境变量优先，缺项回退到本模块旁的 llm_config.json。

    为什么需要文件兜底：环境变量依赖父进程环境——由旧进程（如早已运行的
    桌面程序）拉起的子进程继承的是过期环境，setx 之后也读不到新值；
    配置文件则不挑父进程，任何启动方式都能读到最新配置。
    """
    cfg: dict = {}
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            file_cfg = json.load(f)
        if isinstance(file_cfg, dict):
            cfg = {k: v for k, v in file_cfg.items() if isinstance(v, str) and v}
    except (OSError, ValueError):
        cfg = {}
    # 环境变量覆盖文件（文件兜底，环境优先）
    for env_name, key in (
        ("JARVIS_API_KEY", "api_key"),
        ("JARVIS_BASE_URL", "base_url"),
        ("JARVIS_MODEL", "model"),
        ("JARVIS_EXTRA_BODY", "extra_body"),
    ):
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    return cfg


_MAX_TOOL_ROUNDS = 4      # Agent 循环最大工具调用轮数（超出即止步，直接汇报末轮结果）
_TOOL_RESULT_LIMIT = 800  # 回灌给大模型的单条工具结果最大字符数（超出截断，防上下文膨胀）


def _request_llm(url: str, payload: dict, headers: dict) -> str | None:
    """
    单次请求大模型并取出回复文本；任何失败返回 None。
    瞬态故障间隔 1 秒按原参数重试一次；失败透明化，真实原因写日志，绝不静默吞掉。
    """
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        # 失败透明化：真实原因写进服务器日志，绝不静默吞掉（静默失败是可靠性大敌）
        print(f"[brain] 大模型调用失败: {type(e).__name__}: {e}")
        # 瞬态故障重试：间隔 1 秒按原参数再试一次（直连实测 4 秒，失败多为瞬态）
        print("[brain] 1 秒后按原参数重试一次...")
        time.sleep(1)
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=_API_TIMEOUT)
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e2:
            print(f"[brain] 重试仍失败: {type(e2).__name__}: {e2}")
            return None  # 网络 / 格式 / 字段错误重试后仍失败才降级
    return reply or None


def _parse_tool_call(reply: str) -> dict | None:
    """
    工具 JSON 检测：优先整段解析；若回复是「文本 + JSON」混合
    （如『我来写入文件。{"tool": ...}』），扫描每个 { 位置尝试 raw_decode，
    取第一个含 'tool' 键的 JSON 对象；都没有返回 None（视为普通文本）。
    解码器用 strict=False：容忍大模型在 JSON 字符串值里写未转义的
    控制字符（最常见的是正文里直接换行），避免把可执行的指令误判成普通文本。
    """
    if hands is None or not reply:
        return None
    text = reply.strip()
    decoder = json.JSONDecoder(strict=False)
    if not text.startswith("{"):
        # 混合回复：逐个 { 位置尝试解码，寻找内嵌的工具调用
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                call, _end = decoder.raw_decode(text[idx:])
            except ValueError:
                continue
            if isinstance(call, dict) and "tool" in call:
                return call
        return None
    try:
        call = json.loads(text, strict=False)
    except ValueError:
        return None
    if isinstance(call, dict) and "tool" in call:
        return call
    return None


def _think_via_llm(user_text: str, history: list[dict]) -> str | None:
    """
    通过 OpenAI 兼容接口请求大模型，返回最终回复文本；任何失败返回 None。
    阶段八 Agent 循环（多步工具链 ≤4 轮）：
      LLM 返回工具 JSON -> 经 _execute_tool 执行 ->
      结果（截断 800 字）以「[工具结果] ...」作为 user 消息回灌 -> 再次请求，
      直至 LLM 返回普通文本作为最终回复；
      第 4 轮仍返回工具 JSON 则执行后直接返回该结果文本。
    工具 JSON 检测与 [[NEEDS_CONFIRM]] 待确认拦截保持现有行为：
    命中待确认即中断循环，直接返回确认询问，待确认状态机不动。
    """
    cfg = _load_llm_config()
    api_key = cfg.get("api_key")
    if not api_key:
        return None  # 未配置 API Key，直接走规则兜底

    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model", "gpt-4o-mini")
    url = f"{base_url}/chat/completions"

    messages = [{"role": "system", "content": _build_system_prompt()}]
    for turn in (history or [])[-_HISTORY_TURNS:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    payload = {"model": model, "messages": messages, "temperature": 0.7}
    # 通用扩展点：extra_body 为 JSON 字符串时合并进请求体
    # （例如智谱思考型模型用 {"thinking": {"type": "disabled"}} 关闭推理加速）
    extra_body = cfg.get("extra_body")
    if extra_body:
        try:
            extra = json.loads(extra_body)
            if isinstance(extra, dict):
                payload.update(extra)
        except ValueError:
            pass  # 配置写错时静默忽略，不阻断对话
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Agent 循环：每轮先请求大模型；payload['messages'] 与 messages 是同一列表，
    # 循环内追加消息即自动带入下一轮请求
    for round_no in range(1, _MAX_TOOL_ROUNDS + 1):
        reply = _request_llm(url, payload, headers)
        if not reply:
            return None  # 大模型链路故障：整体降级，交给规则兜底
        call = _parse_tool_call(reply)
        if call is None:
            # 防泄漏 + 自愈：回复形似工具调用（含 "tool"/"args"）却解析失败时，
            # 那是格式残缺的执行指令——绝不能当普通文本播报给先生；
            # 轮次预算内回灌一条格式纠正指令让大模型重发（畸形是瞬态非确定性），
            # 预算耗尽才如实汇报失败
            if '"tool"' in reply and '"args"' in reply:
                print("[brain] 大模型返回了无法解析的工具 JSON（格式残缺），要求其重发。")
                if round_no < _MAX_TOOL_ROUNDS:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[系统提示] 你上一条回复是无法解析的 JSON，未被执行。"
                            "请严格输出一个合法的 JSON 工具调用"
                            "（字符串值内的换行必须写成 \\n 转义、引号必须成对转义），"
                            "或者放弃工具、改用纯文本直接回答。"
                        ),
                    })
                    continue
                return (
                    "抱歉先生，我在组织执行指令时格式出了点问题，这一步没能完成；"
                    "请您再说一遍，我重新组织一次。"
                )
            return reply  # 普通文本：Agent 循环结束，作为最终回复
        result = _execute_tool(call["tool"], call.get("args") or {})
        if not isinstance(result, str):
            result = str(result)
        # 待确认拦截（[[NEEDS_CONFIRM]]）：保持现有行为——状态机已挂起，
        # 直接返回确认询问，不再回灌大模型，等先生亲口确认
        if _pending_shell is not None:
            return result
        if round_no >= _MAX_TOOL_ROUNDS:
            # 第 4 轮仍返回工具 JSON：执行后直接返回该结果文本，循环止步
            return result
        # 结果回灌：assistant 记录本轮工具调用，user 携带 [工具结果] 供继续推理
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"[工具结果] {result[:_TOOL_RESULT_LIMIT]}"})
    return None  # pragma: no cover - 循环内必有返回，此行仅为防御


# == 对外接口（契约签名，勿改动） ==

def think(user_text: str, history: list[dict]) -> str:
    """
    Nolan 大脑主入口。

    参数：
        user_text: 用户说的一句话（中文文本）。
        history:   对话历史，形如 [{"role": "user"|"assistant", "content": str}, ...]。

    返回：
        Nolan 的回复文本（纯文本，可语音播报）；
        用户表达退出意图时返回 '__EXIT__'。
    """
    # 1. 空输入
    if not isinstance(user_text, str) or not user_text.strip():
        return "抱歉先生，我没有听清您的话，能再说一遍吗？"

    text = user_text.strip()

    # 2. 退出意图
    if any(p in text for p in _EXIT_PATTERNS):
        return EXIT_SIGNAL

    # 3. 待确认状态机（run_shell / gui_control 共用）：在记忆意图之前检查，
    #    确认 / 取消 / 新指令覆盖均在此分派
    pending_reply = _handle_pending_shell(text)
    if pending_reply is not None:
        return pending_reply

    # 4. 记忆意图：记住 / 回忆 / 忘掉，结果文本零包装直接返回
    memory_reply = _handle_memory_intent(text)
    if memory_reply is not None:
        return memory_reply

    # 5. 提醒意图：提醒我 / 我的提醒，结果文本零包装直接返回
    reminder_reply = _handle_reminder_intent(text)
    if reminder_reply is not None:
        return reminder_reply

    # 6. 规则意图解析：命中即执行，结果文本零包装直接返回
    #    （run_shell 的待确认拦截在 _execute_tool 内统一处理）
    #    复合任务（含「然后/接着/并且」等连接词）跳过规则层——
    #    规则层是单意图分发，会劫持多步任务；交给 LLM Agent 循环拆解。
    intent = None if _is_composite(text) else _parse_intent(text)
    if intent is not None and hands is not None:
        tool, args = intent
        return _execute_tool(tool, args)

    # 7. 大模型层（含长期记忆与工具协议）
    reply = _think_via_llm(text, history)
    if reply:
        return reply

    # 8. 规则闲聊兜底
    return random.choice(_FALLBACK_REPLIES).format(text=text)
