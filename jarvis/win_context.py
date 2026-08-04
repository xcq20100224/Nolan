# -*- coding: utf-8 -*-
"""
win_context.py —— Nolan 的「窗口上下文记忆」（Gap5：不再每次都从「这是哪里」开始）

第一性原理：GUI 自动化的每一步推理都依赖上下文，而同一软件同一窗口的上下文
是高度可复用的物理事实——记事本的编辑框永远在那里，网易云「我喜欢的音乐」
入口从不挪窝，上次「坐标点击点空、按名吸附成功」的教训下次依然成立。
但 VLM 每次睁眼都是零上下文：不知道这个窗口有哪些已知控件、上次哪种定位
策略有效、哪个坑上次踩过。把「每次重新理解世界」换成「带着履历回到现场」，
可靠性从「单步掷骰子」向「熟手回工位」移动。

与 skills.py 的边界（互补，不重复造）：
  * skills.py   固化「任务级动作模板」——这类任务按什么顺序做哪几步；
  * win_context 沉淀「窗口级现场履历」——这个窗口里有哪些已知控件、
    上次成功的定位策略、上次失败的物理教训。粒度更细，随用随取，
    不需要任务整体命中也能提供增量信息。

设计边界（记忆模块自身的可靠性）：
  * 零 GUI 依赖：不 import pyautogui/uia/comtypes，控件/成败证据由调用方
    （eyes/reliability）收集注入，本模块只做存取与摘要——可在无真机环境
    用临时目录全量回归（test_win_context.py）；
  * 绝不抛给调用方：记忆是增强不是主路，任何异常（坏输入/坏磁盘/坏 JSON）
    都就地吞掉返回安全默认，增强失效时主流程行为一字不变；
  * JSON 损坏自动备份重开（.corrupt 后缀），写入原子化（临时文件 + os.replace），
    全模块一把线程锁，单次读写毫秒级；
  * 容量纪律：控件 50/窗口（淘汰最久未见），成败各 10 条/窗口（滚动淘汰），
    整文件超 200KB 按 last_ts 淘汰最旧窗口——记忆贵精不贵多。

存储：jarvis/data/win_context.json（目录不存在自动创建，可人工检查）
  {window_key: {
     "known_controls": [{"name", "type", "last_seen", "seen_count"}...],
     "successes":      [{"action", "target", "strategy", "ts"}...],
     "failures":       [{"action", "error_class", "lesson", "ts"}...],
     "last_task": 任务原文,
     "last_ts":   最后活跃时间戳}}
  window_key 由 make_key(进程名, 窗口标题) 正则化生成（数字易变，占位化）。

接口契约（签名固定，主控按此集成）：
    make_key(process_name, window_title) -> str          进程+标题 -> 窗口标识
    record_controls(window_key, controls)                控件清单增量登记
    record_success(window_key, action, target, strategy) 成功定位登记
    record_failure(window_key, action, error_class, lesson) 失败教训登记
    get_context(window_key) -> dict | None               取某窗口全部上下文
    brief(window_key, max_chars=300) -> str              注入 VLM prompt 的简报
    prune(max_age_days=30) -> int                        清理过期窗口记录
"""

import json
import os
import re
import threading
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "win_context.json")

_LOCK = threading.RLock()  # 全模块一把锁：读-改-写序列原子化

# 容量纪律
_MAX_CONTROLS = 50            # 每窗口已知控件上限（淘汰最久未见的）
_MAX_EPISODES = 10            # 每窗口 successes/failures 各自上限（滚动 FIFO）
_MAX_FILE_BYTES = 200 * 1024  # 整文件上限，超出按 last_ts 淘汰最旧窗口

# 失败类别白名单——对齐 reliability.py 的七分类（不在列的一律归 unknown）
_VALID_ERROR_CLASSES = frozenset((
    "focus_lost", "app_not_responding", "target_missing", "coord_drift",
    "text_mismatch", "timeout", "unknown",
))

# 失败类别中文名（brief 用）
_ERROR_CLASS_CN = {
    "focus_lost": "焦点丢失",
    "app_not_responding": "应用未响应",
    "target_missing": "目标未出现",
    "coord_drift": "坐标漂移",
    "text_mismatch": "文本校验不符",
    "timeout": "超时",
    "unknown": "未知原因",
}


# ---------------------------------------------------------------------------
# 窗口标识：进程名 + 标题正则化
# ---------------------------------------------------------------------------

