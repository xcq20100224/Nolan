# -*- coding: utf-8 -*-
"""
句级流式对话冒烟测试（test_stream_smoke.py · 纯单元级，不起服务、不碰真机 GUI）。

覆盖范围（对应 /api/chat/stream 的四个关键环节）：
  1. 增量切句器 _SentenceStreamer：逐字/随机分块喂入与 mouth._split_sentences
     整段切分结果完全一致（含 <8 字碎片并入下句、流尾残余 flush）；
  2. SSE 帧格式 _sse_encode：'data: <json>\n\n'，可解出原对象；
  3. 分流预检 _stream_hit_rule_intent：规则意图（提醒/记忆/时间/打开/触发/退出）
     一律回退整段，纯闲聊/问答放行流式；
  4. LLM 流式消费者 _llm_stream_worker（假 httpx，零网络）：
     正常流（delta 顺序 + 句队列 + llm_done）、工具 JSON 中止、早期失败回退、
     晚期失败按部分内容收尾、空流回退；
  5. TTS 生产线程 _tts_stream_producer（打桩合成，零网络）：保序 + 哨兵收尾；
  6. 处理器级全链路 _handle_chat_stream（假 httpx + 打桩合成 + 假 wfile）：
     SSE 事件序列完整（delta→sentence→done），历史落账；
  7. 回退路径处理器级验证：规则意图输入 → fallback 事件与 /api/chat 同形
     （本项含 1 次真实 TTS 合成——fallback 内 synth_for，省 API 预算内）；
  8. 真实单句合成 _synth_sentence_url 1 次（GLM-TTS→缓存入库→缓存命中零合成）。

运行：python test_stream_smoke.py
退出码：全部断言通过 0，任一失败 1。
"""

import json
import os
import queue
import sys
import threading
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import server  # noqa: E402  被测模块（内部会把 ../jarvis 加入 sys.path）
import mouth  # noqa: E402  切句基准（server 已把 jarvis 目录入 sys.path）

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """断言计数器：通过/失败一目了然，失败带细节。"""
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ---- 假 httpx：零网络模拟 GLM 流式响应 ----

def _sse_line(content: str) -> str:
    """构造一行 OpenAI 兼容流式分片。"""
    return "data: " + json.dumps({"choices": [{"delta": {"content": content}}]},
                                 ensure_ascii=False)


class _FakeResp:
    """假流式响应：iter_lines 逐行吐分片；fail_at 指定在第几行抛异常（模拟流中断）。"""

    def __init__(self, lines, fail_at=None):
        self._lines = list(lines)
        self._fail_at = fail_at

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for i, line in enumerate(self._lines):
            if self._fail_at is not None and i == self._fail_at:
                raise ConnectionError("模拟流中断")
            yield line


class _FakeHttpx(types.SimpleNamespace):
    """假 httpx 模块：stream() 返回假响应或按指定异常直接失败。"""

    def __init__(self, resp=None, raise_exc=None):
        super().__init__()
        self._resp = resp
        self._raise_exc = raise_exc

    def stream(self, method, url, json=None, headers=None, timeout=None):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._resp


def _install_fake_httpx(fake):
    """替换 sys.modules['httpx']，返回还原函数（worker 内部局部 import httpx 生效）。"""
    old = sys.modules.get("httpx")
    sys.modules["httpx"] = fake

    def _restore():
        if old is not None:
            sys.modules["httpx"] = old
        else:
            sys.modules.pop("httpx", None)

    return _restore


def _run_worker(lines=None, fail_at=None, raise_exc=None):
    """跑一次 LLM 消费者线程，返回 (事件列表, 句队列残量)。"""
    resp = None if raise_exc else _FakeResp(lines if lines is not None else [], fail_at)
    restore = _install_fake_httpx(_FakeHttpx(resp=resp, raise_exc=raise_exc))
    events = []
    sq: "queue.Queue" = queue.Queue()
    eq: "queue.Queue" = queue.Queue()
    cancel = threading.Event()
    splitter = server._SentenceStreamer()
    try:
        server._llm_stream_worker("你好", [], splitter, sq, eq, cancel)
    finally:
        restore()
    while not eq.empty():
        events.append(eq.get())
    rest = []
    while not sq.empty():
        rest.append(sq.get())
    return events, rest


