# -*- coding: utf-8 -*-
"""
Nolan · PPT 引擎（ppt_maker.py）
一句话生成带演讲稿的真 .pptx 文件：
  - 内容：单次 LLM 调用（注入式 llm_caller，默认 brain.glm_one_shot）返回严格 JSON；
  - 文件：python-pptx 造 16:9 幻灯片，每页演讲稿写进 notes_slide（PowerPoint/WPS 备注窗格可见）；
  - 落盘：jarvis/files/ 下，文件名 {主题安全名}_{YYYYMMDD-HHMM}.pptx；
  - 降级：LLM 无响应 / 输出无法解析 -> ok=False，不造空壳冒充成功。

集成契约（签名冻结，主控按此接线）：
    make_ppt(topic: str, pages: int = 8, style: str = "工作汇报", llm_caller=None) -> dict
    成功 {"ok": True, "path": 绝对路径, "file_name": "xxx.pptx", "pages": 物理总页数(含封面), "title": "..."}
    失败 {"ok": False, "error": "人话原因"}

pages 参数语义：向 LLM 请求的内容页数量（钳制 3-20）；返回值里的 pages 是物理总页数（内容页 + 1 封面）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
FILES_DIR = MODULE_DIR / "files"

MIN_PAGES = 3
MAX_PAGES = 20

# Nolan 视觉规范：低饱和暖色；禁蓝紫渐变、禁高饱和背景
COLOR_TITLE = "3B322C"    # 深棕灰（标题）
COLOR_BODY = "4A4440"     # 正文
COLOR_ACCENT = "C0604A"   # 陶土（强调）
COLOR_BG = "FAF7F2"       # 米白（背景）
FONT_NAME = "微软雅黑"

MAX_BULLETS = 5
MAX_BULLET_LEN = 40       # 防御性截断（要求 LLM ≤30 字，留余量）


# ---------------------------------------------------------------- LLM 内容生成

_PROMPT_TMPL = """你是资深演示文稿策划。请为主题「{topic}」设计一份 {pages} 页的{style} PPT 大纲。

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 围栏。格式严格如下：
{{
  "title": "整套 PPT 的主标题（≤20字）",
  "slides": [
    {{
      "heading": "本页小标题（≤15字）",
      "bullets": ["3到5条要点，每条≤30字，口语化、有信息量"],
      "speaker_notes": "本页演讲稿：这页讲什么、怎么讲（开场怎么引入、要点怎么串）、建议用时几分钟。口语化中文，100-200字。"
    }}
  ]
}}

要求：
- slides 数组恰好 {pages} 项；
- 内容贴合「{style}」场景，逻辑递进（背景/现状 -> 核心内容 -> 总结/行动）；
- bullets 每条必须是完整短句，不要只有关键词；
- speaker_notes 是真人上台能照着讲的稿子，不是要点的复读。"""

_REPAIR_PROMPT = """你上一次的输出无法被解析为 JSON。请重新输出：只输出一个合法 JSON 对象本身——不要 markdown 围栏、不要解释、不要任何多余文字。第一个字符必须是 {{，最后一个字符必须是 }}。

