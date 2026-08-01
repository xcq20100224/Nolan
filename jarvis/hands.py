# -*- coding: utf-8 -*-
"""
hands.py —— 贾维斯的「手」

负责执行真实动作：开应用、开网页、搜索、抓网页、读写沙盒文件、
执行本机命令、报时、控制系统媒体播放、屏幕界面自动化（gui_control）。
大脑（brain.py）只需调用 execute(name, args)，
拿到的就是可以直接语音播报的口语化中文结果文本。

设计原则：每个函数只做一件物理上必要的事；危险操作必须有边界
（沙盒目录、三级命令安全闸：safe 直接执行 / confirm 需主人确认 /
blocked 直接拒绝）；永不向调用方抛异常。
"""

import os
import re
import json
import shutil
import string
import subprocess
import time
import urllib.parse
from datetime import datetime

import ctypes  # 发送 Windows 全局媒体键（user32.keybd_event），零第三方依赖

# 阶段五：眼睛模块（屏幕感知 + GUI 自动化，由并行工程师按契约编写）。
# 防御降级：eyes 尚未就绪、import 失败或其内部初始化异常时，
# gui_control 工具仍然注册在工具表中，确认执行时礼貌说明不可用，
# 绝不让「手」模块因为「眼睛」缺席而整体瘫痪。
try:
    import eyes as _eyes
except Exception:  # noqa: BLE001 - 任何导入期异常都按「眼睛不可用」处理
    _eyes = None

# ---------------------------------------------------------------------------
# 常量与边界
# ---------------------------------------------------------------------------

# 沙盒目录：只允许读写 jarvis\files\ 下的文件
SANDBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")

# 应用别名表：用户口语叫法（查找时转小写）-> 统一搜索词
APP_ALIASES = {
    "记事本": "notepad",
    "计算器": "calc",
    "画图": "mspaint",
    "vscode": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "代码编辑器": "Visual Studio Code",
    "浏览器": "Microsoft Edge",
    "edge": "Microsoft Edge",
    "微软浏览器": "Microsoft Edge",
    "谷歌浏览器": "Google Chrome",
    "chrome": "Google Chrome",
    "谷歌": "Google Chrome",
    "微信": "微信",
    "wechat": "微信",
    "excel": "Excel",
    "表格": "Excel",
    # ---- 音乐 / 视频 / 办公 / 社交等常用应用扩充（别名 -> 开始菜单搜索词）----
    # 注意：长短别名共存时（如「网易云音乐」与「网易云」、「qq音乐」与「qq」），
    # 依赖 _extract_app_hint 的长键优先扫描与 _resolve_alias 的最长键匹配兜底。
    "网易云音乐": "网易云音乐",
    "cloudmusic": "网易云音乐",
    "网易云": "网易云音乐",
    "qq音乐": "QQ音乐",
    "酷狗音乐": "酷狗音乐",
    "酷狗": "酷狗音乐",
    "抖音": "抖音",
    "哔哩哔哩": "哔哩哔哩",
    "b站": "哔哩哔哩",
    "bilibili": "哔哩哔哩",
    "爱奇艺": "爱奇艺",
    "腾讯视频": "腾讯视频",
    "steam": "Steam",
    "钉钉": "钉钉",
    "飞书": "飞书",
    "qq": "QQ",
    "word": "Word",
    "文档": "Word",
    "ppt": "PowerPoint",
    "幻灯片": "PowerPoint",
    "迅雷": "迅雷",
    "百度网盘": "百度网盘",
}

# 已知安装路径：搜索词 -> 候选 exe 绝对路径列表（PATH 找不到时兜底）
KNOWN_APP_PATHS = {
    "Visual Studio Code": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
}

# 开始菜单目录（用户级 + 系统级），用于快捷方式递归搜索
START_MENU_DIRS = [
    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
]

# 桌面目录（用户级 + 公共），很多应用只在桌面上放快捷方式
DESKTOP_DIRS = [
    os.path.expanduser("~/Desktop"),
    r"C:\Users\Public\Desktop",
]

# .lnk 快捷方式的全部搜索目录：开始菜单 + 桌面
LNK_SEARCH_DIRS = START_MENU_DIRS + DESKTOP_DIRS

# 通用后缀词：口语里描述「这是个应用」的冗余成分，归一化时循环剥掉。
# 长的放前面只是易读，循环剥离本身与顺序无关（如「应用程序」会先剥「程序」再剥「应用」）。
GENERIC_SUFFIXES = (
    "应用程序", "浏览器", "客户端", "电脑版", "软件",
    "应用", "程序", "助手",
)

# 归一化时要忽略的首尾标点（英文标点 + 常见中文标点）
_TERM_PUNCT = string.punctuation + "，。！？；：、（）【】《》「」『』…—·～"

# 允许执行的 shell 命令白名单（按首词匹配）——旧 run_command 专用，保留兼容
CMD_WHITELIST = ["echo", "dir", "ping", "ipconfig", "tasklist", "whoami", "ver", "python"]

# 危险 shell 元字符：含任何一个即拒绝执行——旧 run_command 专用，保留兼容
CMD_FORBIDDEN_CHARS = ["&", "|", ";", ">", "<", "`"]

# 各类输出的截断长度
SEARCH_MAX_CHARS = 1500  # search_web 搜索结果列表
FETCH_MAX_CHARS = 800    # 网页正文摘要
FILE_MAX_CHARS = 1000    # 文件内容
CMD_MAX_CHARS = 1000     # 旧 run_command 输出
SHELL_MAX_CHARS = 2000   # run_shell 输出（stdout+stderr 合并后）

# 星期口语化对照
WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ---------------------------------------------------------------------------
# 工具一：打开本地应用
# ---------------------------------------------------------------------------

def _start_app(target: str) -> bool:
    """尝试 os.startfile 打开目标，成功返回 True；任何失败都吞掉返回 False。"""
    try:
        os.startfile(target)
        return True
    except Exception:
        return False


def _cmd_start(term: str) -> None:
    """
    cmd start 兜底拉起：别名/PATH/已知路径/lnk/ShellExecute 全部失败后的
    最后一招——`start ""` 经 cmd 再试一次解析（立即返回不阻塞）。
    任何失败都吞掉；是否真拉起由调用方的窗口自检判定。
    """
    try:
        subprocess.run(
            f'start "" "{term}"', shell=True, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=os.path.expanduser("~"),
        )
    except Exception:
        pass


def _process_running(exe_basename: str) -> bool:
    """
    tasklist 查进程是否存在（按镜像名匹配，大小写不敏感，自动补 .exe）。
    任何异常返回 False——进程探测是补救链的辅助证据，绝不拖垮主流程。
    """
    try:
        name = (exe_basename or "").strip().lower()
        if not name:
            return False
        if not name.endswith(".exe"):
            name += ".exe"
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh", "/fi", f"imagename eq {name}"],
            capture_output=True, timeout=10,
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
        return name in text.lower()
    except Exception:
        return False


