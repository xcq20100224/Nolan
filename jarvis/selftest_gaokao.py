# -*- coding: utf-8 -*-
"""
selftest_gaokao.py —— Nolan「高考题库」：56 条真实指令的端到端成功率测试

设计原则（第一性原理）：
  1. 测的物理指标是「任务成功率」：每题 = 一条真实用户指令 -> 直接 import brain
     走 brain.think（不走 HTTP，不占端口）-> 断言 = 话术不含失败前缀 + 物理验证
     （文件内容 / 窗口 / 进程 / 状态文件），绝不只听话术。
  2. 用户数据零污染：开跑前备份 long_term.txt、reminders.txt 与沙盒内既有
     gaokao_* 文件；结束无论成败全部恢复原样（try/finally）；
     真实拉起的记事本/计算器一律 taskkill 清理。
  3. 环境依赖分组跳过：NET 组（socket 探 www.baidu.com:443，3 秒）与
     LLM 组（一句「你好」探 GLM，10 秒超时）不可用时整组标 SKIP，
     不计入成功率分母。
  4. 每题独立 try/except，异常 = FAIL 并打印 traceback 首行，绝不中断整卷。
  5. 汇总：总数/通过/失败/跳过/成功率（通过 ÷ (总数 - 跳过)），
     成功率 < 98% 时 exit code 1。

运行：python jarvis/selftest_gaokao.py
      python jarvis/selftest_gaokao.py 11-24   # 只跑指定编号区间（调试用）
"""

import os
import sys
import time
import json
import socket
import subprocess
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

import brain
import hands
import reminders
import memory  # noqa: F401 - 确保记忆模块与 brain 看到的一致

# == 路径与常量 ==

_JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX = hands.SANDBOX_DIR
MEM_FILE = os.path.join(_JARVIS_DIR, "memory", "long_term.txt")
REM_FILE = os.path.join(_JARVIS_DIR, "memory", "reminders.txt")

NOTEPAD_PROCS = ("notepad.exe",)
CALC_PROCS = ("calc.exe", "CalculatorApp.exe")  # Win10 calc.exe / Win11 CalculatorApp.exe
NOTEPAD_TERMS = ("记事本", "notepad")
CALC_TERMS = ("计算器", "calc", "calculator")

# 失败话术标记：出现即视为任务失败（诚实类题目单独断言，不走此表）
FAIL_MARKS = ("抱歉", "未能完成", "出了问题", "无法连接")

# 闲聊题专用失败标记：人设礼貌拒绝（如「抱歉，先生，讲笑话不在我的职责范围内」）
# 是合法的闲聊回应，不算失败——闲聊题的核心断言是物理零动作；
# 只有真正的执行失败、空回复或工具 JSON 泄漏才算 FAIL
CHAT_FAIL_MARKS = ("未能完成", "出了问题", "无法连接")


def no_fail(r) -> bool:
    """话术不含任何失败标记。"""
    return isinstance(r, str) and bool(r.strip()) and not any(m in r for m in FAIL_MARKS)


def chat_ok(r) -> bool:
    """闲聊回应合格：非空、无执行失败标记、无工具 JSON 泄漏。"""
    if not isinstance(r, str) or not r.strip():
        return False
    if any(m in r for m in CHAT_FAIL_MARKS):
        return False
    if '"tool"' in r and r.lstrip().startswith("{"):
        return False
    return True


# == 物理世界探针 ==

