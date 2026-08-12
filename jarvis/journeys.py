# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 共同经历簿（journeys.py）· T2 关系记忆

第一性原理：memory/episodic 记的是「事实」与「短期情景」，本模块记的是第三样东西——
「关系」：Nolan 和主人一起做过什么、成果落在哪个文件。它是人机连结的物理载体，
让 Nolan 一周后有资格说出「上次那份开学 PPT 要不要再改一版」。

与 episodic.py 的边界（互补、不重复）：
- episodic 管「短期情景」：近 48 小时高显著度事件流（对话/任务/错误/里程碑）。
- journeys 管「共同作品/长期经历」：只记产生实物的合作（做 PPT / 改 PPT / 写文件），
  默认回看 30 天，按「新→旧」渲染为可自然提起的关系简报。
- 本模块绝不修改 episodic，两模块独立存储、独立注入。

设计取舍：
- 追加式 JSONL（一行一条经历）：写入是 O(1) 追加，天然抗半截写入——
  崩溃最多坏最后一行，读取时跳过坏行即可；比整文件 JSON 更适合「只增不改」的流水。
- 无内存缓存：经历写入频率极低（每成功工具至多一条），每次都直接读写磁盘，
  测试只需替换模块级 _STORE_FILE 路径即可完全隔离。
- 绝不抛异常：与 episodic 同款纪律——记忆模块是辅助系统，宁可丢一条经历，
  不可让主流程崩。所有公开函数整体 try/except 兜底。

存储：jarvis\\memory\\journeys.jsonl（__file__ 定位，目录自动创建）
数据模型（每条经历一行 JSON）：
    {
        "ts":       1736000000.0,    # epoch 秒（float）
        "kind":     "ppt_made" | "ppt_edited" | "file_written",
        "summary":  "做了PPT《开学季》，共8页",   # 人话摘要，渲染时直接使用
        "artifact": "开学季_20260104.pptx"        # 成果文件名（文件柜里），可为空串
    }

接口契约（签名一字不差，主控按此集成）：
    def record(kind: str, summary: str, artifact: str = "") -> None
    def record_for_tool(tool: str, args: dict, result) -> None
    def brief_for_prompt(max_items: int = 8, days: int = 30) -> str