def _exe_candidates(term: str, started: list) -> list:
    """
    进程探测用的 exe basename 候选（去重）：
      1. 成功启动过的目标里的 .exe 文件名（去扩展名）；
      2. 别名表中映射到同一搜索词的纯英文键（如 cloudmusic -> 网易云音乐）；
      3. 搜索词本身（纯 ASCII 时，去空格）。
    """
    cands = []

    def _add(x):
        x = (x or "").strip().lower()
        if x and x not in cands:
            cands.append(x)

    for target in started:
        base = os.path.basename(str(target)).lower()
        if base.endswith(".exe"):
            _add(base[:-4])
    for key, val in APP_ALIASES.items():
        if val == term and re.fullmatch(r"[a-z0-9_\-]+", key):
            _add(key)
    if re.fullmatch(r"[a-z0-9_\- ]+", term or ""):
        _add(term.replace(" ", ""))
    return cands


def _normalize_term(name: str) -> str:
    """
    把口语化应用名归一化成搜索词：
    转小写、去首尾空白和标点、循环剥掉通用后缀词（浏览器/软件/应用程序/
    应用/程序/客户端/电脑版/助手）以及独立的 app/application 英文字样。
    例如 'chrome浏览器' -> 'chrome'、'微信电脑版' -> '微信'、
    'Chrome App' -> 'chrome'。
    剥空时回退到仅小写去空白的结果，避免把「浏览器」这种本名剥没了。
    """
    term = name.strip().lower().strip(_TERM_PUNCT).strip()
    original = term
    changed = True
    while changed and term:
        changed = False
        # 循环剥中文通用后缀词（剥完可能又露出新的后缀，如「应用程序」）
        for suffix in GENERIC_SUFFIXES:
            if term.endswith(suffix) and len(term) > len(suffix):
                term = term[: -len(suffix)].rstrip()
                changed = True
        # 剥独立的英文 app/application 字样（前面必须有空格才算独立词）
        for eng in (" application", " app"):
            if term.endswith(eng) and len(term) > len(eng):
                term = term[: -len(eng)].rstrip()
                changed = True
    return term or original


def _resolve_alias(app: str) -> str:
    """
    通用别名解析：先归一化，再精确匹配；无命中则做双向子串匹配
    （别名键包含在词中，或词包含在别名键中），取最长匹配键；
    都没有则返回归一化后的搜索词本身，交给后续步骤兜底。
    """
    term = _normalize_term(app)
    if not term:
        return term
    # 第一优先：归一化后精确命中别名键
    if term in APP_ALIASES:
        return APP_ALIASES[term]
    # 第二优先：双向子串，取最长键（避免短键误伤，如「谷歌」抢「谷歌浏览器」…
    # 其实两者同值，但最长键原则对长短别名共存时更稳）
    candidates = [
        key for key in APP_ALIASES
        if key in term or (len(term) >= 2 and term in key)
    ]
    if candidates:
        return APP_ALIASES[max(candidates, key=len)]
    return term


def _lnk_matches(stem: str, term: str) -> bool:
    """
    归一化后的双向子串匹配：搜索词在 lnk 名中，或 lnk 名在搜索词中。
    反向（lnk 名在词中）要求词长 >= 2，防止单字词到处乱配。
    """
    if not stem or not term:
        return False
    return term in stem or (len(term) >= 2 and stem in term)


def _pick_best_lnk(candidates: list[str], term: str) -> str | None:
    """
    从候选 .lnk 路径中挑出最像「应用本体」的一个。
    双方先归一化再比较。排序规则：卸载类（卸载/uninstall）垫底
    > 完全同名 > 前缀匹配 > 子串包含 > 文件名短的优先。
    """
    if not candidates:
        return None
    norm = _normalize_term(term)

    def rank(path: str) -> tuple:
        stem = _normalize_term(os.path.splitext(os.path.basename(path))[0])
        is_uninstall = ("卸载" in stem) or ("uninstall" in stem)
        return (
            is_uninstall,          # 卸载类快捷方式排最后
            stem != norm,          # 完全同名最优先
            not stem.startswith(norm),  # 前缀匹配次之
            len(stem),             # 名字越短越可能是应用本体
        )

    return min(candidates, key=rank)


def _find_start_menu_lnk(term: str) -> str | None:
    """
    在开始菜单与桌面目录里递归找 .lnk：
    文件名与搜索词各自归一化后做双向子串匹配。
    """
    norm = _normalize_term(term)
    candidates = []
    for base in LNK_SEARCH_DIRS:
        try:
            for root, _dirs, files in os.walk(base):
                for fname in files:
                    if not fname.lower().endswith(".lnk"):
                        continue
                    stem = _normalize_term(os.path.splitext(fname)[0])
                    if _lnk_matches(stem, norm):
                        candidates.append(os.path.join(root, fname))
        except Exception:
            continue  # 目录不存在或无权限时跳过，继续下一个目录
    return _pick_best_lnk(candidates, term)


