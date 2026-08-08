# -*- coding: utf-8 -*-
"""
Nolan · PPT 对话式编辑器（ppt_editor.py）——「说改就改」
按自然语言指令修改文件柜里已生成的 PPT：
  - 定位：file_name 只取裸文件名（拒绝任何路径成分，防路径穿越），
    在 jarvis/files/ 下找 {stem}.deck.json 编辑存档；无存档 → 人话说明（旧版生成，请重做）；
  - 修改计划：一次 LLM 调用（brain.glm_one_shot 惰性导入，复用 ppt_maker 的 JSON 解析），
    全页清单（物理页码 + 版式 + 标题）+ 用户指令 -> JSON 计划
    {page, action: rewrite|layout|image|title, detail, new_layout?, new_title?, image_prompt?}；
    非法/缺字段/页码越界 -> 人话解释；
  - 四种动作：
    rewrite  用存档 outline 对应项 + deck 上下文（deck_title、前后页标题、research）
             调 ppt_maker._gen_page 逐页精写，替换 deck.pages 对应页（版式不变，旧图保留）；
    layout   outline 项 layout 改为 new_layout，重跑精写产出新版式结构，替换 deck 页并同步存档大纲；
    image    第 1 页换封面图（1344x768），第 N>=2 页换该页图（仅 bullets 版式可挂图，1024x1024；
             非 bullets 页先人话说明不可配图）——调 ppt_maker._gen_one_image，
             生图配置缺失/生图失败走人话，存档不变；
    title    第 1 页改 deck["title"]（同步 outline["title"]），
             第 N>=2 页改该页 page_title 并同步 outline 项；
  - 重渲染：ppt_layouts.render_deck（防御式导入）重建 16:9 Presentation，
    覆盖写回原 pptx（文件名不变）；渲染失败 -> 人话且 deck.json 不更新（保持旧态一致）；
  - 更新存档：渲染成功后把 deck/outline 改动写回 deck.json；
  - 公开接口 edit_ppt(file_name, instruction) -> str：返回 Nolan 人话话术（供 hands 播报），
    永不抛异常。

物理页码语义：第 1 页 = 封面（deck["title"]/["subtitle"]/["cover_image"]），
第 N>=2 页 = deck["pages"][N-2] = outline["pages"][N-2]（两者同序对齐）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
FILES_DIR = MODULE_DIR / "files"          # 文件柜：pptx 与 .deck.json 都在这

# 版式中文名：人话话术与页面清单用
_LAYOUT_CN = {
    "toc": "目录", "section": "章节分隔", "bullets": "要点", "two_column": "双栏对比",
    "big_number": "大数字", "quote": "金句", "chart": "图表", "closing": "收尾",
}

_ACTIONS = {"rewrite", "layout", "image", "title"}


# ---------------------------------------------------------------- 依赖装载（全部防御式，便于测试 mock）

def _load_ppt_maker():
    """惰性导入 ppt_maker（生成引擎复用：JSON 解析 / 逐页精写 / 归一化 / 生图）。"""
    import ppt_maker
    return ppt_maker


def _load_render_deck():
    """惰性导入 ppt_layouts.render_deck（路 A 排版引擎）。"""
    from ppt_layouts import render_deck
    return render_deck


def _llm_call(prompt: str) -> str:
    """大脑一次调用：惰性导入，避免模块加载即拖起大脑。"""
    from brain import glm_one_shot
    return glm_one_shot(prompt)


# ---------------------------------------------------------------- 修改计划 prompt

_PLAN_PROMPT = """你是 PPT 编辑助手。用户要修改一份已生成的 PPT，请把用户的修改要求解析成一个结构化编辑计划。

【PPT 全页清单】（物理页码：第 1 页是封面，第 N 页（N≥2）是内容页）
{page_list}

【用户修改要求】
{instruction}

