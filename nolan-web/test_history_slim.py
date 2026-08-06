# -*- coding: utf-8 -*-
"""历史瘦身单元测试（纯逻辑，不起服务、不写真实 jarvis/files/uploads）。

目标机制：前端把附件抽取全文拼进用户消息（标记块
[附件《存储名》内容开始]…[附件内容结束，请基于以上内容回答]）；
server._slim_for_history 在写入 _history 前把附件块替换为紧凑存根
（文件名/字数/uploads/*.extracted.txt 回读路径），全文不再躺历史，
LLM 需要时经 read_file 工具回读（hands._sandbox_path 特许 uploads/ 一层子目录）。

覆盖：
    1. 无附件消息原样返回（含普通文本里出现「附件」二字不误伤）
    2. 单附件块替换为存根：文件名/字数/回读路径正确，问题文本保留，全文消失
    3. 多附件逐块替换
    4. 残缺标记保守原样返回（缺结尾标记 / 头部不完整）
    5. 落盘副本缺失时存根降级（不指读不到的路），全文仍被移出
    6. 文件名净化与 upload 落盘规则对齐（不安全字符 → '_'）
    7. _chat 端到端（mock brain/synth_for/mouth）：brain 收到全文，
       _history 里只留存根；第二轮 brain 收到的历史也只有存根

运行：python test_history_slim.py
"""
import importlib.util
import os
import sys
import tempfile

# 动态加载 server.py（不启动 HTTP 服务，只取纯函数，与 test_file_channel 同一模式）
spec = importlib.util.spec_from_file_location(
    "nolan_server", os.path.join(os.path.dirname(__file__), "server.py"))
server = importlib.util.module_from_spec(spec)
sys.modules["nolan_server"] = server
spec.loader.exec_module(server)

_checks = 0


def check(name, cond, detail=""):
    global _checks
    _checks += 1
    assert cond, f"{name}: {detail}"
    print(f"✅ {name}")


def att_block(name, body):
    """按前端 ChatApp.tsx 的拼接格式构造一个附件块。"""
    return f"[附件《{name}》内容开始]\n{body}\n[附件内容结束，请基于以上内容回答]\n"


