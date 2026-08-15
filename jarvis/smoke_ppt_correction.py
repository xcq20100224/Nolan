# -*- coding: utf-8 -*-
"""PPT 纠正轮端到端复现（真机·走网页后端 SSE）。

按 2026-08-08 截图病例打两轮：
  轮 1：「给我做一个关于端午节的PPT，3页」→ 应生成 PPT（progress 事件 >= 3）
  轮 2：「不对，站在历史老师的视角，讲给初中生听」
        → 修复前：Nolan 在聊天里写一段文稿（无 progress，无新文件）
        → 修复后：确定性路由重做 PPT（progress 事件 >= 3，话题含「端午节」「历史老师」）

验收：两轮都出现 >= 3 条 progress，且轮 2 收尾话术提到 PPT/文件柜。
运行：先起 server.py（7901），再 python smoke_ppt_correction.py
"""
import json
import sys
import time

import httpx

URL = "http://localhost:7901/api/chat/stream"


def run_turn(text: str):
    events = []
    t0 = time.time()
    with httpx.stream("POST", URL, json={"text": text}, timeout=280) as r:
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            events.append(ev)
            if ev.get("type") == "progress":
                print(f"  [{time.time()-t0:5.1f}s] progress: {ev.get('step')}")
            elif ev.get("type") in ("fallback", "done"):
                tail = (ev.get("reply") or ev.get("text") or "")[:60]
                print(f"  [{time.time()-t0:5.1f}s] {ev['type']}: {tail}")
    return events


def main():
    print("轮 1：给我做一个关于端午节的PPT，3页")
    ev1 = run_turn("给我做一个关于端午节的PPT，3页")
    p1 = sum(1 for e in ev1 if e.get("type") == "progress")

    print("轮 2：不对，站在历史老师的视角，讲给初中生听")
    ev2 = run_turn("不对，站在历史老师的视角，讲给初中生听")
    p2 = sum(1 for e in ev2 if e.get("type") == "progress")
    fin2 = next((e for e in reversed(ev2) if e.get("type") in ("fallback", "done")), {})
    text2 = fin2.get("reply") or fin2.get("text") or ""

    ok = p1 >= 3 and p2 >= 3 and ("PPT" in text2 or "文件柜" in text2)
    print(f"\n轮1 progress={p1} | 轮2 progress={p2} | 轮2收尾提到成品={'是' if ('PPT' in text2 or '文件柜' in text2) else '否'}")
    print("验收:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
