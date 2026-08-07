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
阶段十一起说话卫生：工具 JSON 提取器三段强化（拆围栏 / 混合 raw_decode / 平衡扫描+修复），
泄漏兜底死契约——解析失败的工具 JSON 剥离后只留口语部分返回；
think 出口闸统一过 speak_filter，代码/JSON/命令行是思考不是台词，永远不许成为返回值。
阶段十二起附件防劫持：_think_impl 入口拆分「指令」与「附件」——
意图路由的唯一合法输入是主人的嘴（指令），不是主人递过来的纸（附件）；
所有规则层只看剥离附件后的纯指令，附件原文仅作为上下文交给大模型层；
记忆萃取/情景记录钩子同样只吃指令，附件全文不进记忆库。

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
import threading
import time
import urllib.parse

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
    import memory_v2  # Gap3 结构化长期记忆（画像注入 + 轮后萃取）
except ImportError:  # pragma: no cover - 未就绪时降级，行为与旧版一致
    memory_v2 = None

try:
    import episodic  # H1 情景记忆（时间线经历：近期事件注入 + 轮后记录）
except ImportError:  # pragma: no cover
    episodic = None

try:
    import reminders  # 阶段四「主动提醒」模块（由并行工程师编写，按契约调用）
except ImportError:  # pragma: no cover - reminders 未就绪时大脑仍可降级运行
    reminders = None

try:
    import triggers  # P4「条件触发」引擎：如果X就Y / 每隔N做Y / 每当X就Y
except ImportError:  # pragma: no cover - triggers 未就绪时大脑仍可降级运行
    triggers = None

try:
    import auth_policy  # H3 分级授权：白名单自主 / 黑名单必确认 / 默认保持现状
except ImportError:  # pragma: no cover - auth_policy 未就绪时一律走现行确认流程
    auth_policy = None

try:
    import speak_filter  # 说话卫生：可念性过滤（代码/JSON 是思考，不是台词）
except ImportError:  # pragma: no cover - 模块缺失时降级，出口闸自动失效
    speak_filter = None

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
    "任务完成后用陈述句直接报告结果：做好了什么、结果放在哪里，说清楚即结束，"
    "禁止以反问或请求指示的句子收尾（如「需要我……吗」「要不要我……」「我来为您确认一下」）；"
    "但执行删除、发送、接管键鼠等危险或不可逆操作前，必须先向先生确认，确认环节不受此限。"
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

_URL_RE = re.compile(r"https?://\S+|www\.[^\s，。！？；]+")
_FILE_RE = re.compile(r"[^\s，。！？；：「」『』“”]+\.[A-Za-z0-9]+")
_LOCAL_APPS = ("记事本", "计算器", "画图")
_TIME_KEYS = ("几点", "时间", "日期", "几号", "星期几", "礼拜几")
_SEARCH_KEYS = ("搜索", "搜一下", "查一下", "百度一下")
_READ_KEYS = ("读取", "看看文件", "读一下", "读")

# 「打开 X 的网站/官网/网页」：网址意图而非本机应用——
# open_app 会把「哔哩哔哩的网站」整串当应用名去瞎找程序，必然失败。
# 常见站点直达域名；未知站点打开必应「X 官网」搜索页，主人一眼看到入口。
_SITE_DOMAINS = {
    "哔哩哔哩": "https://www.bilibili.com", "b站": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "知乎": "https://www.zhihu.com", "微博": "https://weibo.com",
    "淘宝": "https://www.taobao.com", "京东": "https://www.jd.com",
    "抖音": "https://www.douyin.com", "百度": "https://www.baidu.com",
    "github": "https://github.com", "网易云": "https://music.163.com",
    "腾讯视频": "https://v.qq.com", "爱奇艺": "https://www.iqiyi.com",
    "优酷": "https://youku.com", "小红书": "https://www.xiaohongshu.com",
}
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
    ("提醒", "闹钟", "叫醒"),                                  # 提醒组
    ("读给", "读出来", "念给"),                                # 朗读组
)

# 「搜索并总结 X」算单意图：搜索快速通道（_answer_from_search）内部自带总结，
# 判复合会让它绕进 Agent 循环用本地低质量抓取（实测 P1 第 2/7 题）
_SEARCH_AND_SUMMARY = (("搜索", "搜一下", "查一下", "百度"),
                       ("总结", "概括", "归纳"))


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
    if hits == 2:
        # 「搜索并总结 X」单意图化：命中恰为搜索组+总结组时不判复合
        hit_search = any(w in text for w in _SEARCH_AND_SUMMARY[0])
        hit_summary = any(w in text for w in _SEARCH_AND_SUMMARY[1])
        if hit_search and hit_summary:
            return False
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
        # 「设（一个|个|置）…（的）提醒」句式（规划器拆步常见说法）：
        # 提取「提醒」前的时间与事项交给 reminders
        m = re.search(r"设(?:一个|个|置|一下)?(.+?)的?提醒", text)
        if m:
            raw = m.group(1).strip(" ，。！？：:帮我请把")
            if raw:
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


