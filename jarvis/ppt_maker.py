# -*- coding: utf-8 -*-
"""
Nolan · PPT 引擎（ppt_maker.py）——两阶段精写版
一句话生成带演讲稿的真 .pptx 文件：
  - 阶段 1 · 大纲：1 次 LLM 调用产出 总标题 + 每页 {page_title, core_point, keywords}；
  - 阶段 2 · 逐页精写：每页 1 次 LLM 调用（串行），产出 4-6 条要点（每条 30-60 字）
    与 150-250 字演讲稿；每页过质量闸，不达标重写（最多 2 次），仍不达标取历次最好；
    某页调用异常时用大纲 core_point 扩成兜底要点，页数永不缺斤短两；
  - 文件：python-pptx 造 16:9 幻灯片，正文字号按内容密度三档自适应（18/16/14pt），
    每页演讲稿写进 notes_slide（PowerPoint/WPS 备注窗格可见）；
  - 落盘：jarvis/files/ 下，文件名 {主题安全名}_{YYYYMMDD-HHMM}.pptx；
  - 降级：大纲阶段失败 -> ok=False；逐页阶段任何单页失败 -> 兜底页顶上，绝不整单失败。

集成契约（签名冻结，主控按此接线）：
    make_ppt(topic: str, pages: int = 8, style: str = "工作汇报", llm_caller=None) -> dict
    成功 {"ok": True, "path": 绝对路径, "file_name": "xxx.pptx", "pages": 物理总页数(含封面), "title": "..."}
    失败 {"ok": False, "error": "人话原因"}

pages 参数语义：向 LLM 请求的内容页数量（钳制 3-20）；返回值里的 pages 是物理总页数（内容页 + 1 封面）。

耗时预期：N 内容页 = 1 + N 次串行 LLM 调用，10 页约 60-120 秒。
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

MAX_BULLETS = 6
MAX_BULLET_LEN = 80       # 防御性截断（要求 LLM 30-60 字，留余量）

# ---- 质量闸（逐页验收）----
GATE_MIN_BULLETS = 4                 # 每页至少 4 条要点
GATE_MIN_CHARS_DEFAULT = 180         # 正文总字数基线（科普分享等）
GATE_MIN_CHARS_STRICT = 220          # 工作汇报 / 课堂讲解 更严
GATE_STRICT_STYLES = {"工作汇报", "课堂讲解"}
MAX_REWRITES = 2                     # 每页最多重写 2 次（即最多 3 次尝试）

# 最近一次 make_ppt 的运行统计（供验收/调试读取，不属于公开契约）
last_run: dict = {}


# ---------------------------------------------------------------- 通用：LLM 输出解析

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


_REPAIR_PROMPT = """你上一次的输出无法被解析为 JSON。请重新输出：只输出一个合法 JSON 对象本身——不要 markdown 围栏、不要解释、不要任何多余文字。第一个字符必须是 {{，最后一个字符必须是 }}。

原始需求：
{prompt}"""


def _call_json(prompt: str, caller, repair_once: bool = True):
    """调 LLM 并解析 JSON；解析失败可按需修复重试 1 次。返回 dict 或 None。"""
    try:
        raw = caller(prompt)
    except Exception:
        raise                      # 调用异常上交，由调用方决定兜底策略
    data = _extract_json(str(raw)) if raw and str(raw).strip() else None
    if data is None and repair_once:
        try:
            raw2 = caller(_REPAIR_PROMPT.format(prompt=prompt))
        except Exception:
            raw2 = None
        if raw2 and str(raw2).strip():
            data = _extract_json(str(raw2))
    return data


# ---------------------------------------------------------------- 阶段 1：大纲

_OUTLINE_PROMPT = """你是资深演示文稿策划。请为主题「{topic}」设计一份 {pages} 页的{style} PPT 大纲。

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 围栏。格式严格如下：
{{
  "title": "整套 PPT 的主标题（≤20字）",
  "pages": [
    {{
      "page_title": "本页小标题（≤15字）",
      "core_point": "本页核心论点，一句话（30-50字）：必须具体、有信息量，是本页正文要论证的靶心",
      "keywords": ["本页 3-5 个关键词：具体的技术名词、数据点、案例名或机制名"]
    }}
  ]
}}

