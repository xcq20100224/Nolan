# -*- coding: utf-8 -*-
"""R3 真机冒烟：生成 -> 存档 -> 对话修改 -> 重渲染闭环（一次性脚本）"""
import json
import os
import time

import ppt_editor
import ppt_maker

FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")

# ---- 第一步：生成（带存档）
t0 = time.time()
r = ppt_maker.make_ppt("城市露营经济", pages=6, style="科普分享")
print("生成 ok:", r.get("ok"), "| 耗时: %.1fs" % (time.time() - t0))
if not r.get("ok"):
    raise SystemExit("生成失败：" + str(r.get("error")))
fn = r["file_name"]
stem = fn[:-5] if fn.endswith(".pptx") else fn
archive_path = os.path.join(FILES, stem + ".deck.json")
print("pptx:", fn, "| 存档存在:", os.path.isfile(archive_path))
mtime0 = os.path.getmtime(os.path.join(FILES, fn))
arch = json.load(open(archive_path, encoding="utf-8"))
print("存档字段:", sorted(arch.keys()))
print("原始第3页版式:", arch["deck"]["pages"][1]["layout"],
      "| 标题:", arch["deck"]["pages"][1].get("page_title"))

# ---- 第二步：对话修改（把第 3 页改成大数字版式）
time.sleep(1.2)  # 保证 mtime 可区分
t1 = time.time()
msg = ppt_editor.edit_ppt(fn, "把第3页改成大数字版式")
print("编辑耗时: %.1fs" % (time.time() - t1))
print("话术:", msg)

# ---- 第三步：验证闭环
mtime1 = os.path.getmtime(os.path.join(FILES, fn))
arch2 = json.load(open(archive_path, encoding="utf-8"))
new_layout = arch2["deck"]["pages"][1]["layout"]
print("pptx 已覆写:", mtime1 > mtime0)
print("修改后第3页版式:", new_layout,
      "| stats:", arch2["deck"]["pages"][1].get("stats"))
from pptx import Presentation
p = Presentation(os.path.join(FILES, fn))
print("重渲染后物理页数:", len(p.slides))