# == 条件触发意图（P4）：如果X就Y / 每隔N做Y / 每当X就Y（在提醒意图之前判定） ==
#
# 为什么排在提醒意图之前：「每隔 30 分钟提醒我喝水」同时含「每隔」与「提醒我」，
# 先进提醒分支会被 reminders 当一次性定时解析（解析不出周期，白白劫持）。
# 防误劫双闸门：必须同时含触发词与动作词——
# 「如果明天下雨怎么办」是提问（无动作词），绝不拦截。

_TRIGGER_KEYS = ("如果", "若", "要是", "每当", "每次", "一旦", "每隔")
_TRIGGER_ACTION_KEYS = ("提醒", "告诉", "叫醒", "叫我", "执行", "帮我", "替我", "播报")
_TRIGGER_LIST_KEYS = ("我的触发", "触发列表", "条件提醒列表", "我的条件提醒")


def _handle_trigger_intent(text: str) -> str | None:
    """条件触发意图分支：命中且解析成功返回确认文本；否则返回 None 放行。"""
    if triggers is None:
        return None
    if any(k in text for k in _TRIGGER_LIST_KEYS):
        try:
            return triggers.list_pending()
        except Exception:
            return None
    if not any(k in text for k in _TRIGGER_KEYS):
        return None
    if not any(k in text for k in _TRIGGER_ACTION_KEYS):
        return None
    try:
        return triggers.add(text)  # 解析不出返回 None，自然放行后续层
    except Exception:  # pragma: no cover - 触发引擎异常时大脑不崩，降级放行
        return None


def eval_condition(condition: str) -> bool | None:
    """P4 条件评估（triggers.check_due 的 evaluator 注入点）：
    联网搜索核实一个自然语言条件此刻是否成立。
    返回 True/False；无法评估（LLM 不在线/回答模糊）返回 None——
    本轮顺延不触发，绝不瞎猜。"""
    cfg = _load_llm_config()
    # 日期锚点：GLM 不知道「今天」是哪天（实测把 2026 年的真条件判假），
    # 条件评估几乎都含时间前提（明天/现在/最近），必须显式锚定
    import time as _time
    _lt = _time.localtime()
    today = "%d年%d月%d日" % (_lt.tm_year, _lt.tm_mon, _lt.tm_mday)
    verdict = _glm_web_search(
        "参考信息：今天是%s。请联网核实并判断：「%s」此刻是否属实？"
        "先简述依据，结尾必须单独给出判定字「是」或「否」。" % (today, condition),
        cfg)
    if not verdict:
        return None
    v = verdict.strip()
    # 从结尾取判定字：判定词只出现在结尾，防正文里的「是否」干扰
    tail = v[-12:]
    if re.search(r"是[。！!．.\s]*$", tail) and "不是" not in tail:
        return True
    if re.search(r"否[。！!．.\s]*$", tail):
        return False
    return None


def _parse_write_file(text: str) -> tuple | None:
    """解析写文件两种句式，解析不出返回 None。
    「把计划/安排/总结/结果/清单/日程写到 F」类：内容是**指代待生成内容的抽象名词**，
    规则层只会把「计划」二字写进文件（实测真这么干过），必须放行给 LLM 生成后写入。"""
    _ABSTRACT_CONTENT = ("计划", "安排", "总结", "结果", "清单", "日程", "行程",
                         "笔记", "心得", "报告", "大纲", "方案", "攻略", "要点", "规划")
    # 内容指代待生成/前序结果：「上一步的结果」「X算出来」「X等于多少」——
    # 规则层只能写字面量，必须放行给 LLM 生成（实测「上一步的结果」被字面写入）
    _GENERATIVE_HINTS = ("上一步", "算出来", "计算", "等于多少", "的结果")
    # 句式一：把 xxx 写到/记到 yyy
    m = re.search(r"把(.+?)(?:写到|记到)\s*([^\s，。！？]+)", text)
    if m:
        content = m.group(1).strip()
        if any(h in content for h in _GENERATIVE_HINTS):
            return None
        # 内容整体是抽象名词（允许「的/今天/明天/本周」等修饰）：需要生成，放行
        stripped = content
        for w in ("今天的", "明天的", "本周的", "这周的", "今天的", "我的", "的"):
            stripped = stripped.replace(w, "")
        if stripped in _ABSTRACT_CONTENT or content in _ABSTRACT_CONTENT:
            return None
        return ("write_file", {"name": m.group(2), "content": content})
    # 句式二：写文件 yyy 内容 xxx
    # 「内容(为|是) X」的连接词消化：规划器拆步常把「内容 第一题」润色成
    # 「内容为 第一题」，不消化会把「为」字写进文件（实测高考 q46）
    m = re.search(r"写文件\s*([^\s，。！？]+)\s*内容[:：]?\s*(?:为|是)?\s*(.+)", text)
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
        # 排除「我一般几点起床」类：疑问主体是用户自身习惯/属性，不是问当下时间，
        # 放行给记忆/大模型层（长期记忆已注入系统提示，LLM 能据记忆回答）。
        # 含「现在/今天」等时间副词的一定是当下时间查询，优先报时。
        _NOW_WORDS = ("现在", "今天", "明天", "后天", "昨天", "当前", "这会儿", "此刻")
        if (any(w in text for w in _NOW_WORDS)
                or not re.search(r"我[^，。！？]{0,10}(一般|通常|平时|习惯|生日|电话|地址|名字|喜欢|几|什么|哪|多少)", text)):
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

    # 「打开 X 的网站/官网/网页」：网址意图，优先于 open_app 判定
    if not url_match:
        site_match = re.search(r"打开\s*(.+?)\s*的?\s*(?:网站|官网|网页)\s*$", text)
        if site_match:
            site = site_match.group(1).strip()
            domain = _SITE_DOMAINS.get(site) or _SITE_DOMAINS.get(site.lower())
            if domain:
                return ("open_url", {"url": domain})
            return ("open_url", {"url": "https://www.bing.com/search?q="
                                       + urllib.parse.quote(site + " 官网")})

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
        # 复合任务过滤：「应用名」里再含动作动词（播放/输入/搜索等），
        # 说明是「打开 X 并做 Y」的复合指令，open_app 只能完成前半截，
        # 必须放行给 LLM 走 gui_control 全链路，避免只打开不干活
        _VERBS = ("播放", "输入", "搜索", "点击", "写", "打开", "暂停", "下载")
        if (app_name and app_name not in _OPEN_APP_BLACKLIST
                and not any(v in app_name for v in _VERBS)):
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
        # 清洗开头的连接/动作残留：规划器子任务常是「搜索并总结 X」，
        # 剥掉触发词后「并总结」混进 query 会把搜索带偏（实测）
        for junk in ("并总结", "再总结", "然后总结", "接着总结", "总结一下",
                     "并概括", "并", "再", "总结", "概括"):
            if query.startswith(junk):
                query = query[len(junk):].lstrip(" ，。：:")
                break
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


