# -*- coding: utf-8 -*-
"""
eyes.py —— Nolan 的「眼睛」与「手」的延伸（阶段五：屏幕感知 + GUI 自动化）

职责：把「点选网易云音乐列表中的歌曲」这类软件界面内操作，
拆成一条可验证的闭环链路：

    截屏 -> 视觉模型理解界面并返回动作 JSON（含预期效果 expect）
    -> pyautogui 执行 -> sleep 1 秒 -> 重新截屏复核上一步是否生效
    （未生效则换策略重试，连续 2 次判失败；done 也需复核通过才生效）
    -> ... 直到 done / fail / 步数上限

视觉模型配置驱动：模型名与附加请求体来自 jarvis/llm_config.json 的
可选字段 vision_model（默认 glm-4.5v）与 vision_extra_body
（默认 '{"thinking": {"type": "disabled"}}'，glm-4.5v 需关闭思考模式）；
主模型调用失败（HTTP 错误 / 超时）自动降级到 glm-4v-flash 再试一次。

安全闸（三层，均为硬约束）：
  1. 步数上限 max_steps：任何任务最多走 max_steps 步，防死循环；
  2. pyautogui FAILSAFE：主人把鼠标甩到屏幕任意角落，立即中止并返回中止话术；
  3. 禁令写进 VLM 感知 prompt：禁止输入密码、禁止支付、禁止删除文件、
     禁止向联系人发消息——VLM 遇到此类界面必须返回 fail。

坐标约定（高 DPI 必须处理）：
  进程起手调 SetProcessDPIAware，保证截屏与 pyautogui 坐标同为物理像素；
  发送给 VLM 的截图缩放到宽 <= 1280，VLM 返回的坐标基于该截图；
  执行前统一换算：实际坐标 = VLM 坐标 x (物理宽 / 截图宽)。

接口契约（签名一字不差）：
    perform(task: str, max_steps: int = 12) -> str  # 口语化结果
    screenshot_b64() -> str                         # JPEG base64（宽 <= 1280）
    locate_and_crop(description: str) -> str | None # 通用截屏元素：定位 + 裁剪保存

Gap2 可靠性增强：复核未生效不再「盲重试」——先经 reliability 模块把失败
归约为有限几类物理原因（焦点丢失/目标未出现/坐标漂移/文本校验不符/超时/
应用未响应），按对策表结构化修复（每类独立重试预算 + 逐次加长退避，
全局总预算封顶防死循环）；UIA 控件树能阳性确认的复核直接跳过截图 + VLM
往返。reliability 缺失或处置异常时退回旧逻辑，默认路径行为不变。
"""

import base64
import ctypes
import io
import json
import os
import re
import time

import httpx
import pyautogui
import pyperclip
from PIL import ImageGrab

# UIA 元素树感知：comtypes 直调 UI Automation，枚举前台窗口可操作控件。
# 防御式导入：UIA 不可用时纯视觉模式照常工作（截图 + VLM 不变）。
try:
    import uia as _uia
except Exception:
    _uia = None

# 技能固化：成功任务的动作序列沉淀与重放（J3）。
# 防御式导入：模块缺失时退化回纯 VLM 闭环，功能不缺、只是不学习。
try:
    import skills as _skills
except Exception:
    _skills = None

# 可靠性增强（Gap2）：失败分类 + 结构化重试 + UIA 廉价复核。
# 防御式导入：模块缺失时复核失败处置退回旧的「提示换策略、连续 2 次判
# 失败」逻辑，默认路径行为与旧版一字不差。
try:
    import reliability as _rel
except Exception:
    _rel = None

# 屏幕状态流（Gap5-P1）：动作前后界面指纹 diff，「没变不重看」——
# 等待后界面无实质变化时省下二次 VLM 复核往返；diff 实况同时作为
# 地面实况写入历史，让 VLM 知道上一刀下去界面到底变了没有。
try:
    import perception as _perception
except Exception:
    _perception = None

# 窗口上下文记忆（Gap5-P2）：记住每个窗口的已知控件、成功定位策略与
# 失败教训，注入 VLM prompt——不再每次都从「这是哪里」重新理解。
try:
    import win_context as _wctx
except Exception:
    _wctx = None


def _wkey(target_hint: str | None) -> str:
    """当前窗口的上下文键：hint 优先，否则取前台标题；进程名不可得传空。"""
    if _wctx is None:
        return ""
    try:
        title = target_hint or (_uia.foreground_title() if _uia else "")
        return _wctx.make_key("", title or "未知窗口")
    except Exception:
        return ""


def _capture_screen_state(target_hint: str | None, shot_b64: str,
                          controls: list):
    """从现成的截图与控件清单算屏幕状态指纹（失败返回 None，绝不抛）。"""
    if _perception is None:
        return None
    try:
        title = target_hint or (_uia.foreground_title() if _uia else "")
        return _perception.capture_state(
            (0, title or "?"), base64.b64decode(shot_b64), controls or [])
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 初始化与常量
# ---------------------------------------------------------------------------

# 起手声明 DPI 感知：保证 ImageGrab 截屏与 pyautogui 坐标都使用物理像素，
# 否则高 DPI 机器上两者坐标系不一致，点击位置会整体偏移。
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:  # 极少数环境（非 Windows / 权限受限）下静默降级
    pass

# FAILSAFE 是 pyautogui 默认行为，这里显式再确认一次：
# 鼠标被甩到屏幕角落会抛 FailSafeException，我们在 perform 里捕获并中止。
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1  # 每个 pyautogui 调用之间的最小间隔，防动作过快

_VLM_FALLBACK_MODEL = "glm-4v-flash"  # 主视觉模型失败时的自动降级模型
_DEFAULT_VISION_MODEL = "glm-4.5v"    # llm_config.json 缺 vision_model 时的默认主模型
# glm-4.5v 必须关闭思考模式，否则响应慢且可能输出思考过程污染动作 JSON
_DEFAULT_VISION_EXTRA_BODY = '{"thinking": {"type": "disabled"}}'
_VLM_TIMEOUT = 60.0                # VLM 请求超时（秒）
_SHOT_MAX_WIDTH = 1280             # 发送给 VLM 的截图最大宽度（像素）
_JPEG_QUALITY = 80                 # 截图 JPEG 质量（兼顾清晰度与体积）
_STEP_INTERVAL = 1.0               # 每步执行后的等待秒数，等界面响应

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "llm_config.json")

# 通用截屏元素的裁剪保存目录：jarvis\files\captures\（网页端经 /api/files 访问）
_CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "files", "captures")
_CROP_MARGIN = 10  # 裁剪时在定位框外扩的边距（物理像素）

# 口语化返回话术（Nolan 可靠管家人设：正式、精确、如实汇报）
_MSG_FAILSAFE = "先生，检测到您将鼠标移至屏幕角落，操作已被安全中止。"
_MSG_FAIL_PREFIX = "先生，任务未能完成。"
_MSG_VLM_DOWN = "先生，我的视觉模块暂时无法连接，任务无法执行。"

