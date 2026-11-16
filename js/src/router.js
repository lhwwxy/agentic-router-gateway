// 参考实现：决策 → 落盘 → 奖励结算的编排（框架无关）。
// dsh 宿主里这套编排走 dsh 的 waterfall/会话事件；在网关形态（LiteLLM / Higress）
// 里由各适配层把"请求进入 / 响应结束 / 失败重试"翻译成 decide/settle 调用。
// Python 与 Go 移植必须与本实现行为一致（见 ../spec/ROUTING_SPEC.md 与对拍测试）。

import { classify, featuresOf } from './classify.js'
import {
  recommendRule, recommendUcb, recommendLin, recommendKnn,
  rewardOf, applyReward, correctReward, resolveTier,
} from './strategies.js'
import { metaPick, DEFAULT_WEIGHTS } from './meta.js'
import { Store } from './store.js'

export const LIN_GRADUATION = 50   // LinUCB 毕业样本线
export const KNN_GRADUATION = 30   // k-NN 毕业经验线
const DECISION_WINDOW = 200        // 内存中保留的最近决策数（用于 settle 匹配）

export function emptyTiers() {
  return { providers: [], tiers: { fast: [], mid: [], strong: [] } }
}

export function freshState() {
  return {
    mode: 'shadow', // shadow: 只记录不干预；active: 真实改写模型
    seq: 0,
    decisions: [],
    ucb: { total: 0, arms: {} },
    lin: {
      total: 0,
      tierN: { fast: 0, mid: 0, strong: 0 },
      tierW: { fast: new Array(9).fill(0), mid: new Array(9).fill(0), strong: new Array(9).fill(0) },
    },
    knn: { pool: [] },
    exp3: { total: 0, linGraduated: false, knnGraduated: false, weights: { ...DEFAULT_WEIGHTS } },
  }
}

function rebuildFeatures(decision) {
  const f = [1, decision.complexity, Math.min(decision.len || 0, 4000) / 4000]
  const types = ['code', 'write', 'summarize', 'research', 'agentic', 'qa']
  for (const ty of types) f.push(decision.type === ty ? 1 : 0)
  return f
}

export class Router {
  constructor(opts = {}) {
    this.store = opts.store || new Store(opts.home)
    this.crossProvider = opts.crossProvider !== false
    this.modelTiers = opts.modelTiers || emptyTiers()
    this.prices = opts.prices
    this.state = this.store.loadState() || freshState()
    if (opts.mode === 'active' || opts.mode === 'shadow') this.state.mode = opts.mode
  }

  get mode() { return this.state.mode }

  setMode(mode) {
    if (mode !== 'active' && mode !== 'shadow') throw new Error('mode must be "active" or "shadow"')
    this.state.mode = mode
    this.#persist()
  }

  // 输入本轮用户文本，返回路由决策（含目标模型）。provider 可为空（无会话上下文时）。
  decide(text, provider, availableTiers) {
    const state = this.state
    const cls = classify(text)
    const f = featuresOf(cls, text)
    const tiers = Array.isArray(availableTiers) && availableTiers.length
      ? availableTiers
      : ['fast', 'mid', 'strong']

    const recs = {
      rule: recommendRule(cls),
      ucb: recommendUcb(cls, state.ucb, tiers),
      lin: recommendLin(cls, f, state.lin, tiers),
      knn: recommendKnn(cls, f, state.knn, tiers),
    }

    state.exp3.linGraduated = state.lin.total >= LIN_GRADUATION
    state.exp3.knnGraduated = state.knn.pool.length >= KNN_GRADUATION
    const meta = metaPick(recs, state.exp3)
    const chosenRec = meta.byName[meta.chosen] || recs.rule
    // k-NN 冷启动可能给出 null 档位：回退规则表
    const tier = chosenRec.tier || recs.rule.tier
    const resolved = resolveTier(tier, provider, this.modelTiers, { crossProvider: this.crossProvider })

    const decision = {
      id: ++state.seq,
      ts: Date.now(),
      type: cls.type,
      complexity: cls.complexity,
      len: (text || '').length,
      mode: state.mode,
      chosen: meta.chosen,
      pChosen: meta.ps[meta.chosen] || 0,
      pool: meta.pool,
      tier,
      used: resolved.used,
      provider: resolved.provider,
      model: resolved.target,
      note: resolved.note,
      reason: (chosenRec && chosenRec.reason) || '',
      settled: false,
    }
    state.decisions.push(decision)
    if (state.decisions.length > DECISION_WINDOW) state.decisions.shift()
    this.#persist()
    this.store.appendJsonl('decisions.jsonl', decision)
    return decision
  }

  // 结果回传：{ success, dtMs, costYuan, toolErrors, retries } → 计算奖励并喂飞轮
  settle(decisionId, outcome = {}) {
    const state = this.state
    const d = state.decisions.find((x) => x.id === decisionId && !x.settled)
    if (!d) return null
    d.settled = true

    const tag = outcome.success === false ? 'failed' : 'clean-close'
    const { reward, breakdown } = rewardOf({
      toolErrors: outcome.toolErrors || 0,
      retries: outcome.retries || 0,
      costYuan: outcome.costYuan || 0,
    }, tag, outcome.dtMs)

    const rec = {
      clsType: d.type,
      ucbTier: d.used,
      linTier: d.used,
      f: rebuildFeatures(d),
      reward,
      chosen: d.chosen,
      pChosen: d.pChosen,
      pool: d.pool,
    }
    applyReward(state, rec)
    d.reward = reward
    d.linLr = rec.linLr
    this.#persist()
    this.store.appendJsonl('rewards.jsonl', { decisionId: d.id, ts: Date.now(), reward, breakdown })
    return { reward, breakdown, tier: d.used }
  }

  // 人类反馈修正（👍/👎）：把某条已结算决策的奖励改写并修正各专家
  correct(decisionId, newReward) {
    const state = this.state
    const d = state.decisions.find((x) => x.id === decisionId && x.settled)
    if (!d) return null
    const rec = {
      clsType: d.type,
      ucbTier: d.used,
      linTier: d.used,
      f: rebuildFeatures(d),
      reward: d.reward,
      chosen: d.chosen,
      pChosen: d.pChosen,
      pool: d.pool,
      linLr: d.linLr,
    }
    const result = correctReward(state, rec, newReward)
    d.reward = newReward
    this.#persist()
    return result
  }

  #persist() {
    this.store.saveState(this.state)
  }
}