def _confirm_prompt(tool: str, args: dict) -> str:
    """生成待确认请求文案（run_shell / gui_control 专用话术，其余工具通用）。"""
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
    return f"先生，操作「{tool}」需要您亲口确认。您确认执行吗？"


def _execute_tool(tool: str, args: dict) -> str:
    """
    统一工具执行入口（规则路径与 LLM 路径共用）。
    hands 返回以 '[[NEEDS_CONFIRM]]' 开头的文本时，将工具与参数存入待确认状态机，
    并向先生如实复述风险、请求确认；其余结果文本零包装直接返回。

    H3 分级授权（auth_policy，防御式接入：模块缺失/判定异常一律走现行逻辑）：
      "confirm"（黑名单）——执行前直接挂起待确认状态机，强制确认，
        即使该工具原本不需要确认；
      "auto"（白名单）——hands 请求确认时附加 confirmed=True 自动放行；
      "default"——现行逻辑原样（缺省策略文件时所有调用都是这一档，
        这是默认零回退死契约）。
    安全边界：分级授权只管「确认流程」，绝不解除 hands/VLM 硬编码的安全禁令。
    """
    global _pending_shell
    args = args or {}
    # H3 黑名单前置闸：已带 confirmed=True 的重放不再重复判定
    if auth_policy is not None and not args.get("confirmed"):
        try:
            _pre = auth_policy.decide(tool, args)
        except Exception:  # noqa: BLE001 - 策略故障降级为现行流程
            _pre = "default"
        if _pre == "confirm":
            print(f"[brain] 分级授权：黑名单拦截「{tool}」，强制要求先生亲口确认。")
            _pending_shell = {"tool": tool, "args": dict(args)}
            return _confirm_prompt(tool, args)
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
        # H3 白名单放行：策略判定免确认，附加 confirmed=True 直接重放执行
        if auth_policy is not None:
            try:
                _post = auth_policy.decide(tool, args)
            except Exception:  # noqa: BLE001 - 策略故障降级为现行流程
                _post = "default"
            if _post == "auto":
                print(f"[brain] 分级授权：白名单放行「{tool}」，免确认直接执行。")
                auto_args = dict(args)
                auto_args["confirmed"] = True
                return hands.execute(tool, auto_args)
        _pending_shell = {"tool": tool, "args": dict(args)}
        return _confirm_prompt(tool, args) if tool in ("run_shell", "gui_control") else result
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
    # Gap3 结构化画像注入：偏好/习惯/人物的浓缩摘要，与上面的逐行记忆互补
    if memory_v2 is not None:
        try:
            profile = memory_v2.profile_summary().strip()
        except Exception:  # pragma: no cover - 画像异常视为无画像
            profile = ""
        if profile:
            prompt += "\n以下是你对主人的画像摘要（偏好与习惯）：\n" + profile
    # H1 情景记忆注入：近 48 小时高显著度经历（任务成败/错误/里程碑）
    if episodic is not None:
        try:
            epi = episodic.brief_for_prompt().strip()
        except Exception:  # pragma: no cover
            epi = ""
        if epi:
            prompt += "\n" + epi
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
        "写作类任务（邮件/短信/文案/诗歌/清单等）：直接把成品全文作为回复交给主人；"
        "只有主人明确给出文件名时才调 write_file 保存，且保存后仍要把全文复述给主人——"
        "只说一句「已保存」等于让主人自己翻文件，不算完成。"
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
    # 多引擎支持（A/B 实测）：brain_api_key / brain_base_url / brain_model
    # 存在时大脑走独立引擎（如 Kimi），缺省用主配置，行为与旧版一致；
    # 视觉/语音引擎不受影响（eyes/mouth 各自读自己的覆盖键）
    for alt_key, key in (
        ("brain_api_key", "api_key"),
        ("brain_base_url", "base_url"),
        ("brain_model", "model"),
    ):
        if cfg.get(alt_key):
            cfg[key] = cfg[alt_key]
    # 引擎专属参数隔离：extra_body 是智谱系参数（thinking 开关），
    # 大脑切到别家引擎时必须用 brain_extra_body 显式指定，否则清空——
    # 把 GLM 的私有参数发给 Kimi/GPT 会直接 400
    if cfg.get("brain_base_url"):
        cfg["extra_body"] = cfg.get("brain_extra_body", "")
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


