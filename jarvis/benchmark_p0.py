# -*- coding: utf-8 -*-
"""
benchmark_p0.py —— P0 真实任务水库：300 条主人真实日常任务的长期可靠性追踪

与 benchmark_j1.py（20 题定点验收）的区别：
  J1 是「能不能干活」的体检；P0 是「天天干活可不可靠」的心电图。
  300 条覆盖 10 个真实场景类目，分批跑（每天一批或按需），
  每批结果追加写入 results_p0.jsonl——成功率趋势曲线是 P0 的核心交付物，
  可靠性有没有变好，用时间序列说话，不用感觉说话。

断言类型（元组第一个元素）：
  ("c", 词)               话术包含词
  ("nf",)                 话术无失败标记
  ("len", N)              话术长度 >= N
  ("re", 正则)            话术匹配正则
  ("chat",)               闲聊合格（非空/无失败/无工具泄漏）
  ("file", 文件名, [词…])  沙盒文件存在且包含全部词
  ("mem", [词…])          长期记忆包含全部词
  ("rem", [词…])          提醒文件包含全部词
  ("proc", [exe…])        进程在运行
  ("gui",)                GUI 任务：走确认流，话术无失败标记

任务元组：(编号, 指令, 断言, 依赖, 类目)
  依赖：None | "net" | "llm" | "gui"（gui 任务慢，单独区间跑）

运行：
  python jarvis/benchmark_p0.py            # 全量（数小时，建议分批）
  python jarvis/benchmark_p0.py 1-30       # 跑区间
  python jarvis/benchmark_p0.py stats      # 查看历史成功率趋势
"""

import os
import re
import sys
import json
import socket
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain
import hands
import memory  # noqa: F401

_JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX = hands.SANDBOX_DIR
MEM_FILE = os.path.join(_JARVIS_DIR, "memory", "long_term.txt")
REM_FILE = os.path.join(_JARVIS_DIR, "memory", "reminders.txt")
RESULTS_FILE = os.path.join(_JARVIS_DIR, "memory", "results_p0.jsonl")

# 失败话术判定：系统级失败一律以「抱歉先生」开头（brain/hands 全系统统一格式），
# 按开头判定；「未能完成」等词保留内容匹配。绝不用「抱歉」单词判失败——
# 道歉/检讨类写作成品天然含「抱歉，我」，内容词会误杀正常成品。
FAIL_PREFIX = "抱歉先生"
FAIL_MARKS = ("未能完成", "出了问题", "无法连接", "超出安全上限", "已被安全中止")
CHAT_FAIL_MARKS = ("未能完成", "出了问题", "无法连接")


def no_fail(r):
    return isinstance(r, str) and bool(r.strip()) and \
        not r.strip().startswith(FAIL_PREFIX) and \
        not any(m in r for m in FAIL_MARKS)


def chat_ok(r):
    if not isinstance(r, str) or not r.strip():
        return False
    if any(m in r for m in CHAT_FAIL_MARKS):
        return False
    return not ('"tool"' in r and r.lstrip().startswith("{"))


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _proc_running(*exe):
    try:
        import subprocess
        out = subprocess.check_output(["tasklist"], text=True,
                                      errors="replace").lower()
        return any(e.lower() in out for e in exe)
    except Exception:
        return False


def _kill(*exe):
    import subprocess
    for e in exe:
        subprocess.run(["taskkill", "/f", "/im", e], capture_output=True)


def _mouse_home():
    """GUI 题前置：把鼠标移到屏幕中央。
    gui_control 的安全机制是「鼠标甩到屏幕角落 = 紧急中止」——跑批时鼠标若恰好
    停在角落（上一题操作后遗留），后续每题都会开局即被中止、空转假过。"""
    try:
        import pyautogui
        w, h = pyautogui.size()
        pyautogui.moveTo(w // 2, h // 2)
    except Exception:
        pass  # 无 GUI 环境时静默跳过（GUI 题自身会因依赖判定跳过）


def _cn_variants(word):
    """纯数字断言词 -> 中文数字写法集合。Nolan 是语音助手，答「六十」和「60」
    等价（TTS 场景中文数字反而更自然），任一写法命中即过。"""
    if not word.isdigit():
        return {word}
    n = int(word)
    if n == 0:
        return {word, "零"}
    d = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]

    def four(x):  # 0-9999 -> 中文（「十五」省略前导一）
        s, zero, xs = "", False, str(x)
        for i, ch in enumerate(xs):
            v, pos = int(ch), len(xs) - 1 - i
            if v == 0:
                zero = True
                continue
            if zero and s:
                s += "零"
            zero = False
            if not (v == 1 and pos == 1 and not s):
                s += d[v]
            s += units[pos]
        return s

    high, low = divmod(n, 10000)
    parts = []
    if high:
        parts.append(four(high) + "万")
    if low:
        if high and low < 1000:
            parts.append("零")
        parts.append(four(low))
    cn = "".join(parts)
    out = {word, cn}
    if n >= 1000:
        out.add(f"{n:,}")  # 千分位：65,536
    if "二" in cn:
        out.add(cn.replace("二", "两", 1))  # 量词前读「两」：两斤/两百
    return out


