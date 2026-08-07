# -*- coding: utf-8 -*-
"""
ppt_research.py —— PPT 生成前的联网研究模块（R1 交付物）

交接契约（ppt_maker 侧防御式 import，非空注入 prompt，空串走旧路径）：

    def research_topic(topic: str, max_queries: int = 2, budget_sec: float = 25.0) -> str:
        对 topic 做联网研究，返回事实摘要文本（≤1200字）：
        关键数据点（带数字与年份）、真实案例、最新动态，尽量标注来源。
        任何失败（网络/配置/超时/无结果）返回空串 ""。绝不抛异常。
        总耗时硬上界 budget_sec。

通道选型（真机对比结论，见交付报告）：
  主通道：GLM API 联网搜索工具（chat/completions + tools.web_search），
          必须带 thinking disabled（llm_config.json 的 extra_body）——
          开思考实测 24.7s 顶满预算，关思考实测 4.8~5.7s，且回答自带
          「分条事实 + 数字 + 年份 + 来源」格式，可直接作为摘要，省下
          一次压缩调用，把时间预算留给第二次查询。
  兜底通道：本地抓必应搜索结果页（li.b_algo 解析），快（~2s）但中文
          长尾查询质量差（实测返回词典释义/无关页面），只在 GLM 通道
          整体失败时用；拿到原文后若预算允许再交一次 LLM 压缩，
          压缩失败则截断原文兜底，绝不空跑。
"""

import json
import os
import time
import urllib.parse

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_SUMMARY_MAX_CHARS = 1200   # 契约：返回摘要总长上限
_HTTP_TIMEOUT = 15.0        # 单次 HTTP 调用超时上限（契约要求 ≤15s）
_MIN_CALL_BUDGET = 3.0      # 剩余预算低于此值不再发起任何 HTTP 调用（发了也收不回）
_SECOND_QUERY_MIN = 8.0     # 第一次查询成功后，剩余预算高于此值才发第二次查询
_COMPRESS_MIN = 6.0         # 兜底路径上，剩余预算高于此值才做 LLM 压缩调用

# GLM 联网搜索的系统提示：直接产出最终摘要格式，省一次压缩调用
_SEARCH_SYSTEM_PROMPT = (
    "你是研究助理。用联网搜索调研用户主题，分条列出关键事实："
    "每条含具体数字与年份，尽量标注来源；每条不超过80字；不超过10条。"
    "只要事实，不要开场白与总结语；查不到就如实说，绝不编造。"
)

# 兜底路径的压缩提示：把必应原始结果压成同格式摘要
_COMPRESS_SYSTEM_PROMPT = (
    "你是研究助理。把用户给的搜索结果原文提炼成事实摘要：分条列出含具体"
    "数字与年份的关键数据、真实案例、最新动态，尽量保留来源；每条不超过"
    "80字；不超过10条。原文里没有的事实绝不编造。"
)

# 抓取必应用的桌面版 Chrome UA（必应按 UA 分流，裸 httpx UA 会拿到异常页面）
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 配置加载（与 brain.py 同款策略：文件兜底，环境变量优先）
# ---------------------------------------------------------------------------

def _load_llm_config() -> dict:
    """读取本模块旁的 llm_config.json；环境变量 JARVIS_* 覆盖同名键。
    任何读取/解析失败返回 {}——配置缺失按契约返回空串，绝不抛异常。"""
    cfg: dict = {}
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "llm_config.json"
    )
    try:
        with open(config_path, encoding="utf-8") as f:
            file_cfg = json.load(f)
        if isinstance(file_cfg, dict):
            cfg = {k: v for k, v in file_cfg.items() if isinstance(v, str) and v}
    except (OSError, ValueError):
        cfg = {}
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


def _parse_extra_body(cfg: dict) -> dict:
    """把配置里的 extra_body（JSON 字符串，如 thinking 开关）解析成可合并的
    dict；缺失或解析失败返回 {}。关思考是延迟达标的关键，绝不允许它炸掉主流程。"""
    try:
        raw = cfg.get("extra_body") or ""
        obj = json.loads(raw) if raw else {}
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# 主通道：GLM API 联网搜索（服务端检索 + 回答一步完成）
# ---------------------------------------------------------------------------

