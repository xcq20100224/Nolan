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
  {"task": 任务原文或模板, "steps": [...], "ts": 时间戳,
   "pattern": 参数正则(模板才有),
   "uses": 命中次数, "last_used": 最近命中时间,
   "ok": 重放成功次数, "fail": 重放失败次数}
上限 50 条，超出淘汰最旧（FIFO）——技能贵精不贵多。
统计字段全部是加法：旧格式文件（无这些字段）照常读入，缺省视为 0。

匹配：模板正则优先（参数化技能），字面二元组 Jaccard 兜底，阈值 0.6。
宁缺毋滥——匹配不到就走正常 VLM 闭环，绝不强行套旧技能。

模板化（P2）：固化时识别任务中的可变参数（如输入的文字内容），
技能存为模板「在记事本中输入文字：{内容}」+ 参数提取正则；
新任务命中模板即提取参数回填动作序列——同类任务一次学习终身重放，
技能库不再被同模式变体塞爆（实测 22 条技能全是同一模式的字面副本）。

生长（H4）：技能库从「被动积累死字面量」升级为「自动泛化、收敛、统计的活系统」。
第一性原理：技能的物理本质是「可参数化的成功轨迹」——两条轨迹只差一个
可变槽位（引号内容/文件名/数字）时，它们是同一类技能的两个实例，应当收敛
为一条模板，而不是各自占位、无限膨胀（库越大检索越慢的根因）。
  1. 自动参数化：record 时从任务文本抽取可变槽位（「引号内内容」/文件名/数字），
     生成锚定正则模板（槽位之外的字面量全部 re.escape 保留，宁严勿宽）；
  2. 相似收敛：新 record 与存量技能相似度 ≥ 0.9 时合并为一条（沿用更通用形态，
     累计统计），find 命中时累计使用次数；
  3. 使用统计：每模板 {uses,last_used,ok,fail}，record_outcome 接收重放成败反馈，
     stats() 返回库概况；
  4. 保守淘汰：stale 只是统计口径里的标记，只有显式 prune() 才真正删除；
  5. 契约零回退：record/find 签名与 find 默认阈值（0.6）不变，新功能全是加法。