# == 工具 JSON 提取与泄漏兜底 ==
#
# 事故现场（真实截图）：LLM 回复「先生，我将用PowerPoint…{一大串工具JSON}」，
# JSON 没被解析执行，整段（含 PowerShell）成了回复文本被 TTS 念出。
# 两道闸门：①提取器尽可能把混合/围栏/瑕疵 JSON 救回来执行；
# ②救不回来时，泄漏兜底死契约——剥离 JSON 只还口语部分，
# 原始 JSON/代码永远不许成为 think() 的返回值。

_FENCE_UNWRAP_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)

# 泄漏检测：形似工具调用的残影（"tool": 键，容忍单引号/花括号间空白）
_TOOL_JSON_HINT_RE = re.compile(r"[\{\"']\s*tool[\"']\s*:", re.IGNORECASE)

# 泄漏兜底通用话术（口语部分为空时使用）
_LEAK_FALLBACK = "先生，我在处理这个任务，请稍候看结果。"


def _unwrap_fences(text: str) -> str:
    """拆掉 markdown 代码围栏、保留内容——LLM 常把工具 JSON 包在
    ```json ... ``` 里，围栏本身不是 JSON，不拆会挡住院括号扫描。"""
    text = _FENCE_UNWRAP_RE.sub(lambda m: m.group(1), text)
    # 残余孤立围栏标记行（未闭合围栏的开头，如一行 ```json）直接删行
    return re.sub(r"^```[^\n]*$", "", text, flags=re.MULTILINE)


def _repair_json(candidate: str) -> str:
    """轻量修复 LLM 常见 JSON 瑕疵：对象/数组收尾前的多余逗号。"""
    return re.sub(r",(\s*[}\]])", r"\1", candidate)


def _balanced_json_candidates(text: str):
    """从每个 { 位置做花括号平衡扫描（字符串内转义感知），产出候选子串。
    raw_decode 搞不定的场景（JSON 后紧跟文本、轻微瑕疵）靠它截出候选再修复。"""
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _as_tool_call(obj) -> dict | None:
    """解析结果若是含 tool 键的 dict 则返回，否则 None。"""
    return obj if isinstance(obj, dict) and "tool" in obj else None


def _parse_tool_call(reply: str) -> dict | None:
    """
    工具 JSON 检测：返回含 'tool' 键的调用 dict；不是工具调用返回 None。
    强化后的三段策略：
      1. 拆 markdown 围栏后整段解析（纯 JSON 回复，含围栏包裹情形）；
      2. 混合回复（前置口语文本 + 跨行 JSON）：逐个 { 位置 raw_decode，
         解码器 strict=False，容忍字符串值里的未转义换行；
      3. 平衡扫描兜底：花括号配对截出候选，原文与轻量修复（去尾逗号）
         各试一次 json.loads——对付 JSON 后紧跟文本、尾逗号等瑕疵。
    """
    if hands is None or not reply:
        return None
    text = _unwrap_fences(reply.strip())
    decoder = json.JSONDecoder(strict=False)
    stripped = text.strip()
    # 1. 整段解析
    if stripped.startswith("{"):
        try:
            call = _as_tool_call(json.loads(stripped, strict=False))
            if call is not None:
                return call
        except ValueError:
            pass
    # 2. 逐个 { 位置 raw_decode（含第 1 步失败的整段，raw_decode 容忍尾部杂散）
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            call = _as_tool_call(decoder.raw_decode(text[idx:])[0])
            if call is not None:
                return call
        except ValueError:
            continue
    # 3. 平衡扫描 + 修复解析（跨行、转义引号、尾逗号等瑕疵场景）
    for cand in _balanced_json_candidates(text):
        for variant in (cand, _repair_json(cand)):
            try:
                call = _as_tool_call(json.loads(variant, strict=False))
            except ValueError:
                continue
            if call is not None:
                return call
    return None


def _looks_like_tool_json(reply: str) -> bool:
    """检测回复是否形似工具调用（含 "tool": 残影）却没能解析执行。
    比旧实现 '"tool"' in reply and '"args"' in reply 更宽：容忍单引号、
    键前无引号、args 缺失等残缺形态——残缺的执行指令绝不能当普通文本播报。"""
    return bool(_TOOL_JSON_HINT_RE.search(reply or ""))


