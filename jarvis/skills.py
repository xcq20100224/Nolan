# -*- coding: utf-8 -*-
"""
skills.py —— Nolan 的「技能固化」：成功过一次的任务，不再靠运气做第二次

第一性原理：VLM 逐步推理的成功率受模型随机性制约（单步 ~90%），
而一次被验证成功的动作序列是确定的物理事实——把它沉淀为技能，
下次遇到同类任务直接重放，可靠性从「每次掷骰子」变成「查答案」。

重放之所以可靠，是因为阶段 J1 起点击已升级为按名定位：
技能只记录「点什么名字的元素、输入什么文字、按什么键」，
坐标（随窗口位置漂移）不参与固化；重放时用 UIA 按名重新解析当前位置。

存储：jarvis/memory/skills.jsonl，每行一条
  {"task": 任务原文, "steps": [{"action","text","keys"}...], "ts": 时间戳}
上限 50 条，超出淘汰最旧（FIFO）——技能贵精不贵多。

匹配：字符二元组 Jaccard 相似度，阈值 0.6。
宁缺毋滥——匹配不到就走正常 VLM 闭环，绝不强行套旧技能。
"""

import json
import os
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "memory", "skills.jsonl")
_MAX_SKILLS = 50
_MATCH_THRESHOLD = 0.6

# 参与固化的物理动作（wait/done/fail 只是过程量，不进技能）
_REPLAYABLE = ("left_click", "double_click", "type", "key", "scroll")


def _load() -> list:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _save(skills: list) -> None:
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            for s in skills[-_MAX_SKILLS:]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("[skills] 写入失败（跳过固化）：%s" % exc)


def record(task: str, steps: list) -> bool:
    """
    固化一条技能：任务原文 + 可重放动作序列。
    只保留 _REPLAYABLE 动作；少于 1 个有效动作不固化（没什么可学的）。
    同任务（完全同文）已存在时更新而不是堆积。
    """
    task = (task or "").strip()
    clean = [{"action": s["action"],
              "text": s.get("text", ""),
              "keys": s.get("keys", "")}
             for s in (steps or [])
             if isinstance(s, dict) and s.get("action") in _REPLAYABLE]
    if not task or not clean:
        return False
    skills = _load()
    skills = [s for s in skills if s.get("task") != task]
    skills.append({"task": task, "steps": clean, "ts": int(time.time())})
    _save(skills)
    print("[skills] 已固化技能「%s」（%d 个动作）" % (task, len(clean)))
    return True


def _bigrams(text: str) -> set:
    t = (text or "").lower().replace(" ", "")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（0~1）。中文按字算，足够区分任务模式。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def find(task: str) -> tuple | None:
    """
    按任务描述找最相近的已固化技能，返回 (匹配到的任务原文, 动作序列)；
    相似度低于阈值返回 None（宁缺毋滥，走正常 VLM 闭环）。
    """
    best, best_sim = None, 0.0
    for s in _load():
        sim = _similarity(task, s.get("task", ""))
        if sim > best_sim:
            best, best_sim = s, sim
    if best is not None and best_sim >= _MATCH_THRESHOLD:
        print("[skills] 命中已固化技能「%s」（相似度 %.2f）"
              % (best["task"], best_sim))
        return best["task"], best["steps"]
    return None