def main():
    slim = server._slim_for_history

    with tempfile.TemporaryDirectory() as uploads:
        # -- 1. 无附件消息原样返回 --
        plain = "先生今天天气怎么样，附件两个字只是普通话题"
        check("无附件消息原样返回", slim(plain, uploads) == plain)
        check("空消息原样返回", slim("", uploads) == "")

        # -- 2. 单附件块替换为存根 --
        name1 = "20260806-120000_周报.txt"
        body1 = "本周完成了历史瘦身改造。" * 100  # 1700 字，够分量
        with open(os.path.join(uploads, name1 + ".extracted.txt"),
                  "w", encoding="utf-8") as f:
            f.write(body1 + "（落盘的是全文，可能更长）")
        msg = att_block(name1, body1) + "帮我总结一下这份周报"
        out = slim(msg, uploads)
        rel1 = f"uploads/{name1}.extracted.txt"
        stub1 = (f"「附件《{name1}》（共{len(body1)}字，全文已存 {rel1}，"
                 f"可用 read_file 工具回读「{rel1}」）」")
        check("单附件：存根格式精确匹配", stub1 in out, out)
        check("单附件：附件全文已移出", body1 not in out)
        check("单附件：问题文本原样保留", out.endswith("帮我总结一下这份周报"))
        check("单附件：起止标记均已清除",
              "[附件《" not in out and "附件内容结束" not in out)

        # -- 3. 多附件逐块替换 --
        name2 = "20260806-120501_会议纪要.md"
        body2 = "纪要要点：瘦身优先。" * 50
        with open(os.path.join(uploads, name2 + ".extracted.txt"),
                  "w", encoding="utf-8") as f:
            f.write(body2)
        msg2 = att_block(name1, body1) + att_block(name2, body2) + "两份对比一下"
        out2 = slim(msg2, uploads)
        check("多附件：两块均替换为存根",
              stub1 in out2 and f"「附件《{name2}》（共{len(body2)}字" in out2)
        check("多附件：两份全文均已移出", body1 not in out2 and body2 not in out2)
        check("多附件：问题文本保留", out2.endswith("两份对比一下"))

        # -- 4. 残缺标记保守原样返回 --
        broken_no_end = f"[附件《{name1}》内容开始]\n{body1}\n（后面没有结束标记）"
        check("残缺（缺结束标记）原样返回", slim(broken_no_end, uploads) == broken_no_end)
        broken_head = f"[附件《{name1}\n{body1}\n[附件内容结束，请基于以上内容回答]"
        check("残缺（头部不完整）原样返回", slim(broken_head, uploads) == broken_head)
        mixed = att_block(name1, body1) + broken_no_end
        check("完整块+残缺块并存：整条原样返回", slim(mixed, uploads) == mixed)

        # -- 5. 落盘副本缺失：存根降级，不指读不到的路 --
        name3 = "20260806-121000_图片解读.txt"
        body3 = "（VLM 对图片的解读文本，无 .extracted.txt 落盘）"
        msg3 = att_block(name3, body3) + "这图讲了什么"
        out3 = slim(msg3, uploads)
        check("落盘缺失：存根降级为无回读路径",
              f"「附件《{name3}》（共{len(body3)}字，无全文落盘副本可回读）」" in out3,
              out3)
        check("落盘缺失：不给 read_file 指路", "read_file" not in out3)
        check("落盘缺失：全文仍被移出", body3 not in out3)

        # -- 6. 文件名净化与 upload 落盘对齐 --
        raw_name = "20260806-122000_报 告（终版）.txt"  # 空格/括号按规则净化为 '_'
        safe_name = server._sanitize_filename(raw_name)
        assert safe_name != raw_name and " " not in safe_name
        body4 = "净化名对齐测试正文。"
        with open(os.path.join(uploads, safe_name + ".extracted.txt"),
                  "w", encoding="utf-8") as f:
            f.write(body4)
        out4 = slim(att_block(raw_name, body4), uploads)
        check("净化对齐：存根路径用净化名",
              f"uploads/{safe_name}.extracted.txt" in out4, out4)

        # -- 7. _chat 端到端：brain 收全文，历史留存根 --
        captured = []

        class FakeBrain:
            @staticmethod
            def think(text, history):
                captured.append({"text": text, "history": history})
                return "好的先生，已读。"

        orig_brain, orig_synth, orig_mouth = server.brain, server.synth_for, server.mouth
        orig_history = server._history
        try:
            server.brain = FakeBrain
            server.synth_for = lambda _t: None  # 不联网合成
            server.mouth = None                 # 不碰音箱打断
            server._history = []

            r1 = server._chat(msg)
            check("端到端：响应契约不变", r1["reply"] == "好的先生，已读。"
                  and r1["audio_url"] is None and "exit" not in r1)
            check("端到端：brain 当前轮收到附件全文", body1 in captured[0]["text"])
            # _chat 内部 slim 走默认 _UPLOADS_DIR（真实目录无此测试文件 → 降级存根），
            # 断言「历史内容 == 默认路径下的 slim 结果」即可验证接线正确
            expected_slim = server._slim_for_history(msg)
            hist_user = server._history[0]["content"]
            check("端到端：历史里是 slim 版（降级存根）而非原文",
                  hist_user == expected_slim and hist_user != msg)
            check("端到端：历史里附件全文已移出、文件名与字数在",
                  body1 not in hist_user
                  and f"「附件《{name1}》（共{len(body1)}字" in hist_user)
            check("端到端：历史 assistant 轮正常落账",
                  server._history[1] == {"role": "assistant", "content": "好的先生，已读。"})

            server._chat("第二轮追问")
            check("端到端：第二轮 brain 拿到的历史只有存根",
                  captured[1]["history"][0]["content"] == expected_slim
                  and body1 not in captured[1]["history"][0]["content"])
        finally:
            server.brain, server.synth_for, server.mouth = orig_brain, orig_synth, orig_mouth
            server._history = orig_history

    print(f"\n全部 {_checks} 项检查通过。")


if __name__ == "__main__":
    main()