def _colloquial_or_generic(reply: str) -> str:
    """
    泄漏兜底（死契约）：检测到工具 JSON 但解析/执行失败时，
    最终对先生返回的文本必须剥离 JSON 和代码，只留口语部分
    （如「先生，我将用PowerPoint为您制作…」）；口语部分为空时
    返回通用话术。原始 JSON/代码永远不许成为 think() 的返回值。
    """
    if speak_filter is not None:
        clean = speak_filter.speakable(reply, max_chars=None)
        if clean:
            return clean
    return _LEAK_FALLBACK


def _speak_guard(reply: str) -> str:
    """
    出口闸：think() 的任何返回值在交给先生（可见与可念）之前，
    统一过一道「不可念内容」检查——代码、JSON、命令行、路径一律剥离；
    剥完为空用泄漏兜底话术。可见与可念同一标准（max_chars=None，
    不截断全文，只剥不可念内容）。__EXIT__ 信号原样放行。
    """
    if not reply or reply == EXIT_SIGNAL or speak_filter is None:
        return reply
    clean = speak_filter.speakable(reply, max_chars=None)
    if clean == reply.strip():
        return reply  # 纯口语，原样通过（零改写零风险）
    return clean or _LEAK_FALLBACK


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
            # 防泄漏 + 自愈：回复形似工具调用（含 "tool": 残影）却解析失败时，
            # 那是格式残缺的执行指令——绝不能当普通文本播报给先生；
            # 轮次预算内回灌一条格式纠正指令让大模型重发（畸形是瞬态非确定性），
            # 预算耗尽走泄漏兜底：剥离 JSON 只留口语部分，原始代码绝不上屏上嘴
            if _looks_like_tool_json(reply):
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
                return _colloquial_or_generic(reply)
            # 普通文本：Agent 循环结束，作为最终回复
            # （think() 出口闸会统一再过一遍不可念内容检查，此处原样返回）
            return reply
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

# == 分层规划（P1）：大目标 -> 拆步 -> 逐步执行 -> 汇总 ==
#
# 为什么需要这一层：P0 证明单步任务可靠（300/300），但「帮我准备明天出差」
# 这类大目标是 3 步以上复合——平铺 Agent 循环（4 轮上限）实测漏步、乱序、
# 内容错（P1 基线仅 20%）。第一性原理：把大目标还原为一串单步任务，
# 每个子任务复用已验证的 think 全链路（规则层/快速通道/Agent 循环），
# 确定性来自「单步可靠」而不是更大的临场自由。

_PLAN_DEPTH = 0  # 规划执行深度：子任务执行期间不再触发规划器（防递归）
_PLAN_MAX_STEPS = 6


def _llm_plan(text, cfg):
    """规划器：大目标 -> 有序子任务清单（每步一句单意图中文指令），失败返回 None。"""
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": (
                "你是 Nolan 的规划器。把主人的大目标拆成 2-%d 个有序子任务，"
                "每个子任务是一句可以独立执行的简单中文指令（单意图，不含「然后/接着/并且」）。\n"
                "可利用的能力：查时间天气、搜索并总结、写/读文件（文件名带扩展名）、"
                "记住事情、设提醒、心算算术、打开应用或网站、执行系统命令、操作软件界面。\n"
                "规则：后续步骤需要前面结果时，在指令里写「上一步的结果」；"
                "提醒类子任务必须用「提醒我 + 时间 + 事项」完整句式，"
                "时间规范为「X点X分」或「明早X点」（如「提醒我明早7点收拾出差行李」）；"
                "文件类子任务把文件名说完整。\n"
                "只输出 JSON 数组，例如："
                "[\"查一下北京明天天气\", \"把行李清单写到 出差清单.txt\"]，"
                "不要输出任何其他字符。" % _PLAN_MAX_STEPS)},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }
    extra_body = cfg.get("extra_body")
    if extra_body:
        try:
            extra = json.loads(extra_body)
            if isinstance(extra, dict):
                payload.update(extra)
        except ValueError:
            pass
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    reply = _request_llm(base_url + "/chat/completions", payload, headers)
    if not reply:
        return None
    # 解析 JSON 数组：定位首个 [ 后 raw_decode，容忍前后杂散文本
    decoder = json.JSONDecoder(strict=False)
    for idx, ch in enumerate(reply):
        if ch != "[":
            continue
        try:
            steps, _end = decoder.raw_decode(reply[idx:])
        except ValueError:
            continue
        if isinstance(steps, list) and all(isinstance(s, str) and s.strip() for s in steps):
            steps = [s.strip() for s in steps if s.strip()][: _PLAN_MAX_STEPS]
            return steps if len(steps) >= 2 else None
    print("[brain] 规划器返回了无法解析的步骤列表：%r" % reply[:120])
    return None