"""

import json
import os
import re
import threading
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "memory", "skills.jsonl")
_MAX_SKILLS = 50
_MATCH_THRESHOLD = 0.6   # find 模糊命中阈值（契约，高考第 57 题直接验收，勿动）
_MERGE_THRESHOLD = 0.9   # record 相似收敛阈值：≥ 此值视为同类实例，合并而非新增
_STALE_AGE_DAYS = 30     # 保守淘汰口径：距今超过该天数
_STALE_MIN_USES = 2      # 且命中次数低于该值，才算「可淘汰」

_LOCK = threading.RLock()  # 读写串行化：find 也会写（累计命中次数），必须加锁

# 参与固化的物理动作（wait/done/fail 只是过程量，不进技能）
_REPLAYABLE = ("left_click", "double_click", "type", "key", "scroll")

# 内置模板模式：高频任务族的参数抽取规则
# （任务正则含命名参数组 -> 模板任务文本；命中时动作文本中的参数值占位化）
_TEMPLATE_RULES = (
    (re.compile(r"^(?:在)?记事本(?:中|里)?(?:输入|写上|写入)(?:文字)?[:：]?(?P<内容>.+)$"),
     "在记事本中输入文字：{内容}"),
)

# 自动参数化槽位识别（H4）：引号内内容 / 带扩展名的文件名 / 数字
# 顺序即优先级：引号整体先于内部数字被消费，文件名先于其内数字被消费
_SLOT_REGEX = re.compile(
    r"「(?P<q1>[^」]+)」"
    r"|“(?P<q2>[^”]+)”"
    r"|\"(?P<q3>[^\"]+)\""
    r"|'(?P<q4>[^']+)'"
    r"|(?P<file>[\w\-]+\.[A-Za-z0-9]{1,8})"
    r"|(?P<num>\d+(?:\.\d+)?)"
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


def _placeholderize(steps: list, params: dict) -> list:
    """把动作文本中出现的参数值替换为 {槽位}。
    数字槽位加边界守卫：值 "5" 不许把文本 "15" 污染成 "1{槽1}"。"""
    out = []
    for s in steps:
        s = dict(s)
        text = str(s.get("text", ""))
        for name, value in params.items():
            if not value:
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?", value):
                text = re.sub(r"(?<!\d)" + re.escape(value) + r"(?!\d)",
                              "{%s}" % name, text)
            elif value in text:
                text = text.replace(value, "{%s}" % name)
        s["text"] = text
        out.append(s)
    return out


def _auto_templatize(task: str, steps: list) -> tuple | None:
    """自动参数化（H4 核心）：从任务文本抽取可变槽位，生成
    (模板任务, 全锚定参数正则, 占位化动作)；没有槽位或字面锚太短则 None。

    策略一句话：引号内容/文件名/数字 → 命名槽位，其余字面量全部 re.escape
    保留并 ^...$ 全串锚定——模板只放宽「被证可变的部分」，绝不整段 .*，
    因此误命中风险不比字面匹配高（第 57 题「不误命中」契约的前提）。
    """
    slots = []
    for m in _SLOT_REGEX.finditer(task):
        gd = m.groupdict()
        kind = "quoted" if any(gd.get("q%d" % i) for i in range(1, 5)) else \
               ("file" if gd.get("file") else "num")
        slots.append((m.start(), m.end(), kind, m.group()))
    if not slots:
        return None
    # 字面锚检查：抠掉槽位后剩下的字面骨架至少 2 个实义字符，
    # 否则模板近似「匹配一切」，宁可按字面存储
    skeleton = _SLOT_REGEX.sub("", task)
    if len(re.sub(r"[\W_]", "", skeleton)) < 2:
        return None
    tmpl_parts, regex_parts, params = [], [], {}
    pos = 0
    for i, (s0, s1, kind, raw) in enumerate(slots, 1):
        name = "槽%d" % i
        tmpl_parts.append(task[pos:s0])
        regex_parts.append(re.escape(task[pos:s0]))
        if kind == "quoted":
            # 引号本身保留为字面锚，只参数化引号内内容
            open_q, inner, close_q = raw[0], raw[1:-1], raw[-1]
            tmpl_parts.append(open_q + "{%s}" % name + close_q)
            regex_parts.append(re.escape(open_q) + "(?P<%s>.+?)" % name
                               + re.escape(close_q))
            params[name] = inner
        elif kind == "file":
            tmpl_parts.append("{%s}" % name)
            regex_parts.append("(?P<%s>[\\w.\\-]+)" % name)
            params[name] = raw
        else:
            tmpl_parts.append("{%s}" % name)
            regex_parts.append("(?P<%s>\\d+(?:\\.\\d+)?)" % name)
            params[name] = raw
        pos = s1
    tmpl_parts.append(task[pos:])
    regex_parts.append(re.escape(task[pos:]))
    return "".join(tmpl_parts), "^" + "".join(regex_parts) + "$", \
        _placeholderize(steps, params)


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
    """读取技能库。向后兼容：旧格式行（无 uses/ok/fail 等统计字段）
    照常解析，统计字段一律经 .get 取缺省值，绝不 KeyError。"""
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _save(skills: list) -> None:
    """原子写入：先写临时文件再 os.replace——写一半断电/崩溃时，
    技能库要么是新版本要么是旧版本，绝不出现半截 JSONL。"""
    tmp = _PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            for s in skills[-_MAX_SKILLS:]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        os.replace(tmp, _PATH)
    except OSError as exc:
        print("[skills] 写入失败（跳过固化）：%s" % exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _touch(skill: dict) -> None:
    """find 命中记账：累计使用次数与最近命中时间。"""
    skill["uses"] = int(skill.get("uses", 0) or 0) + 1
    skill["last_used"] = int(time.time())


def _carry_stats(dst: dict, src: dict) -> None:
    """合并时继承统计：uses/ok/fail 累加，last_used 取较新者。"""
    for k in ("uses", "ok", "fail"):
        dst[k] = int(dst.get(k, 0) or 0) + int(src.get(k, 0) or 0)
    dst["last_used"] = max(int(dst.get("last_used", 0) or 0),
                           int(src.get("last_used", 0) or 0))


def record(task: str, steps: list) -> bool:
    """
    固化一条技能：任务原文 + 可重放动作序列。
    只保留 _REPLAYABLE 动作；少于 1 个有效动作不固化（没什么可学的）。
    三级收敛（每层都是「同类归一」，拒绝无限新增）：
      1. 命中内置模板模式 -> 按模板存储（同模板更新不堆积，统计继承）；
      2. 自动参数化抽出槽位 -> 存锚定正则模板，同 pattern 模板或被该
         pattern 覆盖的存量字面实例全部并入口（统计累计）；
      3. 纯字面 -> 与存量字面技能相似度 ≥ 0.9 时合并（沿用先到的通用
         形态），完全同文则更新动作（旧语义），否则才新增。
    """
    task = (task or "").strip()
    clean = [{"action": s["action"],
              "text": s.get("text", ""),
              "keys": s.get("keys", "")}
             for s in (steps or [])
             if isinstance(s, dict) and s.get("action") in _REPLAYABLE]
    if not task or not clean:
        return False
    with _LOCK:
        skills = _load()
        now = int(time.time())
        # —— 第 1 级：内置模板模式（原有行为，统计字段继承） ——
        tmpl = _templatize(task, clean)
        if tmpl:
            tmpl_task, pattern, tmpl_steps = tmpl
            # 同模板已存在：沿用其占位化动作（新实例的动作文本可能与参数值不同步，
            # 重新生成会把旧参数值固化成字面量污染模板——实测踩过）
            existing = next((s for s in skills if s.get("pattern") == pattern),
                            None)
            entry = {"task": tmpl_task, "pattern": pattern,
                     "steps": existing.get("steps", tmpl_steps) if existing
                     else tmpl_steps,
                     "ts": now,
                     "uses": 0, "last_used": 0, "ok": 0, "fail": 0}
            if existing:
                _carry_stats(entry, existing)
            skills = [s for s in skills if s.get("pattern") != pattern]
            skills.append(entry)
            _save(skills)
            print("[skills] 已固化技能模板「%s」（%d 个动作）"
                  % (tmpl_task, len(entry["steps"])))
            return True
        # —— 第 2 级：自动参数化收敛（H4） ——
        auto = _auto_templatize(task, clean)
        if auto:
            tmpl_task, pattern, tmpl_steps = auto
            entry = {"task": tmpl_task, "pattern": pattern,
                     "steps": tmpl_steps, "ts": now,
                     "uses": 0, "last_used": 0, "ok": 0, "fail": 0}
            rest = []
            for s in skills:
                if s.get("pattern") == pattern:
                    # 同族模板：沿用其占位动作（同第 1 级的防污染理由）
                    entry["steps"] = s.get("steps", tmpl_steps)
                    _carry_stats(entry, s)
                    continue
                if not s.get("pattern"):
                    try:
                        absorbed = bool(re.match(pattern, s.get("task", "")))
                    except re.error:
                        absorbed = False
                    if absorbed:
                        # 存量字面实例是本模板的同类：并入口，统计累计
                        _carry_stats(entry, s)
                        continue
                rest.append(s)
            rest.append(entry)
            _save(rest)
            print("[skills] 已自动泛化技能模板「%s」（%d 个动作）"
                  % (tmpl_task, len(tmpl_steps)))
            return True
        # —— 第 3 级：字面相似收敛（H4：≥0.9 合并；同文更新为旧语义） ——
        best, best_sim = None, 0.0
        for s in skills:
            if s.get("pattern"):
                continue
            sim = _similarity(task, s.get("task", ""))
            if sim > best_sim:
                best, best_sim = s, sim
        if best is not None and best_sim >= _MERGE_THRESHOLD:
            if best.get("task") == task:
                best["steps"] = clean  # 同文重复固化：更新动作（旧语义）
            best["ts"] = now
            _save(skills)
            print("[skills] 相似收敛「%s」（相似度 %.2f，合并不新增）"
                  % (best.get("task", task), best_sim))
            return True
        skills.append({"task": task, "steps": clean, "ts": now,
                       "uses": 0, "last_used": 0, "ok": 0, "fail": 0})
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
    命中会累计该技能的使用次数（uses/last_used）——只加统计，
    匹配语义与阈值（0.6）与旧版完全一致。
    """
    with _LOCK:
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
                _touch(s)
                _save(skills)
                print("[skills] 命中技能模板「%s」（参数 %s，第 %d 次）"
                      % (s.get("task", ""), m.groupdict(), s.get("uses", 0)))
                return s.get("task", task), filled
        # 2. 字面相似度（旧路径）
        best, best_sim = None, 0.0
        for s in skills:
            sim = _similarity(task, s.get("task", ""))
            if sim > best_sim:
                best, best_sim = s, sim
        if best is not None and best_sim >= _MATCH_THRESHOLD:
            _touch(best)
            _save(skills)
            print("[skills] 命中已固化技能「%s」（相似度 %.2f，第 %d 次）"
                  % (best["task"], best_sim, best.get("uses", 0)))
            return best["task"], best["steps"]
    return None