# VLM 感知 prompt：定义动作协议 + 坐标系 + 安全禁令（硬编码，不可绕过）
_VLM_SYSTEM = (
    "你是 Nolan 的屏幕感知模块。我会给你一张电脑屏幕截图和一个任务目标，"
    "你每步只能返回一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：\n"
    '{"action": "left_click|double_click|type|key|scroll|wait|done|fail", '
    '"x": 像素x, "y": 像素y, "text": "输入内容", "keys": "如 ctrl+v", '
    '"thought": "一句话决策理由", '
    '"expect": "动作生效后屏幕上应出现的可见变化"}\n'
    "协议细则：\n"
    "1. x、y 是基于我发给你的这张截图的像素坐标（截图左上角为原点）。"
    "只有 left_click、double_click、scroll 需要坐标；scroll 的 text 填 up 或 down。"
    "left_click、double_click 时把你要点的元素的可见文字填进 text"
    "（例如按钮或菜单上的「我喜欢的音乐」「确定」），系统会优先按文字精确定位，"
    "坐标仍需给出作为兜底。\n"
    "2. type 用于输入文字（text 为内容，支持中文）；key 用于按键"
    "（keys 为单键如 enter，或组合键如 ctrl+v）。\n"
    "3. wait 用于等待界面加载；done 表示任务已完成；fail 表示无法完成。\n"
    "4. done 时把 thought 写成给主人的一句话完成汇报；fail 时写成失败原因。\n"
    "5. 每步只做一个动作，逐步逼近目标，不要试图一步完成所有事。\n"
    "6. 我会附上已执行的动作历史；请结合当前截图判断："
    "若任务目标已在屏幕上完成，立即返回 done，绝不重复已成功的动作。\n"
    "7. 若目标窗口尚未出现或界面仍在加载，返回 wait 等待，不要急着 fail；"
    "只有反复等待后仍确认无法完成时才返回 fail。\n"
    "8. 常识提示：记事本等编辑器打开后，窗口中央的大片空白区域就是文本区，"
    "深色主题下文本区呈深色属正常现象；这类任务可直接返回 type 输入文字，"
    "不要因为文本区是空白的就判定窗口未打开或无法输入。\n"
    "9. 桌面应用常识：深色侧边栏导航是音乐/视频类应用的标准布局"
    "（如网易云音乐左侧的「发现音乐 / 我喜欢 / 歌单」导航栏），"
    "看到这类布局应优先在侧边栏中寻找入口。"
    "音乐播放状态常识：点击播放后，底部播放栏的圆形按钮变成「暂停」图标"
    "（两条竖线）即表示歌曲正在播放，任务目标已达成，应立即返回 done，"
    "不要重复点击播放（再点会变成暂停）。\n"
    "10. 若目标应用窗口不在屏幕上、未打开或被其他窗口遮挡，必须返回 fail，"
    "并在 thought 中以「屏幕上没有找到<应用名>」开头明确报告，"
    "绝不要乱点其他无关应用的界面来碰运气。\n"
    "11. 物理动作（left_click/double_click/type/key/scroll）必须填 expect："
    "一句话描述这个动作生效后，屏幕上应该出现什么可见变化"
    "（例如「输入框里出现文字 hello」），用于执行后复核；"
    "wait、done、fail 不需要填 expect。\n"
    "安全禁令（最高优先级，绝不可违背）：\n"
    "- 禁止输入任何密码、验证码或支付信息；\n"
    "- 禁止进行任何支付、转账、下单操作；\n"
    "- 禁止删除任何文件或数据；\n"
    "- 禁止向任何联系人发送消息；\n"
    "遇到上述情况或界面要求上述操作时，必须返回 fail 并在 thought 中说明原因。"
)


# ---------------------------------------------------------------------------
# 感知：截屏
# ---------------------------------------------------------------------------

def screenshot_b64() -> str:
    """截全屏，缩放到宽 <= 1280，编码为 JPEG base64 字符串返回。"""
    img = ImageGrab.grab()  # DPI 感知后为物理像素
    if img.width > _SHOT_MAX_WIDTH:
        new_h = round(img.height * _SHOT_MAX_WIDTH / img.width)
        img = img.resize((_SHOT_MAX_WIDTH, new_h), ImageGrab.Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _screenshot_size() -> tuple[int, int]:
    """返回发送给 VLM 的截图尺寸 (宽, 高)，与 screenshot_b64 的缩放逻辑一致。"""
    sw, sh = pyautogui.size()  # DPI 感知后为物理分辨率
    if sw > _SHOT_MAX_WIDTH:
        sh = round(sh * _SHOT_MAX_WIDTH / sw)
        sw = _SHOT_MAX_WIDTH
    return sw, sh


# ---------------------------------------------------------------------------
# 坐标换算：VLM 截图像素 -> 屏幕物理像素
# ---------------------------------------------------------------------------

def _vlm_to_screen(x: float, y: float,
                   shot_w: int | None = None,
                   shot_h: int | None = None) -> tuple[int, int]:
    """
    把 VLM 基于缩放截图返回的坐标换算成屏幕物理像素坐标。
    换算式：实际坐标 = VLM 坐标 x (物理宽 / 截图宽)（高向同理，按比例）。
    结果会被钳制在屏幕范围内，防止越界点击。
    """
    screen_w, screen_h = pyautogui.size()
    if shot_w is None or shot_h is None:
        shot_w, shot_h = _screenshot_size()
    sx = screen_w / shot_w
    sy = screen_h / shot_h
    px = int(round(x * sx))
    py = int(round(y * sy))
    # 钳制到屏幕内，避免 VLM 幻觉坐标导致的越界异常
    px = max(0, min(px, screen_w - 1))
    py = max(0, min(py, screen_h - 1))
    return px, py


# ---------------------------------------------------------------------------
# 思考：调视觉模型（配置驱动，主模型失败自动降级 glm-4v-flash）
# ---------------------------------------------------------------------------

def _load_llm_config() -> dict:
    """读取与 brain 同一份 jarvis/llm_config.json，取 api_key 与 base_url。"""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _vision_config() -> tuple[str, dict]:
    """
    从 llm_config.json 读取视觉模型配置：
      - vision_model：主视觉模型名，缺省回落到 glm-4.5v；
      - vision_extra_body：附加请求体（默认关闭思考模式），
        兼容 JSON 字符串与已解析的 dict 两种写法，解析失败按空 dict 处理。
    返回 (模型名, 附加请求体 dict)。
    """
    cfg = _load_llm_config()
    model = str(cfg.get("vision_model") or _DEFAULT_VISION_MODEL).strip()
    if not model:
        model = _DEFAULT_VISION_MODEL
    extra_raw = cfg.get("vision_extra_body", _DEFAULT_VISION_EXTRA_BODY)
    if isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        try:
            extra = json.loads(str(extra_raw))
            if not isinstance(extra, dict):
                extra = {}
        except (json.JSONDecodeError, TypeError, ValueError):
            print("[eyes] vision_extra_body 解析失败，按空附加请求体处理")
            extra = {}
    return model, extra


def _ask_vlm_once(image_b64: str, user_text: str, system: str | None,
                  model: str, extra_body: dict) -> str:
    """
    单次视觉模型调用：发送一张截图 + 文本，返回模型的文本回复。
    OpenAI 兼容格式，image_url 传 base64 data URI；httpx 超时 60 秒。
    vision_extra_body 合并进请求 payload（如 thinking 关闭）。
    失败时抛出异常，由 _ask_vlm 决定降级或上抛。
    """
    cfg = _load_llm_config()
    # 多引擎支持（A/B 实测）：vision_base_url / vision_api_key 存在时
    # 视觉走独立引擎（如 Kimi），缺省与大脑同引擎，行为与旧版一致
    url = str(cfg.get("vision_base_url") or cfg["base_url"]).rstrip("/") \
        + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + str(cfg.get("vision_api_key") or cfg["api_key"]),
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or "你是一个屏幕内容描述助手。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + image_b64},
                    },
                ],
            },
        ],
        "temperature": 0.1,  # 动作决策要低随机性
    }
    payload.update(extra_body)  # 合并 vision_extra_body（如 thinking disabled）
    with httpx.Client(timeout=_VLM_TIMEOUT) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def _ask_vlm(image_b64: str, user_text: str, system: str | None = None) -> str:
    """
    配置驱动的视觉模型调用：先用 llm_config.json 指定的主模型，
    调用失败（HTTP 错误 / 超时 / 鉴权）自动降级到 glm-4v-flash 再试一次。
    降级请求不带 vision_extra_body：thinking 字段是 glm-4.5 系参数，
    带上去可能让旧模型直接报错，降级链路必须尽可能朴素以保证可用。
    两次都失败时抛出异常，由调用方决定话术。
    """
    model, extra = _vision_config()
    try:
        return _ask_vlm_once(image_b64, user_text, system, model, extra)
    except Exception as exc:
        if model == _VLM_FALLBACK_MODEL:
            raise  # 主模型已是降级模型，继续降级只会死循环
        print("[eyes] 主视觉模型 %s 调用失败（%s），降级到 %s 重试一次"
              % (model, exc, _VLM_FALLBACK_MODEL))
        return _ask_vlm_once(image_b64, user_text, system,
                             _VLM_FALLBACK_MODEL, {})