def _open_app(app: str) -> str:
    """
    通用化打开本机应用，永不抛异常。
    解析顺序：别名表（归一化 + 模糊匹配）-> PATH(shutil.which) -> 已知安装路径
    -> 开始菜单/桌面 .lnk 递归搜索 -> ShellExecute 直接解析 -> cmd start 兜底。

    执行后自检（可靠闭环）：每一级 _start_app 不再「不抛异常即成功」，
    必须等目标窗口真实出现（_wait_for_window）才宣布成功；未出现则继续下一级。
    窗口搜索词只用解析后的应用名（标点截断 + 别名表键 + exe basename），
    绝不让「打开网易云音乐，播放第一首歌」这类混合句的任务描述混进搜索词。
    全部候选启动后仍无窗口时走托盘/已运行补救链（进程探测 -> 二次唤出 ->
    再等等 -> 如实区分「已经在运行」与「没看到窗口」），绝不误报失败。
    """
    try:
        app = app.strip()
        if not app:
            return "抱歉先生，您没有告诉我要打开什么应用。"

        # 语音指令常把任务描述混进应用名：取标点前的第一段作为候选应用名
        name = re.split(r"[，。！？；,.!?;]", app, maxsplit=1)[0].strip() or app

        # 第一步：通用别名解析（归一化 + 精确 + 双向子串），得到统一搜索词
        term = _resolve_alias(name)
        if not term:
            term = name

        # 窗口自检搜索词：别名解析词 + 候选名 + 别名表键（中英文系统的
        # 窗口标题各覆盖一边；绝不含标点后的任务描述）
        wait_terms = []
        for t in (term, name):
            t = (t or "").strip()
            if t and t not in wait_terms:
                wait_terms.append(t)
        for key, val in APP_ALIASES.items():
            if val == term and key not in wait_terms:
                wait_terms.append(key)

        # 幂等短路：目标窗口已在屏幕上时不重复拉起进程，直接如实确认
        for t in wait_terms:
            if _find_window(t):
                return f"好的先生，{name}已经打开了。"

        def _appeared(timeout: float = _WINDOW_VERIFY_TIMEOUT) -> bool:
            """把等待预算均摊给各候选搜索词，任一窗口出现即视为启动成功。"""
            per = max(1.0, timeout / len(wait_terms))
            return any(_wait_for_window(t, timeout=per) for t in wait_terms)

        started = []  # 成功 _start_app 过的目标，供托盘补救链二次唤出与进程探测

        def _start_and_record(target: str) -> bool:
            if _start_app(target):
                started.append(target)
                return True
            return False

        # 第二步：PATH 查找，找到直接用
        try:
            found = shutil.which(term)
        except Exception:
            found = None
        if found and _start_and_record(found):
            if _appeared():
                return f"好的先生，{name}已经打开了。"
            print(f"[hands] 「{name}」经 PATH 启动后未检测到窗口，继续尝试下一级……")

        # 第三步：已知安装路径探测
        for path in KNOWN_APP_PATHS.get(term, []):
            if os.path.isfile(path) and _start_and_record(path):
                if _appeared():
                    return f"好的先生，{name}已经打开了。"
                print(f"[hands] 「{name}」经已知路径启动后未检测到窗口，继续尝试下一级……")

        # 第四步：开始菜单快捷方式递归搜索
        lnk = _find_start_menu_lnk(term)
        if lnk and _start_and_record(lnk):
            if _appeared():
                return f"好的先生，{name}已经打开了。"
            print(f"[hands] 「{name}」经快捷方式启动后未检测到窗口，继续尝试下一级……")

        # 第五步：直接让 ShellExecute 解析搜索词
        # （mspaint、calc 等经 App Paths / 执行别名注册，which 找不到但能直接启动）
        if _start_and_record(term):
            if _appeared():
                return f"好的先生，{name}已经打开了。"
            print(f"[hands] 「{name}」经 ShellExecute 启动后未检测到窗口，用 cmd start 兜底……")

        # 兜底：cmd start 再拉起一次，最后再等一次窗口
        _cmd_start(term)
        if _appeared():
            return f"好的先生，{name}已经打开了。"

        # 托盘/已在运行补救链：进程确实活着 -> 二次唤出 -> 再等等 ->
        # 仍无窗口则如实说「已经在运行」（这是成功，应用可用），绝不误报失败
        exe_names = _exe_candidates(term, started)
        if any(_process_running(x) for x in exe_names):
            print(f"[hands] 「{name}」窗口未出现但进程存在，尝试二次唤出……")
            if started:
                _start_app(started[-1])  # 单实例应用二次 ShellExecute 通常唤出窗口
            if _appeared(timeout=5.0):
                return f"好的先生，{name}已经打开了。"
            return f"好的先生，「{name}」已经在运行了（窗口可能收在托盘里）。"

        # 进程也不存在：如实说明，绝不谎称已打开
        return (
            f"抱歉先生，已尝试启动「{name}」但没有看到它的窗口"
            "（可能被系统拦截或它只在托盘运行）。"
        )
    except Exception:
        return f"抱歉先生，打开{app}时出了问题，请稍后再试。"


# ---------------------------------------------------------------------------
# 工具二、三：打开网址 / 必应搜索
# ---------------------------------------------------------------------------

def _open_url(url: str) -> str:
    """用默认浏览器打开网址。"""
    try:
        url = url.strip()
        if not url:
            return "抱歉先生，您没有告诉我网址。"
        # 没写协议时补上 https，避免浏览器识别失败
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        os.startfile(url)
        return "好的先生，已经在浏览器里为您打开了。"
    except Exception as e:
        return f"抱歉先生，打开网页时出了问题：{e}"


def _web_search(query: str) -> str:
    """打开必应搜索该关键词。"""
    try:
        query = query.strip()
        if not query:
            return "抱歉先生，您没有告诉我要搜索什么。"
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        os.startfile(url)
        return f"好的先生，已经为您在必应上搜索「{query}」。"
    except Exception as e:
        return f"抱歉先生，搜索时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具十四：网页搜索 search_web（把结果文本拿回来给 AI 自己读）
# ---------------------------------------------------------------------------
#
# 与 _web_search 的本质区别：_web_search 只是用浏览器打开搜索页给主人看，
# AI 自己看不到任何内容；_search_web 直接抓取必应搜索结果页并解析出
# 标题与摘要文本返回——研究 / 新闻 / 资料类 Agent 循环（搜新闻 -> 总结
# -> 写文件）必须用它，否则拿不到结果文本，任务必然失败。
# 实测方案：带 Chrome UA 的 httpx GET 必应搜索页返回 200，
# 结果卡片可用 li.b_algo 选择器稳定解析（h2 标题 + .b_caption p 摘要）。

def _search_web(query: str) -> str:
    """
    搜索网页并返回结果文本：httpx 抓必应搜索结果页，bs4 解析 li.b_algo，
    取前 5 条的 h2 标题与 .b_caption p 摘要，按『一、标题\n摘要』格式化，
    总长截断 1500 字；无结果返回固定话术；任何异常返回礼貌说明，绝不抛异常。
    """
    try:
        import httpx
        from bs4 import BeautifulSoup

        query = query.strip()
        if not query:
            return "抱歉先生，您没有告诉我要搜索什么。"
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        resp = httpx.get(
            url,
            # 带桌面版 Chrome UA：必应按 UA 分流，裸 httpx UA 会拿到异常页面
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                )
            },
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("li.b_algo")[:5]
        if not items:
            return "没有找到相关结果"

        numbers = ["一", "二", "三", "四", "五"]
        blocks = []
        for idx, item in enumerate(items):
            h2 = item.select_one("h2")
            title = (
                " ".join(h2.get_text(separator=" ").split()) if h2 else "（无标题）"
            )
            cap = item.select_one(".b_caption p")
            summary = (
                " ".join(cap.get_text(separator=" ").split()) if cap else "（无摘要）"
            )
            blocks.append(f"{numbers[idx]}、{title}\n{summary}")
        return "\n".join(blocks)[:SEARCH_MAX_CHARS]
    except Exception as e:
        return f"抱歉先生，搜索网页时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具四：抓取网页正文
# ---------------------------------------------------------------------------

def _fetch_url(url: str) -> str:
    """抓取网页正文并返回前 800 字摘要。"""
    try:
        import httpx
        from bs4 import BeautifulSoup

        url = url.strip()
        if not url:
            return "抱歉先生，您没有告诉我网址。"
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        # 剔除脚本与样式，只留正文
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())

        if not text:
            return "抱歉先生，这个网页里没有读到任何文字内容。"
        return "网页主要内容如下：" + text[:FETCH_MAX_CHARS]
    except Exception as e:
        return f"抱歉先生，抓取网页失败了：{e}"