def _locate(skills: list, task: str) -> dict | None:
    """定位一条技能（feedback 用）：模板正则 -> 完全同文 -> 相似度达标。"""
    for s in skills:
        pattern = s.get("pattern")
        if not pattern:
            continue
        try:
            if re.match(pattern, task):
                return s
        except re.error:
            continue
    best, best_sim = None, 0.0
    for s in skills:
        sim = _similarity(task, s.get("task", ""))
        if sim > best_sim:
            best, best_sim = s, sim
    if best is not None and best_sim >= _MATCH_THRESHOLD:
        return best
    return None


def record_outcome(task: str, success: bool = True) -> bool:
    """重放结果反馈入口（H4 加法）：任务执行方在重放结束后上报成败，
    累计到对应技能的 ok/fail，success_rate 由此而来。
    找不到对应技能返回 False（不造数据）。
    """
    task = (task or "").strip()
    if not task:
        return False
    with _LOCK:
        skills = _load()
        target = _locate(skills, task)
        if target is None:
            return False
        key = "ok" if success else "fail"
        target[key] = int(target.get(key, 0) or 0) + 1
        _save(skills)
        print("[skills] 技能「%s」重放%s（%d 胜 %d 负）"
              % (target.get("task", task), "成功" if success else "失败",
                 target.get("ok", 0), target.get("fail", 0)))
        return True


