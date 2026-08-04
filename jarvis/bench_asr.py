# -*- coding: utf-8 -*-
"""P5 模型换代 · ASR 基准测试（先测量再修：同一批语料，同一台机器，数字说话）。

用法：
    python bench_asr.py make              # 生成 4 段标准中文语音语料（edge-tts）
    python bench_asr.py run <模型名> [beam]  # 用指定模型测延迟+准确率（beam 缺省 5）
"""
import difflib
import os
import subprocess
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_CORPUS_DIR = os.path.join(_DIR, "bench_corpus")

# 4 段标准语料：覆盖 Nolan 真实使用场景（短指令/中句/长句/唤醒词）
CORPUS = {
    "短指令": "打开记事本，把今天的日期写进去",
    "中句": "帮我搜一下今天的人工智能新闻，总结三条写到日报点txt",
    "长句": "如果明天北京下雨的话，就在早上八点钟提醒我带伞，顺便把天气预报读给我听",
    "唤醒": "诺兰，现在几点了",
}


def make():
    """用 edge-tts 生成标准语料 mp3（与生产环境 TTS 同一引擎，最贴近真人听感）。"""
    import asyncio
    import edge_tts
    os.makedirs(_CORPUS_DIR, exist_ok=True)

    async def _gen(text, out):
        await edge_tts.Communicate(text, "zh-CN-YunxiNeural").save(out)

    for name, text in CORPUS.items():
        out = os.path.join(_CORPUS_DIR, f"{name}.mp3")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            print(f"已存在，跳过：{name}.mp3")
            continue
        last = None
        for attempt in range(1, 6):  # 网络抖动重试：edge-tts 偶发连接重置
            try:
                asyncio.run(_gen(text, out))
                last = None
                break
            except Exception as e:
                last = e
                print(f"  {name} 第{attempt}次失败（{type(e).__name__}），3秒后重试……")
                time.sleep(3)
        if last is not None:
            raise last
        print(f"生成：{name}.mp3（{text}）")
    print("语料就绪 ✅")


def _load_audio(path):
    """mp3 → 16kHz float32 numpy（与耳朵模块的输入规格一致）。"""
    import numpy as np
    import io
    from pydub import AudioSegment
    seg = AudioSegment.from_file(path).set_frame_rate(16000).set_channels(1)
    samples = np.array(seg.get_array_of_samples()).astype(np.float32) / 32768.0
    return samples


def run(model_name, beam=5):
    from faster_whisper import WhisperModel
    print(f"加载模型 {model_name}（cpu/int8，beam={beam}，与生产一致）……")
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    load_s = time.perf_counter() - t0
    print(f"模型加载：{load_s:.1f}s")

    results = []
    for name, expect in CORPUS.items():
        path = os.path.join(_CORPUS_DIR, f"{name}.mp3")
        t0 = time.perf_counter()
        # 直接喂文件路径：faster-whisper 内置 PyAV 解码（无需 pydub/ffmpeg），
        # info.duration 给出音频时长；与生产链路（numpy float32）解码结果等价
        segments, info = model.transcribe(path, language="zh",
                                          beam_size=beam, vad_filter=True)
        text = "".join(s.text for s in segments).strip()
        elapsed = time.perf_counter() - t0
        dur = float(info.duration)
        # 准确率：归一化后与原文做相似度
        norm = lambda s: "".join(c for c in s if c.isalnum() or "一" <= c <= "鿿")
        acc = difflib.SequenceMatcher(None, norm(text), norm(expect)).ratio()
        results.append((name, dur, elapsed, acc, text))
        print(f"  {name}: 音频{dur:.1f}s 转写{elapsed:.2f}s "
              f"倍率{elapsed/dur:.2f}x 准确率{acc:.0%}")
        print(f"    识别：{text}")

    avg_ratio = sum(r[2] / r[1] for r in results) / len(results)
    avg_acc = sum(r[3] for r in results) / len(results)
    total = sum(r[2] for r in results)
    print(f"\n== {model_name} 汇总 ==")
    print(f"平均转写倍率：{avg_ratio:.2f}x（<1 表示快于实时，越小越好）")
    print(f"平均准确率：{avg_acc:.0%}")
    print(f"4 段总耗时：{total:.2f}s")
    return avg_ratio, avg_acc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "make":
        make()
    elif sys.argv[1] == "run":
        model = sys.argv[2] if len(sys.argv) > 2 else "medium"
        beam = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        run(model, beam)