def _describe_screen(shot_b64: str) -> str:
    """
    失败时补问一句「当前屏幕显示什么」，用于具体化失败报告。
    只读感知，不执行任何动作；自身调用失败返回空串（降级为无屏幕描述），
    绝不允许因为补充描述失败而掩盖原始失败原因。
    """
    if not shot_b64:
        return ""
    try:
        reply = _ask_vlm(shot_b64,
                         "用一句话客观描述这张屏幕截图当前显示的主要内容"
                         "（看到了哪些应用的窗口、界面停留大概在什么位置）。")
        return reply.strip()
    except Exception as exc:
        print("[eyes] 失败报告的屏幕状态补问失败：%s" % exc)
        return ""


# 复核模块 prompt：只看截图实际可见内容回答是非题，拿不准一律 false
_VLM_VERIFY_SYSTEM = (
    "你是 Nolan 的执行复核模块。我会给你一张电脑屏幕截图和一个判断问题，"
    "你只根据截图中实际可见的内容回答，绝不猜测、绝不脑补。\n"
    "只返回一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：\n"
    '{"ok": true或false, "reason": "一句话依据"}\n'
    "拿不准时返回 false 并在 reason 里说明缺什么。"
)


def _verify(shot_b64: str, question: str) -> tuple:
    """
    执行复核（闭环核心）：问 VLM 一个关于当前截图的是非问题，
    返回 (ok, reason)。ok 为 True/False 是有效判断；
    复核调用本身失败或回复无法解析时返回 (None, "")，由调用方按
    「复核不可用，静默放行」处理——复核是可靠性增强，绝不成为新的故障点。
    """
    if not shot_b64:
        return None, ""
    try:
        raw = _ask_vlm(shot_b64, question, system=_VLM_VERIFY_SYSTEM)
    except Exception as exc:
        print("[eyes] 复核调用失败（按放行处理）：%s" % exc)
        return None, ""
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return None, ""
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        return None, ""
    return obj["ok"], str(obj.get("reason", ""))


def _parse_action(raw: str) -> dict | None:
    """
    从 VLM 回复中解析动作 JSON。VLM 偶尔会裹 markdown 代码块或前后缀文字，
    先做宽松提取再严格校验 action 字段；解析失败返回 None。
    """
    text = raw.strip()
    # 剥离可能的 ```json ... ``` 包裹
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    candidate = m.group(0)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        obj = None
    if obj is None:
        # VLM 常见笔误："x": 204, 475, —— y 被写成了裸数字，修复后重试
        if '"y"' not in candidate:
            repaired = re.sub(
                r'("x"\s*:\s*-?\d+)\s*,\s*(-?\d+)(\s*,)',
                r'\1, "y": \2\3', candidate)
            if repaired != candidate:
                try:
                    obj = json.loads(repaired)
                except json.JSONDecodeError:
                    return None
            else:
                return None
        else:
            return None
    legal = {"left_click", "double_click", "type", "key",
             "scroll", "wait", "done", "fail"}
    if not isinstance(obj, dict) or obj.get("action") not in legal:
        return None
    return obj


# ---------------------------------------------------------------------------
# 动作：pyautogui 执行
# ---------------------------------------------------------------------------

def _snap(x: float, y: float, controls: list | None) -> tuple:
    """UIA 就近吸附：有控件清单则对齐到最近控件中心，失败/无清单原样返回。"""
    if not controls or _uia is None:
        return x, y
    try:
        return _uia.snap_to_element(controls, x, y)
    except Exception:
        return x, y


def _do_action(action: dict, shot_w: int, shot_h: int,
               controls: list | None = None) -> None:
    """
    执行单步动作。坐标先经 _vlm_to_screen 换算为物理像素；
    若携带 UIA 控件清单（controls），落鼠标前再做一次 snap_to_element
    就近吸附，把 VLM 的目测坐标对齐到真实控件中心，降低点偏概率。
    type 用 pyperclip 复制 + ctrl+v 粘贴，天然支持中文；
    key 支持单键（enter）与组合键（ctrl+v）。
    pyautogui.FailSafeException 不在此捕获，向上抛给 perform 统一中止。
    """
    act = action["action"]

    if act in ("left_click", "double_click"):
        # 按名定位优先：VLM 在 text 里给出目标元素的可见文字时，
        # 先用 UIA 按名查找精确中心——名称是精确的，坐标是估计的；
        # 找不到再退回坐标 + 就近吸附的老路
        target_text = str(action.get("text", "")).strip()
        named_xy = None
        if target_text and controls and _uia is not None:
            try:
                named_xy = _uia.find_element(controls, target_text)
            except Exception:
                named_xy = None
        if named_xy:
            x, y = named_xy
            print("[eyes] 按名定位命中「%s」-> (%d, %d)" % (target_text, x, y))
        else:
            x, y = _vlm_to_screen(float(action.get("x", 0)),
                                  float(action.get("y", 0)), shot_w, shot_h)
            x, y = _snap(x, y, controls)
        pyautogui.moveTo(x, y, duration=0.2)
        if act == "left_click":
            pyautogui.click()
        else:
            pyautogui.doubleClick()

    elif act == "scroll":
        # scroll 坐标可选：给了就先移动鼠标过去，再滚动
        if "x" in action and "y" in action:
            x, y = _vlm_to_screen(float(action["x"]), float(action["y"]),
                                  shot_w, shot_h)
            x, y = _snap(x, y, controls)
            pyautogui.moveTo(x, y, duration=0.2)
        direction = str(action.get("text", "down")).lower()
        pyautogui.scroll(3 if direction == "up" else -3)

    elif act == "type":
        # 剪贴板粘贴方案：pyautogui.write 不支持中文，pyperclip + ctrl+v 通吃
        pyperclip.copy(str(action.get("text", "")))
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")

    elif act == "key":
        keys = str(action.get("keys", "")).lower().strip()
        parts = [k.strip() for k in keys.split("+") if k.strip()]
        if not parts:
            return
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)

    elif act == "wait":
        time.sleep(1.0)

    # done / fail 不执行任何物理动作，由 perform 的循环逻辑处理