# ---------------------------------------------------------------------------
# 工具五、六、七：沙盒文件三件套
# ---------------------------------------------------------------------------

def _sandbox_path(name: str) -> str:
    """把用户给的文件名安全地落到沙盒目录里，杜绝路径穿越。"""
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    # 只取文件名部分，任何 ../ 、绝对路径都会被剥掉
    safe_name = os.path.basename(name.strip())
    return os.path.join(SANDBOX_DIR, safe_name)


def _read_file(name: str) -> str:
    """读取沙盒目录下的文件，内容截断 1000 字。"""
    try:
        path = _sandbox_path(name)
        if not os.path.isfile(path):
            return f"抱歉先生，文件柜里找不到「{os.path.basename(path)}」这个文件。"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(FILE_MAX_CHARS)
        return f"文件「{os.path.basename(path)}」的内容如下：{content}"
    except Exception as e:
        return f"抱歉先生，读文件时出了问题：{e}"


def _write_file(name: str, content: str) -> str:
    """以 UTF-8 覆盖写沙盒文件；写后重读校验，读回一致才宣布成功。"""
    try:
        path = _sandbox_path(name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        # 写后重读自检：写入调用返回不代表落盘内容正确，读回比对才算数
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                readback = f.read()
        except Exception:
            readback = None
        if readback != content:
            return "抱歉先生，写文件后校验没通过，请让我重试。"
        return f"好的先生，内容已经写进文件「{os.path.basename(path)}」了。"
    except Exception as e:
        return f"抱歉先生，写文件时出了问题：{e}"


def _list_files() -> str:
    """列出沙盒目录下的所有文件。"""
    try:
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        names = sorted(
            n for n in os.listdir(SANDBOX_DIR)
            if os.path.isfile(os.path.join(SANDBOX_DIR, n))
        )
        if not names:
            return "先生，文件柜里还没有任何文件。"
        return "先生，文件柜里现在有这些文件：" + "、".join(names) + "。"
    except Exception as e:
        return f"抱歉先生，查看文件柜时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具八：旧版受限 shell 命令（已从工具表移除，函数保留仅供兼容参考）
# ---------------------------------------------------------------------------

def _run_command(cmd: str) -> str:
    """执行白名单内的 shell 命令，拒绝危险元字符，输出截断 1000 字。"""
    try:
        cmd = cmd.strip()
        if not cmd:
            return "抱歉先生，您没有告诉我要执行什么命令。"

        first_word = cmd.lower().split()[0]
        if first_word not in CMD_WHITELIST:
            return (
                f"抱歉先生，为了安全我只能执行这些命令：{'、'.join(CMD_WHITELIST)}，"
                f"「{first_word}」不在其中。"
            )

        if any(ch in cmd for ch in CMD_FORBIDDEN_CHARS):
            return "抱歉先生，这条命令里含有危险字符，为了安全我不能执行。"

        result = subprocess.run(
            cmd.split(), capture_output=True, shell=False, timeout=20
        )
        # 不直接 text=True：Git 系工具输出 UTF-8、Windows 原生命令输出 GBK，
        # 固定一种编码必然有一种会炸。先抓字节，再按 UTF-8 -> GBK 顺序尝试解码。
        raw = (result.stdout or b"") + (result.stderr or b"")
        for enc in ("utf-8", "gbk"):
            try:
                output = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            output = raw.decode("gbk", errors="replace")
        output = output.strip()
        if not output:
            return "先生，命令执行完了，没有任何输出。"
        return "命令执行结果如下：" + output[:CMD_MAX_CHARS]
    except Exception as e:
        return f"抱歉先生，执行命令时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具八（新）：通用 shell 命令执行 run_shell + 三级安全闸
# ---------------------------------------------------------------------------
#
# 安全闸设计（可靠性的一部分，不是累赘）：
#   safe    —— 只读查询类，直接执行；
#   confirm —— 写 / 删 / 改 / 装 / 杀进程类，必须主人确认后才执行；
#   blocked —— 可能造成不可逆系统破坏，无论确认与否一律拒绝。
# 判定一律在 cmd.lower() 之后进行，避免大小写绕过。

# 删除/清空类动词（整词匹配，防止误伤 taskkill 等词）
_DEL_VERB_RE = re.compile(
    r"(?:^|[\s;&|/\"'])(?:del|erase|rd|rmdir|remove-item)(?=[\s;&|\"']|$)"
)


def _targets_system_root(c: str) -> bool:
    """
    判断（已转小写的）命令是否以系统根级路径为操作目标：
    C:\\ 根、C:\\Windows（含其下任意子路径）、C:\\Users 根（含通配 C:\\Users\\*）。
    用前视断言判定：路径后紧跟空白 / 引号 / 命令连接符 / 结束即视为根目标，
    因此 "C:\\" 带引号、C:\\Users -Recurse 带参数等写法都无法绕过。
    """
    # C:\Windows 及其下任何内容
    if "c:\\windows" in c:
        return True
    # C:\ 根（含带引号 "C:\"）：反斜杠后不再有路径成分
    if re.search(r"c:\\+(?=[\s\"'&|;]|$)", c):
        return True
    # 裸 C:（del c: /f 这类作用于 C 盘当前目录的写法）
    if re.search(r"c:(?=[\s\"'&|;]|$)", c):
        return True
    # C:\Users 根（允许 C:\Users\* 通配），但 C:\Users\某人\... 不算根
    if re.search(r"c:\\+users(?:\\+\*)?(?=[\s\"'&|;]|$)", c):
        return True
    return False


def shell_risk(cmd: str) -> str:
    """
    三级风险判定：返回 'safe' | 'confirm' | 'blocked'。
    blocked：可能造成不可逆系统破坏；confirm：写删改类需主人确认；safe：其余。
    """
    c = (cmd or "").lower()

    # ---- blocked：明确的高危指令，出现即拒绝 ----
    if any(k in c for k in ("format", "diskpart", "shutdown",
                            "reg delete", "takeown", "icacls")):
        return "blocked"
    if "cipher /w" in c:  # 覆写擦除空闲空间，数据不可恢复
        return "blocked"
    # fork 炸弹特征（cmd 的 %0|%0 与 bash 的 :(){ :|:& };:）
    if "%0|%0" in c or ":(){" in c.replace(" ", ""):
        return "blocked"

    # ---- 删除类动词 + 系统根级目标：blocked ----
    has_del = bool(_DEL_VERB_RE.search(c))
    if has_del and _targets_system_root(c):
        return "blocked"

    # ---- confirm：写 / 删 / 改 / 装 / 杀进程类 ----
    if has_del:  # 删除非系统路径
        return "confirm"
    confirm_keywords = (
        "reg add", "reg import",   # 注册表写入
        "netsh",                   # 网络配置
        "taskkill", "stop-process",  # 结束进程
        "set-content", "out-file",  # 写文件
        "move-item", "move",       # 移动文件
        "pip install", "npm install",  # 安装第三方包
        "schtasks",                # 计划任务
    )
    if any(k in c for k in confirm_keywords):
        return "confirm"
    # copy 到系统盘根（如 copy a.txt c:\）
    if re.search(r"(?:^|[\s;&|])copy[\s]", c) and _targets_system_root(c):
        return "confirm"

    # ---- 其余视为只读查询，直接执行 ----
    return "safe"


# CLI 白名单：run_shell 首词命中即按命令行程序同步执行（含/不含扩展名都匹配）；
# 白名单外的 .exe 或可解析 exe 视为 GUI 程序，改走 cmd start 非阻塞拉起。
_CLI_WHITELIST = {
    "python", "python3", "pip", "git", "curl", "wget", "ping", "ipconfig",
    "netstat", "tasklist", "taskkill", "where", "whoami", "hostname",
    "echo", "dir", "type", "copy", "move", "ren", "del",
    "node", "npm", "ffmpeg", "reg", "sc", "chkdsk", "sfc",
    "powershell", "pwsh", "cmd",
}


def _first_word(cmd: str) -> str:
    """提取命令首词；双引号包裹的带空格路径取引号内整体。"""
    cmd = (cmd or "").lstrip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 1:
            return cmd[1:end]
    parts = cmd.split()
    return parts[0] if parts else ""


def _looks_like_gui_launch(cmd: str) -> tuple:
    """
    判定命令是否为「直接拉起 GUI 程序」（同步执行会阻塞 60 秒的那种）：
      (a) 首词 basename 以 .exe 结尾且去扩展名后不在 CLI 白名单；
      (b) 首词无扩展名但 shutil.which 能解析到 exe，且该 exe basename 不在白名单。
    白名单外无法解析的返回 (False, 首词)，保持原同步执行路径不动。
    返回 (是否 GUI 拉起, 用于话术的纯名)。
    """
    word = _first_word(cmd)
    if not word:
        return False, ""
    base = os.path.basename(word).lower()
    stem = base[:-4] if base.endswith(".exe") else base
    if stem in _CLI_WHITELIST:
        return False, stem
    # (a) 显式 .exe 且不在白名单
    if base.endswith(".exe"):
        return True, stem
    # (b) 无扩展名：which 能解析到 exe 且其 basename 不在白名单
    if "." not in base:
        try:
            resolved = shutil.which(word)
        except Exception:
            resolved = None
        if resolved and resolved.lower().endswith(".exe"):
            rstem = os.path.basename(resolved).lower()[:-4]
            if rstem not in _CLI_WHITELIST:
                return True, stem
    return False, stem


def _run_shell(cmd: str, confirmed: bool = False) -> str:
    """
    通用命令执行：在主人家目录下以 shell 方式执行任意 cmd/PowerShell 命令。
    blocked 直接拒绝；confirm 未确认时返回 '[[NEEDS_CONFIRM]]' 开头的待确认文本；
    确认后（confirmed=True）或 safe 命令直接执行。
    输出 stdout+stderr 合并截断 2000 字，沿用 UTF-8 -> GBK 字节解码方案。
    """
    try:
        cmd = (cmd or "").strip()
        if not cmd:
            return "抱歉先生，您没有告诉我要执行什么命令。"

        risk = shell_risk(cmd)
        if risk == "blocked":
            return "抱歉先生，这条命令可能造成不可逆的系统破坏，我必须拒绝执行。"
        if risk == "confirm" and not confirmed:
            return (
                "[[NEEDS_CONFIRM]] 先生，这条命令属于写删改操作，存在一定风险，"
                "需要您确认后我才能执行。\n"
                f"命令原文：{cmd}\n"
                "如确认无误，请说「确认执行」；若改变主意，请说「取消」。"
            )

        # GUI 拉起非阻塞化：首词是白名单外的 GUI 程序时，同步 subprocess.run
        # 会阻塞 60 秒（GUI 进程不退出）；改写成 cmd start 立即返回。
        # 改写后的命令必须重新过安全闸，等级变了就放弃改写、走原路径。
        is_gui, gui_name = _looks_like_gui_launch(cmd)
        if is_gui:
            rewritten = f'start "" {cmd}'
            if shell_risk(rewritten) == risk:
                try:
                    subprocess.run(
                        rewritten, shell=True, timeout=10,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        cwd=os.path.expanduser("~"),
                    )
                    return f"好的先生，已启动「{gui_name}」。"
                except Exception:
                    pass  # 改写路径失败则回落到原同步路径

        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=60,
            cwd=os.path.expanduser("~"),
        )
        # 不直接 text=True：Git 系工具输出 UTF-8、Windows 原生命令输出 GBK，
        # 固定一种编码必然有一种会炸。先抓字节，再按 UTF-8 -> GBK 顺序尝试解码。
        raw = (result.stdout or b"") + (result.stderr or b"")
        for enc in ("utf-8", "gbk"):
            try:
                output = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            output = raw.decode("gbk", errors="replace")
        output = output.strip()

        if result.returncode != 0:
            head = f"先生，命令执行完毕，但退出码为{result.returncode}，说明执行过程中出现了错误。"
        else:
            head = "先生，命令执行完毕。"
        if not output:
            return head + "没有任何输出。"
        return head + "输出如下：" + output[:SHELL_MAX_CHARS]
    except subprocess.TimeoutExpired:
        return "抱歉先生，这条命令执行超过60秒仍未结束，我已经把它中止了。"
    except Exception as e:
        return f"抱歉先生，执行命令时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具九：报时