要求：
- pages 数组恰好 {pages} 项，对应 {pages} 页内容；
- 全篇逻辑递进（背景/现状 -> 核心内容 -> 总结/行动），页与页之间分工明确、互不重复；
- 每页 core_point 必须是可论证的具体论点，禁止「介绍XX」「XX很重要」式空话；
- keywords 要能给后续写作提供弹药（名词、数字、案例），不要空泛形容词；
- 内容贴合「{style}」场景。"""


def _gen_outline(topic: str, pages: int, style: str, caller):
    """阶段 1：拿大纲。返回 (规范化大纲 dict, None) 或 (None, 人话错误)。"""
    prompt = _OUTLINE_PROMPT.format(topic=topic, pages=pages, style=style)
    try:
        data = _call_json(prompt, caller, repair_once=True)
    except Exception:
        return None, "大脑调用时出错了，请稍后再试"
    if data is None:
        return None, "大脑返回的内容格式乱了（不是合法 JSON），重试后仍失败，稍后再试"

    title = str(data.get("title") or "").strip()[:30]
    pages_raw = data.get("pages")
    if not title or not isinstance(pages_raw, list) or not pages_raw:
        return None, "大脑返回的大纲结构不完整（缺标题或缺页面），稍后再试"

    outline_pages = []
    for i, item in enumerate(pages_raw[:pages]):
        if not isinstance(item, dict):
            continue
        page_title = str(item.get("page_title") or "").strip()[:20] or f"第 {i + 1} 节"
        core_point = str(item.get("core_point") or "").strip()
        if not core_point:
            core_point = f"围绕「{page_title}」展开本页的核心内容与关键结论。"
        keywords_raw = item.get("keywords")
        keywords = []
        if isinstance(keywords_raw, list):
            for k in keywords_raw[:6]:
                k = str(k).strip()
                if k:
                    keywords.append(k[:20])
        outline_pages.append({
            "page_title": page_title,
            "core_point": core_point,
            "keywords": keywords,
        })
    if not outline_pages:
        return None, "大脑返回的大纲结构不完整（缺标题或缺页面），稍后再试"
    return {"title": title, "pages": outline_pages}, None


# ---------------------------------------------------------------- 阶段 2：逐页精写

# 风格写作要求：注入逐页 prompt，让口吻与密度匹配场景
_STYLE_HINTS = {
    "工作汇报": "专业克制、数据导向：多用结论与数字说话，每条要点给出指标、对比或明确行动",
    "课堂讲解": "循序渐进、善用类比：从直觉到机制，类比要贴切，术语首次出现时用一句话解释",
    "科普分享": "通俗但不低幼、保证事实密度：用大白话讲清机制，但每条都要落到具体事实、数字或案例上",
}

_PAGE_PROMPT = """你正在为一份{style} PPT 逐页精写正文，现在写第 {idx}/{total} 页。

【全篇背景】
- 总主题：「{topic}」
- 整套 PPT 标题：「{deck_title}」
- 前一页标题：「{prev_title}」；后一页标题：「{next_title}」（内容不得与它们重复撞车）

【本页任务】
- 本页小标题：「{page_title}」
- 本页核心论点（必须围绕它展开论证）：{core_point}
- 可用素材关键词：{keywords}

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 围栏。格式严格如下：
{{
  "bullets": ["要点1", "要点2", "要点3", "要点4"],
  "speaker_note": "本页演讲稿"
}}

硬性要求（验收线，达不到会被退回重写）：
- bullets 必须 4 到 6 条；每条 30 到 60 字；
- 每条要点必须承载具体信息：真实数据、具体案例、机制解释或可操作结论，
  禁止「AI很强大」「前景广阔」这类空话套话；
- 要点之间互补不重复，合起来要能论证本页核心论点；
- 风格要求：{style_hint}；
- speaker_note 为 150-250 字的口语化演讲稿：这页怎么开场、要点之间怎么串、
  如何自然过渡到下一页，是真人上台能照着讲的稿子，不是要点的复读。"""

# 重写 prompt：带上一次不达标的具体原因，换措辞再要一次
_PAGE_REWRITE_PROMPT = """你上一次为这一页写的内容没有通过验收，需要重写。

【不达标原因】{feedback}

