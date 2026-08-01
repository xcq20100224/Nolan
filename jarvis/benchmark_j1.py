# -*- coding: utf-8 -*-
"""
benchmark_j1.py —— J1 基准集：20 个主人真实日常任务的成功率度量

与 selftest_gaokao.py（闭卷题库）的本质区别：
  题库测「已知能力不退化」，本基准测「真实世界能不能干活」。
  任务全部来自主人的真实使用场景（问时间、查天气、写日报、开软件、
  记事情、定提醒、真机操作记事本），断言全部落在物理世界
  （文件内容 / 进程窗口 / 状态文件 / 话术内容），不听话术自嗨。

输出（J1 的核心交付物）：
  每题 PASS/FAIL/SKIP + 耗时 + 失败归因（眼瞎 / 手滑 / 脑短路 / 通道），
  末尾汇总成功率。这个数字是 J2/J3 一切优化的靶子。

运行：python jarvis/benchmark_j1.py        # 全量
      python jarvis/benchmark_j1.py 15-20   # 只跑区间（调试用）
"""

import os
import re
import sys
import socket
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain
import hands
import memory  # noqa: F401 - 与 brain 看到同一份记忆模块

_JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX = hands.SANDBOX_DIR
MEM_FILE = os.path.join(_JARVIS_DIR, "memory", "long_term.txt")
REM_FILE = os.path.join(_JARVIS_DIR, "memory", "reminders.txt")

FAIL_MARKS = ("抱歉", "未能完成", "出了问题", "无法连接")
CHAT_FAIL_MARKS = ("未能完成", "出了问题", "无法连接")


def no_fail(r) -> bool:
    return isinstance(r, str) and bool(r.strip()) and \
        not any(m in r for m in FAIL_MARKS)


def chat_ok(r) -> bool:
    if not isinstance(r, str) or not r.strip():
        return False
    if any(m in r for m in CHAT_FAIL_MARKS):
        return False
    return not ('"tool"' in r and r.lstrip().startswith("{"))