"""

import json
import os
import re
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 存储路径（模块级变量，测试可整体替换为临时目录下的路径）
# ---------------------------------------------------------------------------

_MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_STORE_FILE = os.path.join(_MEMORY_DIR, "journeys.jsonl")

_SUMMARY_MAX = 120   # 摘要硬上限，与 episodic 对齐
_INSTR_MAX = 30      # edit_ppt 指令摘要硬上限（契约要求 ≤30 字）


# ---------------------------------------------------------------------------
# 内部读写
# ---------------------------------------------------------------------------

def _append(event: dict) -> None:
    """追加一行 JSONL 并 flush。目录自动创建；失败静默。"""
    try:
        os.makedirs(os.path.dirname(_STORE_FILE), exist_ok=True)
        with open(_STORE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
    except OSError:
        pass


def _read_all() -> list:
    """读出全部经历；坏行跳过，文件不存在返回空表。永不抛异常。"""
    events = []
    try:
        if not os.path.exists(_STORE_FILE):
            return events
        with open(_STORE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # 半截写入/损坏行：丢这一条，保住其余
                if isinstance(e, dict) and "ts" in e:
                    events.append(e)
    except OSError:
        pass
    return events


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def record(kind: str, summary: str, artifact: str = "") -> None:
    """写入一条共同经历。任何异常都不抛出。"""
    try:
        event = {
            "ts": time.time(),
            "kind": str(kind or "event"),
            "summary": str(summary or "").strip()[:_SUMMARY_MAX],
            "artifact": os.path.basename(str(artifact or "").strip()),
        }
        if not event["summary"]:
            return  # 没有摘要的经历没有意义，不写
        _append(event)
    except Exception:
        pass  # 兜底：记忆模块永不炸主流程


def record_for_tool(tool: str, args: dict, result) -> None:
    """把成功的工具结果映射为一条共同经历；失败/无关工具不记录。永不抛异常。

    映射规则（与 hands.py 定型话术一一对应）：
    - make_ppt  成功 → kind="ppt_made"，summary 含《标题》与页数
      （从 result 话术正则提取，提取不到回退 args.topic），artifact=文件名；
    - edit_ppt  成功 → kind="ppt_edited"，summary 含指令摘要（≤30 字）；
    - write_file 成功 → kind="file_written"，artifact=文件名；
    - 其余工具一律不记录（没产生共同作品）。
    失败判定沿用全项目约定：结果文本以「抱歉」开头即为失败。
    """
    try:
        if not isinstance(result, str) or not result.strip():
            return
        if result.startswith("抱歉"):
            return  # 失败结果不记入共同经历
        if not isinstance(args, dict):
            args = {}

        if tool == "make_ppt":
            # 话术形如：「……《开学季》，文件名 开学季_xxx.pptx，共 8 页，……」
            m_title = re.search(r"《([^》]+)》", result)
            title = m_title.group(1) if m_title else str(args.get("topic") or "").strip()
            if not title:
                title = "未命名"
            m_pages = re.search(r"共\s*(\d+)\s*页", result)
            pages_txt = f"，共{m_pages.group(1)}页" if m_pages else ""
            # 不排除「.」（文件名含扩展名），只按空白与中文/英文逗号句号截断，
            # 再剥掉句读残留（如话术结尾正好是句号时）
            m_file = re.search(r"文件名\s*([^\s，。,]+)", result)
            artifact = m_file.group(1).rstrip("。") if m_file else ""
            record("ppt_made", f"做了PPT《{title}》{pages_txt}", artifact)
        elif tool == "edit_ppt":
            file_name = str(args.get("file_name") or "").strip()
            instr = str(args.get("instruction") or "").strip()[:_INSTR_MAX]
            summary = f"按「{instr}」改了PPT" if instr else "改了一版PPT"
            record("ppt_edited", summary, file_name)
        elif tool == "write_file":
            name = str(args.get("name") or "").strip()
            if not name:
                # 兜底：从话术「内容已经写进文件「X」了」里提取
                m_file = re.search(r"文件「([^」]+)」", result)
                name = m_file.group(1) if m_file else ""
            record("file_written", f"写了文件「{os.path.basename(name) or '未命名'}」", name)
        # 其余工具：不产生共同作品，不记录
    except Exception:
        pass  # 兜底：记忆模块永不炸主流程


def brief_for_prompt(max_items: int = 8, days: int = 30) -> str:
    """最近 days 天共同经历的中文简报（新→旧，最多 max_items 条），供注入 system prompt。

    渲染形如：
        以下是你和主人一起做过的事（共同经历，可在合适时机自然提起）：
        - 8月11日 做了PPT《开学季》，共8页（文件柜：开学季_xxx.pptx）
        - 8月10日 写了文件「日记.txt」（文件柜：日记.txt）
    无经历或任何异常返回 ""。永不抛异常。
    """
    try:
        max_items = max(1, int(max_items))
        days = max(1, int(days))
        now = time.time()
        cutoff = now - days * 86400.0

        events = []
        for e in _read_all():
            try:
                ts = float(e.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if ts < cutoff or ts > now + 3600:  # 超龄或未来时戳（异常数据）都不要
                continue
            events.append((ts, e))
        events.sort(key=lambda x: x[0], reverse=True)  # 新→旧
        events = events[:max_items]
        if not events:
            return ""

        now_dt = datetime.now()
        lines = []
        for ts, e in events:
            dt = datetime.fromtimestamp(ts)
            # 跨年补年份，同年内「8月11日」即可
            if dt.year != now_dt.year:
                day_label = f"{dt.year}年{dt.month}月{dt.day}日"
            else:
                day_label = f"{dt.month}月{dt.day}日"
            line = f"- {day_label} {e.get('summary', '')}"
            artifact = str(e.get("artifact") or "").strip()
            if artifact:
                line += f"（文件柜：{artifact}）"
            lines.append(line)

        return ("以下是你和主人一起做过的事（共同经历，可在合适时机自然提起）：\n"
                + "\n".join(lines))
    except Exception:
        return ""
