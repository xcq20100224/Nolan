# -*- coding: utf-8 -*-
"""brain.py 阶段二自测：编译 + import + _parse_intent 映射断言 + think 流程。"""

import sys

failures = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        failures.append(label)


import brain

# --- _parse_intent 映射断言（不依赖 hands） ---
p = brain._parse_intent
check("get_time 几点", p("现在几点了") == ("get_time", {}))
check("get_time 星期", p("今天星期几") == ("get_time", {}))
check("get_time 日期", p("今天几号") == ("get_time", {}))

check("open_app 记事本", p("打开记事本") == ("open_app", {"app": "记事本"}))
check("open_app 计算器", p("帮我打开计算器") == ("open_app", {"app": "计算器"}))
check("open_app 画图", p("打开画图程序") == ("open_app", {"app": "画图"}))

check("open_url", p("打开 https://www.example.com") == ("open_url", {"url": "https://www.example.com"}))
check("fetch_url 看看", p("看看 https://www.example.com 的内容") == ("fetch_url", {"url": "https://www.example.com"}))
check("fetch_url 总结", p("帮我总结一下 https://www.example.com") == ("fetch_url", {"url": "https://www.example.com"}))

check("web_search 搜索", p("搜索 钢铁侠") == ("web_search", {"query": "钢铁侠"}))
check("web_search 百度一下", p("百度一下明天的天气") == ("web_search", {"query": "明天的天气"}))

check("read_file", p("读取 note.txt") == ("read_file", {"name": "note.txt"}))
check("read_file 看看文件", p("帮我看看文件 report.md") == ("read_file", {"name": "report.md"}))

check("write_file 句式一", p("把 明天记得买牛奶 写到 todo.txt") == ("write_file", {"name": "todo.txt", "content": "明天记得买牛奶"}))
check("write_file 句式二", p("写文件 memo.txt 内容 下午三点开会") == ("write_file", {"name": "memo.txt", "content": "下午三点开会"}))
check("write_file 解析不出返回None", p("记录") is None)

check("list_files", p("列出有哪些文件") == ("list_files", {}))

check("run_shell 运行", p("运行 echo hello") == ("run_shell", {"cmd": "echo hello"}))
check("run_shell 执行", p("执行 python --version") == ("run_shell", {"cmd": "python --version"}))

check("闲聊返回None", p("今天天气怎么样") is None)

# --- think 流程 ---
check("think 空输入", "没有听清" in brain.think("", []))
check("think 空白输入", "没有听清" in brain.think("   ", []))
check("think 退出 再见", brain.think("再见", []) == "__EXIT__")
check("think 退出 关机睡觉", brain.think("关机睡觉吧", []) == "__EXIT__")

if brain.hands is None:
    print("SKIP: hands 模块尚不存在，think 全流程执行分支未测（规则意图命中时会降级到闲聊/LLM）")
    # 无 hands 时命中规则意图应降级不崩溃
    r = brain.think("现在几点了", [])
    check("无hands时不崩溃", isinstance(r, str) and len(r) > 0)
else:
    r = brain.think("现在几点了", [])
    check("think get_time 全流程", isinstance(r, str) and len(r) > 0)
    r = brain.think("列出有哪些文件", [])
    check("think list_files 全流程", isinstance(r, str) and len(r) > 0)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部断言通过。")
