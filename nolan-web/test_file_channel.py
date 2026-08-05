# -*- coding: utf-8 -*-
"""文件通道单元测试（纯逻辑，不起服务、不写真实 jarvis/files/uploads）。

覆盖：
    1.  文本抽取：txt 正常读取
    2.  文本抽取：超过 100KB 截断
    3.  PDF 抽取：reportlab 内存构造样本 → pypdf 抽出文本
    4.  DOCX 抽取：python-docx 内存构造样本 → 抽出段落
    5.  文件名净化：路径剥离、危险字符替换、空名兜底
    6.  大小上限：>20MB 拒绝；>100KB 文本文件正常截断抽取
    7.  不支持类型：明确报错（不乱猜）
    8.  上传目录防护：uploads 不在 files 内时拒绝写入
    9.  files_list 结构：字段齐全、kind 合法、mtime 倒序、内部文件已排除
    10. 路径穿越拒绝：'..' / 绝对路径 / 盘符一律 None；合法相对路径放行

运行：python test_file_channel.py
"""
import importlib.util
import os
import sys
import tempfile

# 动态加载 server.py（不启动 HTTP 服务，只取纯函数，与 test_bargein_echo 同一模式）
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


def make_sample_pdf(path):
    """用 reportlab 在内存里构造一页带文本的 PDF 样本。"""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path)
    c.drawString(72, 720, "Hello Nolan file channel PDF sample")
    c.save()


def make_sample_docx(path):
    """用 python-docx 在内存里构造带段落的 DOCX 样本。"""
    import docx
    doc = docx.Document()
    doc.add_paragraph("第一段：Nolan 文件通道测试。")
    doc.add_paragraph("第二段：拖拽阅读验收。")
    doc.save(path)


