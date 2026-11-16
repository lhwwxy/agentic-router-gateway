// 核心回归用例：这些数值同时是 Python / Go 移植的"对拍基准"（golden numbers）。
// 修改任何公式时，必须同步更新 python/tests 与 go/core 的同名用例。

import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { classify, featuresOf } from '../src/classify.js'
import {
  recommendRule, rewardOf, applyReward, correctReward, resolveTier, tierOf,
} from '../src/strategies.js'
import { Router, freshState } from '../src/router.js'

test('classify: 基础分类（golden）', () => {
  assert.deepEqual(classify('你好'), { type: 'other', complexity: 0 })
  const c1 = classify('帮我总结这篇文章的要点')
  assert.equal(c1.type, 'summarize')
  assert.equal(c1.complexity, 0.05)
  const c2 = classify('def solve(x):\n  return x + 1\n```python')
  assert.equal(c2.type, 'code')
  assert.equal(c2.complexity, 0.55)
  const c3 = classify('请帮我重构这个模块并做性能优化')
  assert.equal(c3.type, 'agentic')
  assert.equal(c3.complexity, 0.55)
})

test('classify: 特征向量（golden）', () => {
  const text = 'def solve(x):\n  return x + 1\n```python'
  const f = featuresOf(classify(text), text)
  assert.deepEqual(f, [1, 0.55, 38 / 4000, 1, 0, 0, 0, 0, 0])
})

test('tierOf: 模型档位识别', () => {
  assert.equal(tierOf('deepseek-v4-flash'), 'fast')
  assert.equal(tierOf('gpt-mini'), 'fast')
  assert.equal(tierOf('gpt-4o'), 'mid')
  assert.equal(tierOf('claude-pro'), 'strong')
  assert.equal(tierOf(null), 'unknown')
})

test('recommendRule: 阈值表', () => {
  assert.equal(recommendRule({ complexity: 0.8 }).tier, 'strong')
  assert.equal(recommendRule({ complexity: 0.2 }).tier, 'fast')
  assert.equal(recommendRule({ complexity: 0.5 }).tier, 'mid')
})

test('resolveTier: 同 provider / 跨 provider / mid 回退（golden）', () => {
  const tiers = {
    providers: [{ provider: 'a' }, { provider: 'b' }],
    tiers: { fast: ['a/flash'], mid: ['b/mid'], strong: ['a/pro'] },
  }
  assert.deepEqual(resolveTier('fast', 'a', tiers), { target: 'flash', provider: 'a', used: 'fast', note: '' })
  assert.deepEqual(resolveTier('mid', 'a', tiers), { target: 'mid', provider: 'b', used: 'mid', note: 'cross-provider' })
  assert.deepEqual(resolveTier('mid', 'a', tiers, { crossProvider: false }), { target: 'pro', provider: 'a', used: 'strong', note: 'mid->strong fallback' })
  assert.equal(resolveTier('mid', 'a', null).note, 'tiers-not-ready')
})

test('rewardOf: 质量代理奖励（golden）', () => {
  assert.equal(rewardOf({}, 'clean-close', 5000).reward, 1)
  assert.equal(rewardOf({}, 'failed', 5000).reward, 0)
  // 工具失败2(0.4) + 重试1(0.2) + 成本0.02元(0.4) + 延迟45s(0.1) → 负 → 钳 0
  assert.equal(rewardOf({ toolErrors: 2, retries: 1, costYuan: 0.02 }, 'clean-close', 45000).reward, 0)
  // 1 - 0.2(一次工具失败) - 0.04(成本) = 0.76
  assert.equal(rewardOf({ toolErrors: 1, costYuan: 0.002 }, 'clean-close', 5000).reward, 0.76)
})

