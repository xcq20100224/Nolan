# -*- coding: utf-8 -*-
"""
P1 大目标基准：分层规划能力测量（复用 P0 跑道与断言）。

P0 证明：单步任务 300/300 可靠。P1 要量的是另一件事——
「帮我准备明天出差」这种大目标，Nolan 能不能自己拆成
查天气 -> 写清单 -> 设提醒 多步执行并汇总。

现状预判（先跑基线验证）：复合任务直接交给平铺 Agent 循环（4 轮上限），
3 步以上的目标必然超轮或漏步。本基准先证明问题存在，再作为规划器的验收尺。

用法（与 P0 相同）：
    python benchmark_p1.py          # 全量
    python benchmark_p1.py 1-5      # 范围
    python benchmark_p1.py stats    # 成功率趋势
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import benchmark_p0 as p0


# (id, 大目标, 复合断言, 依赖, "K")
# 断言设计原则：每个子任务都有独立物理验证（文件落盘/提醒入库/记忆在案/
# 回复含关键结果），绝不让「回复听着像完成了」蒙混过关。
TASKS = [
    (1, "帮我准备明天出差北京：查一下北京明天天气，把行李清单写到 出差清单.txt，再设一个明早7点的提醒",
     ("all", [("file", "出差清单.txt", ["行李"]),
              ("rem", ["出差"]),
              ("nf",)]), "net", "K"),
    (2, "把今天的人工智能新闻总结三条写到 日报.txt，然后提醒我晚上9点看一下",
     ("all", [("file", "日报.txt", ["AI"]),
              ("rem", ["日报"]),
              ("nf",)]), "net", "K"),
    (3, "帮我安排今天的学习计划：上午学英语，下午写代码，把计划写到 学习.txt",
     ("all", [("file", "学习.txt", ["英语", "代码"]),
              ("nf",)]), None, "K"),
    (4, "打开记事本，然后算一下 123 乘 456 等于多少，告诉我结果",
     ("all", [("proc", ["notepad.exe"]),
              ("c", "56088")]), None, "K"),
    (5, "查一下上海明天天气，再把结果写到 上海天气.txt",
     ("all", [("file", "上海天气.txt", ["雨"]),
              ("nf",)]), "net", "K"),
    (6, "帮我整理明天的工作：早上9点晨会、下午3点评审，写到 日程.txt，再设一个明早8点半的提醒，提醒我准备工作",
     ("all", [("file", "日程.txt", ["晨会", "评审"]),
              ("rem", ["工作"]),
              ("nf",)]), None, "K"),
    (7, "搜一下马斯克最近的新闻，总结写到 马斯克.txt，再记住我喜欢关注马斯克",
     ("all", [("file", "马斯克.txt", ["马斯克"]),
              ("mem", ["马斯克"]),
              ("nf",)]), "net", "K"),
    (8, "把 1 加到 100 的和算出来，写到 数学.txt，再读给我听",
     ("all", [("file", "数学.txt", ["5050"]),
              ("c", "5050")]), None, "K"),
    (9, "看看现在几点，然后把当前时间写到 时间记录.txt",
     ("all", [("file", "时间记录.txt", ["20"]),
              ("nf",)]), None, "K"),
    (10, "打开计算器，算 888 加 222，把结果写到 加法.txt",
     ("all", [("file", "加法.txt", ["1110"]),
              ("nf",)]), None, "K"),
]


def main():
    args = sys.argv[1:]
    if args and args[0] == "stats":
        p0.show_stats(RESULTS_FILE, label="P1")
        return
    lo, hi = 1, len(TASKS)
    if args and "-" in args[0]:
        lo, hi = map(int, args[0].split("-", 1))
    rows = [t for t in TASKS if lo <= t[0] <= hi]
    p0.run_tasks(rows, RESULTS_FILE, "== P1 大目标基准 · 分层规划 · %d-%d ==" % (lo, hi))


RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "memory", "results_p1.jsonl")


if __name__ == "__main__":
    main()
