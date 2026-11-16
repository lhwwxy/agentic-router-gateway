# -*- coding: utf-8 -*-
"""核心回归用例：golden 数值与 js/test/core.test.js 一一对拍。

修改任何公式时，必须同步更新 js 与 go 的同名用例。
运行：python3 -m unittest discover -s tests  （在 python/ 目录下）
"""

import math
import os
import shutil
import tempfile
import unittest

from agentic_router.classify import classify, features_of
from agentic_router.meta import DEFAULT_WEIGHTS
from agentic_router.router import Router, fresh_state
from agentic_router.strategies import (
    apply_reward, correct_reward, recommend_rule, resolve_tier, reward_of,
    tier_of,
)


class TestClassify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(classify("你好"), {"type": "other", "complexity": 0})
        c1 = classify("帮我总结这篇文章的要点")
        self.assertEqual(c1["type"], "summarize")
        self.assertEqual(c1["complexity"], 0.05)
        c2 = classify("def solve(x):\n  return x + 1\n```python")
        self.assertEqual(c2["type"], "code")
        self.assertEqual(c2["complexity"], 0.55)
        c3 = classify("请帮我重构这个模块并做性能优化")
        self.assertEqual(c3["type"], "agentic")
        self.assertEqual(c3["complexity"], 0.55)

    def test_features(self):
        text = "def solve(x):\n  return x + 1\n```python"
        f = features_of(classify(text), text)
        self.assertEqual(f, [1.0, 0.55, 38 / 4000, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0])


class TestTiers(unittest.TestCase):
    def test_tier_of(self):
        self.assertEqual(tier_of("deepseek-v4-flash"), "fast")
        self.assertEqual(tier_of("gpt-mini"), "fast")
        self.assertEqual(tier_of("gpt-4o"), "mid")
        self.assertEqual(tier_of("claude-pro"), "strong")
        self.assertEqual(tier_of(None), "unknown")

    def test_recommend_rule(self):
        self.assertEqual(recommend_rule({"complexity": 0.8})["tier"], "strong")
        self.assertEqual(recommend_rule({"complexity": 0.2})["tier"], "fast")
        self.assertEqual(recommend_rule({"complexity": 0.5})["tier"], "mid")

    def test_resolve_tier(self):
        tiers = {
            "providers": [{"provider": "a"}, {"provider": "b"}],
            "tiers": {"fast": ["a/flash"], "mid": ["b/mid"], "strong": ["a/pro"]},
        }
        self.assertEqual(resolve_tier("fast", "a", tiers),
                         {"target": "flash", "provider": "a", "used": "fast", "note": ""})
        self.assertEqual(resolve_tier("mid", "a", tiers),
                         {"target": "mid", "provider": "b", "used": "mid", "note": "cross-provider"})
        self.assertEqual(resolve_tier("mid", "a", tiers, {"crossProvider": False}),
                         {"target": "pro", "provider": "a", "used": "strong", "note": "mid->strong fallback"})
        self.assertEqual(resolve_tier("mid", "a", None)["note"], "tiers-not-ready")


class TestReward(unittest.TestCase):
    def test_golden(self):
        self.assertEqual(reward_of({}, "clean-close", 5000)[0], 1)
        self.assertEqual(reward_of({}, "failed", 5000)[0], 0)
        # 工具失败2(0.4) + 重试1(0.2) + 成本0.02(0.4) + 延迟45s(0.1) → 负 → 钳 0
        r, _ = reward_of({"toolErrors": 2, "retries": 1, "costYuan": 0.02}, "clean-close", 45000)
        self.assertEqual(r, 0)
        # 1 - 0.2(一次工具失败) - 0.04(成本) = 0.76
        r, _ = reward_of({"toolErrors": 1, "costYuan": 0.002}, "clean-close", 5000)
        self.assertEqual(r, 0.76)


