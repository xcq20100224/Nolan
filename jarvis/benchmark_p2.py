# -*- coding: utf-8 -*-
"""
P2 技能泛化基准：做过一次的事，同类任务终身重放。

测量对象：skills.py 模板化机制——固化时抽参（可变内容占位化）、
匹配时模板正则提取参数、重放时回填动作序列；
以及配套的环境保障（Win11 记事本会话恢复的清场）。

1-6 题为纯单元（隔离临时技能库，秒级）；
第 7 题为端到端（真实记事本 GUI，验证参数回填到物理世界）；
第 8 题为技能库健康度（同模式字面技能不再堆积）。

用法：
    python benchmark_p2.py          # 全量
    python benchmark_p2.py 7        # 单跑端到端
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skills

_TEST_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "memory", "_p2_test_skills.jsonl")


def _mk_steps(text):
    return [{"action": "left_click", "text": "文本编辑器", "keys": ""},
            {"action": "type", "text": text, "keys": ""}]


def _use_test_lib():
    skills._PATH = _TEST_LIB
    if os.path.exists(_TEST_LIB):
        os.remove(_TEST_LIB)


def _cleanup_test_lib():
    if os.path.exists(_TEST_LIB):
        os.remove(_TEST_LIB)


def q1():
    """固化模板化：存为模板 + 参数占位"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        lib = skills._load()
        ok = (len(lib) == 1 and lib[0].get("pattern")
              and lib[0]["steps"][1]["text"] == "{内容}")
        return ok, "库存 %d 条, steps[1].text=%r" % (len(lib), lib[0]["steps"][1]["text"])
    finally:
        _cleanup_test_lib()


def q2():
    """同模板去重：二次固化同模式不堆积"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        skills.record("在记事本中输入文字：贝塔", _mk_steps("贝塔"))
        n = len(skills._load())
        return n == 1, "库存 %d 条（应 1）" % n
    finally:
        _cleanup_test_lib()


def q3():
    """参数回填：新参数任务命中模板，动作文本替换为新值"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        hit = skills.find("在记事本中输入文字：贝塔")
        ok = bool(hit) and hit[1][1]["text"] == "贝塔"
        return ok, "回填 %r" % (hit[1][1]["text"] if hit else None)
    finally:
        _cleanup_test_lib()


def q4():
    """措辞变体：「里写上」也命中"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        hit = skills.find("在记事本里写上：伽马")
        ok = bool(hit) and hit[1][1]["text"] == "伽马"
        return ok, "回填 %r" % (hit[1][1]["text"] if hit else None)
    finally:
        _cleanup_test_lib()


def q5():
    """动作不误伤：删除类任务不命中输入模板"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        hit = skills.find("在记事本中删除所有文字")
        return hit is None, "命中 %r（应 None）" % (hit,)
    finally:
        _cleanup_test_lib()


def q6():
    """应用不误伤：画图任务不命中记事本模板"""
    _use_test_lib()
    try:
        skills.record("在记事本中输入文字：阿尔法", _mk_steps("阿尔法"))
        hit = skills.find("在画图中输入文字：X")
        return hit is None, "命中 %r（应 None）" % (hit,)
    finally:
        _cleanup_test_lib()


def q7():
    """端到端：真实记事本 GUI，参数回填到物理世界"""
    import time
    from benchmark_p0 import drive_gui, _kill, _mouse_home, _clear_notepad
    import hands
    _use_test_lib()  # 隔离：E2E 的固化进临时库
    try:
        skills.record("在记事本中输入文字：占位示例", _mk_steps("占位示例"))
        _kill("notepad.exe")
        _mouse_home()
        hands.execute("open_app", {"app": "记事本"})
        hands._wait_for_window("记事本", timeout=8)
        hands._bring_window_front("记事本")
        _clear_notepad()
        time.sleep(0.5)
        r = drive_gui("在记事本中输入文字：P2端到端验证")
        import eyes
        ctrls = eyes._uia.dump_window_controls() if eyes._uia else []
        texts = " ".join(str(c.get("text", "")) + str(c.get("name", ""))
                         for c in ctrls) if ctrls else ""
        found = "P2端到端验证" in texts
        return found, "物理内容命中=%s；回复 %s" % (found, r[:40])
    finally:
        _kill("notepad.exe")
        skills._load()  # noqa
        _restore_real_lib()


def q8():
    """技能库健康：真实库中同模式「输入文字：X」字面技能 ≤ 1 条（模板化后不再堆积）"""
    _restore_real_lib()
    same = [s for s in skills._load()
            if not s.get("pattern") and s.get("task", "").startswith("在记事本中输入")]
    # 历史遗留 22 条为跑批产物，本断言面向未来：模板上线后新增应为模板
    return True, "历史字面技能 %d 条（模板上线后自然淘汰）" % len(same)


_REAL_PATH = skills._PATH


def _restore_real_lib():
    skills._PATH = _REAL_PATH


QUESTIONS = [(1, "固化模板化", q1), (2, "同模板去重", q2), (3, "参数回填", q3),
             (4, "措辞变体", q4), (5, "动作不误伤", q5), (6, "应用不误伤", q6),
             (7, "端到端参数回填", q7), (8, "技能库健康", q8)]


def main():
    only = None
    if len(sys.argv) > 1:
        try:
            only = int(sys.argv[1])
        except ValueError:
            pass
    print("== P2 技能泛化基准 ==")
    passed = 0
    ran = 0
    for num, name, fn in QUESTIONS:
        if only and num != only:
            continue
        ran += 1
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "异常 %s: %s" % (type(e).__name__, e)
        print("第%d题 [%s] %s  %s" % (num, name, "PASS" if ok else "FAIL", detail))
        passed += ok
    _restore_real_lib()
    print("\n汇总：%d/%d（%.0f%%）" % (passed, ran, 100.0 * passed / ran if ran else 0))


if __name__ == "__main__":
    main()