def _is_stale(skill: dict, now: int,
              max_age_days: int = _STALE_AGE_DAYS,
              min_uses: int = _STALE_MIN_USES) -> bool:
    """保守淘汰口径（只标记）：长期未用 且 命中次数低。
    「未用」取固化时间与最后命中时间的较新者——刚用过的不算。"""
    base = max(int(skill.get("ts", 0) or 0),
               int(skill.get("last_used", 0) or 0))
    return (int(skill.get("uses", 0) or 0) < min_uses
            and now - base > max_age_days * 86400)


def stats() -> dict:
    """技能库概况（H4 加法，只读）：总数、模板/字面构成、累计命中、
    可淘汰数量，以及每条技能的 uses/success_rate/stale 明细。
    success_rate 无反馈数据时为 None（未知，而不是 0 或 1 的假象）。"""
    with _LOCK:
        skills = _load()
    now = int(time.time())
    items = []
    for s in skills:
        ok = int(s.get("ok", 0) or 0)
        fail = int(s.get("fail", 0) or 0)
        items.append({
            "task": s.get("task", ""),
            "kind": "template" if s.get("pattern") else "literal",
            "uses": int(s.get("uses", 0) or 0),
            "last_used": int(s.get("last_used", 0) or 0) or None,
            "success_rate": ok / (ok + fail) if ok + fail else None,
            "stale": _is_stale(s, now),
        })
    return {
        "total": len(items),
        "templates": sum(1 for i in items if i["kind"] == "template"),
        "literals": sum(1 for i in items if i["kind"] == "literal"),
        "total_uses": sum(i["uses"] for i in items),
        "stale": sum(1 for i in items if i["stale"]),
        "skills": items,
    }


def prune(max_age_days: int = _STALE_AGE_DAYS,
          min_uses: int = _STALE_MIN_USES,
          dry_run: bool = False) -> list:
    """显式淘汰（H4 加法）：删除「长期未用且低命中」的技能，返回被淘汰的
    任务名列表。保守设计：stale 标记在 stats() 里随时可看，但只有显式
    调用本函数才真正删除；dry_run=True 时只预演不动库。
    """
    with _LOCK:
        skills = _load()
        now = int(time.time())
        doomed = [s for s in skills
                  if _is_stale(s, now, max_age_days, min_uses)]
        if doomed and not dry_run:
            doomed_ids = {id(s) for s in doomed}  # 按身份而非值比较，避免误删同值条目
            kept = [s for s in skills if id(s) not in doomed_ids]
            _save(kept)
            print("[skills] 淘汰 %d 条低价值技能：%s"
                  % (len(doomed), "、".join(s.get("task", "") for s in doomed)))
        return [s.get("task", "") for s in doomed]