原始需求：
{prompt}"""


def _default_llm_caller():
    from brain import glm_one_shot  # 延迟导入，避免模块加载即拖起大脑
    return glm_one_shot


def _extract_json(text: str):
    """从 LLM 输出中提取 JSON 对象：先去 markdown 围栏直解，失败再做花括号平衡扫描。"""
    if not text:
        return None
    s = text.strip()
    # 拆围栏：```json ... ``` / ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    candidates = [s]
    if fence:
        candidates.insert(0, fence.group(1).strip())
    for cand in candidates:
        # 直解
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        # 花括号平衡扫描（尊重字符串与转义）
        start = cand.find("{")
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cand)):
            ch = cand[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cand[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
    return None


def _gen_content(topic: str, pages: int, style: str, llm_caller):
    """调 LLM 拿大纲；解析失败重试一次；再失败返回 None。"""
    prompt = _PROMPT_TMPL.format(topic=topic, pages=pages, style=style)
    try:
        raw = llm_caller(prompt)
    except Exception:
        return None, "大脑调用时出错了，请稍后再试"
    if not raw or not str(raw).strip():
        return None, "大脑这会儿没回话（LLM 无响应），没造空壳 PPT 糊弄你，稍后再试"

    data = _extract_json(str(raw))
    if data is None:
        # 重试一次：明确要求只输出 JSON
        try:
            raw2 = llm_caller(_REPAIR_PROMPT.format(prompt=prompt))
        except Exception:
            raw2 = None
        if raw2 and str(raw2).strip():
            data = _extract_json(str(raw2))
    if data is None:
        return None, "大脑返回的内容格式乱了（不是合法 JSON），重试后仍失败，稍后再试"
    return data, None


def _normalize(data: dict, pages: int):
    """把 LLM JSON 归一成可靠结构；结构太烂返回 None。"""
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()[:30]
    slides_raw = data.get("slides")
    if not title or not isinstance(slides_raw, list) or not slides_raw:
        return None
    slides = []
    for i, item in enumerate(slides_raw[:pages]):
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "").strip()[:20] or f"第 {i + 1} 节"
        bullets_raw = item.get("bullets")
        bullets = []
        if isinstance(bullets_raw, list):
            for b in bullets_raw[:MAX_BULLETS]:
                b = str(b).strip()
                if b:
                    bullets.append(b[:MAX_BULLET_LEN])
        if not bullets:
            bullets = [heading]
        notes = str(item.get("speaker_notes") or "").strip()
        if not notes:
            notes = (f"这一页讲「{heading}」。围绕这几点展开："
                     + "；".join(bullets)
                     + "。建议用时约 1 分钟，讲完要点后自然过渡到下一页。")
        slides.append({"heading": heading, "bullets": bullets, "speaker_notes": notes})
    if not slides:
        return None
    return {"title": title, "slides": slides}


# ---------------------------------------------------------------- PPTX 造文件

def _rgb(hex_str: str):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_str)


def _set_run_font(run, size_pt, color_hex, bold=False):
    from pptx.util import Pt
    from pptx.oxml.ns import qn
    f = run.font
    f.size = Pt(size_pt)
    f.bold = bold
    f.color.rgb = _rgb(color_hex)
    f.name = FONT_NAME
    # 中文字形需显式设置 East Asian typeface
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", FONT_NAME)


def _set_bg(slide):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb(COLOR_BG)


def _add_textbox(slide, left, top, width, height):
    from pptx.util import Inches
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    return tf


def _add_accent_bar(slide, left, top, width, height):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(COLOR_ACCENT)
    bar.line.fill.background()
    bar.shadow.inherit = False
    return bar


def _build_pptx(title: str, slides: list, style: str, out_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches
    from pptx.enum.text import PP_ALIGN

    prs = Presentation()
    prs.slide_width = Inches(13.333)   # 16:9
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # ---- 封面
    s = prs.slides.add_slide(blank)
    _set_bg(s)
    _add_accent_bar(s, 5.667, 2.55, 2.0, 0.07)
    tf = _add_textbox(s, 1.0, 2.8, 11.333, 1.6)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _set_run_font(p.add_run(), 40, COLOR_TITLE, bold=True)
    p.runs[0].text = title
    tf2 = _add_textbox(s, 1.0, 4.4, 11.333, 0.8)
    p2 = tf2.paragraphs[0]
    p2.alignment = PP_ALIGN.CENTER
    r2 = p2.add_run()
    r2.text = f"{style} · {time.strftime('%Y年%m月%d日')}"
    _set_run_font(r2, 16, COLOR_BODY)
    s.notes_slide.notes_text_frame.text = (
        f"开场白：各位好，今天汇报的主题是「{title}」。"
        f"先用一句话点明这次分享的核心价值，再交代整体结构（共 {len(slides)} 个部分），"
        "语速放慢，与听众做一次眼神交流。建议用时约 30 秒。")

    # ---- 内容页
    for idx, sl in enumerate(slides, start=1):
        s = prs.slides.add_slide(blank)
        _set_bg(s)
        _add_accent_bar(s, 0.6, 0.62, 0.09, 0.62)
        tf_h = _add_textbox(s, 0.9, 0.5, 11.8, 0.9)
        ph = tf_h.paragraphs[0]
        rh = ph.add_run()
        rh.text = f"{idx:02d}  {sl['heading']}"
        _set_run_font(rh, 28, COLOR_TITLE, bold=True)

        tf_b = _add_textbox(s, 1.1, 1.7, 11.2, 5.2)
        for j, bullet in enumerate(sl["bullets"]):
            pb = tf_b.paragraphs[0] if j == 0 else tf_b.add_paragraph()
            pb.space_after = Inches(0.18)
            rb = pb.add_run()
            rb.text = "•  " + bullet
            _set_run_font(rb, 18, COLOR_BODY)

        # 演讲稿物理写入备注（PowerPoint/WPS 备注窗格可见）
        s.notes_slide.notes_text_frame.text = sl["speaker_notes"]

    prs.save(str(out_path))


# ---------------------------------------------------------------- 落盘

_SAFE_KEEP = re.compile(r"[0-9A-Za-z一-鿿_-]+")


def _safe_name(topic: str) -> str:
    parts = _SAFE_KEEP.findall(topic)
    name = "_".join(p for p in parts if p)[:30].strip("_")
    return name or "未命名"


def _alloc_path(topic: str) -> Path:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    base = f"{_safe_name(topic)}_{stamp}"
    path = FILES_DIR / f"{base}.pptx"
    n = 2
    while path.exists():
        path = FILES_DIR / f"{base}-{n}.pptx"
        n += 1
    return path


# ---------------------------------------------------------------- 公开 API

def make_ppt(topic: str, pages: int = 8, style: str = "工作汇报", llm_caller=None) -> dict:
    """一句话生成带演讲稿的 .pptx。契约见模块 docstring。"""
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "没说 PPT 主题，巧妇难为无米之炊"}
    style = (style or "工作汇报").strip() or "工作汇报"
    try:
        pages = int(pages)
    except (TypeError, ValueError):
        pages = 8
    pages = max(MIN_PAGES, min(MAX_PAGES, pages))

    caller = llm_caller or _default_llm_caller()
    data, err = _gen_content(topic, pages, style, caller)
    if data is None:
        return {"ok": False, "error": err}

    norm = _normalize(data, pages)
    if norm is None:
        return {"ok": False, "error": "大脑返回的大纲结构不完整（缺标题或缺页面），稍后再试"}

    try:
        out_path = _alloc_path(topic)
        _build_pptx(norm["title"], norm["slides"], style, out_path)
    except Exception as e:
        return {"ok": False, "error": f"PPT 文件生成失败：{e}"}

    return {
        "ok": True,
        "path": str(out_path.resolve()),
        "file_name": out_path.name,
        "pages": len(norm["slides"]) + 1,   # 物理总页数 = 内容页 + 封面
        "title": norm["title"],
    }