def make_key(process_name, window_title):
    """
    生成窗口标识。标题中的数字一律占位化为 '#'——页码/未读数/进度百分比
    每次都在变，而「这是同一个窗口」才是可复用的物理事实。
    异常输入返回空串（调用方应跳过登记，本模块也拒绝空 key 的写入）。
    """
    try:
        proc = re.sub(r"\s+", "", str(process_name or "")).lower()
        title = re.sub(r"\s+", " ", str(window_title or "").strip())
        title = re.sub(r"\d+", "#", title)[:40]
        if not proc and not title:
            return ""
        return (proc + "|" + title)[:80]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 底层存取：损坏自愈 + 原子写入（内部函数，假设已持锁）
# ---------------------------------------------------------------------------

def _load() -> dict:
    """读全量记忆。JSON 损坏时备份为 .corrupt.<ts> 并重开空库——绝不抛。"""
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # ValueError 涵盖 JSONDecodeError
        try:
            backup = "%s.corrupt.%d" % (_PATH, int(time.time()))
            os.replace(_PATH, backup)  # 留证可人工检查，不静默吞数据
            print("[win_context] 记忆文件损坏，已备份 %s（%s）" % (backup, exc))
        except OSError:
            pass
        return {}


def _save(data: dict) -> None:
    """原子写入：先写同目录临时文件再 os.replace——写一半崩了，
    旧文件依然完整（replace 在同卷上是原子操作）。"""
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _PATH)
    except OSError as exc:
        print("[win_context] 写入失败（跳过本次登记）：%s" % exc)


def _shrink_to_budget(data: dict) -> dict:
    """整文件超预算时按 last_ts 淘汰最旧窗口，直到达标或只剩空库。"""
    try:
        while data and len(json.dumps(data, ensure_ascii=False)
                         .encode("utf-8")) > _MAX_FILE_BYTES:
            oldest = min(data, key=lambda k: data[k].get("last_ts", 0)
                         if isinstance(data[k], dict) else 0)
            del data[oldest]
    except Exception:
        pass
    return data


def _window(data: dict, key: str) -> dict:
    """取/建某窗口的记录槽位，并刷新 last_ts。"""
    win = data.get(key)
    if not isinstance(win, dict):
        win = {}
        data[key] = win
    win.setdefault("known_controls", [])
    win.setdefault("successes", [])
    win.setdefault("failures", [])
    win.setdefault("last_task", "")
    win["last_ts"] = time.time()
    return win


# ---------------------------------------------------------------------------
# 登记 API（调用方注入证据；全部静默容错，返回 bool 表示是否落盘）
# ---------------------------------------------------------------------------

def record_controls(window_key, controls) -> bool:
    """
    控件清单增量登记：见过的 seen_count+1 并刷新 last_seen；新面孔建档。
    controls 接受 [{"name","type"}...] 或 ["控件名"...]（字符串视为无名类型）。
    超 _MAX_CONTROLS 时淘汰最久未见的——常客留下，过客放行。
    """
    try:
        key = str(window_key or "")
        if not key or not isinstance(controls, (list, tuple)):
            return False
        with _LOCK:
            data = _load()
            win = _window(data, key)
            known = [c for c in win["known_controls"] if isinstance(c, dict)]
            now = time.time()
            index = {str(c.get("name", "")): c for c in known}
            for item in controls:
                if isinstance(item, dict):
                    name = str(item.get("name", "") or "")[:60]
                    ctype = str(item.get("type", "") or "")[:30]
                else:
                    name, ctype = str(item or "")[:60], ""
                if not name:
                    continue
                hit = index.get(name)
                if hit is None:
                    hit = {"name": name, "type": ctype,
                           "last_seen": now, "seen_count": 0}
                    known.append(hit)
                    index[name] = hit
                hit["seen_count"] = int(hit.get("seen_count", 0)) + 1
                hit["last_seen"] = now
                if ctype:
                    hit["type"] = ctype
            # 容量淘汰：最久未见的先走
            known.sort(key=lambda c: c.get("last_seen", 0), reverse=True)
            win["known_controls"] = known[:_MAX_CONTROLS]
            _save(_shrink_to_budget(data))
        return True
    except Exception as exc:
        print("[win_context] record_controls 异常（已吞）：%s" % exc)
        return False


def record_success(window_key, action, target, strategy) -> bool:
    """
    成功定位登记：哪个动作、对什么目标、用了哪种定位策略
    （如「UIA按名吸附」「坐标点击」）。滚动保留最近 _MAX_EPISODES 条。
    """
    try:
        key = str(window_key or "")
        action = str(action or "")[:80]
        if not key or not action:
            return False
        with _LOCK:
            data = _load()
            win = _window(data, key)
            win["successes"] = (win["successes"] + [{
                "action": action,
                "target": str(target or "")[:60],
                "strategy": str(strategy or "")[:40],
                "ts": time.time(),
            }])[-_MAX_EPISODES:]
            _save(_shrink_to_budget(data))
        return True
    except Exception as exc:
        print("[win_context] record_success 异常（已吞）：%s" % exc)
        return False


