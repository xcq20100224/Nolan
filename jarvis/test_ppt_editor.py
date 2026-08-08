# -*- coding: utf-8 -*-
"""ppt_editor 单元测试：mock LLM 与生图，覆盖定位/四种动作/渲染/存档回写/各类人话降级。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ppt_editor  # noqa: E402


# ------------------------------------------------------------ 测试替身

class FakeMaker:
    """ppt_maker 替身：计划解析/逐页精写/归一化/生图全部可控。"""
    LAYOUTS = {"toc", "section", "bullets", "two_column",
               "big_number", "quote", "chart", "closing"}

    def __init__(self):
        self.plan = None                # _call_json 的返回值（None 模拟 LLM 返回垃圾）
        self.calls = []                 # 调用记录
        self.img_cfg = ("http://fake", "fake-key")
        self.img_path = "C:\\fake\\new_img.png"
        self.gen_note = "重写后的演讲稿"

    def _call_json(self, prompt, caller, repair_once=True):
        self.calls.append(("call_json", prompt))
        return self.plan

    def _gen_page(self, topic, deck_title, style, item, idx, total,
                  prev_t, next_t, caller, research=""):
        self.calls.append(("gen_page", dict(item)))
        layout = item.get("layout", "bullets")
        if layout == "two_column":
            content = {"left": {"heading": "左栏", "points": ["新左1", "新左2", "新左3"]},
                       "right": {"heading": "右栏", "points": ["新右1", "新右2", "新右3"]}}
        else:
            content = {"bullets": ["新要点一", "新要点二", "新要点三", "新要点四"]}
        return {"layout": layout, "content": content,
                "speaker_note": self.gen_note, "rewrites": 0, "fallback": False}

    def _normalize_page(self, item, page):
        """高仿 ppt_maker._normalize_page：只覆盖本测试用到的两种版式。"""
        self.calls.append(("normalize", item.get("layout")))
        if page["layout"] == "two_column":
            return {"layout": "two_column", "page_title": item["page_title"],
                    "left": page["content"]["left"], "right": page["content"]["right"],
                    "speaker_note": page["speaker_note"]}
        return {"layout": "bullets", "page_title": item["page_title"],
                "bullets": page["content"]["bullets"],
                "speaker_note": page["speaker_note"]}

    def _load_image_config(self):
        return self.img_cfg

    def _gen_one_image(self, prompt, base, key, assets_dir, seq, size="1024x1024"):
        self.calls.append(("gen_image", size, prompt))
        return self.img_path


def _make_archive(tmp: Path, stem: str = "咖啡报告_20250101-1200"):
    """在临时文件柜里造一份合法存档 + 假 pptx（哨兵字节）。返回 (存档路径, pptx路径, 存档dict)。"""
    data = {
        "topic": "咖啡行业观察",
        "style": "工作汇报",
        "pages": 4,
        "research": "研究材料若干",
        "outline": {
            "title": "咖啡行业正在重新定价",
            "subtitle": "工作汇报",
            "cover_image_prompt": "咖啡豆特写",
            "pages": [
                {"layout": "bullets", "page_title": "市场规模三年翻倍",
                 "core_point": "市场在涨", "keywords": ["规模"]},
                {"layout": "two_column", "page_title": "连锁与独立店分流",
                 "core_point": "两业态分化", "keywords": ["连锁"],
                 "left_heading": "连锁", "right_heading": "独立"},
                {"layout": "bullets", "page_title": "现磨咖啡胜出的三个原因",
                 "core_point": "现磨赢", "keywords": ["现磨"],
                 "image_prompt": "一杯手冲咖啡"},
            ],
        },
        "deck": {
            "title": "咖啡行业正在重新定价",
            "subtitle": "工作汇报 · 2025年01月01日",
            "cover_image": None,
            "pages": [
                {"layout": "bullets", "page_title": "市场规模三年翻倍",
                 "bullets": ["旧要点1", "旧要点2", "旧要点3", "旧要点4"],
                 "speaker_note": "旧稿1"},
                {"layout": "two_column", "page_title": "连锁与独立店分流",
                 "left": {"heading": "连锁", "points": ["旧左1"]},
                 "right": {"heading": "独立", "points": ["旧右1"]},
                 "speaker_note": "旧稿2"},
                {"layout": "bullets", "page_title": "现磨咖啡胜出的三个原因",
                 "bullets": ["旧要点a", "旧要点b", "旧要点c", "旧要点d"],
                 "image": "C:\\fake\\old_img.png",
                 "speaker_note": "旧稿3"},
            ],
        },
        "pptx_name": f"{stem}.pptx",
    }
    archive = tmp / f"{stem}.deck.json"
    archive.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    pptx = tmp / f"{stem}.pptx"
    pptx.write_bytes(b"SENTINEL-OLD-PPTX")          # 哨兵：验证覆写发生
    return archive, pptx, data


class EditorTestBase(unittest.TestCase):
    """公共环境：临时文件柜 + FakeMaker + render 记录器。"""

    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_obj.name)
        self.archive, self.pptx, self.data = _make_archive(self.tmp)
        self.maker = FakeMaker()
        self.render_calls = []

        def fake_render(prs, deck, style):
            self.render_calls.append((deck, style))

        self.render_deck = fake_render
        # 打补丁：文件柜指向临时目录；ppt_maker / render_deck 全部替身
        patches = [
            mock.patch.object(ppt_editor, "FILES_DIR", self.tmp),
            mock.patch.object(ppt_editor, "_load_ppt_maker", lambda: self.maker),
            mock.patch.object(ppt_editor, "_load_render_deck", lambda: self.render_deck),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp_obj.cleanup)

    def archive_bytes(self) -> bytes:
        return self.archive.read_bytes()

    def reload_archive(self) -> dict:
        return json.loads(self.archive.read_text(encoding="utf-8"))


# ------------------------------------------------------------ 定位与人话降级

class TestLocate(EditorTestBase):

    def test_no_archive_human_words(self):
        r = ppt_editor.edit_ppt("不存在的文件.pptx", "重写第2页")
        self.assertIn("没有可编辑存档", r)
        self.assertIn("重新做一份", r)
        self.assertEqual(self.maker.calls, [])          # 没动 LLM

    def test_path_traversal_rejected(self):
        for bad in ("../secret.pptx", "..\\secret.pptx", "a/b.pptx", ".."):
            r = ppt_editor.edit_ppt(bad, "重写第2页")
            self.assertIn("文件名本身", r, bad)
        self.assertEqual(self.maker.calls, [])

    def test_llm_garbage_human_words(self):
        self.maker.plan = None                            # 模拟大脑返回垃圾
        before = self.archive_bytes()
        r = ppt_editor.edit_ppt(self.pptx.name, "重写第2页")
        self.assertIn("格式乱了", r)
        self.assertEqual(self.archive_bytes(), before)    # 存档不变
        self.assertEqual(self.pptx.read_bytes(), b"SENTINEL-OLD-PPTX")

    def test_page_out_of_range_human_words(self):
        self.maker.plan = {"page": 99, "action": "rewrite", "detail": "x"}
        r = ppt_editor.edit_ppt(self.pptx.name, "重写第99页")
        self.assertIn("一共 4 页", r)

    def test_unknown_action_human_words(self):
        self.maker.plan = {"page": 0, "action": "unknown", "detail": "没懂"}
        r = ppt_editor.edit_ppt(self.pptx.name, "随便弄弄")
        self.assertIn("没太理解", r)


# ------------------------------------------------------------ 四种动作

class TestRewrite(EditorTestBase):

    def test_rewrite_replaces_page_and_keeps_image(self):
        self.maker.plan = {"page": 4, "action": "rewrite", "detail": "加上成本数据"}
        r = ppt_editor.edit_ppt(self.pptx.name, "重写第4页，加上成本数据")

        self.assertIn("第 4 页", r)
        self.assertIn("文件柜里同名打开就是新版", r)
        # 精写被调：outline 对应项 + 修改要求注入 core_point
        gen = [c for c in self.maker.calls if c[0] == "gen_page"]
        self.assertEqual(len(gen), 1)
        self.assertIn("加上成本数据", gen[0][1]["core_point"])
        # deck 页被替换：要点换新、版式不变、演讲稿更新、旧图保留
        saved = self.reload_archive()
        pg = saved["deck"]["pages"][2]
        self.assertEqual(pg["bullets"], ["新要点一", "新要点二", "新要点三", "新要点四"])
        self.assertEqual(pg["layout"], "bullets")
        self.assertEqual(pg["speaker_note"], "重写后的演讲稿")
        self.assertEqual(pg["image"], "C:\\fake\\old_img.png")
        # 渲染被调 + pptx 被覆写（哨兵消失）
        self.assertEqual(len(self.render_calls), 1)
        self.assertNotEqual(self.pptx.read_bytes(), b"SENTINEL-OLD-PPTX")
        self.assertTrue(self.pptx.read_bytes().startswith(b"PK"))   # 真 pptx zip 头


class TestLayout(EditorTestBase):

    def test_layout_change_regenerates_and_syncs_outline(self):
        self.maker.plan = {"page": 2, "action": "layout",
                           "new_layout": "two_column", "detail": "改成两栏"}
        r = ppt_editor.edit_ppt(self.pptx.name, "把第2页换成两栏版式")

        self.assertIn("双栏对比版式", r)
        saved = self.reload_archive()
        pg = saved["deck"]["pages"][0]
        self.assertEqual(pg["layout"], "two_column")
        self.assertEqual(pg["left"]["points"], ["新左1", "新左2", "新左3"])
        # 存档大纲同步
        self.assertEqual(saved["outline"]["pages"][0]["layout"], "two_column")
        self.assertEqual(len(self.render_calls), 1)

    def test_layout_invalid_human_words(self):
        self.maker.plan = {"page": 2, "action": "layout", "new_layout": " galaxy ", "detail": ""}
        r = ppt_editor.edit_ppt(self.pptx.name, "把第2页换成 galaxy 版式")
        self.assertIn("八种", r)


class TestImage(EditorTestBase):

    def test_cover_image_1344x768(self):
        self.maker.plan = {"page": 1, "action": "image",
                           "image_prompt": "暖色咖啡豆山", "detail": "换封面图"}
        r = ppt_editor.edit_ppt(self.pptx.name, "换张封面图")

        self.assertIn("封面", r)
        img = [c for c in self.maker.calls if c[0] == "gen_image"]
        self.assertEqual(img[0][1], "1344x768")          # 封面尺寸
        saved = self.reload_archive()
        self.assertEqual(saved["deck"]["cover_image"], "C:\\fake\\new_img.png")

    def test_page_image_bullets_1024(self):
        self.maker.plan = {"page": 2, "action": "image",
                           "image_prompt": "咖啡馆场景", "detail": "换图"}
        r = ppt_editor.edit_ppt(self.pptx.name, "给第2页换张图")

        img = [c for c in self.maker.calls if c[0] == "gen_image"]
        self.assertEqual(img[0][1], "1024x1024")
        saved = self.reload_archive()
        self.assertEqual(saved["deck"]["pages"][0]["image"], "C:\\fake\\new_img.png")

    def test_image_on_non_bullets_human_words(self):
        self.maker.plan = {"page": 3, "action": "image",
                           "image_prompt": "x", "detail": "换图"}   # 第3页是 two_column
        before = self.archive_bytes()
        r = ppt_editor.edit_ppt(self.pptx.name, "给第3页换张图")
        self.assertIn("挂不了配图", r)
        self.assertEqual(self.archive_bytes(), before)

    def test_image_failure_keeps_archive(self):
        self.maker.plan = {"page": 2, "action": "image", "image_prompt": "x", "detail": ""}
        self.maker.img_path = None                          # 模拟生图失败
        before = self.archive_bytes()
        r = ppt_editor.edit_ppt(self.pptx.name, "给第2页换张图")
        self.assertIn("没生成成功", r)
        self.assertEqual(self.archive_bytes(), before)      # 存档不变
        self.assertEqual(self.render_calls, [])             # 没渲染

    def test_image_config_missing_human_words(self):
        self.maker.plan = {"page": 2, "action": "image", "image_prompt": "x", "detail": ""}
        self.maker.img_cfg = None                           # 模拟配置缺失
        r = ppt_editor.edit_ppt(self.pptx.name, "给第2页换张图")
        self.assertIn("没配置好", r)


class TestTitle(EditorTestBase):

    def test_cover_title(self):
        self.maker.plan = {"page": 1, "action": "title",
                           "new_title": "咖啡行业的下半场", "detail": "改标题"}
        r = ppt_editor.edit_ppt(self.pptx.name, "把封面标题改成「咖啡行业的下半场」")
        self.assertIn("咖啡行业的下半场", r)
        saved = self.reload_archive()
        self.assertEqual(saved["deck"]["title"], "咖啡行业的下半场")
        self.assertEqual(saved["outline"]["title"], "咖啡行业的下半场")

    def test_page_title_syncs_outline(self):
        self.maker.plan = {"page": 2, "action": "title",
                           "new_title": "市场规模四年翻三倍", "detail": "改标题"}
        r = ppt_editor.edit_ppt(self.pptx.name, "把第2页标题改成「市场规模四年翻三倍」")
        self.assertIn("第 2 页", r)
        saved = self.reload_archive()
        self.assertEqual(saved["deck"]["pages"][0]["page_title"], "市场规模四年翻三倍")
        self.assertEqual(saved["outline"]["pages"][0]["page_title"], "市场规模四年翻三倍")
        self.assertEqual(len(self.render_calls), 1)


class TestRenderFailure(EditorTestBase):

    def test_render_failure_keeps_archive(self):
        def boom(prs, deck, style):
            raise RuntimeError("排版炸了")
        self.render_deck = boom
        self.maker.plan = {"page": 2, "action": "title", "new_title": "新标题", "detail": ""}
        before = self.archive_bytes()
        r = ppt_editor.edit_ppt(self.pptx.name, "把第2页标题改成新标题")
        self.assertIn("重新排版生成文件失败", r)
        self.assertIn("保持了原文件不动", r)
        self.assertEqual(self.archive_bytes(), before)      # 存档不更新
        self.assertEqual(self.pptx.read_bytes(), b"SENTINEL-OLD-PPTX")


if __name__ == "__main__":
    unittest.main(verbosity=2)