def _glm_web_search(query: str, cfg: dict, timeout: float) -> str:
    """调 GLM chat/completions 并挂 web_search 工具，返回基于搜索的分条事实
    文本；任何失败（网络/状态码/字段缺失/端点非智谱）返回空串。"""
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    # web_search 工具是智谱私有协议，非智谱端点直接放弃（发了也是 400）
    if not base_url or not api_key or "bigmodel.cn" not in base_url:
        return ""
    payload = {
        "model": cfg.get("model", "glm-5.2"),
        "messages": [
            {"role": "system", "content": _SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        "tools": [{"type": "web_search", "web_search": {"enable": True}}],
        "temperature": 0.3,
        **_parse_extra_body(cfg),
    }
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            base_url + "/chat/completions",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        return reply or ""
    except Exception:  # noqa: BLE001 - 契约：任何失败都降级为空串
        return ""


# ---------------------------------------------------------------------------
# 兜底通道：本地抓必应搜索结果页（复刻 hands._search_web 的实测方案）
# ---------------------------------------------------------------------------

def _bing_search(query: str, timeout: float) -> str:
    """httpx 抓必应搜索结果页，bs4 解析 li.b_algo 取前 5 条标题+摘要；
    无结果或任何异常返回空串。"""
    try:
        from bs4 import BeautifulSoup

        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        resp = httpx.get(
            url,
            headers={"User-Agent": _CHROME_UA},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("li.b_algo")[:5]
        if not items:
            return ""
        numbers = ["一", "二", "三", "四", "五"]
        blocks = []
        for idx, item in enumerate(items):
            h2 = item.select_one("h2")
            title = " ".join(h2.get_text(separator=" ").split()) if h2 else ""
            cap = item.select_one(".b_caption p")
            summary = " ".join(cap.get_text(separator=" ").split()) if cap else ""
            if title or summary:
                blocks.append(f"{numbers[idx]}、{title}\n{summary}")
        return "\n".join(blocks)[:1500]
    except Exception:  # noqa: BLE001
        return ""


def _compress_with_llm(raw_text: str, topic: str, cfg: dict, timeout: float) -> str:
    """兜底路径的压缩调用：把必应原文提炼成分条事实摘要（纯文本对话，
    不带搜索工具，延迟低）；任何失败返回空串，由调用方用原文截断兜底。"""
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    if not base_url or not api_key:
        return ""
    payload = {
        "model": cfg.get("model", "glm-5.2"),
        "messages": [
            {"role": "system", "content": _COMPRESS_SYSTEM_PROMPT},
            {"role": "user", "content": f"主题：{topic}\n\n搜索结果原文：\n{raw_text}"},
        ],
        "temperature": 0.3,
        **_parse_extra_body(cfg),
    }
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            base_url + "/chat/completions",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        return reply or ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 摘要整形：行级去重 + 长度截断
# ---------------------------------------------------------------------------

def _shape_summary(text: str) -> str:
    """把若干份回答合并成分条摘要：按行去重（保序）、去空行，整体截断到
    契约上限 1200 字。截断发生在行边界上，绝不切出半行残句。"""
    lines = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    out = ""
    for line in lines:
        candidate = (out + "\n" + line) if out else line
        if len(candidate) > _SUMMARY_MAX_CHARS:
            break
        out = candidate
    if not out and lines:
        # 边界：第一行独自就超上限时硬截断该行——有内容绝不空跑（契约底线）
        out = lines[0][:_SUMMARY_MAX_CHARS]
    return out


# ---------------------------------------------------------------------------
# 对外契约入口
# ---------------------------------------------------------------------------

def research_topic(topic: str, max_queries: int = 2, budget_sec: float = 25.0) -> str:
    """对 topic 做联网研究，返回事实摘要文本（≤1200字）。

    流程：主查询（topic + 数据 趋势 2025 2026）与备用查询（topic 本身）
    依次走 GLM 联网搜索；全部失败则降级必应抓取 +（预算允许时）LLM 压缩，
    压缩失败用原文截断兜底。任何失败返回空串，绝不抛异常；
    总耗时硬上界 budget_sec。
    """
    try:
        topic = (topic or "").strip()
        if not topic:
            return ""
        cfg = _load_llm_config()
        if not cfg.get("api_key") or not cfg.get("base_url"):
            return ""  # 配置缺失：契约要求返回空串

        deadline = time.monotonic() + max(float(budget_sec), 0.0)

        def _left() -> float:
            """剩余预算秒数。"""
            return deadline - time.monotonic()

        def _timeout() -> float:
            """本次 HTTP 调用的超时：预算与单次上限取小，不足最小预算返回 0。"""
            left = _left()
            if left < _MIN_CALL_BUDGET:
                return 0.0
            return min(_HTTP_TIMEOUT, left)

        # 查询序列：主查询 + 备用查询，条数受 max_queries 约束
        queries = [topic + " 数据 趋势 2025 2026", topic][: max(1, int(max_queries))]

        # ---- 主通道：GLM 联网搜索 ----
        answers = []
        for idx, q in enumerate(queries):
            t = _timeout()
            if t <= 0:
                break  # 预算耗尽，立即止步
            # 已有一份答案时，第二次查询要留够后续整形的余量，避免顶穿预算
            if idx > 0 and answers and _left() < _SECOND_QUERY_MIN:
                break
            ans = _glm_web_search(q, cfg, t)
            if ans:
                answers.append(ans)

        if answers:
            # 搜索通道的回答已是「分条事实 + 数字 + 年份 + 来源」格式，
            # 合并去重截断即可返回，省下压缩调用保预算（契约允许直接用）
            return _shape_summary("\n".join(answers))

        # ---- 兜底通道：必应抓取 +（预算允许时）LLM 压缩 ----
        t = _timeout()
        if t <= 0:
            return ""
        raw = _bing_search(topic, t)
        if not raw:
            return ""
        if _left() >= _COMPRESS_MIN:
            t = _timeout()
            if t > 0:
                summary = _compress_with_llm(raw, topic, cfg, t)
                if summary:
                    return _shape_summary(summary)
        # 压缩不可用/失败：原文截断兜底，有原文绝不空跑
        return _shape_summary(raw)
    except Exception:  # noqa: BLE001 - 契约死线：任何意外都返回空串
        return ""