def check(spec, reply):
    """按断言规格检查，返回 (ok, 说明)。"""
    kind = spec[0]
    if kind == "all":  # 复合断言：子断言全部通过（大目标题的多重验证）
        details = []
        for sub in spec[1]:
            ok, d = check(sub, reply)
            details.append(("✓" if ok else "✗") + d)
            if not ok:
                return (False, "；".join(details))
        return (True, "；".join(details))
    if kind == "c":
        hit = any(v in reply for v in _cn_variants(spec[1]))
        return (hit, "含「%s」" % spec[1])
    if kind == "nf":
        return (no_fail(reply), "话术 %s…" % reply[:36])
    if kind == "len":
        return (no_fail(reply) and len(reply) >= spec[1], "%d 字" % len(reply))
    if kind == "re":
        return (bool(re.search(spec[1], reply)) and no_fail(reply), "正则 %s" % spec[1])
    if kind == "chat":
        return (chat_ok(reply), "闲聊 %d 字" % len(reply))
    if kind == "file":
        c = _read(os.path.join(SANDBOX, spec[1]))
        miss = [w for w in spec[2] if w not in c]
        return (not miss, "%s %d 字%s" % (spec[1], len(c), " 缺%s" % miss if miss else ""))
    if kind == "mem":
        c = _read(MEM_FILE)
        miss = [w for w in spec[1] if w not in c]
        return (not miss, "记忆%s" % ("含" if not miss else "缺%s" % miss))
    if kind == "rem":
        c = _read(REM_FILE)
        miss = [w for w in spec[1] if w not in c]
        return (not miss, "提醒%s" % ("含" if not miss else "缺%s" % miss))
    if kind == "proc":
        ok = _proc_running(*spec[1])
        return (ok, "进程%s" % ("在" if ok else "不在"))
    if kind == "gui":
        return (no_fail(reply), "GUI %s…" % reply[:36])
    return (False, "未知断言 %s" % kind)


