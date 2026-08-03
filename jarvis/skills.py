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

匹配：模板正则优先（参数化技能），字面二元组 Jaccard 兜底，阈值 0.6。
宁缺毋滥——匹配不到就走正常 VLM 闭环，绝不强行套旧技能。

模板化（P2）：固化时识别任务中的可变参数（如输入的文字内容），
技能存为模板「在记事本中输入文字：{内容}」+ 参数提取正则；
新任务命中模板即提取参数回填动作序列——同类任务一次学习终身重放，
技能库不再被同模式变体塞爆（实测 22 条技能全是同一模式的字面副本）。
"""

import json
import os
import re
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "memory", "skills.jsonl")
_MAX_SKILLS = 50
_MATCH_THRESHOLD = 0.6

# 参与固化的物理动作（wait/done/fail 只是过程量，不进技能）
_REPLAYABLE = ("left_click", "double_click", "type", "key", "scroll")

# 内置模板模式：高频任务族的参数抽取规则
# （任务正则含命名参数组 -> 模板任务文本；命中时动作文本中的参数值占位化）
_TEMPLATE_RULES = (
    (re.compile(r"^(?:在)?记事本(?:中|里)?(?:输入|写上|写入)(?:文字)?[:：]?(?P<内容>.+)$"),
     "在记事本中输入文字：{内容}"),
)


def _templatize(task: str, steps: list) -> tuple | None:
    """固化时尝试模板化：命中内置模式则返回 (模板任务, 模式源, 占位化动作)，
    否则 None（按旧字面逻辑存储）。"""
    for regex, tmpl in _TEMPLATE_RULES:
        m = regex.match(task)
        if not m:
            continue
        params = m.groupdict()
        new_steps = []
        for s in steps:
            s = dict(s)
            text = str(s.get("text", ""))
            for name, value in params.items():
                if value and text == value:
                    s["text"] = "{%s}" % name
            new_steps.append(s)
        return tmpl, regex.pattern, new_steps
    return None


def _fill_params(steps: list, params: dict) -> list:
    """重放回填：动作文本里的 {参数名} 替换为新任务提取到的参数值。"""
    out = []
    for s in steps:
        s = dict(s)
        text = str(s.get("text", ""))
        for name, value in params.items():
            text = text.replace("{%s}" % name, value or "")
        s["text"] = text
        out.append(s)
    return out


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
    命中内置模板模式时按模板存储（同模板更新不堆积）；
    否则按字面存储，同任务（完全同文）已存在时更新而不是堆积。
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
    tmpl = _templatize(task, clean)
    if tmpl:
        tmpl_task, pattern, tmpl_steps = tmpl
        # 同模板已存在：沿用其占位化动作（新实例的动作文本可能与参数值不同步，
        # 重新生成会把旧参数值固化成字面量污染模板——实测踩过）
        existing = next((s for s in skills if s.get("pattern") == pattern), None)
        if existing:
            tmpl_steps = existing.get("steps", tmpl_steps)
        skills = [s for s in skills if s.get("pattern") != pattern]
        skills.append({"task": tmpl_task, "pattern": pattern,
                       "steps": tmpl_steps, "ts": int(time.time())})
        _save(skills)
        print("[skills] 已固化技能模板「%s」（%d 个动作）" % (tmpl_task, len(tmpl_steps)))
        return True
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
    按任务描述找可重放的技能，返回 (匹配到的任务原文, 动作序列)。
    模板正则优先：命中即提取参数回填动作（同类任务直接重放）；
    字面相似度兜底，低于阈值返回 None（宁缺毋滥，走正常 VLM 闭环）。
    """
    skills = _load()
    # 1. 模板匹配：新任务与参数正则匹配 -> 提取参数 -> 回填动作序列
    for s in skills:
        pattern = s.get("pattern")
        if not pattern:
            continue
        try:
            m = re.match(pattern, task)
        except re.error:
            continue
        if m:
            filled = _fill_params(s.get("steps", []), m.groupdict())
            print("[skills] 命中技能模板「%s」（参数 %s）"
                  % (s.get("task", ""), m.groupdict()))
            return s.get("task", task), filled
    # 2. 字面相似度（旧路径）
    best, best_sim = None, 0.0
    for s in skills:
        sim = _similarity(task, s.get("task", ""))
        if sim > best_sim:
            best, best_sim = s, sim
    if best is not None and best_sim >= _MATCH_THRESHOLD:
        print("[skills] 命中已固化技能「%s」（相似度 %.2f）"
              % (best["task"], best_sim))
        return best["task"], best["steps"]
    return None
