# -*- coding: utf-8 -*-
"""
test_checkpoint.py —— B2 长任务续航（检查点恢复）纯 mock 单测

不碰真机 GUI：验证 checkpoint 模块的存取 / 过期 / 清理 / 过期巡检，
以及 eyes.resume_perform 的断点状态恢复（history / done_steps /
executed 接回、从断点步继续、成功后清除检查点）与
「每步结束落盘」的节奏。所有 VLM / 截屏 / 键鼠全部打桩。

运行：python jarvis/test_checkpoint.py   （仅标准库 + 本仓库模块）
"""

import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 纯 mock 环境可能缺 GUI 依赖：打桩让 eyes 可导入（桩仅在真实依赖缺席时启用）
for _name in ("pyautogui", "pyperclip", "httpx"):
    try:
        __import__(_name)
    except Exception:
        _stub = types.ModuleType(_name)
        if _name == "pyautogui":
            _stub.FailSafeException = type("FailSafeException", (Exception,), {})
            _stub.FAILSAFE = True
            _stub.PAUSE = 0.0
        sys.modules[_name] = _stub

import checkpoint
import eyes

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond)))
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       (" | " + str(detail)) if detail else ""))


def _use_tmp_dir():
    """把检查点目录指到临时目录，不碰真实 jarvis/data/checkpoints。"""
    tmp = tempfile.mkdtemp(prefix="ckpt_test_")
    checkpoint._CKPT_DIR = tmp
    return tmp


def test_save_load_roundtrip():
    _use_tmp_dir()
    state = {"step": 7, "history": ["第1步 left_click「确定」"],
             "done_steps": [{"action": "left_click", "text": "确定",
                             "keys": ""}],
             "window_key": "|记事本", "executed": 6, "ts": time.time(),
             "target_hint": "记事本"}
    ok = checkpoint.save("测试任务A", state)
    loaded = checkpoint.load("测试任务A")
    check("存取回环：save 成功", ok)
    check("存取回环：step 恢复", loaded and loaded["step"] == 7)
    check("存取回环：history 恢复",
          loaded and loaded["history"] == state["history"])
    check("存取回环：done_steps 恢复",
          loaded and loaded["done_steps"] == state["done_steps"])
    check("存取回环：executed 恢复", loaded and loaded["executed"] == 6)
    check("存取回环：window_key 恢复",
          loaded and loaded["window_key"] == "|记事本")
    check("目录自动创建", os.path.isdir(checkpoint._CKPT_DIR))
    check("原子写入无临时文件残留",
          not any(f.endswith(".tmp") for f in os.listdir(checkpoint._CKPT_DIR)))
    check("任务名稳定哈希（同任务同文件）",
          checkpoint._path("测试任务A") == checkpoint._path("测试任务A")
          and checkpoint._path("测试任务A") != checkpoint._path("别的任务"))


def test_expiry():
    _use_tmp_dir()
    old = {"step": 3, "history": [], "done_steps": [], "window_key": "",
           "executed": 1, "ts": time.time() - 25 * 3600}
    checkpoint.save("过期任务", old)
    check("过期检查点（>24h）load 返回 None",
          checkpoint.load("过期任务") is None)
    check("过期检查点顺手清除",
          not os.path.isfile(checkpoint._path("过期任务")))
    fresh = {"step": 3, "ts": time.time() - 23 * 3600}
    checkpoint.save("临界任务", fresh)
    check("未过期（23h）load 正常返回",
          checkpoint.load("临界任务") is not None)


def test_clear():
    _use_tmp_dir()
    checkpoint.save("清理任务", {"step": 1, "ts": time.time()})
    checkpoint.clear("清理任务")
    check("clear 后 load 返回 None", checkpoint.load("清理任务") is None)
    check("clear 幂等（不存在也算成功）", checkpoint.clear("清理任务") is True)