# ---------------------------------------------------------------------------
# 失败报告具体化：第几步 + VLM 最后判断 + 当前屏幕状态
# ---------------------------------------------------------------------------

def _join_zh(*parts: str) -> str:
    """中文句读拼接：去掉各分句末尾句号后用「。」连接，保证全文恰好一个结尾句号。"""
    cleaned = [p.strip().rstrip("。") for p in parts if p and p.strip()]
    if not cleaned:
        return ""
    return "。".join(cleaned) + "。"


def _fail_report(step: int, reason: str, shot_b64: str, executed: int) -> str:
    """
    失败话术：失败在第几步、这一步 VLM 的判断、当前屏幕状态简述。
    屏幕状态通过 _describe_screen 补问一次 VLM 获得，补问失败则省略该句。
    """
    parts = [_MSG_FAIL_PREFIX, "卡在第 %d 步：%s" % (step, reason)]
    desc = _describe_screen(shot_b64)
    if desc:
        parts.append("当前屏幕显示的是：%s" % desc)
    if executed > 0:
        parts.append("此前已成功执行 %d 个动作，请您检查" % executed)
    return _join_zh(*parts)


def _over_limit_report(max_steps: int, last_thought: str,
                       shot_b64: str, executed: int) -> str:
    """步数超限话术：上限步数、最后一步 VLM 的判断、当前屏幕状态简述。"""
    parts = ["先生，任务步数超出安全上限（%d 步）" % max_steps]
    if last_thought:
        parts.append("停顿时最后一步的判断是：%s" % last_thought)
    desc = _describe_screen(shot_b64)
    if desc:
        parts.append("当前屏幕显示的是：%s" % desc)
    if executed > 0:
        parts.append("已成功执行 %d 个动作" % executed)
    parts.append("已完成的部分请您检查")
    return _join_zh(*parts)


# ---------------------------------------------------------------------------
# Gap2 结构化处置：复核未生效 -> 收集证据 -> 分类 -> 按对策表修复
#
# 第一性原理：失败不是随机噪声，是可分类的物理原因；盲重试没有分类，
# 所以修不好。这里只做三件事——把现场证据收集齐（UIA 可用时）、
# 交给 reliability 纯函数分类与决策、执行决策中的物理对策。
# 任何环节异常都向上抛（由调用方捕获退回旧逻辑），绝不把增强路径
# 变成新的故障点。
# ---------------------------------------------------------------------------

def _collect_failure_evidence(action: dict, expect: str, why: str,
                              controls_before: list,
                              target_hint: str | None) -> dict:
    """
    收集「复核未生效」的现场证据，供 reliability.classify 分类。
    UIA 不可用时只填动作本身的信息（分类器会按保守类别处置）；
    单项证据采集失败只缺该项，不影响其他证据。
    """
    ev = {
        "action": action.get("action", ""),
        "text": str(action.get("text", "") or ""),
        "expect": expect,
        "verify_reason": why or "",
        "controls_before": len(controls_before or []),
    }
    if action.get("keys"):
        ev["keys"] = action.get("keys")
    # 目标窗口前台状态（焦点丢失判据；无 hint 或 UIA 不可用时缺省）
    if target_hint and _uia is not None:
        try:
            ev["hint_in_foreground"] = (
                target_hint.lower() in _uia.foreground_title().lower())
        except Exception:
            pass
    # 复核时刻重新枚举控件树：看目标是没出现、挪了位置，还是整树消失
    if _uia is not None:
        try:
            after = _uia.dump_window_controls() or []
            ev["controls_after"] = len(after)
            text = ev["text"].strip()
            if text:
                xy = _uia.find_element(after, text)
                ev["named_found_after"] = xy is not None
                if xy:
                    ev["named_xy_after"] = xy
        except Exception:
            pass
    # 实际点击落点（物理像素，供坐标漂移比对）
    if action.get("action") in ("left_click", "double_click") \
            and "x" in action and "y" in action:
        try:
            ev["clicked_xy"] = _vlm_to_screen(float(action.get("x", 0)),
                                              float(action.get("y", 0)))
        except Exception:
            pass
    return ev


def _execute_countermeasure(decision: dict, action: dict,
                            target_hint: str | None) -> None:
    """
    执行 reliability.decide 给出的物理对策。每条对策对应一个物理原因：
      refocus       把目标窗口重新置前（焦点丢失——动作才有落点）；
      wait_recheck  退避等待（目标未加载完 / 应用短暂无响应——等待即解药）；
      relocate      不直接动手，hint 已指引下一步按名定位（名称是 invariant）；
      retype        全选重输（文本没进去——幂等：误报时重输同文结果不变）；
      backoff_retry 退避等待（界面响应慢——给渲染留时间）。
    """
    strategy = decision.get("action")
    backoff = float(decision.get("backoff", 0.0) or 0.0)
    if strategy == "refocus":
        if target_hint and _uia is not None:
            try:
                if _uia.bring_to_front(target_hint):
                    print("[eyes] 对策：目标窗口「%s」已重新置前" % target_hint)
            except Exception:
                pass
        time.sleep(max(backoff, 0.5))  # 等窗口切换动画完成
    elif strategy in ("wait_recheck", "backoff_retry"):
        print("[eyes] 对策：退避等待 %.1f 秒后重查" % backoff)
        time.sleep(backoff)
    elif strategy == "relocate":
        # 不直接点击：点击决策权留给 VLM，hint 已给出按名定位指引；
        # 短退避等界面稳定后进入下一步（控件清单会重新枚举）
        time.sleep(max(backoff, 0.5))
    elif strategy == "retype":
        # 复核已两次确认文字未生效；即使属复核误报，全选后重输同文
        # 结果不变（幂等），不会产生双倍文本
        try:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyperclip.copy(str(action.get("text", "")))
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "v")
            print("[eyes] 对策：已全选并重新输入「%s」"
                  % str(action.get("text", ""))[:30])
        except Exception as exc:
            print("[eyes] 全选重输执行失败（%s），交由下一步观察" % exc)
        time.sleep(max(backoff, 0.5))


