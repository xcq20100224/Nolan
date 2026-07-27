# -*- coding: utf-8 -*-
"""
发声链路自测（selftest_voice.py）—— 验证 mouth.speak 三级链。

用例：
    1) 全链路实跑：speak('先生，这是 Nolan 的新声音。') 无异常，
       且日志显示走了 GLM-TTS 主通道（当前网络实测 GLM-TTS 可用）；
    2) llm_config.json 缺失（monkeypatch 模拟）时降级链不崩溃，
       且确实降级到后续通道。

用法：python selftest_voice.py
"""

import contextlib
import io
import sys

import mouth


def _run_speak_capture(text: str) -> str:
    """执行 speak 并捕获其 stdout 日志，返回日志文本。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mouth.speak(text)
    return buf.getvalue()


def test_glm_tts_primary() -> None:
    """用例 1：全链路实跑，应走 GLM-TTS 主通道且无异常。"""
    print("【用例 1】全链路实跑（应走 GLM-TTS 主通道）...")
    logs = _run_speak_capture("先生，这是 Nolan 的新声音。")
    print(logs, end="")
    assert "GLM-TTS 合成成功" in logs, (
        "❌ 用例 1 失败：日志未显示 GLM-TTS 主通道成功，实际日志见上"
    )
    assert "GLM-TTS 主通道失败" not in logs, (
        "❌ 用例 1 失败：GLM-TTS 主通道发生了降级，实际日志见上"
    )
    print("✅ 用例 1 通过：GLM-TTS 主通道发声成功。\n")


def test_missing_config_degrades() -> None:
    """用例 2：monkeypatch 模拟 llm_config.json 缺失，降级链不崩溃。

    模拟手段：
        - mouth._load_llm_config 返回空字典 → GLM-TTS 必失败；
        - mouth._synthesize_to_file 直接抛异常 → edge-tts 必失败（且不联网）；
        - mouth._speak_offline 换成记录器 → 不真正播报，只记录被调用。
    断言：speak 无异常返回，且 SAPI 兜底被触达。
    """
    print("【用例 2】llm_config.json 缺失（monkeypatch 模拟），降级链不崩溃...")

    orig_load = mouth._load_llm_config
    orig_edge = mouth._synthesize_to_file
    orig_offline = mouth._speak_offline

    called = {"offline": False}

    def fake_load() -> dict:
        return {}  # 模拟配置文件缺失：读不到 api_key/base_url

    def fake_edge(text: str, out_path: str) -> None:
        raise RuntimeError("模拟 edge-tts 不可用")

    def fake_offline(text: str) -> None:
        called["offline"] = True
        print(f"[模拟] SAPI 兜底播报：{text}")

    try:
        mouth._load_llm_config = fake_load
        mouth._synthesize_to_file = fake_edge
        mouth._speak_offline = fake_offline

        logs = _run_speak_capture("先生，配置文件不见了。")
        print(logs, end="")
    finally:
        mouth._load_llm_config = orig_load
        mouth._synthesize_to_file = orig_edge
        mouth._speak_offline = orig_offline

    assert "GLM-TTS 主通道失败" in logs, (
        "❌ 用例 2 失败：配置缺失时未看到 GLM-TTS 降级日志，实际日志见上"
    )
    assert called["offline"], (
        "❌ 用例 2 失败：降级链未触达 SAPI 兜底，实际日志见上"
    )
    print("✅ 用例 2 通过：配置缺失时逐级降级到 SAPI 兜底，全程无异常。\n")


def main() -> int:
    print("=" * 60)
    print("Nolan 发声链路自测（GLM-TTS → edge-tts → SAPI）")
    print("=" * 60 + "\n")
    test_glm_tts_primary()
    test_missing_config_degrades()
    print("=" * 60)
    print("🎉 全部用例通过。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