def test_corrupted_file():
    _use_tmp_dir()
    path = checkpoint._path("坏档任务")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{这不是合法 JSON")
    check("JSON 损坏 load 返回 None（不抛异常）",
          checkpoint.load("坏档任务") is None)


def test_list_stale():
    _use_tmp_dir()
    checkpoint.save("新鲜任务", {"step": 1, "ts": time.time()})
    checkpoint.save("陈旧任务", {"step": 2, "ts": time.time() - 30 * 3600})
    # list_stale 按文件 mtime 判龄：把陈旧文件的 mtime 拨回 30 小时前
    p = checkpoint._path("陈旧任务")
    old_mtime = time.time() - 30 * 3600
    os.utime(p, (old_mtime, old_mtime))
    stale = checkpoint.list_stale()
    tasks = [s["task"] for s in stale]
    check("list_stale 只列过期项", tasks == ["陈旧任务"], tasks)


def _mock_eyes(captured, reply):
    """把 eyes 的感知/思考/动作全部换成假件，只留闭环骨架。"""
    def fake_ask(img, text, system=None):
        captured.setdefault("prompts", []).append(text)
        return reply
    eyes._ask_vlm = fake_ask
    eyes._verify = lambda shot, q: (True, "复核通过")
    eyes.screenshot_b64 = lambda: "ZmFrZQ=="
    eyes._screenshot_size = lambda: (1280, 720)
    eyes._do_action = lambda *a, **k: None
    eyes._vlm_scan_cached = lambda s: []   # B3 补盲在本组测试中不触发
    eyes._uia = None
    eyes._perception = None
    eyes._wctx = None
    eyes._skills = None
    eyes._rel = None
    eyes._episodic = None


def test_resume_restores_state():
    _use_tmp_dir()
    checkpoint.save("续跑任务", {
        "step": 3,
        "history": ["第1步 left_click「确定」", "第2步 type「你好」"],
        "done_steps": [{"action": "left_click", "text": "确定", "keys": ""}],
        "window_key": "", "executed": 2, "ts": time.time(),
        "target_hint": ""})
    captured = {}
    _mock_eyes(captured, '{"action": "done", "thought": "续跑完成汇报"}')
    result = eyes.resume_perform("续跑任务", max_steps=12)
    prompt = captured["prompts"][0] if captured.get("prompts") else ""
    check("续跑返回完成汇报", result == "续跑完成汇报", result[:40])
    check("续跑从断点步继续（第 4 步）", "第 4 步" in prompt)
    check("续跑带断点提示（前 N 步已完成/当前屏幕是现场）",
          "断点续跑" in prompt and "前 3 步已完成" in prompt)
    check("续跑恢复动作历史", "第1步 left_click「确定」" in prompt)
    check("续跑成功后清除检查点", checkpoint.load("续跑任务") is None)


def test_resume_without_checkpoint_equals_perform():
    _use_tmp_dir()
    captured = {}
    _mock_eyes(captured, '{"action": "done", "thought": "全新完成"}')
    result = eyes.resume_perform("全新任务", max_steps=12)
    prompt = captured["prompts"][0] if captured.get("prompts") else ""
    check("无检查点等价于 perform（从第 1 步开始）", "第 1 步" in prompt)
    check("无检查点返回完成汇报", result == "全新完成")


def test_checkpoint_saved_each_step_and_cleared_on_success():
    """每步结束落盘：第一步执行动作后磁盘上有断点；任务成功后清除。"""
    _use_tmp_dir()
    replies = ['{"action": "key", "keys": "enter", "expect": "换行"}',
               '{"action": "done", "thought": "两步完成"}']
    calls = {"n": 0}

    def fake_ask(img, text, system=None):
        r = replies[min(calls["n"], 1)]
        calls["n"] += 1
        return r

    captured = {}
    _mock_eyes(captured, "")
    eyes._ask_vlm = fake_ask
    saved = []
    orig_save = checkpoint.save

    def spy_save(task, state):
        saved.append(dict(state))
        return orig_save(task, state)

    checkpoint.save = spy_save
    try:
        result = eyes.perform("两步任务", max_steps=12)
    finally:
        checkpoint.save = orig_save
    check("动作步结束落盘（step=1）",
          len(saved) >= 1 and saved[0].get("step") == 1,
          [s.get("step") for s in saved])
    check("检查点含 executed 计数", saved and saved[0].get("executed") == 1)
    check("检查点含 done_steps / history 键",
          saved and "done_steps" in saved[0] and "history" in saved[0]
          and "window_key" in saved[0] and "ts" in saved[0])
    check("成功完成后检查点已清除", checkpoint.load("两步任务") is None)
    check("两步任务完成", result == "两步完成", result[:40])


def test_fail_report_clears_checkpoint():
    """VLM 判定彻底失败（executed>0 的 fail）时清除检查点。"""
    _use_tmp_dir()
    checkpoint.save("必败任务", {"step": 2, "history": [], "done_steps": [],
                               "window_key": "", "executed": 1,
                               "ts": time.time(), "target_hint": ""})
    captured = {}
    _mock_eyes(captured, '{"action": "fail", "thought": "目标不存在"}')
    eyes._describe_screen = lambda shot: ""   # 失败补问打桩，省一次 VLM
    result = eyes.resume_perform("必败任务", max_steps=12)
    check("彻底失败返回失败话术", result.startswith("先生，任务未能完成"),
          result[:30])
    check("彻底失败（fail_report）清除检查点",
          checkpoint.load("必败任务") is None)


if __name__ == "__main__":
    test_save_load_roundtrip()
    test_expiry()
    test_clear()
    test_corrupted_file()
    test_list_stale()
    test_resume_restores_state()
    test_resume_without_checkpoint_equals_perform()
    test_checkpoint_saved_each_step_and_cleared_on_success()
    test_fail_report_clears_checkpoint()
    failed = [n for n, ok in _RESULTS if not ok]
    print("\n==== 共 %d 项：%d 通过，%d 失败 ===="
          % (len(_RESULTS), len(_RESULTS) - len(failed), len(failed)))
    if failed:
        for n in failed:
            print("  失败项：%s" % n)
    sys.exit(1 if failed else 0)