class TestFlywheel(unittest.TestCase):
    def test_apply_reward(self):
        state = fresh_state()
        rec = {
            "clsType": "code", "ucbTier": "fast", "linTier": "fast",
            "f": [1.0, 0.5, 0.1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "reward": 1, "chosen": "rule", "pChosen": 1,
            "pool": ["ucb", "linucb", "knn", "rule"],
        }
        apply_reward(state, rec)
        self.assertEqual(state["ucb"]["total"], 1)
        self.assertEqual(state["ucb"]["arms"]["code:fast"], {"n": 1, "mean": 1})
        self.assertEqual(state["lin"]["tierW"]["fast"], [1.0, 0.5, 0.1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(state["lin"]["tierN"]["fast"], 1)
        self.assertEqual(state["lin"]["total"], 1)
        self.assertEqual(len(state["knn"]["pool"]), 1)
        self.assertEqual(state["knn"]["pool"][0],
                         {"f": [1.0, 0.5, 0.1, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], "tier": "fast", "r": 1})
        w1 = 2.5 * math.exp(0.075)
        self.assertAlmostEqual(state["exp3"]["weights"]["rule"], w1, places=12)

        apply_reward(state, dict(rec, pChosen=0.5))
        self.assertEqual(state["ucb"]["arms"]["code:fast"], {"n": 2, "mean": 1})
        # 二次线性更新：lr=0.5, dot=2.26, err=-1.26
        self.assertAlmostEqual(state["lin"]["tierW"]["fast"][0], 0.37, places=12)
        self.assertAlmostEqual(state["lin"]["tierW"]["fast"][1], 0.185, places=12)
        self.assertAlmostEqual(state["lin"]["tierW"]["fast"][2], 0.037, places=12)
        w2 = w1 * math.exp(0.15)
        self.assertAlmostEqual(state["exp3"]["weights"]["rule"], w2, places=12)

    def test_correct_reward(self):
        state = fresh_state()
        rec = {
            "clsType": "code", "ucbTier": "fast", "linTier": "fast",
            "f": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "reward": 1, "chosen": "rule", "pChosen": 1,
            "pool": ["ucb", "linucb", "knn", "rule"],
        }
        apply_reward(state, rec)
        correct_reward(state, rec, 0)  # 👎：1 → 0
        self.assertEqual(state["ucb"]["arms"]["code:fast"]["mean"], 0)
        self.assertEqual(state["ucb"]["arms"]["code:fast"]["n"], 1)
        # lin 回退：w -= lr·delta·f = f - 1·1·f = 0
        self.assertEqual(state["lin"]["tierW"]["fast"], [0.0] * 9)
        # k-NN 追加修正经验
        self.assertEqual(len(state["knn"]["pool"]), 2)
        self.assertEqual(state["knn"]["pool"][1]["r"], 0)


class TestRouter(unittest.TestCase):
    def test_decide_settle_roundtrip(self):
        home = tempfile.mkdtemp(prefix="agentic-router-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        router = Router(home=home, mode="active", model_tiers={
            "providers": [{"provider": "p1"}],
            "tiers": {"fast": ["p1/lite"], "mid": ["p1/base"], "strong": ["p1/pro"]},
        })
        d = router.decide("帮我总结这篇文章的要点", "p1")
        self.assertEqual(d["id"], 1)
        self.assertEqual(d["type"], "summarize")
        self.assertFalse(d["settled"])
        self.assertIn(d["tier"], ["fast", "mid", "strong"])
        self.assertIn(d["model"], ["lite", "base", "pro"])

        r = router.settle(1, success=True, dt_ms=5000)
        self.assertEqual(r["reward"], 1)
        self.assertEqual(router.state["ucb"]["total"], 1)
        self.assertIsNone(router.settle(1, success=True))  # 已结算不可重复

        # 状态持久化：重建 Router 恢复飞轮
        router2 = Router(home=home, mode="active")
        self.assertEqual(router2.state["ucb"]["total"], 1)
        self.assertGreaterEqual(len(router2.state["decisions"]), 1)


class TestLiteLLMHook(unittest.TestCase):
    """无 litellm 依赖也能验证 hook 的决策/结算编排。"""

    def _hook(self):
        from agentic_router.litellm_hook import AgenticRouterHook
        home = tempfile.mkdtemp(prefix="agentic-router-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return AgenticRouterHook(mode="active", tier_models={
            "fast": "flash-model", "mid": "base-model", "strong": "pro-model",
        }, home=home)

    def test_extract_text(self):
        from agentic_router.litellm_hook import _last_user_text
        msgs = [
            {"role": "system", "content": "you are a helpful assistant"},
            {"role": "user", "content": "帮我总结这篇文章的要点"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": [{"type": "text", "text": "再简短一点"}]},
        ]
        self.assertEqual(_last_user_text(msgs), "再简短一点")
        self.assertEqual(_last_user_text([]), "")

    def test_pre_call_rewrites_model_in_active_mode(self):
        import asyncio
        hook = self._hook()
        data = {"model": "base-model", "messages": [
            {"role": "user", "content": "帮我总结这篇文章的要点"}],
            "litellm_call_id": "call-1"}

        async def run():
            return await hook.async_pre_call_hook(None, None, data, "acompletion")
        asyncio.run(run())

        # 决策已挂起；active 模式下模型被改写到三档之一
        self.assertIn(data["model"], ["flash-model", "base-model", "pro-model"])
        self.assertEqual(hook.stats()["pending"], 1)
        self.assertEqual(hook.stats()["mode"], "active")


if __name__ == "__main__":
    unittest.main()