# ---------------------------------------------------------------------------

def _get_time() -> str:
    """返回当前时间、日期、星期的口语化表述。"""
    try:
        now = datetime.now()
        weekday = WEEKDAYS_CN[now.weekday()]
        return (
            f"先生，现在是{now.year}年{now.month}月{now.day}日，{weekday}，"
            f"{now.hour}点{now.minute}分。"
        )
    except Exception as e:
        return f"抱歉先生，看表的时候出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具十：系统媒体控制（Windows 媒体键，ctypes 零依赖）
# ---------------------------------------------------------------------------
#
# 第一性原理：控制「正在播放什么」不需要钻进每个播放器的内部，
# Windows 本身就有全局媒体键——按下它，系统会转发给当前媒体会话
# （网易云音乐、浏览器里的音乐页等）。我们用 ctypes 直接调
# user32.keybd_event 发送这些虚拟键，零第三方依赖。

# action -> (虚拟键码, 口语化确认文案)
_MEDIA_ACTIONS = {
    "play_pause": (0xB3, "好的先生，已为您播放/暂停。"),      # VK_MEDIA_PLAY_PAUSE
    "next": (0xB0, "好的先生，已为您切到下一首。"),            # VK_MEDIA_NEXT_TRACK
    "previous": (0xB1, "好的先生，已为您切回上一首。"),        # VK_MEDIA_PREV_TRACK
    "volume_up": (0xAF, "好的先生，已为您调高音量。"),         # VK_VOLUME_UP
    "volume_down": (0xAE, "好的先生，已为您调低音量。"),       # VK_VOLUME_DOWN
    "mute": (0xAD, "好的先生，已为您切换静音。"),              # VK_VOLUME_MUTE
}