只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown 围栏。格式严格如下：
{{
  "page": 要修改的物理页码（整数，1 到 {total}）,
  "action": "四种之一：rewrite（重写本页正文内容）/ layout（更换本页版式）/ image（更换本页配图或封面图）/ title（修改标题）",
  "detail": "用户对本页修改的具体要求，一句话概括（10-60字）",
  "new_layout": "仅 action 为 layout 时必填：八种版式之一 toc/section/bullets/two_column/big_number/quote/chart/closing",
  "new_title": "仅 action 为 title 时必填：新标题（≤22字）",
  "image_prompt": "仅 action 为 image 时必填：新图的画面描述（写清主体+场景+色调，画面中不含文字）"
}}

判断规则：
- 「把第N页换成XX版式 / 改成两栏 / 换成图表页」→ action=layout；口语版式映射：两栏/双栏=two_column、要点/列表=bullets、大数字=big_number、图表=chart、金句/名言=quote、目录=toc、章节=section、收尾/结尾=closing；
- 「把第N页标题改成XX / 封面标题改成XX」→ action=title；
- 「给第N页换张图 / 重新配图 / 换封面图」→ action=image，image_prompt 按用户描述补全画面细节；
- 「重写第N页 / 第N页内容改一下 / 第N页加上XX」→ action=rewrite；
- 用户没明说页码时，根据页标题内容推断最可能的那一页；
- 完全无法理解用户想改什么时，输出 {{"page": 0, "action": "unknown", "detail": "没理解的原因"}}。"""


# ---------------------------------------------------------------- 定位与读取存档

def _locate_archive(file_name: str):
    """定位编辑存档：只认文件柜下的 {stem}.deck.json；拒绝一切路径成分（防路径穿越）。
    返回 (存档路径, None) 或 (None, 人话错误)。"""
    raw = str(file_name or "").strip()
    if not raw:
        return None, "先生，您没说要改哪份 PPT，给我个文件名就行。"
    # 路径穿越防御：只接受裸文件名，任何目录成分一律拒绝
    if raw != Path(raw).name or raw in (".", ".."):
        return (None, "抱歉先生，出于安全考虑我只能改文件柜里的文件，"
                      "请给我文件名本身，不要带路径。")
    stem = Path(raw).stem
    if not stem:
        return None, "抱歉先生，这个文件名我没认出来，请检查一下再叫我。"
    archive = FILES_DIR / f"{stem}.deck.json"
    if not archive.is_file():
        return (None, f"先生，「{raw}」这份 PPT 没有可编辑存档（可能是旧版生成的），"
                      "我改不了它的内容；您可以让我按原主题重新做一份，新版就支持随时说改就改了。")
    return archive, None


def _load_archive(archive: Path):
    """读取并校验存档契约：必须是含 deck/outline.pages 的 dict。返回 (存档 dict, 人话错误)。"""
    try:
        data = json.loads(archive.read_text(encoding="utf-8"))
    except Exception:
        return None, "先生，这份 PPT 的编辑存档损坏了，我读不出来；可以让我按原主题重新做一份。"
    if (not isinstance(data, dict)
            or not isinstance(data.get("deck"), dict)
            or not isinstance(data["deck"].get("pages"), list)
            or not isinstance(data.get("outline"), dict)
            or not isinstance(data["outline"].get("pages"), list)):
        return None, "先生，这份 PPT 的编辑存档不完整，我改不了；可以让我按原主题重新做一份。"
    return data, None


# ---------------------------------------------------------------- 修改计划：清单 + 校验

def _page_listing(deck: dict) -> str:
    """全页清单（物理页码 + 版式 + 标题），喂给 LLM 做修改定位。"""
    lines = [f"第1页（封面）：主标题「{deck.get('title') or ''}」，副标题「{deck.get('subtitle') or ''}」"]
    for i, pg in enumerate(deck.get("pages") or [], start=2):
        layout = str(pg.get("layout") or "bullets")
        title = (pg.get("page_title")
                 or ("目录" if layout == "toc" else "")
                 or str(pg.get("quote") or "")[:20]
                 or "（无标题）")
        lines.append(f"第{i}页（{_LAYOUT_CN.get(layout, '要点')}版式/{layout}）：「{title}」")
    return "\n".join(lines)


def _validate_plan(plan, total_pages: int, layouts: set):
    """校验 LLM 修改计划。返回 (规范化计划 dict, None) 或 (None, 人话错误)。"""
    try:
        page = int(plan.get("page"))
    except (TypeError, ValueError):
        page = 0
    action = str(plan.get("action") or "").strip().lower()
    detail = str(plan.get("detail") or "").strip()[:120]

    if action not in _ACTIONS or page == 0:
        return (None, "先生，我没太理解您想怎么改。您可以这样说："
                      "「把第3页换成两栏版式」「给第5页换张图」"
                      "「把第2页标题改成……」「重写第4页内容」。")
    if page < 1 or page > total_pages:
        return (None, f"先生，这份 PPT 一共 {total_pages} 页，"
                      f"没有第 {page} 页；您说个 1 到 {total_pages} 之间的页码就行。")

    norm = {"page": page, "action": action, "detail": detail}
    if action == "layout":
        new_layout = str(plan.get("new_layout") or "").strip().lower()
        if new_layout not in layouts:
            return (None, "先生，我没认出要换成哪种版式。一共有八种："
                          "要点、双栏对比、大数字、图表、金句、目录、章节、收尾。")
        norm["new_layout"] = new_layout
    elif action == "title":
        new_title = str(plan.get("new_title") or "").strip()
        if not new_title:
            return None, "先生，您想把标题改成什么？把新标题原话告诉我就行。"
        norm["new_title"] = new_title[:30]
    elif action == "image":
        image_prompt = str(plan.get("image_prompt") or "").strip()
        if not image_prompt:
            return None, "先生，您想换一张什么样的图？描述一下画面我就去生成。"
        norm["image_prompt"] = image_prompt[:300]

    # 封面（第 1 页）只有标题与背景图可改，没有正文和版式
    if page == 1 and action in ("rewrite", "layout"):
        return (None, "先生，封面只有主标题和背景图，没有正文可重写、也没有版式可换；"
                      "您可以让我「改封面标题」或「换封面图」。")
    return norm, None


# ---------------------------------------------------------------- 四种动作的执行

def _regen_page(pm, data: dict, idx: int, item: dict):
    """rewrite/layout 共用：用存档上下文重跑逐页精写并归一化成契约页。"""
    deck = data["deck"]
    pages = deck["pages"]
    titles = [p.get("page_title") or "" for p in pages]
    prev_t = titles[idx - 1] if idx > 0 else "（封面）"
    next_t = titles[idx + 1] if idx + 1 < len(titles) else "（结束页）"
    gen = pm._gen_page(
        str(data.get("topic") or deck.get("title") or ""),
        str(deck.get("title") or ""),
        str(data.get("style") or "工作汇报"),
        item, idx + 1, len(pages), prev_t, next_t, _llm_call,
        str(data.get("research") or ""))
    return pm._normalize_page(item, gen)


def _exec_rewrite(pm, data: dict, plan: dict) -> str:
    """重写本页正文：版式不变，旧图保留。返回人话描述。"""
    idx = plan["page"] - 2
    item = dict(data["outline"]["pages"][idx])
    if plan["detail"]:
        # 把用户的修改要求注入核心论点，精写时照此调整
        item["core_point"] = (item.get("core_point") or "") + f"；本次修改要求：{plan['detail']}"
    new_pg = _regen_page(pm, data, idx, item)
    old_pg = data["deck"]["pages"][idx]
    if new_pg.get("layout") == "bullets" and old_pg.get("image"):
        new_pg["image"] = old_pg["image"]          # 换内容不换图
    data["deck"]["pages"][idx] = new_pg
    return f"重写了正文内容（{plan['detail'] or '按您的要求'}），版式不变，演讲稿也同步更新了"


def _exec_layout(pm, data: dict, plan: dict) -> str:
    """更换本页版式：重跑精写产出新版式结构，同步存档大纲。返回人话描述。"""
    idx = plan["page"] - 2
    new_layout = plan["new_layout"]
    item = dict(data["outline"]["pages"][idx])
    item["layout"] = new_layout
    if plan["detail"]:
        item["core_point"] = (item.get("core_point") or "") + f"；本次修改要求：{plan['detail']}"
    new_pg = _regen_page(pm, data, idx, item)
    # toc 缺 entries 时用全篇标题兜底
    if new_pg.get("layout") == "toc" and not new_pg.get("entries"):
        new_pg["entries"] = [p.get("page_title") for p in data["deck"]["pages"]
                             if p.get("page_title")]
    data["deck"]["pages"][idx] = new_pg
    # 同步存档大纲（精写失败兜底可能降级 bullets，以实际落地版式为准）
    data["outline"]["pages"][idx]["layout"] = new_pg.get("layout", new_layout)
    cn = _LAYOUT_CN.get(new_pg.get("layout", new_layout), new_layout)
    return f"换成了{cn}版式，内容按新版式重新精写了"


def _exec_image(pm, data: dict, plan: dict):
    """换图：第 1 页封面图（1344x768）；第 N>=2 页内容页配图（仅 bullets，1024x1024）。
    返回 (人话描述, None) 或 (None, 人话错误)。任何失败存档不变。"""
    page = plan["page"]
    deck = data["deck"]
    if page == 1:
        size = "1344x768"              # 近 16:9，封面铺屏不形变
        target = "封面背景图"
    else:
        pg = deck["pages"][page - 2]
        if pg.get("layout") != "bullets":
            cn = _LAYOUT_CN.get(str(pg.get("layout") or ""), "当前")
            return (None, f"先生，第 {page} 页是{cn}版式，挂不了配图；"
                          "您可以先让我把这页换成要点版式，再给它配图。")
        size = "1024x1024"
        target = "配图"
    # 生图配置缺失：整条链走人话，不炸单
    try:
        cfg = pm._load_image_config()
    except Exception:
        cfg = None
    if not cfg:
        return None, "先生，生图功能现在没配置好（缺 API 密钥配置），换不了图；其余内容没动。"
    base, key = cfg
    try:
        path = pm._gen_one_image(plan["image_prompt"], base, key,
                                 FILES_DIR / "ppt_assets",
                                 int(time.time()) % 100000, size=size)
    except Exception:
        path = None
    if not path:
        return None, "先生，这张图没生成成功（生图服务刚才没响应），请稍后再让我试一次；其余内容没动。"
    if page == 1:
        deck["cover_image"] = path
    else:
        deck["pages"][page - 2]["image"] = path
    return f"重新生成并换上了{target}", None


def _exec_title(data: dict, plan: dict) -> str:
    """改标题：第 1 页改 deck 主标题（同步 outline），第 N>=2 页改页标题并同步 outline 项。"""
    page = plan["page"]
    new_title = plan["new_title"]
    if page == 1:
        data["deck"]["title"] = new_title
        data["outline"]["title"] = new_title
        return f"封面主标题已改为「{new_title}」"
    data["deck"]["pages"][page - 2]["page_title"] = new_title
    data["outline"]["pages"][page - 2]["page_title"] = new_title
    return f"标题已改为「{new_title}」"


# ---------------------------------------------------------------- 重渲染与存档回写

def _render_to_pptx(deck: dict, style: str, out_path: Path) -> None:
    """用路 A 排版引擎重建 16:9 Presentation 并覆盖写回原 pptx（文件名不变）。
    任何异常上交，由调用方翻人话。"""
    render_deck = _load_render_deck()
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width = Inches(13.333)     # 16:9
    prs.slide_height = Inches(7.5)
    render_deck(prs, deck, style)
    prs.save(str(out_path))


def _write_archive(archive: Path, data: dict) -> None:
    """存档回写：deck/outline 改动落盘（渲染成功之后才调用）。"""
    archive.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 公开接口

def _edit_ppt_inner(file_name: str, instruction: str) -> str:
    # ---- 1. 定位存档
    archive, err = _locate_archive(file_name)
    if err:
        return err
    data, err = _load_archive(archive)
    if err:
        return err

    instruction = str(instruction or "").strip()
    if not instruction:
        return "先生，您想怎么改这份 PPT？比如说「重写第4页内容」「把第3页换成两栏版式」。"

    # ---- 依赖：生成引擎（精写/生图/JSON 解析复用它）
    try:
        pm = _load_ppt_maker()
    except Exception:
        return "抱歉先生，PPT 引擎模块现在不可用，改不了；请稍后再试。"

    deck = data["deck"]
    total_pages = 1 + len(deck["pages"])     # 物理总页数 = 封面 + 内容页

    # ---- 2. LLM 修改计划（一次调用，复用 ppt_maker 的 JSON 解析手法）
    prompt = _PLAN_PROMPT.format(
        page_list=_page_listing(deck), instruction=instruction, total=total_pages)
    try:
        plan = pm._call_json(prompt, _llm_call, repair_once=True)
    except Exception:
        return "抱歉先生，大脑调用时出错了，文件没有改动；请稍后再试。"
    if plan is None:
        return ("先生，大脑刚才返回的内容格式乱了，我没敢动手，文件没有改动；"
                "请换个说法再让我试一次。")
    plan, err = _validate_plan(plan, total_pages, set(getattr(pm, "LAYOUTS", ())))
    if err:
        return err

    # ---- 3. 执行动作（任何失败：内存改动不落盘，存档保持旧态一致）
    action = plan["action"]
    try:
        if action == "rewrite":
            desc = _exec_rewrite(pm, data, plan)
        elif action == "layout":
            desc = _exec_layout(pm, data, plan)
        elif action == "image":
            desc, err = _exec_image(pm, data, plan)
            if err:
                return err
        else:  # title
            desc = _exec_title(data, plan)
    except Exception:
        return "抱歉先生，修改这页内容时出了点意外，文件没有改动；请让我重试。"

    # ---- 4. 重渲染，覆盖写回原 pptx（文件名不变）；失败则存档不更新
    pptx_name = Path(str(data.get("pptx_name") or "")).name or (archive.stem[:-5] + ".pptx")
    pptx_path = FILES_DIR / pptx_name
    try:
        _render_to_pptx(deck, str(data.get("style") or "工作汇报"), pptx_path)
    except Exception:
        return ("抱歉先生，内容改好了但重新排版生成文件失败了，"
                "我保持了原文件不动；请让我重试。")

    # ---- 5. 存档回写（渲染成功之后才落盘，保证 deck.json 与 pptx 一致）
    try:
        _write_archive(archive, data)
    except Exception:
        return ("先生，文件已经更新好了，但编辑存档同步写盘失败，"
                "下次再改这份 PPT 可能认不出这次改动；其他都正常。")

    # ---- 6. 人话话术
    page = plan["page"]
    where = "封面" if page == 1 else f"第 {page} 页"
    return f"先生，{where}已按您的要求{desc}。文件已更新，文件柜里同名打开就是新版。"


def edit_ppt(file_name: str, instruction: str) -> str:
    """按自然语言指令修改文件柜里的 PPT，返回 Nolan 人话话术（供 hands 播报）。
    永不抛异常；找不到存档/指令无法理解/执行失败都给人话解释。"""
    try:
        return _edit_ppt_inner(file_name, instruction)
    except Exception:
        return "抱歉先生，修改 PPT 时出了点意外，文件没有改动；请让我重试。"