def _remedy_verify_failure(step: int, action: dict, expect: str, why: str,
                           controls_before: list, target_hint: str | None,
                           ledger, history: list) -> bool:
    """
    复核未生效的结构化处置（增强路径）：
    收集证据 -> reliability.classify 归类物理原因 -> decide 选对策 ->
    预算内执行修复并把处置说明写入历史（带给下一步 VLM）。
    返回 True 表示已按对策处置（任务继续）；False 表示无可用对策
    （未知类别或预算耗尽），由调用方走旧的判失败逻辑。
    """
    ev = _collect_failure_evidence(action, expect, why, controls_before,
                                   target_hint)
    category = _rel.classify(ev)
    decision = _rel.decide(category, ledger, ev)
    cat_cn = _rel.CATEGORY_CN.get(category, category)
    # Gap5-P2：失败教训沉淀进窗口上下文，下次同窗口任务带着经验上场
    if _wctx is not None:
        try:
            _wctx.record_failure(
                _wkey(target_hint),
                ("%s %s" % (ev["action"], ev["text"])).strip()[:60],
                category,
                ("%s：%s" % (cat_cn,
                             decision.get("hint") or decision.get("reason", "")
                             ))[:120])
        except Exception:
            pass
    if decision["action"] == "give_up":
        print("[eyes] 第 %d 步失败分类：%s -> 放弃自愈（%s）"
              % (step, cat_cn, decision.get("reason", "")))
        return False
    print("[eyes] 第 %d 步失败分类：%s -> 对策 %s（退避 %.1f 秒，账本：%s）"
          % (step, cat_cn, decision["action"], decision["backoff"],
             ledger.summary()))
    ledger.note(category)
    _execute_countermeasure(decision, action, target_hint)
    if decision.get("hint"):
        history.append("第%d步 系统处置：%s" % (step, decision["hint"]))
    return True


# ---------------------------------------------------------------------------
# 主闭环：perform
# ---------------------------------------------------------------------------

