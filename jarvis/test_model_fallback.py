# -*- coding: utf-8 -*-
"""
test_model_fallback.py —— 模型未授权自动降级（零操作升级通道）单测

病例来源（2026-08-15 真机实测）：配置切换到 glm-5.3 后，智谱开放 API 返回
HTTP 403 {"error":{"code":"1220","message":"您无权访问glm-5.3。"}}——
模型存在但账号权限未开通。若不做处理，大脑每一次调用都会 403 变哑。
契约：403/1220 → 立即降级 _MODEL_FALLBACK 重发并记住整段进程；
权限开通后重启即自动用回新模型，全程零代码改动。

运行：python -m unittest test_model_fallback -v（jarvis 目录内）
零网络、零真实 API 调用（httpx.post 全 mock）。
"""
import unittest
from unittest import mock

import httpx

import brain


def _req():
    return httpx.Request("POST", "https://open.bigmodel.cn/api/paas/v4/chat/completions")


def _resp_403_1220():
    return httpx.Response(
        403,
        text='{"error":{"code":"1220","message":"您无权访问glm-5.3。"}}',
        request=_req(),
    )


def _resp_ok(text="好的先生"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}}]},
        request=_req(),
    )


def _http_err(resp):
    return httpx.HTTPStatusError("err", request=_req(), response=resp)