def record_failure(window_key, action, error_class, lesson) -> bool:
    """
    失败教训登记：哪个动作、reliability 归到哪类、留下什么教训。
    error_class 不在七类白名单内一律归 unknown——教训可以模糊，类别不许编造。
    """
    try:
        key = str(window_key or "")
        action = str(action or "")[:80]
        if not key or not action:
            return False
        cls = str(error_class or "")
        if cls not in _VALID_ERROR_CLASSES:
            cls = "unknown"
        with _LOCK:
            data = _load()
            win = _window(data, key)
            win["failures"] = (win["failures"] + [{
                "action": action,
                "error_class": cls,
                "lesson": str(lesson or "")[:120],
                "ts": time.time(),
            }])[-_MAX_EPISODES:]
            _save(_shrink_to_budget(data))
        return True
    except Exception as exc:
        print("[win_context] record_failure 异常（已吞）：%s" % exc)
        return False


def record_task(window_key, task) -> bool:
    """登记该窗口最近在处理的任务原文（last_task，供 brief 还原现场）。"""
    try:
        key = str(window_key or "")
        task = str(task or "")[:120]
        if not key or not task:
            return False
        with _LOCK:
            data = _load()
            win = _window(data, key)
            win["last_task"] = task
            _save(_shrink_to_budget(data))
        return True
    except Exception as exc:
        print("[win_context] record_task 异常（已吞）：%s" % exc)
        return False


# ---------------------------------------------------------------------------
# 读取 API
# ---------------------------------------------------------------------------

def get_context(window_key):
    """取某窗口的全部上下文（深拷贝，调用方改不坏库存）。无记录返回 None。"""
    try:
        key = str(window_key or "")
        if not key:
            return None
        with _LOCK:
            win = _load().get(key)
            if not isinstance(win, dict):
                return None
            return json.loads(json.dumps(win, ensure_ascii=False))
    except Exception as exc:
        print("[win_context] get_context 异常（已吞）：%s" % exc)
        return None


def brief(window_key, max_chars=300) -> str:
    """
    生成注入 VLM prompt 的上下文简报：已知控件（常客优先）+ 最近一次成功的
    定位策略 + 最近失败的教训 + 上次任务。长度硬约束 max_chars，宁截不溢。
    无记录返回空串（调用方拼 prompt 时零成本跳过）。
    """
    try:
        cap = int(max_chars) if int(max_chars) > 20 else 300
        win = get_context(window_key)
        if not win:
            return ""
        parts = []
        ctrls = [c for c in win.get("known_controls", [])
                 if isinstance(c, dict) and c.get("name")]
        if ctrls:
            # 常客优先（seen_count），同频按最近见过排序
            ctrls.sort(key=lambda c: (c.get("seen_count", 0),
                                      c.get("last_seen", 0)), reverse=True)
            names = "、".join(str(c["name"]) for c in ctrls[:8])
            parts.append("本窗口已知控件：%s（共%d个）" % (names, len(ctrls)))
        succ = [s for s in win.get("successes", []) if isinstance(s, dict)]
        if succ:
            s = succ[-1]
            seg = "上次成功：用%s%s" % (s.get("strategy") or "未知策略",
                                       s.get("action") or "操作")
            if s.get("target"):
                seg += "『%s』" % s["target"]
            parts.append(seg)
        fail = [f for f in win.get("failures", []) if isinstance(f, dict)]
        if fail:
            f = fail[-1]
            cls_cn = _ERROR_CLASS_CN.get(f.get("error_class"), "未知原因")
            lesson = str(f.get("lesson") or "")
            # lesson 已含类别描述时不重复冠头，避免「曾坐标漂移，曾坐标漂移…」
            seg = "教训：" + (lesson if cls_cn in lesson
                              else "曾%s%s" % (cls_cn,
                                               "，" + lesson if lesson else ""))
            parts.append(seg)
        if win.get("last_task"):
            parts.append("上次任务：%s" % win["last_task"])
        text = "；".join(parts)
        if len(text) > cap:  # 硬约束：截断保头，尾注省略
            text = text[:cap - 1] + "…"
        return text
    except Exception as exc:
        print("[win_context] brief 异常（已吞）：%s" % exc)
        return ""


def prune(max_age_days=30) -> int:
    """清理 last_ts 超过 max_age_days 的窗口记录，返回清理条数。"""
    try:
        days = float(max_age_days)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with _LOCK:
            data = _load()
            stale = [k for k, v in data.items()
                     if not isinstance(v, dict) or v.get("last_ts", 0) < cutoff]
            for k in stale:
                del data[k]
            if stale:
                _save(data)
            return len(stale)
    except Exception as exc:
        print("[win_context] prune 异常（已吞）：%s" % exc)
        return 0
