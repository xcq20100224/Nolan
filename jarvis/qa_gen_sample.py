# -*- coding: utf-8 -*-
"""QA 用：真实调用 Nolan PPT 引擎生成《一元二次方程》教学课件（含 CogView 配图）。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ppt_maker

t0 = time.time()
r = ppt_maker.make_ppt(
    "一元二次方程解法课件（给初三学生上课用）",
    pages=9,
    style="教学课件",
    with_images=True,
)
r["elapsed_sec"] = round(time.time() - t0, 1)
r["last_run"] = ppt_maker.last_run
print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
