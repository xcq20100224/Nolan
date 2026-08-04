# -*- coding: utf-8 -*-
"""Gap1 流式化 · 端到端延迟基线测量（先测量再修）。

感知延迟 = ASR 转写 + 大脑思考 + TTS 合成（合成完才发声是现行架构的税）。
用法：python bench_latency.py
"""
import time

import mouth


def measure_tts():
    """TTS 合成延迟 vs 文本长度（GLM-TTS 主通道）。"""
    samples = {
        "短(16字)": "好的先生，记事本已经打开了。",
        "中(56字)": "先生，今天的人工智能新闻有三条。第一条，大模型推理成本继续下降；第二条，多模态模型在工业质检落地加速；第三条，开源社区发布了新的语音合成基座。",
        "长(148字)": "先生，这是为您准备的今日简报。宏观经济方面，全球市场持续关注通胀数据的走向，主要央行释放出谨慎的信号。科技产业方面，人工智能领域的竞争进入深水区，多家公司发布了新一代基础模型，推理成本进一步下探，应用层的创新开始加速。供应链方面，关键元器件的交付周期有所缩短。以上是要点，详细内容我已经写入今日简报文件，请您过目。",
    }
    results = {}
    for name, text in samples.items():
        t0 = time.perf_counter()
        try:
            audio = mouth._synthesize_glm_tts(text)
            elapsed = time.perf_counter() - t0
            results[name] = (len(text), elapsed, len(audio))
            print(f"  {name}: {elapsed:.2f}s 合成（{len(audio)//1024}KB）")
        except Exception as e:
            print(f"  {name}: GLM-TTS 失败（{e}），跳过")
            results[name] = (len(text), None, 0)
    return results


def measure_brain():
    """大脑思考延迟：三类典型请求（闲聊/规则工具/搜索总结）。"""
    import brain
    cases = [
        ("闲聊", "你觉得今天适合写代码吗"),
        ("规则工具", "现在几点了"),
        ("搜索总结", "搜一下今天的人工智能新闻"),
    ]
    results = {}
    for name, q in cases:
        t0 = time.perf_counter()
        try:
            reply = brain.think(q, [])
            elapsed = time.perf_counter() - t0
            results[name] = (elapsed, reply)
            print(f"  {name}: 思考 {elapsed:.2f}s → 回复 {len(reply)} 字")
            print(f"    「{reply[:60]}」")
        except Exception as e:
            print(f"  {name}: 失败（{e}）")
            results[name] = (None, "")
    return results


if __name__ == "__main__":
    print("== Gap1 延迟基线 ==\n[1/2] TTS 合成延迟（合成完才发声 = 现行架构的税）")
    tts = measure_tts()
    print("\n[2/2] 大脑思考延迟")
    br = measure_brain()

    print("\n== 汇总（感知延迟拆解，以中句为例）==")
    asr = 5.0  # P5 实测：small/beam1 转写 3.7s 语音 ≈ 5s（1.53x 实时 + VAD 尾静音 1s）
    think = br.get("搜索总结", (None,))[0]
    tts_mid = tts.get("中(56字)", (0, None, 0))[1]
    print(f"  ASR 转写：~{asr:.1f}s（P5 已优化）")
    if think:
        print(f"  大脑思考：~{think:.1f}s")
    if tts_mid:
        print(f"  TTS 合成：~{tts_mid:.1f}s ← 本阶段要砍的税")
    if think and tts_mid:
        print(f"  端到端：~{asr + think + tts_mid:.1f}s")
