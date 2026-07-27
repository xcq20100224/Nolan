# -*- coding: utf-8 -*-
"""brain.py 功能自测脚本（仅测大脑模块自身，不依赖其他模块）"""
import subprocess
import brain

print("== 自测 1：问时间 ==")
r1 = brain.think("现在几点", [])
print("回复:", r1)
assert any(k in r1 for k in ("点", "分")), "时间回复应包含时分信息"

print("\n== 自测 2：问星期 ==")
r1b = brain.think("今天星期几", [])
print("回复:", r1b)
assert "星期" in r1b

print("\n== 自测 3：打开计算器（验证真的启动）==")
r2 = brain.think("打开计算器", [])
print("回复:", r2)
assert "计算器" in r2 and "失败" not in r2, "应返回确认语"
# 验证 calc 进程真的起来了
out = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq CalculatorApp.exe"],
    capture_output=True, text=True,
).stdout + subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq win32calc.exe"],
    capture_output=True, text=True,
).stdout + subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq calc.exe"],
    capture_output=True, text=True,
).stdout
calc_running = any(n in out for n in ("CalculatorApp.exe", "win32calc.exe", "calc.exe"))
print("calc 进程检测到:", calc_running)
assert calc_running, "计算器进程应当已启动"
# 清理：把计算器关掉
subprocess.run(["taskkill", "/F", "/IM", "CalculatorApp.exe"], capture_output=True)
subprocess.run(["taskkill", "/F", "/IM", "win32calc.exe"], capture_output=True)
print("已清理 calc 进程")

print("\n== 自测 4：闲聊兜底 ==")
r3 = brain.think("随便聊聊天气", [])
print("回复:", r3)
assert r3.strip() and "规则大脑" in r3, "应有非空人格化回复并承认是规则大脑"

print("\n== 自测 5：退出意图 ==")
r4 = brain.think("再见，去睡觉吧", [])
print("回复:", r4)
assert r4 == "__EXIT__", "应返回 __EXIT__"

print("\n== 自测 6：空输入 ==")
r5 = brain.think("   ", [])
print("回复:", r5)
assert r5.strip()

print("\n全部自测通过 ✅")