def _probe_net():
    try:
        socket.create_connection(("www.baidu.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _probe_llm():
    try:
        cfg = brain._load_llm_config()
        host = cfg["base_url"].split("//", 1)[-1].split("/")[0]
        socket.create_connection((host, 443), timeout=4).close()
        return True
    except Exception:
        return False


NET_OK = _probe_net()
LLM_OK = _probe_llm()


def drive_gui(task):
    brain._pending_shell = None
    r1 = brain.think(task, [])
    if "确认" in r1:
        r2 = brain.think("确认", [])
        brain._pending_shell = None
        return r2
    return r1


# == 300 条真实任务（10 个类目 × 30 条） ==
# A 信息查询 1-30 | B 写作助理 31-60 | C 文件柜 61-90 | D 记忆提醒 91-120
# E 系统控制 121-150 | F GUI 操作 151-180 | G 计算转换 181-210
# H 日程时间 211-240 | I 闲聊陪伴 241-270 | J 学习解释 271-300

TASKS = [
    # ---------- A. 信息查询（1-30）----------
    (1, "现在几点了", ("re", r"\d"), None, "A"),
    (2, "今天几号", ("re", r"\d"), None, "A"),
    (3, "今天星期几", ("c", "星期"), None, "A"),
    (4, "今年是哪一年", ("c", "2026"), None, "A"),
    (5, "查一下今天上海的天气", ("c", "天气"), "net", "A"),
    (6, "查一下今天广州的天气", ("c", "天气"), "net", "A"),
    (7, "明天北京天气怎么样", ("nf",), "net", "A"),
    (8, "搜一下今天的人工智能新闻", ("len", 60), "net", "A"),
    (9, "搜一下最近的新能源汽车新闻，总结两条", ("len", 50), "net", "A"),
    (10, "搜一下今天的体育新闻", ("len", 50), "net", "A"),
    (11, "查一下美元对人民币汇率", ("nf",), "net", "A"),
    (12, "搜一下特斯拉最新的股价", ("nf",), "net", "A"),
    (13, "查一下黄金现在多少钱一克", ("nf",), "net", "A"),
    (14, "搜一下苹果公司的最新消息", ("len", 40), "net", "A"),
    (15, "查一下本周有什么新电影上映", ("nf",), "net", "A"),
    (16, "搜一下如何做西红柿炒鸡蛋", ("nf",), "net", "A"),
    (17, "查一下北京到上海的高铁要多久", ("nf",), "net", "A"),
    (18, "搜一下最近的科技发布会", ("nf",), "net", "A"),
    (19, "查一下感冒吃什么药比较好", ("nf",), "net", "A"),
    (20, "搜一下怎么快速入睡", ("nf",), "net", "A"),
    (21, "查一下今天的财经新闻头条", ("len", 40), "net", "A"),
    (22, "搜一下最近的航天新闻", ("nf",), "net", "A"),
    (23, "查一下世界杯赛程", ("nf",), "net", "A"),
    (24, "搜一下怎么挑选笔记本电脑", ("nf",), "net", "A"),
    (25, "查一下深圳明天的天气", ("nf",), "net", "A"),
    (26, "搜一下最新的手机处理器对比", ("nf",), "net", "A"),
    (27, "查一下如何申请护照", ("nf",), "net", "A"),
    (28, "搜一下附近有什么好吃的火锅", ("nf",), "net", "A"),
    (29, "查一下比特币现在的价格", ("nf",), "net", "A"),
    (30, "搜一下今天的热搜榜", ("nf",), "net", "A"),
    # ---------- B. 写作助理（31-60）----------
    (31, "帮我写一封请假邮件，理由是感冒，一天", ("len", 60), "llm", "B"),
    (32, "帮我写一段生日快乐祝福给朋友", ("len", 30), "llm", "B"),
    (33, "帮我把这句话翻译成英语：今天天气很好", ("re", r"[A-Za-z]"), "llm", "B"),
    (34, "把「人工智能正在改变世界」翻译成英文", ("re", r"[A-Za-z]"), "llm", "B"),
    (35, "帮我写一个周工作总结的提纲", ("len", 50), "llm", "B"),
    (36, "帮我写一句朋友圈文案，关于周末爬山", ("len", 15), "llm", "B"),
    (37, "帮我润色这句话：这个方案还挺好的", ("len", 10), "llm", "B"),
    (38, "帮我写一个产品发布会的开场白，三句话", ("len", 40), "llm", "B"),
    (39, "帮我写一条道歉短信，我迟到了半小时", ("len", 30), "llm", "B"),
    (40, "帮我给团队写一段加油打气的话", ("len", 30), "llm", "B"),
    (41, "帮我把「机器学习」用三句话解释清楚", ("len", 50), "llm", "B"),
    (42, "帮我写一份租房合同的注意事项清单", ("len", 60), "llm", "B"),
    (43, "帮我写一段自我介绍，用于求职面试", ("len", 60), "llm", "B"),
    (44, "帮我写一首关于秋天的小诗", ("len", 20), "llm", "B"),
    (45, "帮我写一个项目进度汇报的模板", ("len", 60), "llm", "B"),
    (46, "帮我把这段话缩写一半：人工智能是研究、开发用于模拟、延伸和扩展人的智能的理论、方法、技术及应用系统的一门新的技术科学", ("len", 15), "llm", "B"),
    (47, "帮我写一封感谢信给帮过我的同事", ("len", 50), "llm", "B"),
    (48, "帮我列一个读书计划，主题是人工智能", ("len", 50), "llm", "B"),
    (49, "帮我写一条招聘启事，招一个前端工程师", ("len", 50), "llm", "B"),
    (50, "帮我把「尽快完成」说得礼貌一点", ("len", 10), "llm", "B"),
    (51, "帮我写一段会议邀请，周五下午三点讨论新产品", ("len", 40), "llm", "B"),
    (52, "帮我把这句话翻译成日语：谢谢你的帮助", ("nf",), "llm", "B"),
    (53, "帮我写一个健身计划，一周三次", ("len", 60), "llm", "B"),
    (54, "帮我写一份面试问题的清单，招产品经理", ("len", 60), "llm", "B"),
    (55, "帮我写一句励志的话贴在桌上", ("len", 10), "llm", "B"),
    (56, "帮我写一段年终总结的开头", ("len", 40), "llm", "B"),
    (57, "帮我把「我觉得这个不行」说得委婉一点", ("len", 10), "llm", "B"),
    (58, "帮我写一份出差申请的理由", ("len", 30), "llm", "B"),
    (59, "帮我写一段新店开张的宣传语", ("len", 20), "llm", "B"),
    (60, "帮我写一封辞职信的提纲", ("len", 40), "llm", "B"),
    # ---------- C. 文件柜（61-90）----------
    (61, "把 周一早上九点开晨会 写到 日程.txt", ("file", "日程.txt", ["周一", "晨会"]), None, "C"),
    (62, "读一下 日程.txt", ("c", "晨会"), None, "C"),
    (63, "把 牛奶、鸡蛋、面包 写到 采购.txt", ("file", "采购.txt", ["牛奶", "鸡蛋"]), None, "C"),
    (64, "读一下 采购.txt", ("c", "牛奶"), None, "C"),
    (65, "把 今天完成了路线图 v2 写到 工作日志.txt", ("file", "工作日志.txt", ["路线图"]), None, "C"),
    (66, "读一下 工作日志.txt", ("c", "路线图"), None, "C"),
    (67, "列出我文件柜里的文件", ("c", "日程.txt"), None, "C"),
    (68, "把 下个月记得续费域名 写到 待办.txt", ("file", "待办.txt", ["域名"]), None, "C"),
    (69, "读一下 待办.txt", ("c", "域名"), None, "C"),
    (70, "把 密码不要写在纸上 写到 原则.txt", ("file", "原则.txt", ["密码"]), None, "C"),
    (71, "读一下 原则.txt", ("c", "密码"), None, "C"),
    (72, "把 周三下午三点产品评审 写到 会议.txt", ("file", "会议.txt", ["评审"]), None, "C"),
    (73, "读一下 会议.txt", ("c", "评审"), None, "C"),
    (74, "把 妈妈的生日是五月初八 写到 家人.txt", ("file", "家人.txt", ["五月"]), None, "C"),
    (75, "读一下 家人.txt", ("c", "五月"), None, "C"),
    (76, "搜一下今天的人工智能新闻，总结两条写到 早报.txt", ("file", "早报.txt", ["AI"]), "net", "C"),
    (77, "读一下 早报.txt", ("len", 20), "net", "C"),
    (78, "把 灵感：做一个语音优先的助手 写到 灵感.txt", ("file", "灵感.txt", ["语音"]), None, "C"),
    (79, "读一下 灵感.txt", ("c", "语音"), None, "C"),
    (80, "把 体脂率目标 20% 写到 健身.txt", ("file", "健身.txt", ["20"]), None, "C"),
    (81, "读一下 健身.txt", ("c", "20"), None, "C"),
    (82, "把 书《埃隆·马斯克传》 写到 书单.txt", ("file", "书单.txt", ["马斯克"]), None, "C"),
    (83, "读一下 书单.txt", ("c", "马斯克"), None, "C"),
    (84, "把 记得给车加油 写到 杂事.txt", ("file", "杂事.txt", ["加油"]), None, "C"),
    (85, "读一下 杂事.txt", ("c", "加油"), None, "C"),
    (86, "把 报价预算控制在五万以内 写到 项目.txt", ("file", "项目.txt", ["五万"]), None, "C"),
    (87, "读一下 项目.txt", ("c", "五万"), None, "C"),
    (88, "列出文件柜里所有文件", ("nf",), None, "C"),
    (89, "把 下周出差带身份证 写到 出差.txt", ("file", "出差.txt", ["身份证"]), None, "C"),
    (90, "读一下 出差.txt", ("c", "身份证"), None, "C"),
    # ---------- D. 记忆与提醒（91-120）----------
    (91, "记住我喜欢喝美式咖啡", ("mem", ["美式"]), None, "D"),
    (92, "我喜欢喝什么咖啡", ("c", "美式"), "llm", "D"),
    (93, "记住我不吃辣", ("mem", ["辣"]), None, "D"),
    (94, "我有什么忌口", ("c", "辣"), "llm", "D"),
    (95, "记住我的生日是六月一日", ("mem", ["六月"]), None, "D"),
    (96, "我的生日是什么时候", ("c", "六月"), "llm", "D"),
    (97, "记住我习惯早上七点起床", ("mem", ["七点"]), None, "D"),
    (98, "我一般几点起床", ("c", "七点"), "llm", "D"),
    (99, "记住我的项目叫 Nolan", ("mem", ["Nolan"]), None, "D"),
    (100, "我的项目叫什么名字", ("c", "Nolan"), "llm", "D"),
    (101, "10分钟后提醒我喝水", ("rem", ["喝水"]), None, "D"),
    (102, "我有什么提醒", ("c", "喝水"), None, "D"),
    (103, "30分钟后提醒我站起来活动", ("rem", ["活动"]), None, "D"),
    (104, "我现在有哪些待办提醒", ("nf",), None, "D"),
    (105, "明天早上八点提醒我开会", ("rem", ["开会"]), None, "D"),
    (106, "1小时后提醒我吃药", ("rem", ["吃药"]), None, "D"),
    (107, "记住我喜欢深色的界面", ("mem", ["深色"]), None, "D"),
    (108, "我喜欢什么风格的界面", ("c", "深色"), "llm", "D"),
    (109, "记住我的猫叫年糕", ("mem", ["年糕"]), None, "D"),
    (110, "我的猫叫什么名字", ("c", "年糕"), "llm", "D"),
    (111, "2小时后提醒我收快递", ("rem", ["快递"]), None, "D"),
    (112, "45分钟后提醒我关火", ("rem", ["关火"]), None, "D"),
    (113, "记住我不喜欢用感叹号", ("mem", ["感叹号"]), None, "D"),
    (114, "你记得我哪些习惯", ("nf",), "llm", "D"),
    (115, "15分钟后提醒我回电话", ("rem", ["电话"]), None, "D"),
    (116, "记住我每周三去打篮球", ("mem", ["篮球"]), None, "D"),
    (117, "我周三一般干什么", ("c", "篮球"), "llm", "D"),
    (118, "20分钟后提醒我浇花", ("rem", ["浇花"]), None, "D"),
    (119, "记住我的幸运数字是 7", ("mem", ["7"]), None, "D"),
    (120, "我的幸运数字是几", ("c", "7"), "llm", "D"),
    # ---------- E. 系统控制（121-150）----------
    (121, "打开记事本", ("proc", ["notepad.exe"]), None, "E"),
    (122, "打开计算器", ("proc", ["calc.exe", "CalculatorApp.exe"]), None, "E"),
    (123, "打开画图", ("proc", ["mspaint.exe"]), None, "E"),
    (124, "打开网页 www.baidu.com", ("nf",), "net", "E"),
    (125, "打开网页 www.zhihu.com", ("nf",), "net", "E"),
    (126, "打开哔哩哔哩的网站", ("nf",), "net", "E"),
    (127, "暂停音乐播放", ("nf",), None, "E"),
    (128, "继续播放音乐", ("nf",), None, "E"),
    (129, "下一首", ("nf",), None, "E"),
    (130, "上一首", ("nf",), None, "E"),
    (131, "把音量调大一点", ("nf",), None, "E"),
    (132, "把音量调小一点", ("nf",), None, "E"),
    (133, "静音", ("nf",), None, "E"),
    (134, "取消静音", ("nf",), None, "E"),
    (135, "运行 echo p0-ok", ("c", "p0-ok"), None, "E"),
    (136, "运行 whoami", ("nf",), None, "E"),
    (137, "运行 python -c \"print(6*7)\"", ("c", "42"), None, "E"),
    (138, "运行 dir", ("nf",), None, "E"),
    (139, "打开命令提示符", ("proc", ["cmd.exe", "WindowsTerminal.exe", "conhost.exe"]), None, "E"),
    (140, "打开任务管理器", ("proc", ["Taskmgr.exe"]), None, "E"),
    (141, "打开浏览器", ("nf",), None, "E"),
    (142, "打开微信", ("nf",), None, "E"),
    (143, "打开设置", ("nf",), None, "E"),
    (144, "运行 hostname", ("nf",), None, "E"),
    (145, "运行 ipconfig", ("c", "IP"), None, "E"),
    (146, "打开网易云音乐", ("proc", ["cloudmusic.exe"]), None, "E"),
    (147, "运行 time /t", ("nf",), None, "E"),
    (148, "打开微软商店", ("nf",), None, "E"),
    (149, "运行 ver", ("nf",), None, "E"),
    (150, "打开写字板", ("nf",), None, "E"),
    # ---------- F. GUI 操作（151-180，gui 组，慢）----------
    (151, "在记事本中输入：P0 第一条", ("gui",), "gui", "F"),
    (152, "在记事本中输入：今天也要加油", ("gui",), "gui", "F"),
    (153, "在记事本中输入：贾维斯养成中", ("gui",), "gui", "F"),
    (154, "在记事本中输入：第一性原理", ("gui",), "gui", "F"),
    (155, "在记事本中输入：少说多做", ("gui",), "gui", "F"),
    (156, "在记事本中输入：知行合一", ("gui",), "gui", "F"),
    (157, "在记事本中输入：保持饥饿", ("gui",), "gui", "F"),
    (158, "在记事本中输入：工程即魔法", ("gui",), "gui", "F"),
    (159, "在记事本中输入：可靠的同伴", ("gui",), "gui", "F"),
    (160, "在记事本中输入：先做最重要的事", ("gui",), "gui", "F"),
    (161, "在记事本中输入：hello nolan", ("gui",), "gui", "F"),
    (162, "在记事本中输入：trust is earned", ("gui",), "gui", "F"),
    (163, "在记事本中输入：step by step", ("gui",), "gui", "F"),
    (164, "在记事本中输入：make it reliable", ("gui",), "gui", "F"),
    (165, "在记事本中输入：stay focused", ("gui",), "gui", "F"),
    (166, "在记事本中输入：keep going", ("gui",), "gui", "F"),
    (167, "在记事本中输入：nolan 2026", ("gui",), "gui", "F"),
    (168, "在记事本中输入：jarvis mode", ("gui",), "gui", "F"),
    (169, "在记事本中输入：day one", ("gui",), "gui", "F"),
    (170, "在记事本中输入：build to last", ("gui",), "gui", "F"),
    (171, "打开记事本，在里面输入：复合任务一", ("gui",), "gui", "F"),
    (172, "打开记事本，在里面输入：复合任务二", ("gui",), "gui", "F"),
    (173, "打开记事本，输入：复合任务三", ("gui",), "gui", "F"),
    (174, "打开记事本，输入：复合任务四", ("gui",), "gui", "F"),
    (175, "打开记事本，输入：复合任务五", ("gui",), "gui", "F"),
    (176, "打开计算器并计算 25 加 17", ("gui",), "gui", "F"),
    (177, "打开计算器并计算 100 减 36", ("gui",), "gui", "F"),
    (178, "打开计算器并计算 8 乘以 9", ("gui",), "gui", "F"),
    (179, "打开计算器并计算 144 除以 12", ("gui",), "gui", "F"),
    (180, "打开计算器并计算 7 加 8 再减 3", ("gui",), "gui", "F"),
    # ---------- G. 计算与转换（181-210）----------
    (181, "128 加 256 等于多少", ("c", "384"), None, "G"),
    (182, "1024 减 512 等于多少", ("c", "512"), None, "G"),
    (183, "15 乘以 16 等于多少", ("c", "240"), None, "G"),
    (184, "1000 除以 8 等于多少", ("c", "125"), None, "G"),
    (185, "3 的 10 次方是多少", ("c", "59049"), None, "G"),
    (186, "88 加 99 减 50 等于多少", ("c", "137"), None, "G"),
    (187, "1 小时等于多少秒", ("c", "3600"), None, "G"),
    (188, "1 公里等于多少米", ("c", "1000"), None, "G"),
    (189, "100 美元大概多少人民币", ("re", r"\d"), "llm", "G"),
    (190, "一磅等于多少克", ("c", "453"), "llm", "G"),
    (191, "华氏 98.6 度等于摄氏多少度", ("c", "37"), "llm", "G"),
    (192, "1GB 等于多少 MB", ("c", "1024"), None, "G"),
    (193, "一个星期有多少分钟", ("c", "10080"), None, "G"),
    (194, "250 的 20% 是多少", ("c", "50"), None, "G"),
    (195, "90 的三分之一是多少", ("c", "30"), None, "G"),
    (196, "一亩地等于多少平方米", ("c", "666"), "llm", "G"),
    (197, "光速大约是每秒多少公里", ("c", "30万"), "llm", "G"),
    (198, "一年大约有多少小时", ("c", "8760"), None, "G"),
    (199, "1 英里等于多少公里", ("c", "1.6"), "llm", "G"),
    (200, "根号 169 等于多少", ("c", "13"), None, "G"),
    (201, "12 的平方是多少", ("c", "144"), None, "G"),
    (202, "7 乘 8 加 4 等于多少", ("c", "60"), None, "G"),
    (203, "100 以内最大的质数是多少", ("c", "97"), "llm", "G"),
    (204, "一公斤等于多少斤", ("c", "2"), "llm", "G"),
    (205, "360 除以 4 再除以 3 等于多少", ("c", "30"), None, "G"),
    (206, "1 升水大约重多少公斤", ("c", "1"), "llm", "G"),
    (207, "99 加 1 等于多少", ("c", "100"), None, "G"),
    (208, "2 的 16 次方是多少", ("c", "65536"), None, "G"),
    (209, "一小时十五分钟等于多少分钟", ("c", "75"), None, "G"),
    (210, "500 的 8% 是多少", ("c", "40"), None, "G"),
    # ---------- H. 日程与时间（211-240）----------
    (211, "今天距离 2027 年元旦还有多少天", ("re", r"\d"), "llm", "H"),
    (212, "现在距离今天结束还有几个小时", ("re", r"\d"), "llm", "H"),
    (213, "100 天后是几月几号", ("re", r"\d"), "llm", "H"),
    (214, "昨天是几月几号", ("re", r"\d"), "llm", "H"),
    (215, "后天是星期几", ("c", "星期"), "llm", "H"),
    (216, "这个星期五是几号", ("re", r"\d"), "llm", "H"),
    (217, "现在是上午还是下午", ("nf",), None, "H"),
    (218, "现在是第几季度", ("nf",), "llm", "H"),
    (219, "这个月有多少天", ("re", r"\d"), "llm", "H"),
    (220, "下个月一号是星期几", ("c", "星期"), "llm", "H"),
    (221, "30 分钟以后是几点", ("re", r"\d"), "llm", "H"),
    (222, "现在纽约大概是几点", ("nf",), "llm", "H"),
    (223, "现在伦敦大概是几点", ("nf",), "llm", "H"),
    (224, "今年国庆节是星期几", ("c", "星期"), "llm", "H"),
    (225, "距离中秋节还有多少天", ("re", r"\d"), "llm", "H"),
    (226, "今年是闰年吗", ("nf",), "llm", "H"),
    (227, "2028 年是闰年吗", ("nf",), "llm", "H"),
    (228, "下周一 是几月几号", ("re", r"\d"), "llm", "H"),
    (229, "两周后是几月几号", ("re", r"\d"), "llm", "H"),
    (230, "现在东京大概是几点", ("nf",), "llm", "H"),
    (231, "45 天前是几月几号", ("re", r"\d"), "llm", "H"),
    (232, "这个月还剩几天", ("re", r"[0-9零一二三四五六七八九十百千万两]"), "llm", "H"),
    (233, "明天这个时候是几点", ("re", r"\d"), "llm", "H"),
    (234, "现在是今年的第几周", ("re", r"\d"), "llm", "H"),
    (235, "春节通常在几月份", ("nf",), "llm", "H"),
    (236, "从今天到年底还有多少天", ("re", r"\d"), "llm", "H"),
    (237, "上周三是几月几号", ("re", r"\d"), "llm", "H"),
    (238, "90 分钟后是几点", ("re", r"\d"), "llm", "H"),
    (239, "现在洛杉矶大概是几点", ("nf",), "llm", "H"),
    (240, "下个月有几天", ("re", r"[0-9零一二三四五六七八九十百千万两]"), "llm", "H"),
    # ---------- I. 闲聊与陪伴（241-270）----------
    (241, "给我讲个笑话", ("chat",), "llm", "I"),
    (242, "再讲一个笑话", ("chat",), "llm", "I"),
    (243, "我今天有点累", ("chat",), "llm", "I"),
    (244, "夸夸我", ("chat",), "llm", "I"),
    (245, "给我一句今天的鼓励", ("chat",), "llm", "I"),
    (246, "你觉得人工智能会有意识吗", ("chat",), "llm", "I"),
    (247, "陪我聊聊天", ("chat",), "llm", "I"),
    (248, "我今天完成了一个大目标", ("chat",), "llm", "I"),
    (249, "给我讲个冷知识", ("chat",), "llm", "I"),
    (250, "你会做梦吗", ("chat",), "llm", "I"),
    (251, "给我推荐一部电影", ("chat",), "llm", "I"),
    (252, "给我推荐一本书", ("chat",), "llm", "I"),
    (253, "今天心情不好", ("chat",), "llm", "I"),
    (254, "给我讲个睡前故事的开头", ("chat",), "llm", "I"),
    (255, "你最喜欢哪个科学家", ("chat",), "llm", "I"),
    (256, "给我出个脑筋急转弯", ("chat",), "llm", "I"),
    (257, "跟我玩个词语接龙，我先：天空", ("chat",), "llm", "I"),
    (258, "给我一句关于坚持的格言", ("chat",), "llm", "I"),
    (259, "如果你是人类，你最想做什么", ("chat",), "llm", "I"),
    (260, "给我唱两句歌", ("chat",), "llm", "I"),
    (261, "我有点焦虑，怎么办", ("chat",), "llm", "I"),
    (262, "给我讲个程序员才懂的笑话", ("chat",), "llm", "I"),
    (263, "你觉得我能达到目标吗", ("chat",), "llm", "I"),
    (264, "给我推荐一首适合工作时听的歌", ("chat",), "llm", "I"),
    (265, "明天又是新的一天，对我说点什么", ("chat",), "llm", "I"),
    (266, "你觉得自己聪明吗", ("chat",), "llm", "I"),
    (267, "给我讲讲钢铁侠的贾维斯", ("chat",), "llm", "I"),
    (268, "跟我说晚安", ("chat",), "llm", "I"),
    (269, "给我打打气，我明天有重要的事", ("chat",), "llm", "I"),
    (270, "谢谢你，Nolan", ("chat",), "llm", "I"),
    # ---------- J. 学习与解释（271-300）----------
    (271, "用三句话解释什么是量子计算", ("len", 50), "llm", "J"),
    (272, "什么是第一性原理", ("len", 40), "llm", "J"),
    (273, "解释一下什么是区块链", ("len", 40), "llm", "J"),
    (274, "TCP 和 UDP 有什么区别", ("len", 40), "llm", "J"),
    (275, "什么是通货膨胀", ("len", 40), "llm", "J"),
    (276, "解释一下什么是复利", ("len", 40), "llm", "J"),
    (277, "什么是神经网络", ("len", 40), "llm", "J"),
    (278, "HTTP 和 HTTPS 的区别是什么", ("len", 40), "llm", "J"),
    (279, "什么是边际成本", ("len", 40), "llm", "J"),
    (280, "解释一下什么是相对论", ("len", 40), "llm", "J"),
    (281, "什么是基因编辑", ("len", 40), "llm", "J"),
    (282, "什么是杠杆原理", ("len", 40), "llm", "J"),
    (283, "解释一下什么是摩尔定律", ("len", 40), "llm", "J"),
    (284, "什么是供给侧结构性改革", ("len", 40), "llm", "J"),
    (285, "什么是光年", ("len", 40), "llm", "J"),
    (286, "解释一下什么是熵", ("len", 40), "llm", "J"),
    (287, "什么是机器学习中的过拟合", ("len", 40), "llm", "J"),
    (288, "什么是黑天鹅事件", ("len", 40), "llm", "J"),
    (289, "解释一下什么是市盈率", ("len", 40), "llm", "J"),
    (290, "什么是元宇宙", ("len", 40), "llm", "J"),
    (291, "什么是碳中和", ("len", 40), "llm", "J"),
    (292, "解释一下什么是蝴蝶效应", ("len", 40), "llm", "J"),
    (293, "什么是图灵测试", ("len", 40), "llm", "J"),
    (294, "什么是幸存者偏差", ("len", 40), "llm", "J"),
    (295, "解释一下什么是沉没成本", ("len", 40), "llm", "J"),
    (296, "什么是量子纠缠", ("len", 40), "llm", "J"),
    (297, "什么是奥卡姆剃刀", ("len", 40), "llm", "J"),
    (298, "解释一下什么是锚定效应", ("len", 40), "llm", "J"),
    (299, "什么是马太效应", ("len", 40), "llm", "J"),
    (300, "什么是第一宇宙速度", ("len", 40), "llm", "J"),
]


def run_one(row):
    num, task, spec, dep, cat = row
    if dep == "net" and not NET_OK:
        return ("SKIP", 0.0, "无网络", "")
    if dep == "llm" and not LLM_OK:
        return ("SKIP", 0.0, "LLM 不可达", "")
    t0 = datetime.now()
    reply = ""
    try:
        if dep == "gui":
            # GUI 题前置：鼠标归中（防安全中止误触发）；关掉 E 类等残留的系统
            # 窗口（设置/任务管理器/计算器）——截屏分析见到的是前台窗口，残留
            # 窗口会让视觉判断「找不到目标应用」；记事本类重启实例防残字
            _mouse_home()
            _kill("SystemSettings.exe", "Taskmgr.exe", "CalculatorApp.exe")
            if "记事本" in task:
                _kill("notepad.exe")
                hands.execute("open_app", {"app": "记事本"})
                hands._wait_for_window("记事本", timeout=8)
                hands._bring_window_front("记事本")
            reply = drive_gui(task)
        else:
            reply = brain.think(task, [])
        ok, detail = check(spec, reply)
        dt = (datetime.now() - t0).total_seconds()
        return ("PASS" if ok else "FAIL", dt, detail,
                "" if ok else reply[:60])
    except Exception:
        dt = (datetime.now() - t0).total_seconds()
        return ("FAIL", dt,
                "异常：%s" % traceback.format_exc().splitlines()[-1], "")


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def backup_state(tasks=None):
    names = set()
    for t in (tasks or TASKS):
        spec = t[2]
        if spec[0] == "file":
            names.add(spec[1])
        elif spec[0] == "all":  # 复合断言：收集子断言里的 file 文件名
            for sub in spec[1]:
                if sub[0] == "file":
                    names.add(sub[1])
    return {
        "mem": _read_bytes(MEM_FILE),
        "rem": _read_bytes(REM_FILE),
        "sandbox": {n: _read_bytes(os.path.join(SANDBOX, n)) for n in names},
    }


def restore_state(st):
    for path, key in ((MEM_FILE, "mem"), (REM_FILE, "rem")):
        if st[key] is None:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            with open(path, "wb") as f:
                f.write(st[key])
    for n, data in st["sandbox"].items():
        p = os.path.join(SANDBOX, n)
        if data is None:
            try:
                os.remove(p)
            except OSError:
                pass
        else:
            with open(p, "wb") as f:
                f.write(data)


def show_stats(results_file=None, label="P0"):
    """读取历史结果，输出每批成功率趋势。"""
    path = results_file or RESULTS_FILE
    if not os.path.isfile(path):
        print("还没有历史结果，先跑一批。")
        return
    batches = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            b = batches.setdefault(r["batch"], {"PASS": 0, "FAIL": 0, "SKIP": 0})
            b[r["status"]] = b.get(r["status"], 0) + 1
    print("== %s 成功率趋势 ==" % label)
    for batch in sorted(batches):
        b = batches[batch]
        ran = b["PASS"] + b["FAIL"]
        rate = 100.0 * b["PASS"] / ran if ran else 0
        print("%s  跑 %3d 题  成功率 %5.1f%%  (PASS %d / FAIL %d / SKIP %d)"
              % (batch, ran, rate, b["PASS"], b["FAIL"], b.get("SKIP", 0)))


def run_tasks(tasks, results_file, title):
    """通用跑批主循环：备份 -> 逐题执行落盘 -> 恢复清理 -> 汇总。P0/P1 复用。"""
    batch = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(title)
    print("环境：NET=%s LLM=%s  批次：%s\n"
          % ("OK" if NET_OK else "DOWN", "OK" if LLM_OK else "DOWN", batch))
    st = backup_state(tasks)
    results = []
    try:
        for row in tasks:
            num = row[0]
            status, dt, detail, fail_note = run_one(row)
            results.append((num, row[4], status))
            line = "第%03d题 [%s] %-4s %6.1fs  %s" % (num, row[4], status, dt, detail)
            if fail_note:
                line += "  -> " + fail_note
            print(line, flush=True)
            with open(results_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "batch": batch, "num": num, "cat": row[4],
                    "status": status, "sec": round(dt, 1),
                    "detail": detail[:80]}, ensure_ascii=False) + "\n")
    finally:
        restore_state(st)
        _kill("notepad.exe", "CalculatorApp.exe", "mspaint.exe", "Taskmgr.exe",
              "SystemSettings.exe")
        brain._pending_shell = None
    ran = [s for _, _, s in results if s != "SKIP"]
    passed = sum(1 for _, _, s in results if s == "PASS")
    print("\n== 本批汇总 ==")
    print("跑 %d 题 / PASS %d / FAIL %d / SKIP %d / 成功率 %.1f%%" % (
        len(ran), passed, len(ran) - passed,
        sum(1 for _, _, s in results if s == "SKIP"),
        100.0 * passed / len(ran) if ran else 0))
    cats = {}
    for num, cat, status in results:
        if status == "SKIP":
            continue
        c = cats.setdefault(cat, [0, 0])
        c[0] += status == "PASS"
        c[1] += 1
    for cat in sorted(cats):
        p, n = cats[cat]
        print("  类目 %s：%d/%d（%.0f%%）" % (cat, p, n, 100.0 * p / n))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        show_stats()
        return
    lo, hi = 1, len(TASKS)
    if len(sys.argv) > 1 and "-" in sys.argv[1]:
        lo, hi = map(int, sys.argv[1].split("-", 1))
    rows = [t for t in TASKS if lo <= t[0] <= hi]
    run_tasks(rows, RESULTS_FILE,
              "== P0 真实任务水库 · %d-%d ==" % (lo, hi))
    print("\n查看历史趋势：python jarvis/benchmark_p0.py stats")


if __name__ == "__main__":
    main()