class ModelFallbackTest(unittest.TestCase):
    def setUp(self):
        brain._model_demoted = False
        brain._thinking_unsupported_models.clear()

    def tearDown(self):
        brain._model_demoted = False
        brain._thinking_unsupported_models.clear()

    def _payload(self, model="glm-5.3", thinking=True):
        p = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        if thinking:
            p["thinking"] = {"type": "disabled"}
        return p

    def test_403_1220_demotes_and_retries_with_fallback(self):
        """403/1220 → 立即用兜底模型重发成功，并记住降级。"""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json["model"])
            if json["model"] == "glm-5.3":
                raise _http_err(_resp_403_1220())
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep") as sleep_mock:
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertEqual(calls, ["glm-5.3", brain._MODEL_FALLBACK])
        self.assertTrue(brain._model_demoted)
        sleep_mock.assert_not_called()  # 403 永非瞬态，不许睡 1 秒原地重试

    def test_demotion_sticks_for_process(self):
        """降级记忆：第二次请求首发即走兜底，不再反复试探新模型。"""
        brain._model_demoted = True
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json["model"])
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post):
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertEqual(calls, [brain._MODEL_FALLBACK])

    def test_fallback_itself_403_no_infinite_loop(self):
        """兜底模型也 403（极端情况）→ 不递归降级，走普通重试后如实失败。"""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json["model"])
            raise _http_err(_resp_403_1220())

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep"):
            reply = brain._request_llm("http://x", self._payload(model=brain._MODEL_FALLBACK), {})
        self.assertIsNone(reply)
        # 首发 + 瞬态重试各一次，且始终是兜底模型，绝不递归
        self.assertEqual(calls, [brain._MODEL_FALLBACK, brain._MODEL_FALLBACK])

    def test_403_without_1220_is_not_access_denied(self):
        """普通 403（如无 1220/无权访问字样）不触发降级，走瞬态重试通道。"""
        resp = httpx.Response(403, text='{"error":{"code":"1001","message":"其他鉴权问题"}}',
                              request=_req())
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json["model"])
            raise _http_err(resp)

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep"):
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertIsNone(reply)
        self.assertEqual(calls, ["glm-5.3", "glm-5.3"])  # 原参数重试，不换模型
        self.assertFalse(brain._model_demoted)

    def test_500_is_transient_path_not_demotion(self):
        """500 等瞬态错误不触发降级，保持原有「睡 1 秒原参数重试」行为。"""
        resp = httpx.Response(500, text="server error", request=_req())

        def fake_post(url, json=None, headers=None, timeout=None):
            raise _http_err(resp)

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep") as sleep_mock:
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertIsNone(reply)
        sleep_mock.assert_called_once_with(1)
        self.assertFalse(brain._model_demoted)

    def test_chinese_message_variant_detected(self):
        """错误体用中文「无权访问」字样（无 1220 码）同样判定为未授权。"""
        resp = httpx.Response(403, text='{"error":{"message":"您无权访问该模型"}}',
                              request=_req())

        def fake_post(url, json=None, headers=None, timeout=None):
            if json["model"] == "glm-5.3":
                raise _http_err(resp)
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep"):
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertTrue(brain._model_demoted)

    # ==== 1210：新模型始终思考，不支持关闭思考（glm-5.3 实测错误体）====

    def _resp_400_1210(self):
        return httpx.Response(
            400,
            text='{"error":{"code":"1210","message":"该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"}}',
            request=_req(),
        )

    def test_1210_strips_thinking_and_retries_same_model(self):
        """400/1210 → 摘掉 thinking 重发同一模型（不换模型），并记住该模型。"""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(dict(json))
            if "thinking" in json:
                raise _http_err(self._resp_400_1210())
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep") as sleep_mock:
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertEqual([c["model"] for c in calls], ["glm-5.3", "glm-5.3"])
        self.assertIn("thinking", calls[0])
        self.assertNotIn("thinking", calls[1])
        self.assertIn("glm-5.3", brain._thinking_unsupported_models)
        sleep_mock.assert_not_called()

    def test_thinking_strip_sticks_per_model(self):
        """按模型记忆：该模型后续请求首发即不带 thinking。"""
        brain._thinking_unsupported_models.add("glm-5.3")
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(dict(json))
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post):
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("thinking", calls[0])

    def test_1210_then_403_full_chain_fallback_keeps_thinking(self):
        """全链路实测序列：5.3+thinking → 400/1210 剥参 → 403/1220 降级
        → 兜底 5.2 必须带着 thinking 原样设置（5.2 需要它关闭思考）。"""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(dict(json))
            if json["model"] == "glm-5.3" and "thinking" in json:
                raise _http_err(self._resp_400_1210())
            if json["model"] == "glm-5.3":
                raise _http_err(_resp_403_1220())
            return _resp_ok()

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep"):
            reply = brain._request_llm("http://x", self._payload(), {})
        self.assertEqual(reply, "好的先生")
        self.assertEqual([c["model"] for c in calls],
                         ["glm-5.3", "glm-5.3", brain._MODEL_FALLBACK])
        self.assertIn("thinking", calls[2])  # 兜底模型的 thinking 原样保留
        self.assertTrue(brain._model_demoted)

    def test_1210_without_thinking_param_no_loop(self):
        """已不带 thinking 仍 400/1210（异常情况）→ 不递归，走普通重试通道。"""
        def fake_post(url, json=None, headers=None, timeout=None):
            raise _http_err(self._resp_400_1210())

        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep") as sleep_mock:
            reply = brain._request_llm("http://x", self._payload(thinking=False), {})
        self.assertIsNone(reply)
        sleep_mock.assert_called_once_with(1)
        self.assertNotIn("glm-5.3", brain._thinking_unsupported_models)

    def test_glm_web_search_inherits_demotion(self):
        """P0 第 8/76 题病例：联网搜索通道曾直连 httpx 绕过降级，
        glm-5.3 未授权时搜索静默哑掉。契约：走 _request_llm，403 自动降级。"""
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(dict(json))
            if json["model"] == "glm-5.3":
                raise _http_err(_resp_403_1220())
            return _resp_ok("今天人工智能领域有三件事……")

        cfg = {"base_url": "https://open.bigmodel.cn/api/paas/v4",
               "api_key": "x", "model": "glm-5.3"}
        with mock.patch.object(brain.httpx, "post", side_effect=fake_post), \
                mock.patch.object(brain.time, "sleep"):
            reply = brain._glm_web_search("今天的人工智能新闻", cfg)
        self.assertEqual(reply, "今天人工智能领域有三件事……")
        self.assertEqual([c["model"] for c in calls], ["glm-5.3", brain._MODEL_FALLBACK])
        self.assertIn("tools", calls[1])  # 降级不丢 web_search 工具参数
        self.assertTrue(brain._model_demoted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
