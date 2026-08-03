# -*- coding: utf-8 -*-
"""P3 网页版打断 · 回声判定单元测试（纯文本逻辑，不碰麦克风/音箱）。

覆盖：
    1. 回声：转写文本与播报文本完全一致 → 判回声
    2. 回声：ASR 误差版（同音错字+多标点） → 仍判回声
    3. 回声：播报长文中的一小段（用户只听到半截） → 判回声
    4. 主人声音：完全不同的指令 → 不判回声
    5. 主人声音：包含播报里的个别词但整体不同 → 不判回声
    6. 边界：无播报文本 / 空听到文本 → 不判回声（不误拦主人）
"""
import importlib.util
import os
import sys

# 动态加载 server.py（不启动 HTTP 服务，只取纯函数）
spec = importlib.util.spec_from_file_location(
    "nolan_server", os.path.join(os.path.dirname(__file__), "server.py"))
server = importlib.util.module_from_spec(spec)
sys.modules["nolan_server"] = server
spec.loader.exec_module(server)


def check(name, heard, speaking, expect_echo):
    server._now_speaking_text = speaking
    got = server._is_echo(heard)
    assert got == expect_echo, (
        f"{name}: heard={heard!r} speaking={speaking!r} "
        f"期望 echo={expect_echo} 实际 {got}")
    print(f"✅ {name}")


SPEAKING = "先生，今天的人工智能新闻有三条。第一条，大模型推理成本继续下降。"

check("1/6 完全一致的回声", SPEAKING, SPEAKING, True)
check("2/6 ASR 误差版回声",
      "先生今天的人工智能新闻有三条，第1条大模型推理成本继续下降",
      SPEAKING, True)
check("3/6 播报长文的半截回声", "第一条，大模型推理成本继续下降。", SPEAKING, True)
check("4/6 主人新指令", "别说了，帮我打开记事本", SPEAKING, False)
check("5/6 含播报词汇的主人指令", "人工智能新闻别念了，换一条", SPEAKING, False)
check("6/6 空文本边界", "", SPEAKING, False)
check("6/6b 无播报时不误判", "你好", "", False)

print("\n🎉 回声判定单元测试全过")