def _summarize_plan(goal, outcomes, cfg):
    """汇总器：各步结果 -> 一段口语汇报，失败步骤如实说明。"""
    lines = "\n".join(
        "第%d步「%s」：%s" % (i, s, (r or "").replace("\n", " ")[:150])
        for i, s, r in outcomes)
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": (
                "你是 Nolan。主人交代了一个大目标，你已分步执行完毕。"
                "用两三句口语向先生汇报：先说完成了什么、关键结果是什么；"
                "若有步骤失败，如实说哪一步没成、原因是什么。不超过 120 字。")},
            {"role": "user", "content": "主人的目标：%s\n\n各步执行结果：\n%s" % (goal, lines)},
        ],
        "temperature": 0.3,
    }
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    reply = _request_llm(base_url + "/chat/completions", payload, headers)
    if reply:
        return reply
    # 汇总失败兜底：直接拼接各步结果，绝不静默
    return "先生，目标已分步执行，结果如下：" + "；".join(
        "%s：%s" % (s, (r or "")[:60]) for _, s, r in outcomes)


def _plan_and_execute(text, history):
    """分层规划主流程：拆步 -> 逐步 think（结果经对话历史传递）-> 汇总。
    任何一环不可用即降级回平铺 Agent 循环。"""
    global _PLAN_DEPTH
    cfg = _load_llm_config()
    if not cfg.get("api_key"):
        return _think_via_llm(text, history)
    steps = _llm_plan(text, cfg)
    if not steps:
        return _think_via_llm(text, history)
    print("[brain] 规划器拆出 %d 步：%s" % (len(steps), " | ".join(steps)))
    outcomes = []
    _PLAN_DEPTH += 1
    try:
        for i, step in enumerate(steps, 1):
            # 结果传递：前序步骤问答拼成对话历史，LLM 处理「上一步的结果」
            sub_history = []
            for _, prev_step, prev_reply in outcomes:
                sub_history.append({"role": "user", "content": prev_step})
                sub_history.append({"role": "assistant", "content": prev_reply or ""})
            reply = think(step, sub_history)
            outcomes.append((i, step, reply))
    finally:
        _PLAN_DEPTH -= 1
    return _summarize_plan(text, outcomes, cfg)

# 「搜 X 总结写到 F」复合任务正则：query 非贪婪截到「总结/写到」之前，
# 「总结」与条数修饰（两条/3 条）可缺省；文件名必须是带扩展名的词
_SEARCH_WRITE_RE = re.compile(
    r"(?:搜一下|搜索|查一下|百度一下)\s*(?P<query>.+?)"
    r"(?:[，,]?\s*(?:总结|概括|归纳)[^，。！？]{0,8})?[，,]?\s*"
    r"(?:写到|写进|写入|保存到|记到)\s*"
    r"(?P<name>[^\s，。！？；：「」『』“”]+\.[A-Za-z0-9]+)"
)


def _parse_search_write(text):
    """解析「搜 X（总结 N 条）写到 F」复合指令，返回 (query, 文件名) 或 None。
    只处理纯「搜+写」两步任务；带提醒/「然后/还要/再」等第三动作的
    大目标放行给分层规划器，避免本通道把整个多步目标错误吞进 query。"""
    if any(k in text for k in ("提醒", "闹钟", "叫醒", "然后", "接着",
                               "还要", "再设", "再帮", "再记", "顺便")):
        return None
    m = _SEARCH_WRITE_RE.search(text)
    if not m:
        return None
    query = m.group("query").strip(" ，。！？：:帮我请把")
    return (query, m.group("name")) if query else None


def _run_search_write(query, filename):
    """搜索+写文件快速通道：固定三段执行 抓->总结->写，零 LLM 临场决策。
    实测 Agent 循环在此类任务上行为不稳（死循环调 get_time / 写空文件），
    确定性优先；搜索失败时如实上报且绝不写空文件。"""
    summary = _answer_from_search(query)
    if summary.startswith("抱歉先生"):
        return summary
    hands.execute("write_file", {"name": filename, "content": summary})
    return summary + "\n（已为您存到文件柜「%s」）" % filename


def _glm_web_search(query: str, cfg: dict) -> str | None:
    """智谱联网搜索通道：服务端检索+回答一步完成。
    为什么优先它：本地抓必应对中文长尾查询命中率差（实测约 1/3 查询返回
    词典释义/无关结果），智谱服务端搜索质量高一个量级且零额外成本；
    仅在智谱端点启用，任何异常返回 None 走本地兜底。"""
    base_url = cfg.get("base_url", "")
    if "bigmodel.cn" not in base_url or not cfg.get("api_key"):
        return None
    payload = {
        "model": cfg.get("model", "glm-5.2"),
        "messages": [
            {"role": "system", "content": (
                "你是 Nolan 的搜索总结器。用联网搜索查主人问的事，"
                "用两三句口语汇报要点：先给结论，再补关键细节；不超过 100 字；"
                "查不到就如实说，绝不编造。")},
            {"role": "user", "content": query},
        ],
        "tools": [{"type": "web_search", "web_search": {"enable": True}}],
        "temperature": 0.3,
    }
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    try:
        resp = httpx.post(base_url.rstrip("/") + "/chat/completions",
                          json=payload, headers=headers, timeout=_API_TIMEOUT)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        return reply or None
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        print("[brain] 智谱联网搜索失败，降级本地抓取: %s" % e)
        return None