def _media_control(action: str) -> str:
    """
    发送 Windows 全局媒体键控制系统媒体播放。
    action 白名单：play_pause / next / previous / volume_up / volume_down / mute。
    按下（flags=0）后立即抬起（flags=2，KEYEVENTF_KEYUP），模拟一次真实按键。
    未知 action 返回礼貌说明；永不抛异常。
    """
    try:
        action = (action or "").strip().lower()
        entry = _MEDIA_ACTIONS.get(action)
        if entry is None:
            known = "、".join(_MEDIA_ACTIONS.keys())
            return (
                f"抱歉先生，媒体控制只会这几个动作：{known}，"
                f"「{action or '空'}」我做不到。"
            )
        vk, reply = entry
        keybd_event = ctypes.windll.user32.keybd_event
        keybd_event(vk, 0, 0, 0)  # 按下
        keybd_event(vk, 0, 2, 0)  # 抬起（KEYEVENTF_KEYUP）
        return reply
    except Exception as e:
        return f"抱歉先生，控制媒体播放时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具十一：屏幕界面自动化（截屏 + 视觉模型理解 + 鼠标键盘执行）
# ---------------------------------------------------------------------------
#
# 第一性原理：shell 命令够不到图形界面内部（点选网易云音乐列表中的歌曲、
# 在某软件里点某个按钮），这类操作的物理本质是「看屏幕 -> 动鼠标键盘」。
# 因此 gui_control 把任务委托给 eyes 模块：截屏 -> glm-4v-flash 理解界面并
# 返回动作 JSON -> pyautogui 执行 -> 循环直到完成（步数上限兜底）。
#
# 安全闸（可靠性优先于功能数量）：
#   1. 未确认时一律返回 '[[NEEDS_CONFIRM]]' 开头的待确认文本，
#      由 brain 的待确认状态机接管，先生亲口说「确认执行」才会真正动手；
#   2. 步数上限与 pyautogui FAILSAFE（鼠标甩角落中止）由 eyes 内部把关；
#   3. 禁止事项（输密码、支付、删文件、给联系人发消息）硬编码在 eyes 的感知 prompt 中。
#
# 自动开路前导（第一性原理）：线上教训——LLM 跳过 open_app 直接 gui_control，
# 对未打开的应用操作必然失败。「先打开应用」不能只写在 prompt 里建议，
# 必须刻在代码里：gui_control 自己动手前，先检测目标应用窗口，缺失则
# 自动打开、等待出现并置前，再把一个「目标已在屏幕上」的现场交给眼睛。

# EnumWindows 回调函数类型：BOOL CALLBACK EnumWindowsProc(HWND, LPARAM)
_ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _find_window_hwnd(title_substr: str):
    """
    用 ctypes 枚举全部可见顶层窗口（EnumWindows + IsWindowVisible +
    GetWindowTextW 回调收集），返回首个标题包含 title_substr
    （大小写不敏感子串匹配）的窗口句柄；找不到或出错返回 None。
    纯 Win32 API，零第三方依赖。
    """
    needle = (title_substr or "").strip().lower()
    if not needle:
        return None
    try:
        user32 = ctypes.windll.user32
        hits = []

        def _on_window(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True  # 跳过不可见窗口，继续枚举
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True  # 无标题窗口跳过
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if needle in (buf.value or "").lower():
                    hits.append(hwnd)
                    return False  # 找到即停止枚举
            except Exception:
                pass  # 单个窗口读取失败不拖垮整个枚举
            return True

        user32.EnumWindows(_ENUM_PROC(_on_window), 0)
        return hits[0] if hits else None
    except Exception:
        return None


def _find_window(title_substr: str) -> bool:
    """检测屏幕上是否存在标题包含 title_substr 的可见窗口。"""
    return _find_window_hwnd(title_substr) is not None


# 窗口等待参数：每 1 秒轮询一次；open_app 执行后自检默认最多等 8 秒
_WINDOW_VERIFY_TIMEOUT = 8.0


def _wait_for_window(term: str, timeout: float = _WINDOW_VERIFY_TIMEOUT) -> bool:
    """
    窗口等待原语（全模块唯一）：每 1 秒轮询 _find_window(term)，
    窗口出现立即返回 True；超时（默认 8 秒）返回 False。
    open_app 的执行后自检与 gui_control 的自动开路前导都复用它。
    """
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if _find_window(term):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(1.0, remaining))
    except Exception:
        return False


