# -*- coding: utf-8 -*-
"""QA 重放：取修复前存档（1653 那份的大纲+deck），只换「修复后管线」重跑
生图+清洗+排版，产出同内容对照件，量化验证三个差距的改善。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ppt_maker

arc = json.loads((ppt_maker.FILES_DIR /
                  "一元二次方程解法课件_给初三学生上课用_20260815-1653.deck.json"
                  ).read_text(encoding="utf-8"))
deck = arc["deck"]
deck["content_type"] = arc.get("deck", {}).get("content_type") or "教学课件型"
ppt_maker._sanitize_deck_text(deck)          # 修复 1：LaTeX -> Unicode
made = ppt_maker._attach_images(deck, arc["outline"], enabled=True)  # 修复 2：two_column 可配图
out = ppt_maker.FILES_DIR / "一元二次方程_同内容重放_修复后管线.pptx"
ppt_maker._build_pptx(deck, arc["style"], out)
print(json.dumps({"ok": True, "path": str(out), "images_made": made},
                 ensure_ascii=False))