def _answer_from_search(query: str) -> str:
    """
    规则层搜索快速通道：search_web 抓结果文本 -> 单次 LLM 口语化总结。

    为什么存在：「搜一下/查一下 X」的真实意图是知道 X 的内容，
    web_search 只把浏览器打开给主人看，等于把活推回给主人——贾维斯不这么干。
    优先走智谱联网搜索（服务端检索，质量高）；不可用时本地抓必应 +
    单次 LLM 总结（不走 Agent 循环），比 _think_via_llm 省一半延迟且行为确定；
    LLM 不可用时如实降级给原文截断，绝不谎称总结过。
    """
    cfg = _load_llm_config()
    glm_reply = _glm_web_search(query, cfg)
    # 质量门：GLM 未真正调用搜索工具时会给推托回答（实测「抱歉，我现在
    # 没法联网搜索」），此时降级本地必应通道再试一次，诚实性不变
    _GLM_EXCUSES = ("没法联网", "无法联网", "不能联网", "无法搜索",
                    "没法搜索", "查不到", "没办法查")
    if glm_reply and not any(m in glm_reply for m in _GLM_EXCUSES):
        return glm_reply
    result = hands.execute("search_web", {"query": query})
    if not isinstance(result, str) or not result.strip():
        return "抱歉先生，这次搜索没有拿到结果，您换个说法我再试试。"
    cfg = _load_llm_config()
    api_key = cfg.get("api_key")
    if not api_key:
        return "先生，我查到以下内容（大模型不在线，未能总结，给您原文要点）：" + result[:300]
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": (
                "你是 Nolan 的搜索总结器。根据给定的搜索结果，用两三句口语向先生汇报要点："
                "先给结论，再补关键细节；全文不超过 100 字；"
                "结果里没有的信息不要编造；结果空洞或与问题无关时，"
                "如实说「网上没有找到可靠信息」。")},
            {"role": "user", "content": "主人问：%s\n\n搜索结果：\n%s" % (query, result)},
        ],
        "temperature": 0.3,
    }
    extra_body = cfg.get("extra_body")
    if extra_body:
        try:
            extra = json.loads(extra_body)
            if isinstance(extra, dict):
                payload.update(extra)
        except ValueError:
            pass
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    reply = _request_llm(base_url + "/chat/completions", payload, headers)
    if reply:
        return reply
    return "先生，我查到以下内容（总结失败，给您原文要点）：" + result[:300]


def glm_one_shot(prompt: str) -> str | None:
    """
    单发 GLM 调用：prompt 进、文本出（无历史、无工具、无 Agent 循环）。
    供记忆萃取（memory_v2）与主动性生成（proactive）等轻量场景复用；
    任何失败返回 None，调用方自行降级。
    """
    cfg = _load_llm_config()
    if not cfg.get("api_key"):
        return None
    base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
    }
    extra = cfg.get("extra_body")
    if extra:
        try:
            payload.update(json.loads(extra))
        except ValueError:
            pass
    return _request_llm(base_url + "/chat/completions", payload, headers)


# == 附件防劫持：指令 / 附件拆分 ==

# 前端附件标记契约（固定格式，可依赖）：
#   [附件《文件名》内容开始] ... [附件内容结束，请基于以上内容回答]
# 可能有多个附件块，正文指令在最后一个结束标记之后（也可能夹在两附件之间）。
_ATTACHMENT_BLOCK_RE = re.compile(
    r"\[附件《[^》]*》内容开始\].*?\[附件内容结束，请基于以上内容回答\]",
    re.DOTALL,
)


def _split_attachment(text: str) -> tuple:
    """
    拆分「主人的嘴」与「主人递过来的纸」：
    返回 (instruction, full_text)——剥离附件块后的纯指令 + 原文。

    - 无附件标记：(原文, 原文)，零行为变化；
    - 完整附件块（可多个）：instruction 为剔除所有附件块后的指令；
    - 标记残缺（只有开始没有结束、或结束后仍有未闭合的开始）：保守按无附件
      处理，返回 (原文, 原文)——宁可被劫持风险留在已知形态，也不瞎猜截断点。
    """
    if "[附件《" not in text:
        return text, text
    instruction = _ATTACHMENT_BLOCK_RE.sub("\n", text).strip()
    if instruction == text.strip():
        return text, text  # 开始标记无配对结束：整块未识别，保守放行
    if "[附件《" in instruction:
        return text, text  # 仍有未闭合的开始标记（残缺）：保守放行
    return instruction, text


