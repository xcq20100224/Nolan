# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 长期记忆模块自测（selftest_memory.py）
断言链路：
  remember 两条 -> load 含两条 -> recall 提到『两件事』
  -> forget 关键字删一条 -> load 只剩一条
  -> forget 不存在的关键字有礼貌说明
测试结束后把存储文件恢复原状（原文件备份再还原；原本不存在则删除测试产物）。
"""

import os
import shutil

import memory

# 唯一测试标记，避免误伤真实记忆
TAG = "【自测标记-7f3a9c】"

_BAK = memory._MEMORY_FILE + ".selftest.bak"


def _backup() -> bool:
    """备份原记忆文件；返回原本是否存在记忆文件。"""
    if os.path.exists(memory._MEMORY_FILE):
        shutil.copy2(memory._MEMORY_FILE, _BAK)
        return True
    return False


def _restore(existed: bool) -> None:
    """恢复原状：有备份则还原；原本不存在则删除测试产生的文件。"""
    if existed:
        shutil.move(_BAK, memory._MEMORY_FILE)
    else:
        if os.path.exists(memory._MEMORY_FILE):
            os.remove(memory._MEMORY_FILE)
        # 目录若为空也顺手清掉
        if os.path.isdir(memory._MEMORY_DIR) and not os.listdir(memory._MEMORY_DIR):
            os.rmdir(memory._MEMORY_DIR)


def main() -> None:
    existed = _backup()
    # 测试从空记忆开始
    if os.path.exists(memory._MEMORY_FILE):
        os.remove(memory._MEMORY_FILE)

    try:
        # 1. 空记忆时 load 返回空字符串，recall 有礼貌引导
        assert memory.load() == "", "空记忆时 load 应返回空字符串"
        r0 = memory.recall()
        assert "记住" in r0 and "先生" in r0, f"空记忆 recall 应引导用户，实际：{r0}"

        # 2. remember 两条
        c1 = memory.remember(f"{TAG}先生喜欢黑咖啡")
        assert "记住" in c1 and "先生" in c1, f"remember 确认语异常：{c1}"
        c2 = memory.remember(f"{TAG}先生每周三去游泳")
        assert "记住" in c2, f"remember 确认语异常：{c2}"

        # 3. load 含两条
        text = memory.load()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 2, f"load 应含两条，实际 {len(lines)} 条：{text}"
        assert "黑咖啡" in text and "游泳" in text, "load 内容缺失"

        # 4. recall 提到『两件事』
        r1 = memory.recall()
        assert "两件事" in r1, f"recall 应提到『两件事』，实际：{r1}"
        assert "一、" in r1 and "两、" in r1, f"recall 缺少列举序号：{r1}"

        # 5. forget 关键字删一条 -> load 只剩一条
        f1 = memory.forget("黑咖啡")
        assert "一条" in f1, f"forget 应报告删除一条，实际：{f1}"
        text2 = memory.load()
        lines2 = [ln for ln in text2.splitlines() if ln.strip()]
        assert len(lines2) == 1, f"删除后应剩一条，实际 {len(lines2)} 条"
        assert "黑咖啡" not in text2 and "游泳" in text2, "删除目标错误"

        # 6. forget 不存在的关键字有礼貌说明，且不改动记忆
        f2 = memory.forget("不存在的关键词")
        assert "没有找到" in f2, f"零匹配 forget 应有说明，实际：{f2}"
        assert memory.load() == text2, "零匹配 forget 不应改动记忆"

        # 7. 空内容 remember 返回提示，不写入
        before = memory.load()
        c3 = memory.remember("   ")
        assert "再说一遍" in c3 or "没有听清" in c3, f"空内容应有提示，实际：{c3}"
        assert memory.load() == before, "空内容不应写入记忆"

        print("全部断言通过：selftest_memory OK")
    finally:
        _restore(existed)
        print("存储文件已恢复原状")


if __name__ == "__main__":
    main()