def perform(task: str, max_steps: int = 12, target_hint: str | None = None) -> str:
    """
    屏幕感知 + GUI 动作闭环：逐步截屏、问 VLM、执行动作，直到完成。

    target_hint：目标应用窗口标题词（由 hands 前导提取）。提供时每步
    截屏前检查前台窗口，目标不在前台就先置前——LLM 往返的几秒内
    其他窗口（浏览器、弹窗、聊天软件）可能抢占前台遮挡目标，
    整屏截图会把遮挡物当成操作对象（J1 基准实测的头号抖动源）。

    返回口语化结果（Nolan 人设，直接可语音播报）：
      - done        -> VLM 的完成汇报话术
      - fail        -> 具体化失败报告：第几步 + VLM 判断 + 当前屏幕状态
      - 目标应用缺失 -> 明确说出「屏幕上没有找到 X，请先让我用 open_app 打开它」
      - 步数超限     -> 超限话术 + 最后一步判断 + 当前屏幕状态
      - FAILSAFE    -> 固定中止话术
      - VLM 不可达   -> 固定降级话术
    永不向调用方抛异常。
    """
    print("[eyes] 任务开始：%s（步数上限 %d）" % (task, max_steps))

    history: list[str] = []  # 已执行动作历史，每步带给 VLM 防止重复动作
    done_steps: list = []    # 结构化动作序列，任务成功后固化进技能库
    executed = 0             # 已执行的物理动作数（wait 不计）
    repeat_sigs: list = []   # 动作签名序列，用于死循环检测（同一动作连续重复即介入）
    early_fail_retries = 0   # 「零动作早退 fail」的宽限次数
    verify_fails = 0         # 连续「复核未生效」次数（1 次换策略提示，2 次判失败）
    done_rejects = 0         # done 复核驳回次数（最多 2 次，防复核侧死循环）
    last_shot = ""           # 最近一次截屏，失败时用于补问屏幕状态
    last_thought = ""        # 最后一步 VLM 的决策理由，失败报告要带出来
    # 结构化重试账本（Gap2）：每类失败独立预算 + 全局总预算封顶；
    # reliability 缺失时为 None，复核失败处置走旧逻辑
    ledger = _rel.RetryLedger() if _rel is not None else None
    prev_state = None  # 屏幕状态流（Gap5）：上一步的界面指纹，diff 基准

    try:
        for step in range(1, max_steps + 1):
            # 0) 前台保障：目标窗口被遮挡时先置前，再截屏。
            # 失败静默（托盘/标题漂移），不阻断主闭环
            if target_hint and _uia is not None:
                try:
                    if target_hint.lower() not in _uia.foreground_title().lower():
                        if _uia.bring_to_front(target_hint):
                            print("[eyes] 目标窗口「%s」曾被遮挡，已重新置前"
                                  % target_hint)
                            time.sleep(0.5)  # 等窗口完成切换动画
                except Exception:
                    pass

            # 1) 感知：截屏
            shot = screenshot_b64()
            last_shot = shot
            shot_w, shot_h = _screenshot_size()

            # 1.5) UIA 元素树：枚举前台窗口的可操作控件（含屏幕物理坐标），
            # 作为 VLM 坐标的参照系；UIA 不可用时静默降级为空列表
            controls = []
            if _uia is not None:
                try:
                    controls = _uia.dump_window_controls() or []
                except Exception:
                    controls = []
            # Gap5-P2：控件清单增量登记进窗口上下文（毫秒级，失败静默）
            if controls and _wctx is not None:
                try:
                    _wctx.record_controls(_wkey(target_hint), controls)
                except Exception:
                    pass

            # 2) 思考：问 VLM 下一步动作；非法 JSON 原地重试一次
            prompt = "当前任务：%s。这是第 %d 步（上限 %d 步）。" % (
                task, step, max_steps)
            # Gap5-P2：窗口历史经验注入——已知控件/上次成功策略/失败教训
            if _wctx is not None:
                try:
                    _wbrief = _wctx.brief(_wkey(target_hint))
                    if _wbrief:
                        prompt += "本窗口的历史经验：%s。" % _wbrief
                except Exception:
                    pass
            if history:
                prompt += "已执行的动作历史：%s。" % "；".join(history)
            if controls:
                prompt += ("屏幕上的可操作控件（来自无障碍元素树，"
                           "坐标为屏幕物理像素，点击时优先对齐这些控件）：%s。"
                           % _uia.format_controls(controls))
            prompt += "请结合当前截图判断任务是否已完成，返回下一步动作 JSON。"
            try:
                raw = _ask_vlm(shot, prompt, system=_VLM_SYSTEM)
            except Exception as exc:  # 网络 / 鉴权 / 超时（含降级后仍失败）
                print("[eyes] VLM 请求失败：%s" % exc)
                return _MSG_VLM_DOWN

            action = _parse_action(raw)
            if action is None:
                print("[eyes] 第 %d 步 VLM 返回非法 JSON，重试一次：%s"
                      % (step, raw[:200]))
                try:
                    raw = _ask_vlm(shot, prompt + " 上次回复不是合法 JSON，"
                                   "请只返回一个 JSON 对象。",
                                   system=_VLM_SYSTEM)
                except Exception as exc:
                    print("[eyes] VLM 重试请求失败：%s" % exc)
                    return _MSG_VLM_DOWN
                action = _parse_action(raw)
                if action is None:
                    # 第三次机会：换降级模型 glm-4v-flash（指令服从更朴素），
                    # 主模型的随机格式错误不该直接判任务失败
                    print("[eyes] 第 %d 步重试仍非法，换降级模型最后重试" % step)
                    try:
                        raw = _ask_vlm_once(
                            shot,
                            prompt + " 只返回一个合法 JSON 对象，"
                            "不要任何其他文字、不要 markdown 代码块。",
                            _VLM_SYSTEM, _VLM_FALLBACK_MODEL, {})
                        action = _parse_action(raw)
                    except Exception as exc:
                        print("[eyes] 降级模型重试请求失败：%s" % exc)
                if action is None:
                    print("[eyes] 第 %d 步三次尝试均非法，按 fail 处理" % step)
                    return _fail_report(step, "视觉模块连续多次未能给出有效指令",
                                        last_shot, executed)

            thought = str(action.get("thought", ""))
            last_thought = thought
            print("[eyes] 第 %d 步：%s | %s"
                  % (step, action["action"], thought))

            # 3) 终态判定：done 必须先经复核才生效，防 VLM 谎报完成
            if action["action"] == "done":
                ok, why = _verify(
                    shot,
                    "任务目标是「%s」。请只看这张截图判断："
                    "该目标是否已经在屏幕上真正完成？" % task)
                if ok is False and done_rejects < 2:
                    done_rejects += 1
                    print("[eyes] 第 %d 步 done 复核未通过（%s），继续执行（%d/2）"
                          % (step, why, done_rejects))
                    history.append(
                        "第%d步 系统复核：任务尚未真正完成（%s），请继续未完成的部分"
                        % (step, (why or "目标未达成")[:40]))
                    time.sleep(_STEP_INTERVAL)
                    continue
                summary = thought or "任务已完成。"
                print("[eyes] 任务完成%s：%s"
                      % ("（复核通过）" if ok is True else "", summary))
                # 技能固化：经复核确认成功的动作序列沉淀为可复用技能，
                # 下次同类任务直接重放，不再逐步掷骰子
                if _skills is not None and done_steps:
                    try:
                        _skills.record(task, done_steps)
                    except Exception:
                        pass
                return summary
            if action["action"] == "fail":
                reason = thought or "视觉模块判断无法完成。"
                # 目标应用不在屏幕：立即如实报告并提示先 open_app，
                # 不做早退宽限——等待解决不了「应用根本没打开」，
                # 快速把信号还给 brain，让它走 open_app 补救路径
                if "屏幕上没有找到" in reason:
                    # 窗口其实存在、只是被遮挡/未置前时，「缺失」是误报：
                    # 置前后重看（计入早退宽限，防无限循环），不判任务失败
                    if (target_hint and _uia is not None
                            and early_fail_retries < 2):
                        try:
                            hwnd = _uia._find_hwnd_by_title(target_hint)
                        except Exception:
                            hwnd = 0
                        if hwnd:
                            early_fail_retries += 1
                            print("[eyes] 目标窗口存在但被遮挡（VLM 报缺失为误报），"
                                  "置前重看（%d/2）" % early_fail_retries)
                            _uia.bring_to_front(target_hint)
                            time.sleep(1.0)
                            history.append(
                                "第%d步 目标窗口被其他界面遮挡，已重新置前，请重新观察"
                                % step)
                            continue
                    print("[eyes] 目标应用缺失：%s" % reason)
                    return _join_zh(_MSG_FAIL_PREFIX, reason,
                                    "请先让我用 open_app 打开它")
                # 零动作早退宽限：尚未执行任何物理动作时的 fail 没有副作用，
                # 多是界面仍在加载或 VLM 的随机误判，宽限为 wait 重看一次
                # （最多 2 次；安全禁令类 fail 重看后仍会 fail，不会被掩盖）
                if executed == 0 and early_fail_retries < 2:
                    early_fail_retries += 1
                    print("[eyes] 第 %d 步早退 fail（%s），宽限为等待重看（%d/2）"
                          % (step, reason, early_fail_retries))
                    history.append("第%d步 视觉判断「%s」但尚未尝试，继续观察"
                                   % (step, reason[:30]))
                    time.sleep(_STEP_INTERVAL)
                    continue
                print("[eyes] 任务失败（第 %d 步）：%s" % (step, reason))
                return _fail_report(step, reason, last_shot, executed)

            # 4) 执行动作（FAILSAFE 抛异常即中止）
            try:
                _do_action(action, shot_w, shot_h, controls)
            except pyautogui.FailSafeException:
                print("[eyes] FAILSAFE 触发，安全中止")
                return _MSG_FAILSAFE

            # 记入历史：下一步带给 VLM，避免模型无记忆而重复同一动作
            desc = action["action"]
            if action.get("text"):
                desc += "「%s」" % action["text"]
            elif action.get("keys"):
                desc += "「%s」" % action["keys"]
            history.append("第%d步 %s" % (step, desc))
            if action["action"] != "wait":
                executed += 1
                done_steps.append({"action": action["action"],
                                   "text": action.get("text", ""),
                                   "keys": action.get("keys", "")})

            # 4.5) 死循环检测：同一动作（动作+坐标+文本）连续重复——
            # 第 3 次重复给 VLM 换路强提示，第 4 次直接判定失败，
            # 避免像「反复点同一入口 11 次」那样空转到上限
            sig = (action["action"], action.get("x"), action.get("y"),
                   action.get("text"), action.get("keys"))
            repeat_sigs.append(sig)
            if len(repeat_sigs) >= 3 and len(set(repeat_sigs[-3:])) == 1:
                if len(repeat_sigs) >= 4 and len(set(repeat_sigs[-4:])) == 1:
                    print("[eyes] 同一动作连续重复 4 次无效，判定失败")
                    return _fail_report(
                        step,
                        "同一操作重复多次均无效（界面无变化），任务目标可能不存在或需要先登录",
                        last_shot, executed)
                history.append("警告：同一操作已连续 3 次且界面无变化，"
                               "下一步必须换完全不同的策略（滚动页面、换其他入口、"
                               "或直接 fail 并说明真实原因，例如列表为空/需要登录）")

            # 5) 等界面响应，进入下一步
            time.sleep(_STEP_INTERVAL)

            # 5.6) 屏幕状态流（Gap5-P1）：动作后界面到底变了没有，diff 实况
            # 作为地面实况写入历史——VLM 不再凭想象判断自己的动作有没有生效；
            # 截屏+枚举仅毫秒级，相比一次 VLM 往返（秒级）成本可忽略
            if _perception is not None:
                try:
                    _s6 = screenshot_b64()
                    _c6 = _uia.dump_window_controls() if _uia else []
                    curr = _capture_screen_state(target_hint, _s6, _c6 or [])
                    if prev_state is not None and curr is not None:
                        _d6 = _perception.diff_states(prev_state, curr)
                        _change = _perception.describe_change(_d6)
                        if _change:
                            history.append("系统感知：%s" % _change)
                    prev_state = curr or prev_state
                except Exception:
                    pass

            # 5.5) 执行复核（闭环核心）：物理动作携带 expect 时，
            # 先用 UIA 控件树做廉价复核（阳性确认即视为生效，省一次截图 +
            # VLM 往返）；UIA 答不了再重新截屏复核。未生效则先经 reliability
            # 分类物理原因、按对策表结构化修复（增强路径）；模块缺失或处置
            # 异常时退回旧逻辑：记入历史强制换策略，连续 2 次未生效判定失败；
            # 复核不可用（None）静默放行
            expect = str(action.get("expect", "")).strip()
            if action["action"] != "wait" and expect:
                check_shot = ""
                ok, why = None, ""
                # 廉价复核优先：控件树能确认预期元素已出现时，
                # 不必动截图 + VLM（一次本地枚举 vs 一次模型往返）
                if _rel is not None and _uia is not None:
                    try:
                        if _rel.uia_verify(
                                action, expect,
                                lambda: _uia.dump_window_controls() or []):
                            ok, why = True, "控件树确认预期元素已出现"
                    except Exception:
                        ok, why = None, ""
                if ok is not True:
                    try:
                        check_shot = screenshot_b64()
                        ok, why = _verify(
                            check_shot,
                            "刚执行的动作是「%s」，预期屏幕上会出现：%s。"
                            "请看这张截图判断：预期的效果是否已经出现？"
                            % (desc, expect))
                    except Exception:
                        ok, why = None, ""
                if ok is False:
                    # 界面加载宽限：动作可能已生效但页面尚未渲染完，
                    # 等 1.5 秒换一张截图复核第二次，仍不符才计未生效
                    print("[eyes] 第 %d 步复核未见预期，等 1.5 秒二次复核……" % step)
                    time.sleep(1.5)
                    # Gap5-P1「没变不重看」：等完界面仍无实质变化时，
                    # 再问一次 VLM「出现了吗」是纯粹的浪费（答案不会变），
                    # 直接计未生效走处置；变了才值得花一次 VLM 复核
                    _skip2 = False
                    if _perception is not None and prev_state is not None:
                        try:
                            _s7 = screenshot_b64()
                            _c7 = _uia.dump_window_controls() if _uia else []
                            _st7 = _capture_screen_state(target_hint, _s7,
                                                         _c7 or [])
                            if (_st7 is not None
                                    and not _perception.should_review(
                                        prev_state, _st7)):
                                ok, why = False, "等待后界面仍无实质变化"
                                _skip2 = True
                                check_shot = _s7
                                print("[eyes] 第 %d 步界面无实质变化，"
                                      "跳过二次 VLM 复核（省下往返）" % step)
                            prev_state = _st7 or prev_state
                        except Exception:
                            pass
                    if not _skip2:
                        try:
                            check_shot = screenshot_b64()
                            ok2, why2 = _verify(
                                check_shot,
                                "刚执行的动作是「%s」，预期屏幕上会出现：%s。"
                                "请看这张截图判断：预期的效果是否已经出现？"
                                % (desc, expect))
                            if ok2 is not False:
                                ok, why = ok2, why2
                        except Exception:
                            pass
                if ok is False:
                    verify_fails += 1
                    print("[eyes] 第 %d 步复核未生效（期望：%s；实际：%s）（连续 %d 次）"
                          % (step, expect[:30], (why or "未出现")[:30],
                             verify_fails))
                    # 结构化处置（增强路径）：分类 -> 对策 -> 预算内修复，
                    # 已处置则带着处置说明进入下一步；未处置（未知类别 /
                    # 预算耗尽 / 模块缺失 / 处置异常）走旧判失败逻辑
                    handled = False
                    if _rel is not None and ledger is not None:
                        try:
                            handled = _remedy_verify_failure(
                                step, action, expect, why, controls,
                                target_hint, ledger, history)
                        except Exception as exc:
                            print("[eyes] 可靠性处置异常（退回旧逻辑）：%s" % exc)
                            handled = False
                    if not handled:
                        if verify_fails >= 2:
                            return _fail_report(
                                step,
                                "连续两次操作未产生预期效果（期望：%s），"
                                "界面可能未响应或目标不存在" % expect[:40],
                                check_shot or last_shot, executed)
                        history.append(
                            "警告：第%d步操作未生效（期望「%s」未出现，实际：%s），"
                            "下一步必须换完全不同的做法"
                            % (step, expect[:30], (why or "未出现")[:30]))
                else:
                    verify_fails = 0
                    # Gap5-P2：成功定位沉淀——下次同窗口任务带着经验上场
                    if _wctx is not None:
                        try:
                            _wctx.record_success(
                                _wkey(target_hint), desc[:60],
                                str(action.get("text", "")
                                    or action.get("keys", "") or "")[:40],
                                "UIA按名吸附" if controls else "纯截图坐标")
                        except Exception:
                            pass

        # 步数耗尽仍未 done/fail：超限话术 + 最后判断 + 屏幕状态
        print("[eyes] 步数超出上限 %d，中止" % max_steps)
        return _over_limit_report(max_steps, last_thought, last_shot, executed)

    except pyautogui.FailSafeException:
        # 截屏阶段之外也可能触发（例如 moveTo 期间），兜底捕获
        print("[eyes] FAILSAFE 触发（外层），安全中止")
        return _MSG_FAILSAFE
    except Exception as exc:  # 任何意外都不许炸到调用方
        print("[eyes] 未预期异常：%s" % exc)
        return _fail_report(max(1, executed + 1),
                            "执行过程中出现异常（%s），已停止" % exc,
                            last_shot, executed)