def _bring_window_front(title_substr: str) -> bool:
    """
    尝试把标题匹配的窗口还原（SW_RESTORE）并置前。
    Windows 前台权限锁：后台进程直接 SetForegroundWindow 会被系统静默拒绝，
    标准解法是 AttachThreadInput——把本线程输入队列挂到当前前台线程上，
    取得「前台权限」后再 BringWindowToTop + SetForegroundWindow，完事脱钩。
    最后用 GetForegroundWindow 实测验证，返回是否真的置前成功；
    全程容错不抛异常（失败也无妨，眼睛靠截屏感知，不依赖前台状态）。
    """
    try:
        hwnd = _find_window_hwnd(title_substr)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # 9 = SW_RESTORE
        fore = user32.GetForegroundWindow()
        fore_tid = user32.GetWindowThreadProcessId(
            ctypes.c_void_p(fore), None) if fore else 0
        cur_tid = kernel32.GetCurrentThreadId()
        attached = False
        if fore_tid and fore_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fore_tid, True))
        try:
            user32.BringWindowToTop(ctypes.c_void_p(hwnd))
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            user32.SetActiveWindow(ctypes.c_void_p(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fore_tid, False)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _extract_app_hint(task: str) -> str | None:
    """
    从界面操作任务描述里提取目标应用词，供自动开路前导使用。
    提取顺序：
      1. 扫 APP_ALIASES 别名表键（长键优先），任务文本包含即返回对应搜索词；
      2. 正则口语句式：「在XX中/里……」、「打开XX，/。/空格……」；
      3. 都提取不到返回 None——调用方不做前导，直接交给眼睛。
    """
    text = (task or "").strip()
    if not text:
        return None
    lowered = text.lower()
    # 第一优先：别名表键（长键优先，避免短键抢先误伤）
    for key in sorted(APP_ALIASES, key=len, reverse=True):
        if key in lowered:
            return APP_ALIASES[key]
    # 第二优先：口语句式正则提取，命中后剥掉首尾标点
    for pattern in (r"在(.+?)[中里]", r"打开(.+?)[，,。 ]"):
        m = re.search(pattern, text)
        if m:
            hint = m.group(1).strip(_TERM_PUNCT).strip()
            if hint:
                return hint
    return None


# 前导等待参数：每 1 秒查一次窗口，最多等 16 秒（应用冷启动需要时间）
_WINDOW_WAIT_INTERVAL = 1
_WINDOW_WAIT_MAX_SECONDS = 16


def _ensure_app_ready(hint: str) -> None:
    """
    gui_control 自动开路前导：检测目标应用窗口 -> 缺失则复用现有应用解析链
    （别名 -> which -> 已知路径 -> 开始菜单/桌面 lnk -> ShellExecute）启动
    -> 复用 _wait_for_window 原语等待窗口出现（总上限 16 秒，分两段）->
    出现后尝试置前。
    托盘型应用（如网易云音乐）主窗口出现慢，两段等待之间再补一次启动调用把它唤出。
    等待超时不阻断：打印中文日志后继续，交给眼睛模块自行判断并报告。
    任何环节出错都吞掉，绝不让前导把 gui_control 拖垮。
    """
    try:
        if _find_window(hint):
            # 窗口已在屏幕上但可能被其他窗口遮挡：先尝试置前，
            # 否则眼睛截屏看不到目标界面，会误报「屏幕上没有找到」
            _bring_window_front(hint)  # 容错置前，失败也无妨
            return  # 窗口已在屏幕上，无需开路
        print(f"[hands] 未检测到「{hint}」的窗口，先自动打开该应用……")
        _open_app(hint)  # 复用应用解析链启动（返回话术此处不用，眼睛只看屏幕）

        # 第一段等待：前半程预算（_open_app 内部已做过自检，这里等的是窗口真正就绪）
        half = _WINDOW_WAIT_MAX_SECONDS // 2
        if _wait_for_window(hint, timeout=half):
            _bring_window_front(hint)  # 容错置前，失败也无妨
            print(f"[hands] 「{hint}」的窗口已出现，已尝试置前。")
            return

        # 第二段等待：托盘型应用先补一次启动调用唤出主窗口，再等剩余预算
        remaining = _WINDOW_WAIT_MAX_SECONDS - half
        if remaining > 0:
            print(f"[hands] 「{hint}」窗口仍未出现，补一次启动调用尝试唤出……")
            _open_app(hint)
            if _wait_for_window(hint, timeout=remaining):
                _bring_window_front(hint)  # 容错置前，失败也无妨
                print(f"[hands] 「{hint}」的窗口已出现，已尝试置前。")
                return
        print(
            f"[hands] 等待 {_WINDOW_WAIT_MAX_SECONDS} 秒后仍未看到「{hint}」的窗口，"
            "继续交给眼睛模块判断。"
        )
    except Exception:
        pass


def _gui_control(task: str, confirmed: bool = False) -> str:
    """
    操作屏幕上的软件界面：确认后调 eyes.perform(task) 执行并返回其口语化结果。
    未确认返回 '[[NEEDS_CONFIRM]]' 开头的待确认文本；eyes 不可用时礼貌说明；
    永不抛异常。
    """
    try:
        task = (task or "").strip()
        if not task:
            return "抱歉先生，您没有告诉我要在屏幕上完成什么操作。"

        # 安全闸第一道：执行前必须经主人确认（复用 brain 待确认状态机）
        if not confirmed:
            return (
                "[[NEEDS_CONFIRM]] 先生，这项任务需要我接管您的鼠标和键盘，"
                "我会先截屏看懂软件界面，再进行点击、输入等操作。\n"
                f"任务内容：{task}\n"
                "操作期间请尽量不要移动鼠标和键盘；如需紧急中止，"
                "把鼠标快速甩到屏幕任意角落即可。\n"
                "如确认无误，请说「确认执行」；若改变主意，请说「取消」。"
            )

        # 防御降级：眼睛模块不可用时如实说明，不谎称完成
        if _eyes is None:
            return (
                "抱歉先生，屏幕感知模块当前不可用，我暂时无法执行界面内操作，"
                "这一步恐怕需要您亲自动手，或稍后再试。"
            )

        # 自动开路前导（刻在代码里，不靠 prompt 建议）：从任务描述提取目标
        # 应用词，窗口缺失则自动打开并等待置前；提取不到（None）则不做前导
        hint = _extract_app_hint(task)
        if hint:
            _ensure_app_ready(hint)

        # 确认后委托眼睛模块执行（步数上限等安全参数用其默认值；
        # 目标应用词一并传入，眼睛每步截屏前做前台保障）
        return _eyes.perform(task, target_hint=hint)
    except Exception as e:
        return f"抱歉先生，进行界面操作时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具十二：通用截屏元素 capture_screen（截屏 + 视觉定位 + 裁剪保存）
# ---------------------------------------------------------------------------
#
# 第一性原理：gui_control 是「看屏幕 -> 动手」，capture_screen 是
# 「看屏幕 -> 取图」——把屏幕上任意可见元素（歌曲封面、头像、图表）
# 变成文件柜里的一张 PNG。纯感知、不动鼠标键盘，因此无需确认闸。

def _capture_screen(description: str) -> str:
    """
    截取屏幕上的界面元素：委托 eyes.locate_and_crop 定位并裁剪保存。
    eyes 不可用时礼貌降级；定位失败时给出可操作的提示；永不抛异常。
    """
    try:
        description = (description or "").strip()
        if not description:
            return "抱歉先生，您没有告诉我要截取屏幕上的什么内容。"
        # 防御降级：眼睛模块不可用时如实说明，不谎称完成
        if _eyes is None:
            return (
                "抱歉先生，屏幕感知模块当前不可用，我暂时无法截取界面内容，"
                "请稍后再试。"
            )
        path = _eyes.locate_and_crop(description)
        if not path:
            return (
                f"抱歉先生，我在屏幕上没有找到「{description}」，"
                "请您确认它正显示在屏幕上，或换个更具体的说法让我再试。"
            )
        return f"先生，已截取{description}，保存为 {os.path.basename(path)}。"
    except Exception as e:
        return f"抱歉先生，截取屏幕内容时出了问题：{e}"


# ---------------------------------------------------------------------------
# 工具十三：设置网页版聊天背景 set_web_background
# ---------------------------------------------------------------------------
#
# 机制：把文件柜（沙盒）里的图片登记进状态文件 web_background.json，
# 网页版后端经 /api/background 读它、前端轮询应用。本工具只负责
# 「找图 + 写状态」，不碰 HTTP——与网页端解耦，契约即状态文件。
# 找图范围与写状态都限定在沙盒内，basename 防护杜绝路径穿越。

# 背景状态文件：jarvis\files\web_background.json，内容 {"image": "captures/xxx.png"}
_WEB_BG_STATE = os.path.join(SANDBOX_DIR, "web_background.json")


def _set_web_background(name: str) -> str:
    """
    把沙盒里的图片设为网页版聊天背景：在 files\\ 及其 captures 子目录找图，
    找到则写状态文件 web_background.json（image 为相对 files 目录的路径），
    返回确认话术；找不到返回礼貌说明；永不抛异常。
    """
    try:
        name = (name or "").strip()
        if not name:
            return "抱歉先生，您没有告诉我要用哪张图片做聊天背景。"
        # 与沙盒三件套同款防护：只取文件名部分，任何 ../ 、绝对路径都被剥掉
        safe_name = os.path.basename(name)
        # 依次在沙盒根目录、captures 子目录找（capture_screen 的截图在后者）
        candidates = [
            os.path.join(SANDBOX_DIR, safe_name),
            os.path.join(SANDBOX_DIR, "captures", safe_name),
        ]
        found = next((p for p in candidates if os.path.isfile(p)), None)
        if found is None:
            return (
                f"抱歉先生，文件柜里找不到图片「{safe_name}」，"
                "您可以先让我截取或保存这张图片，再设为背景。"
            )
        # 状态文件里的路径相对 files 目录，统一用正斜杠（跨平台、URL 友好）
        rel = os.path.relpath(found, SANDBOX_DIR).replace(os.sep, "/")
        os.makedirs(SANDBOX_DIR, exist_ok=True)
        with open(_WEB_BG_STATE, "w", encoding="utf-8") as f:
            json.dump({"image": rel}, f, ensure_ascii=False)
        # 写后重读自检：JSON 可解析且 image 字段与刚写入一致才算成功
        try:
            with open(_WEB_BG_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = None
        if not isinstance(state, dict) or state.get("image") != rel:
            return "抱歉先生，背景状态写入后校验没通过，请让我重试。"
        return "好的先生，聊天背景已更换。"
    except Exception as e:
        return f"抱歉先生，更换聊天背景时出了问题：{e}"


# ---------------------------------------------------------------------------
# 契约接口
# ---------------------------------------------------------------------------

# 工具注册表：名字 -> (函数, 描述, 参数说明)
_TOOLS = {
    "open_app": (
        _open_app,
        "打开本机应用（记事本/计算器/画图/VSCode/浏览器等，支持别名）",
        {"app": "应用名，如：记事本 / 计算器 / VSCode / 浏览器（支持别名）"},
    ),
    "open_url": (
        _open_url,
        "用默认浏览器打开指定网址",
        {"url": "要打开的网址"},
    ),
    "web_search": (
        _web_search,
        "为主人在浏览器中打开搜索页面（结果展示给主人看，你看不到内容）",
        {"query": "搜索关键词"},
    ),
    "search_web": (
        _search_web,
        "搜索网页并返回结果文本（标题+摘要），供你阅读、总结和进一步处理；"
        "研究/新闻/资料类任务用这个，不要用 web_search",
        {"query": "搜索关键词"},
    ),
    "fetch_url": (
        _fetch_url,
        "抓取网页正文内容，返回前800字摘要，用于阅读网页",
        {"url": "要抓取的网址"},
    ),
    "read_file": (
        _read_file,
        "读取文件柜（沙盒目录）里的文件内容，最多1000字",
        {"name": "文件名"},
    ),
    "write_file": (
        _write_file,
        "把内容写入文件柜（沙盒目录）里的文件，会覆盖原内容",
        {"name": "文件名", "content": "要写入的内容"},
    ),
    "list_files": (
        _list_files,
        "列出文件柜（沙盒目录）里的所有文件",
        {},
    ),
    "run_shell": (
        _run_shell,
        "在主人的 Windows 电脑上执行任意 cmd/PowerShell 命令（像主人自己动手操作一样）；"
        "只读查询直接执行，写删改操作需主人确认，危险命令会被拒绝",
        {"cmd": "要执行的命令原文", "confirmed": "可选，主人已确认风险时为 true"},
    ),
    "get_time": (
        _get_time,
        "获取当前的时间、日期和星期",
        {},
    ),
    "media_control": (
        _media_control,
        "控制系统媒体播放：播放/暂停、上一首/下一首、音量增减、静音（适用于网易云音乐等播放器）",
        {"action": "play_pause|next|previous|volume_up|volume_down|mute 之一"},
    ),
    "gui_control": (
        _gui_control,
        "操作屏幕上的软件界面：截屏理解界面后移动鼠标点击、输入文字，完成 shell 够不到的软件内操作"
        "（如点选网易云音乐列表中的歌曲）；执行前需主人确认",
        {"task": "要完成的界面操作任务描述", "confirmed": "主人已确认时为 true"},
    ),
    "capture_screen": (
        _capture_screen,
        "截取屏幕上的任意界面元素并保存为图片文件：截屏后由视觉模型定位该元素并裁剪，"
        "保存进文件柜的 captures 子目录（如歌曲封面、头像、图标、图表）。"
        "只看不碰鼠标键盘，无需确认。常接在 open_app/gui_control 把目标打开到屏幕之后使用，"
        "也可与 set_web_background 串联把截到的图设为聊天背景",
        {"description": "要截取的界面元素描述，如：网易云音乐我喜欢列表第一首歌的封面"},
    ),
    "set_web_background": (
        _set_web_background,
        "把文件柜（沙盒）里的图片设为网页版聊天背景：只需给图片文件名，"
        "captures 子目录里 capture_screen 截下的图也能直接找到；"
        "通常接在 capture_screen 之后，用其返回的文件名作为 name",
        {"name": "图片文件名，如 capture_20250101_120000.png"},
    ),
}


def list_tools() -> list[dict]:
    """返回工具描述表供 LLM prompt 使用。"""
    return [
        {"name": name, "description": desc, "args": args_desc}
        for name, (_, desc, args_desc) in _TOOLS.items()
    ]


def execute(name: str, args: dict) -> str:
    """
    执行工具，返回口语化中文结果文本（可直接语音播报）。
    未知工具返回错误说明；永不抛异常。
    """
    try:
        entry = _TOOLS.get(name)
        if entry is None:
            known = "、".join(_TOOLS.keys())
            return f"抱歉先生，我不知道「{name}」是什么工具，我只会这些：{known}。"
        func = entry[0]
        args = args or {}
        return func(**args)
    except TypeError:
        return f"抱歉先生，使用工具「{name}」时参数不对，请检查参数是否齐全。"
    except Exception as e:
        return f"抱歉先生，执行工具「{name}」时出了意外：{e}"