请换一种写法重新撰写：补充更具体的数据、案例或机制细节，把篇幅写到线以上。

{original_prompt}"""


def _gate_min_chars(style: str) -> int:
    """质量闸字数线：工作汇报/课堂讲解更严。"""
    return GATE_MIN_CHARS_STRICT if style in GATE_STRICT_STYLES else GATE_MIN_CHARS_DEFAULT


def _norm_bullets(raw) -> list:
    """归一要点列表：清洗、截断、去空。"""
    bullets = []
    if isinstance(raw, list):
        for b in raw[:MAX_BULLETS]:
            b = str(b).strip()
            if b:
                bullets.append(b[:MAX_BULLET_LEN])
    return bullets


def _quality_check(bullets: list, style: str):
    """质量闸：要点数 >= 4 且正文总字数达标。返回 (是否通过, 不达标原因)。"""
    total_chars = sum(len(b) for b in bullets)
    min_chars = _gate_min_chars(style)
    if len(bullets) < GATE_MIN_BULLETS:
        return False, f"要点只有 {len(bullets)} 条（要求至少 {GATE_MIN_BULLETS} 条），正文共 {total_chars} 字（要求 ≥{min_chars} 字）"
    if total_chars < min_chars:
        return False, f"正文总字数只有 {total_chars} 字（要求 ≥{min_chars} 字），要点普遍太短、缺少具体信息"
    return True, ""


def _synthesize_note(page_title: str, bullets: list) -> str:
    """LLM 没给演讲稿时兜底合成，保证 notes 物理非空。"""
    return (f"这一页讲「{page_title}」。开场先用一句话点明本页核心，"
            f"然后围绕这几点逐条展开：" + "；".join(bullets) +
            "。每条要点讲透一个事实再往下走，全部讲完后用一句小结收住，"
            "自然过渡到下一页。建议用时约 2 分钟。")


def _fallback_page(item: dict) -> dict:
    """某页 LLM 彻底失败时的兜底页：用大纲 core_point + keywords 扩成要点，
    保证页数完整、内容贴题，绝不整单失败。"""
    cp = item["core_point"]
    kws = item.get("keywords") or []
    # 按句切 core_point，尽量保留原话
    parts = [p.strip() for p in re.split(r"[。；;！!？?]", cp) if p.strip()]
    bullets = []
    for p in parts[:MAX_BULLETS]:
        bullets.append(p[:MAX_BULLET_LEN])
    # 句数不够时用关键词补弹药
    for kw in kws:
        if len(bullets) >= GATE_MIN_BULLETS:
            break
        bullets.append(f"关于「{kw}」：{cp[:40]}，细节以现场讲解为准。"[:MAX_BULLET_LEN])
    while len(bullets) < 3:
        bullets.append(f"本页核心：{cp[:MAX_BULLET_LEN - 6]}")
    note = (f"这一页讲「{item['page_title']}」。核心论点是：{cp}"
            f"围绕{'、'.join(kws) if kws else '上述要点'}逐条展开说明，"
            "讲清事实与机制后自然过渡到下一页。建议用时约 2 分钟。")
    return {"bullets": bullets, "speaker_note": note,
            "rewrites": 0, "fallback": True}


def _gen_page(topic: str, deck_title: str, style: str, item: dict,
              idx: int, total: int, prev_title: str, next_title: str,
              caller) -> dict:
    """阶段 2 单页：精写 -> 质量闸 -> 不达标重写（最多 2 次）-> 取历次最好 -> 兜底。
    返回 {bullets, speaker_note, rewrites, fallback}。"""
    style_hint = _STYLE_HINTS.get(style, _STYLE_HINTS["科普分享"])
    prompt = _PAGE_PROMPT.format(
        style=style, idx=idx, total=total, topic=topic, deck_title=deck_title,
        prev_title=prev_title, next_title=next_title,
        page_title=item["page_title"], core_point=item["core_point"],
        keywords="、".join(item["keywords"]) if item["keywords"] else "（无）",
        style_hint=style_hint)

    candidates = []   # 历次有效候选：(bullets, note, 是否达标)
    feedback = ""
    for attempt in range(1 + MAX_REWRITES):
        this_prompt = prompt if attempt == 0 else _PAGE_REWRITE_PROMPT.format(
            feedback=feedback, original_prompt=prompt)
        try:
            data = _call_json(this_prompt, caller, repair_once=False)
        except Exception:
            # 调用异常：有候选就见好就收，没有就直接兜底，不再烧调用
            break
        if data is None:
            feedback = "输出不是合法 JSON，无法解析"
            continue
        bullets = _norm_bullets(data.get("bullets"))
        note = str(data.get("speaker_note") or "").strip()
        ok, reason = _quality_check(bullets, style)
        if bullets:
            if not note:
                note = _synthesize_note(item["page_title"], bullets)
            candidates.append((bullets, note, ok))
        if ok:
            return {"bullets": bullets, "speaker_note": note,
                    "rewrites": attempt, "fallback": False}
        feedback = reason if bullets else ("输出缺少 bullets 列表。" + reason)

    if candidates:
        # 重写仍不达标：取历次最好（正文字数最多者），绝不整单失败
        bullets, note, _ = max(candidates, key=lambda c: sum(len(b) for b in c[0]))
        return {"bullets": bullets, "speaker_note": note,
                "rewrites": MAX_REWRITES, "fallback": False}
    return _fallback_page(item)


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


# 正文区物理参数：宽 11.2 英寸、高 5.2 英寸；16:9 下每行约 40 个全角字（18pt）
_BODY_CHARS_PER_LINE = 40


def _fit_body_font(bullets: list):
    """行数自适应：估算排版行数与总字数，超阈值就缩字号（18 -> 16 -> 14 三档），
    连带压缩段后距，保证高密度要点不溢出文本框。"""
    total = sum(len(b) for b in bullets)
    lines = sum(max(1, (len(b) + 3 + _BODY_CHARS_PER_LINE - 1) // _BODY_CHARS_PER_LINE)
                for b in bullets)   # +3 是「•  」前缀
    if total <= 240 and lines <= 7:
        return 18, 0.18
    if total <= 320 and lines <= 9:
        return 16, 0.14
    return 14, 0.10


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

        font_pt, gap_in = _fit_body_font(sl["bullets"])
        tf_b = _add_textbox(s, 1.1, 1.7, 11.2, 5.2)
        for j, bullet in enumerate(sl["bullets"]):
            pb = tf_b.paragraphs[0] if j == 0 else tf_b.add_paragraph()
            pb.space_after = Inches(gap_in)
            rb = pb.add_run()
            rb.text = "•  " + bullet
            _set_run_font(rb, font_pt, COLOR_BODY)

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
    global last_run
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
    stats = {"outline_retries": 0, "page_stats": [], "llm_calls": 0}
    last_run = stats

    # ---- 阶段 1：大纲（失败即整单失败，没有大纲就没有弹药）
    outline, err = _gen_outline(topic, pages, style, caller)
    if outline is None:
        return {"ok": False, "error": err}

    # ---- 阶段 2：逐页精写（串行；单页失败只影响单页，兜底页顶上）
    deck_pages = outline["pages"]
    titles = [p["page_title"] for p in deck_pages]
    slides = []
    for i, item in enumerate(deck_pages):
        prev_t = titles[i - 1] if i > 0 else "（封面）"
        next_t = titles[i + 1] if i + 1 < len(titles) else "（结束页）"
        page = _gen_page(topic, outline["title"], style, item,
                         i + 1, len(deck_pages), prev_t, next_t, caller)
        stats["page_stats"].append({
            "page": i + 1, "title": item["page_title"],
            "bullets": len(page["bullets"]),
            "chars": sum(len(b) for b in page["bullets"]),
            "rewrites": page["rewrites"], "fallback": page["fallback"],
        })
        slides.append({"heading": item["page_title"],
                       "bullets": page["bullets"],
                       "speaker_notes": page["speaker_note"]})

    try:
        out_path = _alloc_path(topic)
        _build_pptx(outline["title"], slides, style, out_path)
    except Exception as e:
        return {"ok": False, "error": f"PPT 文件生成失败：{e}"}

    return {
        "ok": True,
        "path": str(out_path.resolve()),
        "file_name": out_path.name,
        "pages": len(slides) + 1,   # 物理总页数 = 内容页 + 封面
        "title": outline["title"],
    }