# ---------------------------------------------------------------------------
# 技能重放：replay
# ---------------------------------------------------------------------------

def replay(task: str, steps: list, target_hint: str | None = None):
    """
    技能重放：按固化的动作序列直接执行，不做 VLM 逐步推理。
    点击一律按名定位（UIA 重新解析当前位置，坐标不参与固化），
    任何一步解析不到或执行异常立即返回 None，由调用方回退正常视觉闭环。
    全部动作执行完做一次终态复核（全程唯一一次模型调用），
    复核未过同样返回 None——重放是捷径，不是免检通道。
    触发 FAILSAFE 返回中止话术（字符串结果，不是 None）。
    """
    if not steps:
        return None
    print("[eyes] 技能重放：%s（%d 步）" % (task, len(steps)))
    for i, s in enumerate(steps, 1):
        # 前台保障（与 perform 同款）：目标被遮挡先置前
        if target_hint and _uia is not None:
            try:
                if target_hint.lower() not in _uia.foreground_title().lower():
                    _uia.bring_to_front(target_hint)
                    time.sleep(0.5)
            except Exception:
                pass
        act = s.get("action")
        try:
            if act in ("left_click", "double_click"):
                if _uia is None:
                    return None
                controls = _uia.dump_window_controls() or []
                xy = _uia.find_element(controls, str(s.get("text", "")))
                if not xy:
                    print("[eyes] 重放第 %d 步：按名未找到「%s」，放弃重放"
                          % (i, s.get("text")))
                    return None
                pyautogui.moveTo(xy[0], xy[1], duration=0.2)
                if act == "left_click":
                    pyautogui.click()
                else:
                    pyautogui.doubleClick()
            elif act == "type":
                pyperclip.copy(str(s.get("text", "")))
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "v")
            elif act == "key":
                parts = [k.strip() for k in str(s.get("keys", "")).lower()
                         .split("+") if k.strip()]
                if not parts:
                    return None
                if len(parts) == 1:
                    pyautogui.press(parts[0])
                else:
                    pyautogui.hotkey(*parts)
            elif act == "scroll":
                pyautogui.scroll(3 if str(s.get("text", "down")).lower()
                                 == "up" else -3)
            else:
                return None
        except pyautogui.FailSafeException:
            print("[eyes] 重放触发 FAILSAFE，安全中止")
            return _MSG_FAILSAFE
        except Exception as exc:
            print("[eyes] 重放第 %d 步异常（%s），放弃重放" % (i, exc))
            return None
        time.sleep(0.8)

    # 终态复核：唯一一次模型调用，确认目标真实达成
    try:
        ok, why = _verify(
            screenshot_b64(),
            "任务目标是「%s」。请只看这张截图判断："
            "该目标是否已经在屏幕上真正完成？" % task)
    except Exception:
        ok, why = None, ""
    if ok is False:
        print("[eyes] 技能重放终态复核未通过（%s），回退正常闭环" % why)
        return None
    print("[eyes] 技能重放完成：%s" % task)
    return "先生，按我已掌握的技能为您完成了：%s。" % task


