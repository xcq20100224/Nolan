# -*- coding: utf-8 -*-
"""真机冒烟：研究管线 + 结论式标题 端到端验证（一次性脚本）"""
import time
import ppt_maker

t0 = time.time()
r = ppt_maker.make_ppt("全球新能源汽车产业格局与中国机遇", pages=10, style="工作汇报")
dt = time.time() - t0
print("ok:", r.get("ok"), "| 耗时: %.1fs" % dt)
print("文件:", r.get("file_name"), "| 页数:", r.get("pages"), "| 标题:", r.get("title"))
st = ppt_maker.last_run or {}
print("研究材料字数:", st.get("research_chars"), "| 配图张数:", st.get("images"))
for ps in st.get("page_stats", []):
    print(f"  p{ps['page']:02d} [{ps['layout']:<10}] {ps['chars']:3d}字 | {ps['title']}")
