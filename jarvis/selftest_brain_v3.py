# -*- coding: utf-8 -*-
"""
Nolan 语音助手 · 大脑模块阶段三自测（selftest_brain_v3.py）

覆盖断言：
  1. think('记住我喜欢喝美式咖啡') 返回含「记住」的确认（memory 未就位则跳过并注明）
  2. think('你记得什么') 返回非空文本
  3. think('忘掉咖啡') 返回非空文本
  4. _parse_intent('现在几点') 仍命中 get_time（记忆分支不抢工具意图）
  5. 无 JARVIS_API_KEY 时 _think_via_llm 返回 None
  附：EXIT 约定不变；长期记忆注入 system prompt 行为正确。

运行：python selftest_brain_v3.py
说明：测试前备份 memory\\long_term.txt，结束后原样恢复，不污染真实记忆库。
"""

import os
import shutil

import brain

# 与 memory.py 相同的存储定位（只读备份/恢复，不修改 memory.py）
_MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
_MEMORY_FILE = os.path.join(_MEMORY_DIR, "long_term.txt")
_BACKUP_FILE = _MEMORY_FILE + ".selftest_bak"

_passed = 0
_failed = 0


def _check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  [PASS] {name}" + (f" —— {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _backup_memory() -> None:
    """备份记忆文件（不存在则记录为无备份）。"""
    if os.path.exists(_MEMORY_FILE):
        shutil.copy2(_MEMORY_FILE, _BACKUP_FILE)
    elif os.path.exists(_BACKUP_FILE):
        os.remove(_BACKUP_FILE)


def _restore_memory() -> None:
    """恢复记忆文件原状。"""
    try:
        if os.path.exists(_BACKUP_FILE):
            shutil.move(_BACKUP_FILE, _MEMORY_FILE)
        elif os.path.exists(_MEMORY_FILE):
            os.remove(_MEMORY_FILE)
    except OSError as exc:
        print(f"  [WARN] 记忆文件恢复失败：{exc}")


def main() -> None:
    print("brain.py 阶段三自测开始")
    _backup_memory()
    old_key = os.environ.pop("JARVIS_API_KEY", None)  # 确保 LLM 层无 Key
    try:
        # 1. 记忆存储意图（真实链路；memory 未就位则跳过）
        if brain.memory is None:
            print("  [SKIP] 记住意图 —— memory 模块未就位，跳过真实链路断言")
        else:
            r1 = brain.think("记住我喜欢喝美式咖啡", [])
            _check(
                "think('记住我喜欢喝美式咖啡') 返回含「记住」的确认",
                isinstance(r1, str) and "记住" in r1,
                f"回复：{r1!r}",
            )

        # 2. 记忆回忆意图
        r2 = brain.think("你记得什么", [])
        _check("think('你记得什么') 返回非空文本", isinstance(r2, str) and bool(r2.strip()),
               f"回复：{r2!r}")

        # 3. 记忆遗忘意图
        r3 = brain.think("忘掉咖啡", [])
        _check("think('忘掉咖啡') 返回非空文本", isinstance(r3, str) and bool(r3.strip()),
               f"回复：{r3!r}")

        # 4. 记忆分支不抢工具意图
        intent = brain._parse_intent("现在几点")
        _check("_parse_intent('现在几点') 仍命中 get_time",
               intent == ("get_time", {}), f"实际：{intent!r}")

        # 5. 无 JARVIS_API_KEY 时 LLM 层返回 None
        r5 = brain._think_via_llm("你好", [])
        _check("无 JARVIS_API_KEY 时 _think_via_llm 返回 None", r5 is None,
               f"实际：{r5!r}")

        # 附 a：EXIT 约定不变
        r6 = brain.think("再见", [])
        _check("think('再见') 仍返回 '__EXIT__'", r6 == brain.EXIT_SIGNAL, f"实际：{r6!r}")

        # 附 b：长期记忆注入 system prompt（有记忆时含小节标题；清空后不含）
        if brain.memory is None:
            print("  [SKIP] 记忆注入 prompt —— memory 模块未就位，跳过")
        else:
            brain.memory.remember("自测注入：先生喜欢黑咖啡")
            p1 = brain._build_system_prompt()
            brain.memory.forget("自测注入")
            p2 = brain._build_system_prompt()
            _check("有记忆时 system prompt 含「以下是你对主人的长期记忆：」",
                   "以下是你对主人的长期记忆：" in p1 and "自测注入" in p1)
            _check("无记忆时 system prompt 不追加记忆小节",
                   "以下是你对主人的长期记忆：" not in p2)

        # 附 c：人设已改名 Nolan，且无残留旧名
        _check("system prompt 人设为 Nolan", "Nolan" in brain._SYSTEM_PROMPT)
        _check("用户可见文案无「贾维斯」残留",
               "贾维斯" not in brain._SYSTEM_PROMPT
               and all("贾维斯" not in t for t in brain._FALLBACK_REPLIES))
    finally:
        if old_key is not None:
            os.environ["JARVIS_API_KEY"] = old_key
        _restore_memory()

    print(f"自测结束：通过 {_passed} 项，失败 {_failed} 项")
    if _failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
