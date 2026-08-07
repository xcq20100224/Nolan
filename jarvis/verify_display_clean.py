# -*- coding: utf-8 -*-
"""任务 A 验收：真实样本走 server._display_clean 显示层终态剥离，打印前后对比。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nolan-web"))
import server  # noqa: E402

SAMPLES = [
    ("截图真实样本（人话+行尾工具JSON混排）",
     '先生，明白。我将重新制作一份内容更充耳的版本，每页正文扩充为多段要点，'
     '确保信息密度达标。请稍候。 {"tool": "make_ppt", "args": {"topic": '
     '"人工智能：从技术原理到产业实践的全面解析", "pages": 10, "style": "科普分享"}}'),
    ("纯工具调用（无台词→占位）",
     '{"tool": "make_ppt", "args": {"topic": "AI", "pages": 10}}'),
    ("纯人话（应原样通过）",
     "好的先生，PPT 已经做好并放进文件柜了，共 10 页。"),
]

for label, raw in SAMPLES:
    cleaned = server._display_clean(raw)
    print(f"--- {label}")
    print(f"剥离前: {raw}")
    print(f"剥离后: {cleaned}")
    print()