def _read(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _sandbox(name) -> str:
    return _read(os.path.join(SANDBOX, name))


def _proc_running(*exe) -> bool:
    try:
        import subprocess
        out = subprocess.check_output(["tasklist"], text=True,
                                      errors="replace").lower()
        return any(e.lower() in out for e in exe)
    except Exception:
        return False


def _kill(*exe) -> None:
    import subprocess
    for e in exe:
        subprocess.run(["taskkill", "/f", "/im", e],
                       capture_output=True)


# == 环境探针 ==

def _probe_net() -> bool:
    try:
        socket.create_connection(("www.baidu.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _probe_llm() -> bool:
    try:
        cfg = brain._load_llm_config()
        host = cfg["base_url"].split("//", 1)[-1].split("/")[0]
        socket.create_connection((host, 443), timeout=4).close()
        return True
    except Exception:
        return False


NET_OK = _probe_net()
LLM_OK = _probe_llm()


def need_net():
    return None if NET_OK else "无网络"


def need_llm():
    return None if LLM_OK else "LLM 不可达"


# == GUI 任务驱动：走真实确认流 ==

def drive_gui(task: str):
    """think 任务 -> 若进入确认询问则回复「确认」-> 返回最终话术。"""
    brain._pending_shell = None
    r1 = brain.think(task, [])
    if "确认" in r1:
        r2 = brain.think("确认", [])
        brain._pending_shell = None
        return r2
    return r1


# == 20 个真实任务 ==
# 每项：(编号, 指令, 验收函数(话术)->(bool, 说明), 环境依赖, 失败归因类别, 清理函数)

def _t(msg):
    def chk(r):
        return (msg in r, "话术含「%s」" % msg)
    return chk


def _file_has(name, *words):
    def chk(_r):
        c = _sandbox(name)
        miss = [w for w in words if w not in c]
        return (not miss,
                "%s 内容 %d 字%s" % (name, len(c),
                                     "，缺：%s" % miss if miss else ""))
    return chk


def _mem_has(*words):
    def chk(_r):
        c = _read(MEM_FILE)
        miss = [w for w in words if w not in c]
        return (not miss, "长期记忆 %s" % ("含目标词" if not miss else "缺 %s" % miss))
    return chk


def _rem_has(*words):
    def chk(_r):
        c = _read(REM_FILE)
        miss = [w for w in words if w not in c]
        return (not miss, "提醒文件 %s" % ("含目标词" if not miss else "缺 %s" % miss))
    return chk


def _proc(*exe):
    def chk(_r):
        ok = _proc_running(*exe)
        return (ok, "进程 %s" % ("在运行" if ok else "未找到"))
    return chk


def _nofail_msg(r):
    return (no_fail(r), "话术：%s…" % r[:40])


def _weather(r):
    ok = no_fail(r) and any(w in r for w in ("天气", "度", "晴", "雨", "阴", "云"))
    return (ok, "天气话术：%s…" % r[:40])


def _news(r):
    ok = no_fail(r) and len(r) >= 60
    return (ok, "新闻总结 %d 字" % len(r))


def _list_has(word):
    def chk(r):
        return (word in r, "列表话术含「%s」" % word)
    return chk


TASKS = [
    # —— A. 信息服务（不动 GUI）——
    (1, "现在几点了", lambda r: (bool(re.search(r"\d", r)) and no_fail(r),
     "时间话术：%s…" % r[:30]), None, "脑"),
    (2, "查一下今天北京的天气", _weather, need_net, "脑"),
    (3, "搜一下最近的人工智能新闻，给我总结三条", _news, need_net, "脑"),
    (4, "帮我算算 128 加 256 等于多少", _t("384"), None, "脑"),
    (5, "给我讲个笑话", lambda r: (chat_ok(r), "闲聊回应 %d 字" % len(r)),
     need_llm, "脑"),
    # —— B. 文件柜 ——
    (6, "把 周三下午三点产品评审 写到 备忘.txt", _file_has("备忘.txt", "周三", "产品评审"), None, "脑"),
    (7, "读一下 备忘.txt", _t("产品评审"), None, "脑"),
    (8, "把 苹果、香蕉、橙子 写到 购物清单.txt",
     _file_has("购物清单.txt", "苹果", "香蕉", "橙子"), None, "脑"),
    (9, "列出我文件柜里的文件", _list_has("备忘.txt"), None, "脑"),
    (10, "搜一下今天的科技新闻，总结两条写到 日报.txt",
     _file_has("日报.txt",), need_net, "脑"),
    # —— C. 记忆与提醒 ——
    (11, "记住我喜欢喝美式咖啡", _mem_has("美式"), None, "脑"),
    (12, "我喜欢喝什么咖啡", _t("美式"), need_llm, "脑"),
    (13, "10分钟后提醒我喝水", _rem_has("喝水"), None, "脑"),
    (14, "我现在有什么提醒", _t("喝水"), None, "脑"),
    # —— D. 应用与系统 ——
    (15, "打开记事本", _proc("notepad.exe"), None, "通道"),
    (16, "打开计算器", _proc("calc.exe", "CalculatorApp.exe"), None, "通道"),
    (17, "暂停音乐播放", _nofail_msg, None, "通道"),
    (18, "打开网页 www.baidu.com", _nofail_msg, need_net, "通道"),
    # —— E. 真·GUI 闭环（眼睛+手+复核）——
    (19, "在记事本中输入：贾维斯第一步", None, None, "眼手"),  # 特殊驱动
    (20, "打开网易云音乐并播放我喜欢的第一首歌", None, None, "眼手"),  # 特殊驱动
]


# == 备份 / 恢复（用户数据零污染） ==

def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def backup_state():
    names = ("备忘.txt", "购物清单.txt", "日报.txt")
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


# == 失败归因 ==

def classify(cat: str, reply: str) -> str:
    if "视觉模块" in reply or "屏幕上没有找到" in reply:
        return "眼瞎（感知失败）"
    if "复核" in reply or "未生效" in reply or "连续两次操作" in reply:
        return "手滑（执行未生效）"
    if "无法连接" in reply or "未能理解" in reply:
        return "脑短路（决策失败）"
    return {"脑": "脑短路（决策失败）", "通道": "通道故障（应用/网络）",
            "眼手": "眼手协调失败"}.get(cat, "未分类")


# == 主流程 ==

def run_one(num: int):
    row = next(t for t in TASKS if t[0] == num)
    _, task, checker, dep, cat = row
    if callable(dep):
        reason = dep()
        if reason:
            return ("SKIP", 0.0, "环境依赖：%s" % reason, "")
    t0 = datetime.now()
    reply = ""
    try:
        if num == 19:
            _kill("notepad.exe")
            hands.execute("open_app", {"app": "记事本"})
            hands._wait_for_window("记事本", timeout=8)
            hands._bring_window_front("记事本")
            reply = drive_gui(task)
            ok = no_fail(reply)
            detail = "GUI 话术：%s…" % reply[:50]
        elif num == 20:
            # 清场：浏览器等前台窗口会遮挡目标应用，眼睛的整屏截图
            # 会把遮挡物当操作对象（实测教训），基准环境必须干净。
            # 注意：会关闭浏览器，跑基准前请保存好网页内容。
            # 网易云不杀：托盘型应用重启后常只驻托盘（进程在、窗口不可见），
            # 复用已运行实例最可靠；未运行则先启动并等窗口出现
            _kill("msedge.exe", "chrome.exe")
            if not _proc_running("cloudmusic.exe"):
                hands.execute("open_app", {"app": "网易云音乐"})
                hands._wait_for_window("网易云音乐", timeout=16)
            hands._bring_window_front("网易云音乐")
            reply = drive_gui(task)
            ok = no_fail(reply) and _proc_running("cloudmusic.exe")
            detail = "话术 %s；进程 %s" % (
                reply[:40], "在" if _proc_running("cloudmusic.exe") else "不在")
        else:
            reply = brain.think(task, [])
            ok, detail = checker(reply)
        dt = (datetime.now() - t0).total_seconds()
        if ok:
            return ("PASS", dt, detail, "")
        return ("FAIL", dt, detail, classify(cat, reply or ""))
    except Exception:
        dt = (datetime.now() - t0).total_seconds()
        return ("FAIL", dt, "异常：%s" % traceback.format_exc().splitlines()[-1],
                classify(cat, reply or ""))


def main():
    lo, hi = 1, 20
    if len(sys.argv) > 1 and "-" in sys.argv[1]:
        lo, hi = map(int, sys.argv[1].split("-", 1))
    print("== J1 基准集 · 20 个真实任务 ==")
    print("环境：NET=%s LLM=%s\n" % ("OK" if NET_OK else "DOWN",
                                    "OK" if LLM_OK else "DOWN"))
    st = backup_state()
    results = []
    try:
        for num in range(lo, hi + 1):
            status, dt, detail, why = run_one(num)
            results.append((num, status))
            line = "第%02d题 %-4s %5.1fs  %s" % (num, status, dt, detail)
            if why:
                line += "  -> " + why
            print(line, flush=True)
    finally:
        restore_state(st)
        _kill("notepad.exe", "CalculatorApp.exe")
        brain._pending_shell = None
    ran = [s for _, s in results if s != "SKIP"]
    passed = sum(1 for s in ran if s == "PASS")
    print("\n== 汇总 ==")
    print("跑了 %d 题 / PASS %d / FAIL %d / SKIP %d" % (
        len(ran), passed, len(ran) - passed,
        sum(1 for _, s in results if s == "SKIP")))
    if ran:
        print("真实任务成功率：%.1f%%" % (100.0 * passed / len(ran)))


if __name__ == "__main__":
    main()
