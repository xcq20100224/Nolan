# -*- coding: utf-8 -*-
"""
checkpoint.py —— 长任务续航：GUI 任务断点状态的持久化（B2）

第一性原理：长任务崩在第 8 步（VLM 抖动 / 误 FAILSAFE / 进程被杀）时，
前 7 步的物理成果——已打开的窗口、已输入的内容——还在屏幕上；
丢掉的只是「进行到哪一步」这个纯信息状态。信息状态的最小成本保险
就是落盘：每步结束写一次 JSON（毫秒级，相对一步秒级的 VLM 往返
可忽略），崩溃后从断点续跑，而不是一切从头再来。

契约：
    save(task, state)                  原子写入；state 至少含
                                       {step, history, done_steps,
                                        window_key, executed, ts}
    load(task)                         读取并校验有效期；过期 / 损坏 /
                                       不存在一律返回 None
    clear(task)                        任务终结（成功 / 彻底失败）后清除
    list_stale(max_age_hours=24)       列出过期检查点（巡检清理用）

存储：jarvis/data/checkpoints/ckpt_<task 的 sha256 前 16 位>.json，
目录自动创建；写入走「临时文件 + os.replace」原子替换——
进程在写入中途被杀也不会留下半个 JSON 让下次 load 读到脏数据。
"""

import hashlib
import json
import os
import time

# 检查点有效期：屏幕现场不会永远等人——窗口可能已关、内容可能已变，
# 超过 24 小时的断点按「没有检查点」处理，让任务从头再来反而更可靠
_MAX_AGE_SECONDS = 24 * 3600

_CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "checkpoints")


def _path(task: str) -> str:
    """任务 -> 检查点文件路径：稳定哈希做文件名，同一任务永远同一文件。"""
    digest = hashlib.sha256((task or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(_CKPT_DIR, "ckpt_%s.json" % digest)


def save(task, state: dict) -> bool:
    """
    原子写入检查点。state 里补 ts（调用方不给则以现在为准）与 task 原文
    （仅供人工排查时读文件名之外的线索）；任何异常返回 False——
    续航是保险丝，绝不能反过来炸断任务主链路。
    """
    try:
        os.makedirs(_CKPT_DIR, exist_ok=True)
        payload = dict(state or {})
        payload.setdefault("ts", time.time())
        payload["task"] = task or ""
        final = _path(task)
        tmp = final + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, final)  # 同目录原子替换，崩溃不留半文件
        return True
    except Exception as exc:
        print("[checkpoint] 保存失败（不影响任务继续）：%s" % exc)
        return False


def load(task):
    """
    读取检查点：不存在 / JSON 损坏 / 超过 24 小时有效期一律返回 None；
    过期时顺手删除文件，让「过期」与「没有」在磁盘上也一致。
    """
    try:
        path = _path(task)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return None
        ts = float(state.get("ts", 0) or 0)
        if time.time() - ts > _MAX_AGE_SECONDS:
            clear(task)
            return None
        return state
    except Exception:
        return None


def clear(task) -> bool:
    """删除该任务的检查点文件；不存在也算成功（幂等）。"""
    try:
        path = _path(task)
        if os.path.isfile(path):
            os.remove(path)
        return True
    except Exception:
        return False


def list_stale(max_age_hours: float = 24) -> list:
    """
    列出过期检查点：[{"path", "task", "age_hours"}, ...]。
    供巡检脚本定期清理；目录不存在或单项读取失败只缺该项，绝不抛异常。
    """
    stale = []
    try:
        if not os.path.isdir(_CKPT_DIR):
            return stale
        now = time.time()
        for fname in os.listdir(_CKPT_DIR):
            if not (fname.startswith("ckpt_") and fname.endswith(".json")):
                continue
            path = os.path.join(_CKPT_DIR, fname)
            try:
                age_h = (now - os.path.getmtime(path)) / 3600.0
                if age_h <= max_age_hours:
                    continue
                task = ""
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        task = str(json.load(f).get("task", ""))
                except Exception:
                    pass
                stale.append({"path": path, "task": task,
                              "age_hours": round(age_h, 2)})
            except Exception:
                continue
    except Exception:
        pass
    return stale
