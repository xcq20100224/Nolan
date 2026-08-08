# -*- coding: utf-8 -*-
"""
Nolan · 通用进度总线（progress.py）

为什么存在：长任务（make_ppt 生成 PPT 约 2 分钟）执行期间，调用方（网页后端
SSE 链路）需要把执行进度实时推给前端，否则用户只能空等。本模块是「生产者
（任何模块）→ 消费者（server 流式路径）」之间的解耦总线：生产者只管 emit，
有没有订阅者不感知；消费者 begin 后周期 drain 取走事件。

设计三原则：
  1. 绝不抛异常——emit/begin/end/drain 任何路径都静默兜底，进度是锦上添花，
     绝不能反过来拖垮主流程；
  2. 无订阅零开销——未 begin 时 emit 只读一个模块级布尔就返回（CPython 里
     单次原子读），生产者可随意埋点、不计成本；
  3. 线程安全——queue.Queue 承载事件、一把锁保护生命周期切换；生产者与
     消费者天然跑在不同线程（server 的工具执行线程 vs SSE 处理器线程）。

API 契约：
    begin()                清空残留并开始收集（消费者进入订阅态）
    end()                  停止收集（之后 emit 回到零开销空操作；队列残留由下次 begin 清）
    emit(step, i=None, n=None)  投递一条进度：step 人话短文案（≤30字），
                                i/n 可选计数（无计数时省略）
    drain() -> list[dict]  原子地取出全部待发事件（按 emit 先后排序），
                           元素形如 {"step": str, "i"?: int, "n"?: int}
    is_active() -> bool    当前是否处于订阅态（测试/调试辅助）
"""
from __future__ import annotations

import queue
import threading

# 事件队列：queue.Queue 本身线程安全（内部有锁），多生产者 emit 不丢不错序
_q: "queue.Queue" = queue.Queue()
# 生命周期锁：begin/end 与 emit 的「活跃判定 + 入队」临界区共用一把锁，
# 保证 begin 的清空与后续 emit 不错位
_lock = threading.Lock()
# 订阅态标志：模块级布尔赋值/读取在 CPython 下原子，热路径无锁快判
_active = False

# step 文案防御性截断上限（契约 ≤30 字，总线侧留一倍余量兜底）
_MAX_STEP_CHARS = 60


def begin() -> None:
    """开始一次订阅周期：清空上轮残留事件，置订阅态。任何异常静默。"""
    global _active
    try:
        with _lock:
            while True:
                try:
                    _q.get_nowait()
                except queue.Empty:
                    break
            _active = True
    except Exception:
        pass


def end() -> None:
    """结束订阅周期：退订阅态。队列里未取走的事件由下次 begin 清空，不泄漏。"""
    global _active
    try:
        with _lock:
            _active = False
    except Exception:
        pass


def is_active() -> bool:
    """当前是否有订阅者（begin 之后、end 之前为 True）。"""
    return _active


def emit(step: str, i=None, n=None) -> None:
    """投递一条进度事件。绝不抛异常；无订阅时零开销空操作。

    step：人话短文案（≤30 字，超限防御性截断）；
    i/n：可选计数（如第 3/10 页），非整数可转则转、转不动静默丢弃该字段。
    """
    if not _active:
        return  # 无订阅：单次原子读即返回，零开销
    try:
        with _lock:
            if not _active:   # 锁内复检：与并发的 end() 赛跑时宁可丢一条
                return
            ev = {"step": str(step or "").strip()[:_MAX_STEP_CHARS]}
            if not ev["step"]:
                return  # 空文案没有意义，不进队列
            if i is not None:
                try:
                    ev["i"] = int(i)
                except (TypeError, ValueError):
                    pass
            if n is not None:
                try:
                    ev["n"] = int(n)
                except (TypeError, ValueError):
                    pass
            _q.put(ev)
    except Exception:
        pass


def drain() -> list:
    """原子地取走当前队列里的全部事件（按入队先后排序）；无事件返回 []。
    任何异常静默返回已取到的部分。"""
    out = []
    try:
        while True:
            try:
                out.append(_q.get_nowait())
            except queue.Empty:
                break
    except Exception:
        pass
    return out