# == 1. 增量切句器：与 mouth._split_sentences 整段切分一致 ==
print("== 1. 增量切句器等价性 ==")
_TEXTS = [
    "好的。先生，今天天气不错。我们去散步吧！",
    "短句。",
    "这是一个没有句尾标点的长句碎片测试文本",
    "第一；第二？第三！\n第四。",
    "好。短。这是一个完整的句子没错。再来一句收尾。",
    "嗯。哦。好的先生我马上就去办这件事情。",
]
for text in _TEXTS:
    expect = mouth._split_sentences(text)
    for chunk_size in (1, 3, 7, len(text)):
        sp = server._SentenceStreamer()
        got = []
        for i in range(0, len(text), chunk_size):
            got.extend(sp.feed(text[i:i + chunk_size]))
        tail = sp.flush()
        if tail:
            got.append(tail)
        check(f"逐{chunk_size}字喂入等价: {text[:14]}…", got == expect,
              f"got={got} expect={expect}")
# 流尾残余：短句碎片 flush 不丢
sp = server._SentenceStreamer()
check("流尾短句残余 flush 不丢", sp.feed("好。") == [] and sp.flush() == "好。")

# == 2. SSE 帧格式 ==
print("== 2. SSE 帧格式 ==")
frame = server._sse_encode({"type": "delta", "text": "你好"})
check("帧头 data: 帧尾 \\n\\n", frame.startswith(b"data: ") and frame.endswith(b"\n\n"))
check("帧负载可解出原对象",
      json.loads(frame[len(b"data: "):-2].decode("utf-8")) == {"type": "delta", "text": "你好"})

# == 3. 分流预检 ==
print("== 3. 分流预检（规则意图回退 / 闲聊放行）==")
_RULE_INPUTS = [
    "提醒我明天早上八点开会",
    "记住我喜欢喝美式咖啡",
    "现在几点了",
    "打开微信",
    "如果明天下雨就提醒我带伞",
    "再见",
    "把今天很开心写到日记.txt",
]
for t in _RULE_INPUTS:
    check(f"规则意图回退: {t}", server._stream_hit_rule_intent(t) is True)
_CHAT_INPUTS = [
    "你觉得人工智能的未来会怎么样",
    "给我讲一个关于机器人的故事",
    "今天天气怎么样",
    "为什么天空是蓝色的",
]
for t in _CHAT_INPUTS:
    check(f"闲聊放行流式: {t}", server._stream_hit_rule_intent(t) is False)

# == 4. LLM 流式消费者（假 httpx）==
print("== 4. LLM 流式消费者 ==")
events, rest = _run_worker(lines=[
    _sse_line("好的。先生，"),
    _sse_line("今天天气不错"),
    _sse_line("。我们去散步吧！"),
    "data: [DONE]",
])
check("正常流: delta 顺序正确",
      [e[1] for e in events if e[0] == "delta"] == ["好的。先生，", "今天天气不错", "。我们去散步吧！"])
check("正常流: llm_done 全量",
      any(e == ("llm_done", "好的。先生，今天天气不错。我们去散步吧！") for e in events))
check("正常流: 句队列=两句+哨兵（短句并入下句）",
      rest == ["好的。先生，今天天气不错。", "我们去散步吧！", None], f"rest={rest}")

events, rest = _run_worker(lines=[_sse_line('{"tool": "get_time", "args": {}}'), "data: [DONE]"])
check("工具 JSON: 立即 abort", ("abort", "tool-json") in events)
check("工具 JSON: 无任何 delta 出场", not any(e[0] == "delta" for e in events))
check("工具 JSON: 句队列只有哨兵", rest == [None], f"rest={rest}")

events, rest = _run_worker(raise_exc=ConnectionError("模拟建连失败"))
check("早期失败: abort stream-failed", ("abort", "stream-failed") in events)

events, rest = _run_worker(lines=[_sse_line("已出场的半句话"), _sse_line("被中断")], fail_at=1)
check("晚期失败: 已有 delta 出场", any(e[0] == "delta" for e in events))
check("晚期失败: 按部分内容 llm_done 收尾",
      ("llm_done", "已出场的半句话") in events, f"events={events}")
check("晚期失败: 流尾残余成句入队", rest == ["已出场的半句话", None], f"rest={rest}")

events, rest = _run_worker(lines=["data: [DONE]"])
check("空流: abort empty", ("abort", "empty") in events)

# == 5. TTS 生产线程（打桩合成，验证保序与哨兵收尾）==
print("== 5. TTS 生产线程 ==")
_orig_synth = server._synth_sentence_url
server._synth_sentence_url = lambda t: "/api/tts/fake_" + str(abs(hash(t)) % 1000) + ".wav"
try:
    sq: "queue.Queue" = queue.Queue()
    eq: "queue.Queue" = queue.Queue()
    for s in ["第一句出场。", "第二句跟上。", None]:
        sq.put(s)
    server._tts_stream_producer(sq, eq, threading.Event())