with tempfile.TemporaryDirectory() as tmp:
    uploads = os.path.join(tmp, "uploads")

    # 1. 文本抽取：txt 正常读取
    txt_path = os.path.join(tmp, "note.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("先生，这是一段测试文本。")
    text, truncated = server._extract_text(txt_path, ".txt")
    check("1/10 txt 文本抽取", text == "先生，这是一段测试文本。" and not truncated,
          f"text={text!r} truncated={truncated}")

    # 2. 文本抽取：超过 100KB 截断
    big_path = os.path.join(tmp, "big.txt")
    with open(big_path, "w", encoding="utf-8") as f:
        f.write("甲" * (150 * 1024))  # 150KB 汉字（UTF-8 每字 3 字节，远超 100KB）
    text, truncated = server._extract_text(big_path, ".txt")
    check("2/10 超 100KB 文本截断",
          truncated and len(text.encode("utf-8", errors="replace")) <= 100 * 1024 + 8,
          f"truncated={truncated} chars={len(text)}")

    # 3. PDF 抽取（内存构造样本，走完整 _save_upload 链路）
    pdf_path = os.path.join(tmp, "sample.pdf")
    make_sample_pdf(pdf_path)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    stored, text, truncated = server._save_upload(
        "报告.pdf", pdf_bytes, uploads_dir=uploads, files_dir=tmp)
    check("3/10 PDF 抽取", "Nolan file channel" in text and stored.endswith("_报告.pdf"),
          f"stored={stored!r} text={text[:60]!r}")

    # 4. DOCX 抽取（内存构造样本）
    docx_path = os.path.join(tmp, "sample.docx")
    make_sample_docx(docx_path)
    with open(docx_path, "rb") as f:
        docx_bytes = f.read()
    stored, text, _ = server._save_upload(
        "纪要.docx", docx_bytes, uploads_dir=uploads, files_dir=tmp)
    check("4/10 DOCX 抽取", "文件通道测试" in text and "拖拽阅读验收" in text,
          f"stored={stored!r} text={text[:60]!r}")

    # 5. 文件名净化
    check("5a/10 净化：剥离路径",
          server._sanitize_filename("../../../etc/passwd") == "passwd")
    check("5b/10 净化：Windows 反斜杠路径",
          server._sanitize_filename("..\\..\\evil.txt") == "evil.txt")
    check("5c/10 净化：危险字符替换",
          server._sanitize_filename('a<b>:"|?*.txt') == "a_b______.txt",
          server._sanitize_filename('a<b>:"|?*.txt'))
    check("5d/10 净化：空名与纯点兜底",
          server._sanitize_filename("..") == "file"
          and server._sanitize_filename("") == "file")
    check("5e/10 净化：中文名保留",
          server._sanitize_filename("租房合同 注意事项.pdf") == "租房合同_注意事项.pdf",
          server._sanitize_filename("租房合同 注意事项.pdf"))

    # 6. 大小上限
    try:
        server._save_upload("big.txt", b"x" * (server._UPLOAD_MAX_BYTES + 1),
                            uploads_dir=uploads, files_dir=tmp)
        raise AssertionError("6/10 超限文件未被拒绝")
    except ValueError as e:
        check("6/10 超 20MB 拒绝", "上限" in str(e), str(e))

    # 7. 不支持类型：明确报错，且不留文件
    try:
        server._save_upload("evil.exe", b"MZ" + b"\0" * 100,
                            uploads_dir=uploads, files_dir=tmp)
        raise AssertionError("7/10 不支持类型未被拒绝")
    except ValueError as e:
        leftover = os.listdir(uploads) if os.path.isdir(uploads) else []
        check("7/10 不支持类型报错且不留文件",
              "不支持的文件类型" in str(e) and not any(n.endswith(".exe") for n in leftover),
              f"err={e} leftover={leftover}")

    # 8. 上传目录防护：uploads 不在 files 内时拒绝写入
    with tempfile.TemporaryDirectory() as outside:
        try:
            server._save_upload("a.txt", b"hello",
                                uploads_dir=os.path.join(outside, "up"),
                                files_dir=tmp)
            raise AssertionError("8/10 越界目录未被拒绝")
        except ValueError as e:
            check("8/10 uploads 越界拒绝写入", "不在文件柜内" in str(e), str(e))

    # 9. files_list 结构（只读扫描真实 jarvis/files/，不写入）
    payload = server._files_list_payload()
    files = payload.get("files")
    check("9a/10 files_list 返回列表", isinstance(files, list))
    if files:
        item = files[0]
        check("9b/10 files_list 字段齐全",
              all(k in item for k in ("name", "size", "mtime", "kind")),
              f"keys={sorted(item.keys())}")
        check("9c/10 files_list kind 合法",
              all(f["kind"] in ("文档", "图片", "表格", "音频", "其他") for f in files))
        mtimes = [f["mtime"] for f in files]
        check("9d/10 files_list mtime 倒序", mtimes == sorted(mtimes, reverse=True))
        check("9e/10 files_list 排除内部文件",
              not any(f["name"].startswith("tts_cache/")
                      or f["name"] == "server.pid"
                      or f["name"].endswith(".log") for f in files),
              [f["name"] for f in files][:5])
    else:
        print("   （文件柜当前为空，结构子项跳过）")

    # 10. 路径穿越拒绝
    check("10a/10 穿越 '..'", server._resolve_files_path("../brain.py") is None)
    check("10b/10 穿越反斜杠", server._resolve_files_path("..\\brain.py") is None)
    check("10c/10 盘符拒绝", server._resolve_files_path("C:\\Windows\\x.dll") is None)
    check("10d/10 空名拒绝", server._resolve_files_path("") is None)
    ok_path = server._resolve_files_path("uploads/20260101-000000_a.txt")
    check("10e/10 合法子目录放行",
          ok_path is not None
          and os.path.commonpath([ok_path, server._FILES_DIR]) == server._FILES_DIR,
          repr(ok_path))

print(f"\n🎉 文件通道单元测试全过（{_checks} 项断言）")