# ---------------------------------------------------------------------------
# 通用截屏元素：locate_and_crop
# ---------------------------------------------------------------------------
#
# 第一性原理：「把屏幕上看到的某个东西变成一张图片文件」的物理本质是
# 「截屏 -> 找到它的边界框 -> 按物理像素裁剪保存」。与 perform 的动作闭环
# 不同，这里不做任何鼠标键盘操作，是纯感知能力：任何界面元素（歌曲封面、
# 头像、图标、图表）都走同一条链路，机制通用化。

# 元素定位 prompt：只问边界框 JSON，坐标基于发送的缩放截图
_VLM_LOCATE_SYSTEM = (
    "你是 Nolan 的屏幕元素定位模块。我会给你一张电脑屏幕截图和一段元素描述，"
    "你的唯一任务是给出该元素在截图中的边界框。\n"
    "只返回一个 JSON 对象（不要任何多余文字、不要 markdown 代码块）：\n"
    '{"x1": 左上角x, "y1": 左上角y, "x2": 右下角x, "y2": 右下角y}\n'
    "规则：\n"
    "1. 坐标是这张截图的像素坐标（截图左上角为原点），边界框要紧贴元素边缘。\n"
    "2. 若截图中找不到该元素，返回 "
    '{"x1": 0, "y1": 0, "x2": 0, "y2": 0}。'
)


def _parse_bbox(raw: str) -> tuple[float, float, float, float] | None:
    """
    从 VLM 回复中解析边界框 JSON。复用宽松提取策略（剥 markdown 包裹），
    严格校验四个坐标字段齐备且构成正面积矩形；任何不合法返回 None。
    """
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        x1, y1 = float(obj["x1"]), float(obj["y1"])
        x2, y2 = float(obj["x2"]), float(obj["y2"])
    except (KeyError, TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:  # 零面积 / 反向框视为「未找到」
        return None
    return x1, y1, x2, y2


def locate_and_crop(description: str) -> str | None:
    """
    通用截屏元素：定位 description 所指的界面元素并裁剪保存为 PNG。

    链路：截屏（物理像素）-> 缩放截图发视觉模型问边界框 ->
    换算回物理像素 -> 外扩 10 像素边距并钳制在屏幕内 -> 裁剪 ->
    保存到 jarvis\\files\\captures\\capture_时间戳.png（目录自动创建）。

    返回保存文件的完整路径；定位失败 / VLM 不可达 / JSON 非法返回 None。
    永不抛异常（所有意外在日志留痕后按 None 处理）。
    """
    try:
        description = (description or "").strip()
        if not description:
            print("[eyes] locate_and_crop：空描述，放弃")
            return None

        # 1) 感知：截一次屏，物理像素原图与缩放截图同源，
        #    避免两次截屏之间界面变化导致定位框与裁剪图错位
        full = ImageGrab.grab()  # DPI 感知后为物理像素
        shot = full
        if shot.width > _SHOT_MAX_WIDTH:
            new_h = round(shot.height * _SHOT_MAX_WIDTH / shot.width)
            shot = shot.resize((_SHOT_MAX_WIDTH, new_h), ImageGrab.Image.LANCZOS)
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
        shot_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # 2) 思考：问 VLM 该元素的边界框（截图像素坐标）
        prompt = ("请在这张截图中定位以下界面元素：%s。"
                  "只返回它的边界框 JSON。" % description)
        raw = _ask_vlm(shot_b64, prompt, system=_VLM_LOCATE_SYSTEM)
        bbox = _parse_bbox(raw)
        if bbox is None:
            print("[eyes] locate_and_crop：未定位到「%s」，VLM 回复：%s"
                  % (description, raw[:200]))
            return None

        # 3) 换算：截图像素 -> 屏幕物理像素（与 _vlm_to_screen 同一比例关系，
        #    但按本次截图的实际尺寸计算，不依赖全局屏幕状态）
        x1, y1, x2, y2 = bbox
        sx = full.width / shot.width
        sy = full.height / shot.height
        px1 = int(round(x1 * sx))
        py1 = int(round(y1 * sy))
        px2 = int(round(x2 * sx))
        py2 = int(round(y2 * sy))

        # 4) 外扩边距并钳制在屏幕内，防止幻觉坐标越界
        px1 = max(0, px1 - _CROP_MARGIN)
        py1 = max(0, py1 - _CROP_MARGIN)
        px2 = min(full.width, px2 + _CROP_MARGIN)
        py2 = min(full.height, py2 + _CROP_MARGIN)
        if px2 <= px1 or py2 <= py1:
            print("[eyes] locate_and_crop：钳制后裁剪框为空，放弃")
            return None

        # 5) 裁剪保存：文件名 capture_时间戳_毫秒.png，毫秒防同秒连拍覆盖
        os.makedirs(_CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fname = "capture_%s_%03d.png" % (stamp, int(time.time() * 1000) % 1000)
        path = os.path.join(_CAPTURE_DIR, fname)
        full.crop((px1, py1, px2, py2)).save(path, format="PNG")
        print("[eyes] locate_and_crop：已截取「%s」-> %s（框 %d,%d-%d,%d）"
              % (description, path, px1, py1, px2, py2))
        return path
    except Exception as exc:  # VLM 网络失败、截屏失败等一律按定位失败处理
        print("[eyes] locate_and_crop 未预期异常：%s" % exc)
        return None