def _think_impl(user_text: str, history: list[dict]) -> str:
    """
    Nolan 大脑主入口（实现体；公开入口 think 在其外挂了记忆萃取钩子）。

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

    # 附件防劫持（阶段十二）：意图路由的唯一合法输入是主人的嘴（instruction），
    # 不是主人递过来的纸（附件）。下方所有规则层一律只看剥离附件后的纯指令；
    # 附件原文 full_text 只交给大模型层——附件是上下文，LLM 需要读它来分析。
    instruction, full_text = _split_attachment(text)

    # 2. 退出意图（附件里的「再见/退出」不许劫持会话）
    if any(p in instruction for p in _EXIT_PATTERNS):
        return EXIT_SIGNAL

    # 3. 待确认状态机（run_shell / gui_control 共用）：在记忆意图之前检查，
    #    确认 / 取消 / 新指令覆盖均在此分派
    pending_reply = _handle_pending_shell(instruction)
    if pending_reply is not None:
        return pending_reply

    # 4. 记忆意图：记住 / 回忆 / 忘掉，结果文本零包装直接返回
    #    复合任务跳过本层——「搜 X 再记住 Y」被本层劫持会丢掉前半截（实测）
    memory_reply = None if _is_composite(instruction) else _handle_memory_intent(instruction)
    if memory_reply is not None:
        return memory_reply

    # 4.5 条件触发意图（P4）：如果X就Y / 每隔N做Y / 每当X就Y
    #    必须排在提醒意图之前——「每隔30分钟提醒我喝水」含「提醒我」，
    #    先进提醒分支会被当一次性提醒劫持（周期信息丢失）。
    #    复合任务跳过本层，同记忆/提醒层的防劫持理由。
    trigger_reply = None if _is_composite(instruction) else _handle_trigger_intent(instruction)
    if trigger_reply is not None:
        return trigger_reply

    # 5. 提醒意图：提醒我 / 我的提醒，结果文本零包装直接返回
    #    复合任务同样跳过（「写日报然后提醒我晚上看」被本层劫持会丢掉写日报）
    reminder_reply = None if _is_composite(instruction) else _handle_reminder_intent(instruction)
    if reminder_reply is not None:
        return reminder_reply

    # 6. 规则意图解析：命中即执行，结果文本零包装直接返回
    #    （run_shell 的待确认拦截在 _execute_tool 内统一处理）
    #    复合任务（含「然后/接着/并且」等连接词）跳过规则层——
    #    规则层是单意图分发，会劫持多步任务；交给 LLM Agent 循环拆解。
    #    例外：「搜 X 总结写到 F」复合任务走固定三段快速通道（确定性优先，
    #    实测 Agent 循环在此类任务上不稳：死循环调 get_time / 写空文件）。
    if hands is not None:
        sw = _parse_search_write(instruction)
        if sw is not None:
            return _run_search_write(*sw)
    intent = None if _is_composite(instruction) else _parse_intent(instruction)
    if intent is not None and hands is not None:
        tool, args = intent
        if tool == "web_search":
            # 「搜一下 X」要的是答案而不是看浏览器打开：
            # 走 search_web 抓文本 -> LLM 一次性总结的快速通道
            return _answer_from_search(args["query"])
        return _execute_tool(tool, args)

    # 7. 大模型层（含长期记忆与工具协议）：附件原文 full_text 在此进入对话——
    #    附件是主人给的阅读材料，LLM 必须读到它才能完成「分析文件内容」。
    #    复合任务（大目标）先过分层规划器：拆步 -> 逐步执行 -> 汇总；
    #    规划器不可用（无 LLM/拆不出步骤）时降级回平铺 Agent 循环。
    #    _PLAN_DEPTH > 0 说明当前是规划器派生的子任务，直接走原路径防递归。
    if _is_composite(instruction) and _PLAN_DEPTH == 0:
        reply = _plan_and_execute(full_text, history)
    else:
        reply = _think_via_llm(full_text, history)
    if reply:
        return reply

    # 8. 规则闲聊兜底（不回显附件全文，只复述指令）
    return random.choice(_FALLBACK_REPLIES).format(text=instruction or text)


def think(user_text: str, history: list[dict]) -> str:
    """
    大脑公开入口（Gap3 记忆萃取钩子）。

    第一性原理：萃取每轮要花一次 GLM 调用（秒级），绝不能加在
    回复链路上——那是用户能感知的延迟税。因此萃取放守护线程
    异步进行，回复延迟零增加；萃取失败只损失一条记忆，不影响对话。

    说话卫生（死契约）：任何返回值先过 _speak_guard 出口闸——
    代码、工具 JSON、命令行、路径是 Nolan 的思考不是台词，
    原始 JSON/代码永远不许成为 think() 的返回值（可见与可念同一标准）。
    """
    reply = _speak_guard(_think_impl(user_text, history))
    # 记忆萃取/情景记录同样只吃指令（附件全文不该进记忆库）；
    # 纯附件消息（无指令）用占位语记一笔，不录附件原文
    instr = _split_attachment(user_text.strip())[0] if isinstance(user_text, str) else ""
    if not instr:
        instr = "（先生发来附件，未附指令）"
    if episodic is not None and reply and reply != EXIT_SIGNAL:
        try:
            episodic.log_event(
                "conversation",
                "先生：%s｜我：%s" % (instr[:40], reply[:60]))
        except Exception:
            pass
    if memory_v2 is not None and reply and reply != EXIT_SIGNAL:
        u, a = instr, reply

        def _extract():
            try:
                for item in memory_v2.extract_from_turn(u, a, llm_caller=glm_one_shot):
                    memory_v2.remember(**item)
            except Exception as exc:  # pragma: no cover - 萃取失败静默降级
                print("[brain] 记忆萃取异常（已跳过）：%s" % exc)

        threading.Thread(target=_extract, daemon=True, name="memory-extract").start()
    return reply