finally:
    server._synth_sentence_url = _orig_synth
got = []
while not eq.empty():
    got.append(eq.get())
check("生产线程: 句子保序出场",
      [g[1][0] for g in got[:2]] == ["第一句出场。", "第二句跟上。"], f"got={got}")
check("生产线程: 音频 URL 非空", all(g[1][1] for g in got[:2]))
check("生产线程: tts_done 收尾", got[-1] == ("tts_done", None))

# == 6/7. 处理器级全链路（假 httpx + 打桩合成 + 假 wfile）==
print("== 6. 处理器级流式全链路 ==")


class _FakeHandler:
    """假处理器：只实现 _handle_chat_stream 触碰到的表面（wfile 收帧）。"""

    def __init__(self, body: dict):
        self._body = body
        self.wfile_buf = b""
        self.close_connection = False

    def _read_json_body(self):
        return self._body

    def _send_error_json(self, status, message):
        raise AssertionError(f"不应走到错误响应: {status} {message}")

    def send_response(self, status):
        assert status == 200

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def _sse_send(self, obj):
        self.wfile_buf += server._sse_encode(obj)


def _parse_sse(buf: bytes) -> list:
    """把假 wfile 收下的字节流解成事件对象列表。"""
    out = []
    for raw in buf.decode("utf-8").split("\n\n"):
        raw = raw.strip()
        if raw.startswith("data:"):
            out.append(json.loads(raw[len("data:"):].strip()))
    return out


_history_backup = list(server._history)
server._synth_sentence_url = lambda t: "/api/tts/fake.wav"
restore = _install_fake_httpx(_FakeHttpx(resp=_FakeResp([
    _sse_line("好的。先生，"),
    _sse_line("这件事我来办。"),
    "data: [DONE]",
])))
try:
    fake = _FakeHandler({"text": "你觉得人工智能的未来会怎么样"})
    server.NolanHandler._handle_chat_stream(fake)
    evs = _parse_sse(fake.wfile_buf)
finally:
    restore()
    server._synth_sentence_url = _orig_synth
    server._history = _history_backup
check("全链路: 首事件为 delta", evs and evs[0].get("type") == "delta", f"evs={evs}")
check("全链路: sentence 事件带音频 URL",
      any(e.get("type") == "sentence" and e.get("audio_url") == "/api/tts/fake.wav" for e in evs),
      f"evs={evs}")
check("全链路: done 收尾且全量正确",
      evs and evs[-1] == {"type": "done", "reply": "好的。先生，这件事我来办。"},
      f"last={evs[-1] if evs else None}")
check("全链路: 事件序 delta→sentence→done",
      [e["type"] for e in evs] == sorted([e["type"] for e in evs],
                                         key=lambda t: {"delta": 0, "sentence": 1, "done": 2}[t]),
      f"seq={[e['type'] for e in evs]}")

print("== 7. 处理器级回退路径（规则意图 → fallback，含 1 次真实 TTS）==")
_history_backup = list(server._history)
fake = _FakeHandler({"text": "现在几点了"})
server.NolanHandler._handle_chat_stream(fake)  # 预检命中 → _chat 整段 → fallback 事件
evs = _parse_sse(fake.wfile_buf)
server._history = _history_backup
check("回退: 单条 fallback 事件", len(evs) == 1 and evs[0].get("type") == "fallback", f"evs={evs}")
check("回退: reply 为报时文本", bool(evs and evs[0].get("reply")), f"reply={evs[0].get('reply') if evs else None}")
check("回退: audio_url 字段在契约内（可空）",
      bool(evs) and "audio_url" in evs[0],
      f"audio_url={evs[0].get('audio_url') if evs else None}")

# == 8. 真实单句合成（GLM-TTS 1 次 + 缓存命中复验，零额外 API）==
print("== 8. 真实单句合成与缓存 ==")
url1 = server._synth_sentence_url("先生，流式声音通道自检。")
check("真实合成: 返回可服务 URL", isinstance(url1, str) and url1.startswith("/api/tts/"),
      f"url={url1}")
if url1:
    path = os.path.join(server._TTS_CACHE_DIR, url1.rsplit("/", 1)[1])
    check("真实合成: 产物已入缓存目录且非空",
          os.path.isfile(path) and os.path.getsize(path) > 0)
    url2 = server._synth_sentence_url("先生，流式声音通道自检。")
    check("缓存命中: 同文本零合成复用", url2 == url1)

print(f"\n结果：通过 {_PASS}，失败 {_FAIL}")
sys.exit(1 if _FAIL else 0)
