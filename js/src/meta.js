// EXP3 元选择器：把 {UCB, LinUCB, k-NN, rule} 当臂，按权重软最大采样，
// 被选策略按重要性加权更新（reward / p_chosen）。
//
// v0.2：加入标准 EXP3 的 γ 混合（混合率 GAMMA）——采样分布为
//   p_n = (1−γ)·w_n/Σw + γ/K
// 保证每个池内专家至少 γ/K 的采样概率。没有它，早期复合增长的专家权重
// 会指数爆炸（仿真复现过 2e141 量级），其余专家概率永久归零、无法恢复，
// 元选择器锁死在先发优势者上（哪怕它后来学歪了）。
// 重要性加权配合 γ 混合后：被冷落的专家一旦被采到且给出高奖励，
// w *= exp(0.3·(r/p)/|pool|) 中的小 p 会带来巨大回血，博弈恢复均衡。

const DEFAULT_WEIGHTS = { ucb: 1, linucb: 1, knn: 1, rule: 2.5 }

export const GAMMA = 0.05 // γ 混合率：每位专家保底 1.25% 采样概率

export function metaPick(recs, exp3) {
  const weights = exp3.weights
  const pool = ['ucb']
  const byName = { ucb: recs.ucb, rule: recs.rule }
  if (exp3.linGraduated && recs.lin && recs.lin.tier) {
    pool.push('linucb')
    byName.linucb = recs.lin
  }
  if (exp3.knnGraduated && recs.knn && recs.knn.tier) {
    pool.push('knn')
    byName.knn = recs.knn
  }
  pool.push('rule')

  let total = 0
  for (const n of pool) total += weights[n] || 0
  const k = pool.length
  const gamma = exp3.gamma != null ? exp3.gamma : GAMMA
  const ps = {}
  for (const n of pool) {
    const raw = total > 0 ? (weights[n] || 0) / total : 1 / k
    ps[n] = (1 - gamma) * raw + gamma / k
  }
  let r = Math.random()
  let chosen = 'rule'
  for (const n of pool) {
    if (r < ps[n]) { chosen = n; break }
    r -= ps[n]
  }
  return { chosen, ps, pool, byName }
}

export { DEFAULT_WEIGHTS }
