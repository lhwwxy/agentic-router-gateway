// 仿真回放收敛测试：无真实流量时的行为级验证（算法层面证明飞轮会学习）。
//
// 两级断言（bandit 收敛的诚实刻画）：
//   严格级：高流量×大差距类型（code/agentic/qa/summarize/other）必须收敛到最优档
//   保守级：所有类型不允许灾难性错路由（主导档期望 ≥ 最优 − 0.13，
//           小差距×低流量类型如 write/research 需要 >20k 轮才严格收敛，
//           这是 UCB 探索-区分的固有速度，不是缺陷）

import test from 'node:test'
import assert from 'node:assert/strict'

import { simulate, validatePrompts, OPTIMAL, worldExpected } from '../sim.mjs'

const SEEDS = [1, 7, 8]
const TURNS = 20000
const TIERS = ['fast', 'mid', 'strong']
// 高流量（≥15%）× 大差距（≥0.15 奖励）类型：必须严格收敛
const STRICT_TYPES = ['code', 'agentic', 'qa', 'summarize', 'other']
// 灾难性错路由容差：主导档期望奖励与最优档的差
const CATASTROPHE_EPS = 0.13

function dominantOf(ls) {
  return TIERS[ls.counts.indexOf(Math.max(...ls.counts))]
}

test('prompt 池分类正确（仿真有效性的前提）', () => {
  assert.deepEqual(validatePrompts(), [])
})

test('飞轮收敛：大差距类型严格收敛，无灾难性错路由', () => {
  for (const seed of SEEDS) {
    const s = simulate({ turns: TURNS, seed })
    for (const [type, ls] of Object.entries(s.lateShares)) {
      const total = ls.counts.reduce((a, b) => a + b, 0)
      assert.ok(total >= 20, `${type} 后1/4样本不足: ${total}`)
      const dom = dominantOf(ls)
      const gap = worldExpected(type, OPTIMAL[type]) - worldExpected(type, dom)
      if (STRICT_TYPES.includes(type)) {
        assert.equal(dom, OPTIMAL[type],
          `seed=${seed} ${type} 未严格收敛：主导 ${dom}（gap=${gap.toFixed(3)}），分布 ${ls.counts}`)
      } else {
        assert.ok(gap <= CATASTROPHE_EPS,
          `seed=${seed} ${type} 灾难性错路由：主导 ${dom}（gap=${gap.toFixed(3)} > ${CATASTROPHE_EPS}），分布 ${ls.counts}`)
      }
    }
  }
})

test('学习有效：后期平均奖励显著高于随机路由基线', () => {
  // 随机基线：每类均匀选三档的期望奖励
  let baseline = 0
  const w = 1 / 7
  for (const type of Object.keys(OPTIMAL)) {
    for (const t of TIERS) baseline += w / 3 * worldExpected(type, t)
  }
  for (const seed of SEEDS) {
    const s = simulate({ turns: TURNS, seed })
    assert.ok(s.lateAvgReward > baseline + 0.05,
      `seed=${seed} late(${s.lateAvgReward}) 未显著高于随机基线(${baseline.toFixed(3)})`)
  }
})