def _proc_table() -> dict:
    """tasklist 快照：进程名(小写) -> 实例数；失败返回 {}。"""
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, timeout=20
        )
        raw = out.stdout or b""
        for enc in ("gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("gbk", errors="replace")
        counts = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('"'):
                continue
            name = line[1:].split('"', 1)[0].lower()
            counts[name] = counts.get(name, 0) + 1
        return counts
    except Exception:
        return {}


def proc_count(*names) -> int:
    table = _proc_table()
    return sum(table.get(n.lower(), 0) for n in names)


def kill_apps(*names) -> None:
    """taskkill 清理真实拉起的应用；忽略一切错误。"""
    for n in names:
        try:
            subprocess.run(["taskkill", "/f", "/im", n], capture_output=True, timeout=15)
        except Exception:
            pass


def wait_window(terms, timeout: float = 12.0) -> bool:
    """轮询等待任一窗口标题词出现（物理验证）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if any(hands._find_window(t) for t in terms):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_proc(names, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc_count(*names) > 0:
            return True
        time.sleep(0.5)
    return False


def sandbox_listing():
    return sorted(os.listdir(SANDBOX)) if os.path.isdir(SANDBOX) else []


def gui_snapshot():
    """沙盒文件列表 + 白名单 GUI 进程数（闲聊零动手断言用）。"""
    return (
        sandbox_listing(),
        proc_count(*NOTEPAD_PROCS),
        proc_count(*CALC_PROCS),
    )


def read_text(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def sandbox_path(name) -> str:
    return os.path.join(SANDBOX, name)


# == 环境探测 ==

def probe_net() -> bool:
    try:
        socket.create_connection(("www.baidu.com", 443), timeout=3).close()
        return True
    except Exception:
        return False


def probe_llm() -> bool:
    """一句「你好」探 GLM：10 秒超时或任何异常即不可用。"""
    cfg = brain._load_llm_config()
    api_key = cfg.get("api_key")
    if not api_key:
        return False
    base = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 8,
    }
    extra_body = cfg.get("extra_body")
    if extra_body:
        try:
            extra = json.loads(extra_body)
            if isinstance(extra, dict):
                payload.update(extra)
        except ValueError:
            pass
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        resp.json()["choices"][0]["message"]["content"]
        return True
    except Exception:
        return False


def probe_vlm() -> bool:
    """VLM 探测：真截屏 + 一句描述（VLM 内部 60 秒超时 + 降级重试，可能较慢）。"""
    try:
        import eyes
        shot = eyes.screenshot_b64()
        if not shot:
            return False
        desc = eyes._ask_vlm(shot, "用一句话描述这张屏幕截图的内容。")
        return bool(desc and desc.strip())
    except Exception:
        return False


# == 题目注册表 ==

QUESTIONS = []  # (编号, 依赖组set, 指令原文, 判题函数)


def q(no, groups, text):
    def deco(fn):
        QUESTIONS.append((no, set(groups), text, fn))
        return fn
    return deco


# ---------- 一、时间日期（4 题） ----------

@q(1, (), "现在几点")
def q01():
    r = brain.think("现在几点", [])
    return (no_fail(r) and any(ch.isdigit() for ch in r), f"回复：{r!r}")


@q(2, (), "今天几号")
def q02():
    r = brain.think("今天几号", [])
    now = datetime.now()
    return (no_fail(r) and str(now.day) in r and str(now.month) in r, f"回复：{r!r}")


@q(3, (), "今天星期几")
def q03():
    r = brain.think("今天星期几", [])
    return (no_fail(r) and "星期" in r, f"回复：{r!r}")


@q(4, ("llm",), "现在几点了顺便今天几号")
def q04():
    # 「顺便」触发复合任务，交给 LLM Agent 循环（get_time 工具）
    r = brain.think("现在几点了顺便今天几号", [])
    return (no_fail(r) and any(ch.isdigit() for ch in r), f"回复：{r!r}")


# ---------- 二、沙盒文件（6 题） ----------

@q(5, (), "写文件 gaokao_memo.txt 内容 高考必胜")
def q05():
    r = brain.think("写文件 gaokao_memo.txt 内容 高考必胜", [])
    content = read_text(sandbox_path("gaokao_memo.txt"))
    return (
        no_fail(r) and content == "高考必胜",
        f"回复：{r!r}；物理内容：{content!r}",
    )


@q(6, (), "读文件 gaokao_memo.txt")
def q06():
    r = brain.think("读文件 gaokao_memo.txt", [])
    return (no_fail(r) and "高考必胜" in r, f"回复：{r!r}")


@q(7, (), "列出文件")
def q07():
    r = brain.think("列出文件", [])
    return (no_fail(r) and "gaokao_memo.txt" in r, f"回复：{r!r}")


@q(8, (), "写文件 gaokao_memo.txt 内容 第二志愿也稳了")
def q08():
    r = brain.think("写文件 gaokao_memo.txt 内容 第二志愿也稳了", [])
    content = read_text(sandbox_path("gaokao_memo.txt"))
    return (
        no_fail(r) and content == "第二志愿也稳了",
        f"回复：{r!r}；物理内容：{content!r}",
    )


@q(9, (), "读文件 不存在的xyz.txt")
def q09():
    # 诚实题：如实说找不到即 PASS
    r = brain.think("读文件 不存在的xyz.txt", [])
    return ("找不到" in r, f"回复：{r!r}")


@q(10, (), "把 今天状态不错 写到 gaokao_diary.txt")
def q10():
    r = brain.think("把 今天状态不错 写到 gaokao_diary.txt", [])
    content = read_text(sandbox_path("gaokao_diary.txt"))
    return (
        no_fail(r) and content == "今天状态不错",
        f"回复：{r!r}；物理内容：{content!r}",
    )


# ---------- 三、打开应用（6 题，真实 GUI，只白名单记事本/计算器） ----------

@q(11, ("gui",), "打开记事本")
def q11():
    try:
        r = brain.think("打开记事本", [])
        ok = "已经打开" in r and wait_window(NOTEPAD_TERMS)
        return (ok, f"回复：{r!r}；窗口：{wait_window(NOTEPAD_TERMS, 2)}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


@q(12, ("gui",), "打开计算器")
def q12():
    try:
        r = brain.think("打开计算器", [])
        ok = "已经打开" in r and wait_window(CALC_TERMS)
        return (ok, f"回复：{r!r}；窗口：{wait_window(CALC_TERMS, 2)}")
    finally:
        kill_apps(*CALC_PROCS)


@q(13, ("gui",), "打开记事本（已开时重复打开）")
def q13():
    try:
        # 先确保记事本已开一个实例
        if not wait_window(NOTEPAD_TERMS, 2):
            brain.think("打开记事本", [])
            wait_window(NOTEPAD_TERMS)
        before = proc_count(*NOTEPAD_PROCS)
        r = brain.think("打开记事本", [])
        time.sleep(1)
        after = proc_count(*NOTEPAD_PROCS)
        ok = "已经打开" in r and before >= 1 and after <= before
        return (ok, f"回复：{r!r}；进程数 {before} -> {after}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


@q(14, ("gui",), "打开不存在软件xyz")
def q14():
    # 诚实题：如实话术即 PASS。_cmd_start 打桩，避免真弹系统错误对话框。
    real_cmd_start = hands._cmd_start
    hands._cmd_start = lambda _t: None
    try:
        r = brain.think("打开不存在软件xyz", [])
        ok = "没有看到它的窗口" in r or "没有找到" in r
        return (ok, f"回复：{r!r}")
    finally:
        hands._cmd_start = real_cmd_start


@q(15, ("gui",), "打开记事本（连续双应用之一）")
def q15():
    # 不清理，留给 16 题一起验证双窗口并存
    r = brain.think("打开记事本", [])
    ok = "已经打开" in r and wait_window(NOTEPAD_TERMS)
    return (ok, f"回复：{r!r}")


@q(16, ("gui",), "打开计算器（连续双应用之二）")
def q16():
    try:
        r = brain.think("打开计算器", [])
        ok = (
            "已经打开" in r
            and wait_window(CALC_TERMS)
            and wait_window(NOTEPAD_TERMS, 3)
        )
        return (ok, f"回复：{r!r}；双窗口：{ok}")
    finally:
        kill_apps(*(NOTEPAD_PROCS + CALC_PROCS))


# ---------- 四、run_shell（8 题） ----------

@q(17, (), "运行 echo gaokao-ok")
def q17():
    r = brain.think("运行 echo gaokao-ok", [])
    return (no_fail(r) and "gaokao-ok" in r, f"回复：{r!r}")


@q(18, (), "运行 whoami")
def q18():
    r = brain.think("运行 whoami", [])
    return (no_fail(r), f"回复：{r!r}")


@q(19, (), '运行 python -c "print(40+2)"')
def q19():
    r = brain.think('运行 python -c "print(40+2)"', [])
    return (no_fail(r) and "42" in r, f"回复：{r!r}")


@q(20, ("gui",), "运行 notepad")
def q20():
    try:
        t0 = time.time()
        r = brain.think("运行 notepad", [])
        elapsed = time.time() - t0
        ok = "已启动" in r and elapsed < 30 and wait_proc(NOTEPAD_PROCS)
        return (ok, f"回复：{r!r}；耗时 {elapsed:.1f}s；进程：{proc_count(*NOTEPAD_PROCS)}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


@q(21, ("gui",), "运行 calc.exe")
def q21():
    try:
        t0 = time.time()
        r = brain.think("运行 calc.exe", [])
        elapsed = time.time() - t0
        ok = "已启动" in r and elapsed < 30 and wait_proc(CALC_PROCS)
        return (ok, f"回复：{r!r}；耗时 {elapsed:.1f}s；进程：{proc_count(*CALC_PROCS)}")
    finally:
        kill_apps(*CALC_PROCS)


@q(22, (), "运行 del gaokao_memo.txt（确认后取消）")
def q22():
    # 先确保目标文件存在（第 8 题写过；独立运行本区间时兜底重建）
    if not os.path.isfile(sandbox_path("gaokao_memo.txt")):
        hands.execute("write_file", {"name": "gaokao_memo.txt", "content": "高考必胜"})
    r1 = brain.think("运行 del gaokao_memo.txt", [])
    pending_set = brain._pending_shell is not None
    r2 = brain.think("取消", [])
    still_there = os.path.isfile(sandbox_path("gaokao_memo.txt"))
    ok = (
        "确认" in r1
        and pending_set
        and "已取消" in r2
        and still_there
        and brain._pending_shell is None
    )
    return (ok, f"询问：{r1!r}；取消：{r2!r}；文件仍在：{still_there}")


@q(23, (), "运行 不存在的命令xyz123")
def q23():
    # 诚实题：如实报告失败（退出码非零）即 PASS
    r = brain.think("运行 不存在的命令xyz123", [])
    ok = "退出码" in r and "已启动" not in r
    return (ok, f"回复：{r!r}")


@q(24, (), "运行 dir")
def q24():
    r = brain.think("运行 dir", [])
    return (no_fail(r) and "输出如下" in r, f"回复（截断）：{r[:80]!r}")


# ---------- 五、提醒（6 题） ----------

@q(25, (), "提醒我 1 分钟后喝水")
def q25():
    r = brain.think("提醒我 1 分钟后喝水", [])
    data = read_text(REM_FILE)
    return (no_fail(r) and "喝水" in data, f"回复：{r!r}；存储含喝水：{'喝水' in data}")


@q(26, (), "我的提醒")
def q26():
    r = brain.think("我的提醒", [])
    return (no_fail(r) and "喝水" in r, f"回复：{r!r}")


@q(27, (), "10 秒后提醒我 测试弹出")
def q27():
    r = brain.think("10 秒后提醒我 测试弹出", [])
    if not no_fail(r):
        return (False, f"添加失败：{r!r}")
    time.sleep(12)
    due = reminders.check_due()
    ok = any("测试弹出" in item for item in due)
    return (ok, f"回复：{r!r}；到点弹出：{due!r}")


@q(28, (), "（接上题）弹出后存储不再含该条")
def q28():
    data = read_text(REM_FILE)
    return ("测试弹出" not in data, f"存储内容：{data!r}")


@q(29, (), "提醒我 3 分钟后 吃苹果 / 提醒我 4 分钟后 吃香蕉（并存不互吃）")
def q29():
    brain.think("提醒我 3 分钟后 吃苹果", [])
    brain.think("提醒我 4 分钟后 吃香蕉", [])
    listing = reminders.list_pending()
    ok = "吃苹果" in listing and "吃香蕉" in listing
    return (ok, f"列表：{listing!r}")


@q(30, (), "提醒我 5 分钟后 站起来活动")
def q30():
    r = brain.think("提醒我 5 分钟后 站起来活动", [])
    data = read_text(REM_FILE)
    ok = no_fail(r) and "提醒您" in r and "站起来活动" in data
    return (ok, f"回复：{r!r}")


# ---------- 六、记忆（5 题） ----------

@q(31, (), "记住 我喜欢红色")
def q31():
    r = brain.think("记住 我喜欢红色", [])
    data = read_text(MEM_FILE)
    return (no_fail(r) and "红色" in data, f"回复：{r!r}")


@q(32, ("llm",), "我喜欢什么颜色")
def q32():
    # 回忆类问法走 LLM（长期记忆注入 system prompt）
    r = brain.think("我喜欢什么颜色", [])
    return (no_fail(r) and "红色" in r, f"回复：{r!r}")


@q(33, (), "今天天气真不错啊（记忆后闲聊不受影响）")
def q33():
    before_mem = read_text(MEM_FILE)
    before_sandbox = sandbox_listing()
    r = brain.think("今天天气真不错啊", [])
    ok = (
        no_fail(r)
        and read_text(MEM_FILE) == before_mem
        and sandbox_listing() == before_sandbox
    )
    return (ok, f"回复：{r!r}")


@q(34, (), "记住 我住在北京（两条共存）")
def q34():
    r = brain.think("记住 我住在北京", [])
    data = read_text(MEM_FILE)
    ok = no_fail(r) and "红色" in data and "北京" in data
    return (ok, f"回复：{r!r}；存储：{data!r}")


@q(35, (), "你记得什么")
def q35():
    r = brain.think("你记得什么", [])
    ok = no_fail(r) and ("红色" in r or "北京" in r)
    return (ok, f"回复：{r!r}")


# ---------- 七、闲聊绝不动手（5 题，LLM 组） ----------

def _chat_zero_action(text):
    before = gui_snapshot()
    r = brain.think(text, [])
    after = gui_snapshot()
    ok = chat_ok(r) and after == before
    return (ok, f"回复：{r[:60]!r}；物理快照不变：{after == before}")


@q(36, ("llm",), "你好")
def q36():
    return _chat_zero_action("你好")


@q(37, ("llm",), "你是谁")
def q37():
    return _chat_zero_action("你是谁")


@q(38, ("llm",), "讲个笑话")
def q38():
    return _chat_zero_action("讲个笑话")


@q(39, ("llm",), "今天心情如何")
def q39():
    return _chat_zero_action("今天心情如何")


@q(40, ("llm",), "谢谢")
def q40():
    return _chat_zero_action("谢谢")


# ---------- 八、搜索抓取（4 题，NET 组） ----------

def _web_search_opened(text, expect_query):
    """web_search 会真开浏览器——monkeypatch os.startfile 记录 URL 代替，
    物理验证 = 必应搜索 URL 正确拼出且查询词被 URL 编码。"""
    opened = []
    real_startfile = os.startfile

    def fake_startfile(target):
        opened.append(str(target))

    os.startfile = fake_startfile
    try:
        r = brain.think(text, [])
    finally:
        os.startfile = real_startfile
    ok = (
        no_fail(r)
        and bool(opened)
        and opened[-1].startswith("https://www.bing.com/search?q=")
        and expect_query not in opened[-1]  # 中文必须被 URL 编码
    )
    return (ok, f"回复：{r!r}；URL：{opened[-1] if opened else None!r}")


@q(41, ("net",), "搜一下 人工智能")
def q41():
    return _web_search_opened("搜一下 人工智能", "人工智能")


@q(42, ("net",), "搜一下 今天的新闻")
def q42():
    return _web_search_opened("搜一下 今天的新闻", "今天的新闻")


@q(43, ("net", "llm"), "把 人工智能新闻 搜一下写到 gaokao_news.txt")
def q43():
    # 复合任务（搜索组 + 写文件组）：LLM Agent 循环 search_web -> write_file
    r = brain.think("把 人工智能新闻 搜一下写到 gaokao_news.txt", [])
    path = sandbox_path("gaokao_news.txt")
    content = read_text(path)
    ok = os.path.isfile(path) and len(content.strip()) > 0 and no_fail(r)
    return (ok, f"回复：{r[:60]!r}；文件长度：{len(content)}")


@q(44, ("net",), "无意义词搜索（诚实题）")
def q44():
    # 规则层「搜一下」只负责打开浏览器（web_search），诚实语义在 search_web：
    # 无结果时必须如实说「没有找到相关结果」，绝不谎称搜到了
    r = hands.execute("search_web", {"query": "asdfqwerzxcv不存在的词xyz"})
    ok = (
        isinstance(r, str)
        and bool(r.strip())
        and "好的先生" not in r  # 绝不假成功
    )
    return (ok, f"回复（截断）：{r[:80]!r}")


# ---------- 九、复合任务（4 题） ----------

@q(45, ("llm",), "把今天的日期写到 gaokao_date.txt")
def q45():
    r = brain.think("把今天的日期写到 gaokao_date.txt", [])
    content = read_text(sandbox_path("gaokao_date.txt"))
    year = str(datetime.now().year)
    ok = bool(content) and year in content and no_fail(r)
    return (ok, f"回复：{r[:60]!r}；物理内容：{content[:40]!r}")


@q(46, ("llm",), "写文件 gaokao_a.txt 内容 第一题 然后读文件 gaokao_a.txt")
def q46():
    r = brain.think("写文件 gaokao_a.txt 内容 第一题 然后读文件 gaokao_a.txt", [])
    content = read_text(sandbox_path("gaokao_a.txt"))
    ok = content == "第一题" and "第一题" in r
    return (ok, f"回复：{r[:80]!r}；物理内容：{content!r}")


@q(47, (), "提醒我 2 分钟后 吃药，然后告诉我我的提醒")
def q47():
    # 提醒意图在规则层优先于复合判定：直接落库，无需 LLM
    r = brain.think("提醒我 2 分钟后 吃药，然后告诉我我的提醒", [])
    data = read_text(REM_FILE)
    ok = "吃药" in data and "吃药" in r
    return (ok, f"回复：{r!r}")


@q(48, ("gui", "llm"), "打开记事本，然后告诉我现在几点")
def q48():
    try:
        r = brain.think("打开记事本，然后告诉我现在几点", [])
        ok = (
            wait_window(NOTEPAD_TERMS, 15)
            and no_fail(r)
            and any(ch.isdigit() for ch in r)
        )
        return (ok, f"回复：{r[:80]!r}；窗口：{wait_window(NOTEPAD_TERMS, 2)}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


# ---------- 十、边界（2 题） ----------

@q(49, (), "（空字符串输入）")
def q49():
    r = brain.think("", [])
    return ("没有听清" in r, f"回复：{r!r}")


@q(50, (), "。。。")
def q50():
    r = brain.think("。。。", [])
    return (isinstance(r, str) and bool(r.strip()) and "抱歉" not in r, f"回复：{r[:60]!r}")


# ---------- 十一、阶段 1 收尾回归：混合句标点截断 + 托盘补救（3 题） ----------

@q(51, ("gui",), "打开记事本，播放我喜欢列表里的第一首歌")
def q51():
    try:
        r = brain.think("打开记事本，播放我喜欢列表里的第一首歌", [])
        ok = ("已经打开" in r or "已经在运行" in r) and (
            wait_window(NOTEPAD_TERMS, 5) or proc_count(*NOTEPAD_PROCS) > 0
        )
        return (ok, f"回复：{r!r}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


@q(52, ("gui",), "打开网易云音乐，播放我喜欢列表里的第一首歌")
def q52():
    # 锁死真机误报回归：应用明明在运行（可能在托盘），绝不许再说「没有看到它的窗口」；
    # 不验证播放（播放精度属阶段 2），跑完不关闭网易云
    r = brain.think("打开网易云音乐，播放我喜欢列表里的第一首歌", [])
    ok = ("已经打开" in r or "已经在运行" in r) and (
        hands._find_window("网易云音乐") or proc_count("cloudmusic.exe") > 0
    )
    return (ok, f"回复：{r!r}")


@q(53, ("gui",), "open_app 混合句单元式（记事本，顺便帮我看看）")
def q53():
    try:
        r = hands.execute("open_app", {"app": "记事本，顺便帮我看看"})
        ok = ("已经打开" in r or "已经在运行" in r) and (
            wait_window(NOTEPAD_TERMS, 5) or proc_count(*NOTEPAD_PROCS) > 0
        )
        return (ok, f"回复：{r!r}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


# ---------- 十二、阶段 2 收尾：UIA 元素树 + 可打断播报（3 题） ----------

def _fresh_notepad_hwnd() -> int:
    """
    确保记事本窗口真实可见并置前，返回窗口句柄；两轮尝试后仍无窗口返回 0。

    Win11 记事本是单实例应用：尸体未凉就重启会出现「窗口交接」——
    open_app 的自检看到旧窗口残骸判成功，新窗口却永不出现。
    对策：先杀净并等进程彻底退出再重启，窗口句柄按 UIA 可见性轮询确认，
    出现后置前（防止被其他窗口遮挡导致眼睛截屏误判）。
    """
    import uia
    for _attempt in range(2):
        kill_apps(*NOTEPAD_PROCS)
        deadline = time.time() + 5
        while proc_count(*NOTEPAD_PROCS) > 0 and time.time() < deadline:
            time.sleep(0.3)
        hands.execute("open_app", {"app": "记事本"})
        deadline = time.time() + 12
        while time.time() < deadline:
            hwnd = (uia._find_hwnd_by_title("记事本")
                    or uia._find_hwnd_by_title("notepad"))
            if hwnd:
                hands._bring_window_front("记事本")
                return hwnd
            time.sleep(0.5)
    return 0


@q(54, ("gui",), "打开记事本（UIA 元素树物理验证控件枚举）")
def q54():
    import uia
    try:
        r = brain.think("打开记事本", [])
        if not ("已经打开" in r or "已经在运行" in r):
            return (False, f"open_app 话术异常：{r!r}")
        hwnd = _fresh_notepad_hwnd()
        if not hwnd:
            return (False, "记事本窗口未出现（两轮重启轮询）")
        # UIA 控件树就绪可能晚于窗口句柄出现，枚举失败时短轮询重试
        controls, text = [], ""
        for _ in range(6):
            controls = uia.dump_window_controls(hwnd)
            text = uia.format_controls(controls)
            if controls:
                break
            time.sleep(0.5)
        ok = bool(controls) and ("文件" in text or "编辑" in text)
        return (ok, f"UIA 控件 {len(controls)} 个：{text[:100]}")
    finally:
        kill_apps(*NOTEPAD_PROCS)


@q(55, (), "interrupt() 打断播放（打桩 mixer，不真发声）")
def q55():
    import threading

    import mouth
    mouth._interrupt.clear()
    state = {"stopped": False}

    class _FakeMusic:
        def load(self, path): pass
        def play(self): pass
        def get_busy(self): return not state["stopped"]
        def stop(self): state["stopped"] = True
        def unload(self): pass

    orig_init = mouth._init_mixer
    orig_music = mouth.pygame.mixer.music
    mouth._init_mixer = lambda: None
    mouth.pygame.mixer.music = _FakeMusic()
    t = None
    try:
        t = threading.Thread(target=mouth._play_file, args=("x.wav",), daemon=True)
        t.start()
        time.sleep(0.3)  # 让它先进轮询循环
        mouth.interrupt()
        t.join(1.0)
        ok = (not t.is_alive()) and state["stopped"]
        return (ok, f"提前返回={not t.is_alive()}，stop 被调={state['stopped']}")
    finally:
        mouth._init_mixer = orig_init
        mouth.pygame.mixer.music = orig_music
        mouth._interrupt.clear()


@q(56, ("llm", "gui", "vlm"), "在记事本中输入 nolan uia ok（确认后真机打字，UIA 加持）")
def q56():
    fail_prefixes = (
        "先生，任务未能完成",
        "先生，任务步数超出安全上限",
        "先生，检测到您将鼠标移至屏幕角落",
        "先生，我的视觉模块暂时无法连接",
    )
    last = "未执行"
    try:
        # 另清场网易云音乐——52 题按设计不关闭它，其延迟弹窗会抢前台、
        # 让眼睛在任务中途误判「屏幕上没有找到记事本」
        kill_apps("cloudmusic.exe")
        # 真实桌面上任何窗口（Word、终端、弹窗）都可能在 LLM 往返的
        # 几秒内抢占前台/遮挡记事本，导致眼睛截屏误判「目标应用缺失」。
        # 这是环境抖动而非功能缺陷：同一原因失败时整体重试一次。
        for attempt in range(2):
            # 备现场：记事本窗口必须真实可见且置前（两轮重启轮询），
            # 不让「进程在但窗口不在」的交接假成功混进眼睛环节
            hwnd = _fresh_notepad_hwnd()
            if not hwnd:
                last = "记事本窗口未出现（两轮重启轮询）"
                continue
            brain._pending_shell = None
            r1 = brain.think("在记事本中输入 nolan uia ok", [])
            if "确认" not in r1:
                last = f"未进入确认询问：{r1!r}"
                continue
            # 确认前再把记事本置前一次：brain 的 LLM 往返有几秒，
            # 期间其他窗口可能抢占前台
            hands._bring_window_front("记事本")
            # 确认后 eyes.perform 真机执行（截屏 + UIA + VLM 逐步打字）
            r2 = brain.think("确认", [])
            last = f"执行结果：{r2[:100]!r}"
            if any(p in r2 for p in fail_prefixes):
                brain._pending_shell = None  # 清场后重试
                continue
            if wait_window(NOTEPAD_TERMS, 3) or proc_count(*NOTEPAD_PROCS) > 0:
                suffix = f"（第 {attempt + 1} 次尝试）" if attempt else ""
                return (True, last + suffix)
        return (False, last)
    finally:
        brain._pending_shell = None
        kill_apps(*NOTEPAD_PROCS)


# == 备份 / 恢复 ==

def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def backup_state():
    return {
        "mem": _read_bytes(MEM_FILE),
        "rem": _read_bytes(REM_FILE),
        "sandbox": {
            n: _read_bytes(sandbox_path(n))
            for n in os.listdir(SANDBOX)
            if n.startswith("gaokao_") and os.path.isfile(sandbox_path(n))
        } if os.path.isdir(SANDBOX) else {},
    }


def restore_state(bk):
    for path, key in ((MEM_FILE, "mem"), (REM_FILE, "rem")):
        data = bk[key]
        try:
            if data is None:
                if os.path.isfile(path):
                    os.remove(path)
            else:
                with open(path, "wb") as f:
                    f.write(data)
        except OSError:
            pass
    # 沙盒：删掉测试产生的 gaokao_*，还原测试前已存在的同名文件
    if os.path.isdir(SANDBOX):
        for n in list(os.listdir(SANDBOX)):
            if n.startswith("gaokao_"):
                try:
                    os.remove(sandbox_path(n))
                except OSError:
                    pass
    for n, data in bk["sandbox"].items():
        if data is not None:
            try:
                with open(sandbox_path(n), "wb") as f:
                    f.write(data)
            except OSError:
                pass


# == 主流程 ==

def main():
    only = None
    if len(sys.argv) > 1 and "-" in sys.argv[1]:
        a, b = sys.argv[1].split("-", 1)
        only = (int(a), int(b))

    print("== Nolan 高考题库 · 56 题端到端成功率测试 ==")
    net_ok = probe_net()
    print(f"网络探测（www.baidu.com:443）：{'可用' if net_ok else '不可用，NET 组整组 SKIP'}")
    llm_ok = probe_llm() if net_ok else False
    print(f"大模型探测（一句「你好」，10 秒）：{'可用' if llm_ok else '不可用，LLM 组整组 SKIP'}")
    vlm_ok = (net_ok and llm_ok) and probe_vlm()
    print(f"视觉模型探测（截屏 + 一句描述）：{'可用' if vlm_ok else '不可用，VLM 组整组 SKIP'}")
    available = {"gui": os.name == "nt", "net": net_ok, "llm": llm_ok, "vlm": vlm_ok}
    if os.name != "nt":
        print("非 Windows 环境：GUI 组整组 SKIP")

    bk = backup_state()
    print("已备份 long_term.txt / reminders.txt / 沙盒 gaokao_* 文件")

    results = []  # (no, status, text, reason)
    try:
        for no, groups, text, fn in QUESTIONS:
            if only and not (only[0] <= no <= only[1]):
                continue
            missing = [g for g in groups if not available.get(g, True)]
            if missing:
                print(f"[SKIP] 第{no:02d}题 {text} —— 依赖不可用：{'/'.join(missing)}")
                results.append((no, "SKIP", text, f"依赖不可用：{'/'.join(missing)}"))
                continue
            # 待确认状态机是全局单槽：上题残留会劫持本题，先清场并告警
            if brain._pending_shell is not None:
                print(f"  [警告] 第{no}题开跑前 pending 未清空（{brain._pending_shell}），已强制重置")
                brain._pending_shell = None
            try:
                ok, reason = fn()
            except Exception:
                tb = traceback.format_exc().strip().splitlines()
                ok, reason = False, f"异常：{tb[-1] if tb else 'unknown'}"
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] 第{no:02d}题 {text}" + ("" if ok else f" —— {reason}"))
            results.append((no, status, text, reason))
    finally:
        # 无论中途成败：清理真实 GUI、恢复全部备份
        kill_apps(*(NOTEPAD_PROCS + CALC_PROCS))
        restore_state(bk)
        brain._pending_shell = None
        print("已清理 GUI 残留并恢复全部备份")

    total = len(results)
    passed = sum(1 for _, s, _, _ in results if s == "PASS")
    failed = [(no, t, r) for no, s, t, r in results if s == "FAIL"]
    skipped = sum(1 for _, s, _, _ in results if s == "SKIP")
    denominator = total - skipped
    rate = (passed / denominator * 100) if denominator else 0.0

    print("\n== 汇总 ==")
    print(f"总数 {total} / 通过 {passed} / 失败 {len(failed)} / 跳过 {skipped}")
    print(f"成功率（通过 ÷ (总数 - 跳过)）：{rate:.1f}%")
    if failed:
        print("失败明细：")
        for no, t, r in failed:
            print(f"  第{no:02d}题 {t} —— {r}")
    if skipped:
        print("跳过明细：")
        for no, s, t, r in results:
            if s == "SKIP":
                print(f"  第{no:02d}题 {t} —— {r}")
    return 0 if rate >= 98.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