test('applyReward: 飞轮状态转移（golden）', () => {
  const state = freshState()
  const rec = {
    clsType: 'code', ucbTier: 'fast', linTier: 'fast',
    f: [1, 0.5, 0.1, 1, 0, 0, 0, 0, 0],
    reward: 1, chosen: 'rule', pChosen: 1, pool: ['ucb', 'linucb', 'knn', 'rule'],
  }
  applyReward(state, rec)
  assert.equal(state.ucb.total, 1)
  assert.deepEqual(state.ucb.arms['code:fast'], { n: 1, mean: 1 })
  assert.deepEqual(state.lin.tierW.fast, [1, 0.5, 0.1, 1, 0, 0, 0, 0, 0])
  assert.equal(state.lin.tierN.fast, 1)
  assert.equal(state.lin.total, 1)
  assert.equal(state.knn.pool.length, 1)
  assert.deepEqual(state.knn.pool[0], { f: [1, 0.5, 0.1, 1, 0, 0, 0, 0, 0], tier: 'fast', r: 1 })
  const w1 = 2.5 * Math.exp(0.075)
  assert.ok(Math.abs(state.exp3.weights.rule - w1) < 1e-12, 'exp3 rule weight after 1st apply')

  applyReward(state, { ...rec, pChosen: 0.5 })
  assert.deepEqual(state.ucb.arms['code:fast'], { n: 2, mean: 1 })
  // 二次线性更新：lr=0.5, dot=2.26, err=-1.26
  assert.ok(Math.abs(state.lin.tierW.fast[0] - 0.37) < 1e-12)
  assert.ok(Math.abs(state.lin.tierW.fast[1] - 0.185) < 1e-12)
  assert.ok(Math.abs(state.lin.tierW.fast[2] - 0.037) < 1e-12)
  const w2 = w1 * Math.exp(0.15)
  assert.ok(Math.abs(state.exp3.weights.rule - w2) < 1e-12)
})

test('correctReward: 反馈修正（golden）', () => {
  const state = freshState()
  const rec = {
    clsType: 'code', ucbTier: 'fast', linTier: 'fast',
    f: [1, 0, 0, 1, 0, 0, 0, 0, 0],
    reward: 1, chosen: 'rule', pChosen: 1, pool: ['ucb', 'linucb', 'knn', 'rule'],
  }
  applyReward(state, rec)
  correctReward(state, rec, 0) // 👎：1 → 0
  assert.equal(state.ucb.arms['code:fast'].mean, 0) // n 不变，均值修正
  assert.equal(state.ucb.arms['code:fast'].n, 1)
  // lin 回退：w -= lr·delta·f = f - 1·1·f = 0
  assert.deepEqual(state.lin.tierW.fast, [0, 0, 0, 0, 0, 0, 0, 0, 0])
  // k-NN 追加修正经验
  assert.equal(state.knn.pool.length, 2)
  assert.equal(state.knn.pool[1].r, 0)
})

test('Router: decide → settle 全链路', () => {
  const home = mkdtempSync(join(tmpdir(), 'agentic-router-'))
  try {
    const router = new Router({
      home,
      mode: 'active',
      modelTiers: {
        providers: [{ provider: 'p1' }],
        tiers: { fast: ['p1/lite'], mid: ['p1/base'], strong: ['p1/pro'] },
      },
    })
    const d = router.decide('帮我总结这篇文章的要点', 'p1')
    assert.equal(d.id, 1)
    assert.equal(d.type, 'summarize')
    assert.equal(d.settled, false)
    assert.ok(['fast', 'mid', 'strong'].includes(d.tier))
    assert.ok(['lite', 'base', 'pro'].includes(d.model))

    const r = router.settle(1, { success: true, dtMs: 5000 })
    assert.equal(r.reward, 1)
    assert.equal(router.state.ucb.total, 1)
    assert.equal(router.settle(1, { success: true }), null) // 已结算不可重复

    // 状态持久化：重建 Router 恢复飞轮
    const router2 = new Router({ home, mode: 'active' })
    assert.equal(router2.state.ucb.total, 1)
    assert.ok(router2.state.decisions.length >= 1)
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})
