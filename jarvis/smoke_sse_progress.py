# -*- coding: utf-8 -*-
"""端到端联调：SSE 客户端实测 PPT 生成进度流（一次性脚本）"""
import json
import time
import urllib.request

t0 = time.time()
req = urllib.request.Request(
    "http://localhost:7101/api/chat/stream",
    data=json.dumps({"text": "帮我做一个关于咖啡的 PPT，3 页"}).encode(),
    headers={"Content-Type": "application/json"})
events = []
with urllib.request.urlopen(req, timeout=280) as r:
    buf = b""
    while True:
        chunk = r.read(1)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\n\n"):
            line = buf.decode("utf-8", "replace").strip()
            buf = b""
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = d.get("type", "?")
                events.append(t)
                if t == "progress":
                    print("[%.1fs] progress: %s" % (time.time() - t0, d.get("step")))
                elif t in ("done", "fallback", "error"):
                    print("[%.1fs] %s: %s" % (time.time() - t0, t,
                          str(d.get("reply") or d.get("error") or "")[:80]))
                    break
n_prog = events.count("progress")
print("---")
print("progress 事件数:", n_prog, "| 事件序列:", events[:5], "...", events[-3:] if len(events) > 5 else "")
print("总耗时: %.1fs" % (time.time() - t0))
print("验收:", "PASS" if n_prog >= 5 and events[-1] in ("fallback", "done") else "FAIL")
