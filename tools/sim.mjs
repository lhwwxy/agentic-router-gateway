// 仿真回放器：无真实流量时验证飞轮收敛。
//
// 原理：构造一个"模拟世界"——每个 任务类型×档位 有预设的
// 成功率/延迟/成本（即该档位在该类任务上的"真实"表现），
// 生成合成请求打到 Router，奖励按真实公式 rewardOf 计算。
// 跑 N 轮后检查：各类型是否收敛到模拟世界的最优档位、
// 平均奖励是否随使用提升、EXP3 权重是否向更准的专家倾斜。
//
// 用法：
//   node tools/sim.mjs                 # 默认 2000 轮
//   node tools/sim.mjs --turns 5000
//   SEED=42 node tools/sim.mjs         # 固定随机种子复现

import { Router, classify, rewardOf } from '../js/src/index.js'

// ---- 模拟世界：各类型×档位的真实表现（成功率 p / 延迟 ms / 成本元） ----
// 预设的最优档：code/agentic→strong, research/write→mid, summarize/qa/other→fast
export const WORLD = {
  code: {
    fast:   { p: 0.45, dt: 2500,  cost: 0.0003 },
    mid:    { p: 0.60, dt: 9000,  cost: 0.003 },
    strong: { p: 0.95, dt: 32000, cost: 0.004 },
  },
  agentic: {
    fast:   { p: 0.50, dt: 3000,  cost: 0.0004 },
    mid:    { p: 0.62, dt: 10000, cost: 0.003 },
    strong: { p: 0.93, dt: 32000, cost: 0.004 },
  },
  research: {
    fast:   { p: 0.55, dt: 2500,  cost: 0.0003 },
    mid:    { p: 0.80, dt: 8000,  cost: 0.002 },
    strong: { p: 0.85, dt: 32000, cost: 0.004 },
  },
  write: {
    fast:   { p: 0.70, dt: 3000,  cost: 0.0003 },
    mid:    { p: 0.85, dt: 9000,  cost: 0.002 },
    strong: { p: 0.80, dt: 32000, cost: 0.004 },
  },
  summarize: {
    fast:   { p: 0.97, dt: 1500,  cost: 0.0002 },
    mid:    { p: 0.90, dt: 8000,  cost: 0.002 },
    strong: { p: 0.85, dt: 32000, cost: 0.004 },
  },
  qa: {
    fast:   { p: 0.97, dt: 1500,  cost: 0.0002 },
    mid:    { p: 0.88, dt: 8000,  cost: 0.002 },
    strong: { p: 0.80, dt: 32000, cost: 0.004 },
  },
  other: {
    fast:   { p: 0.95, dt: 1500,  cost: 0.0002 },
    mid:    { p: 0.85, dt: 8000,  cost: 0.002 },
    strong: { p: 0.70, dt: 32000, cost: 0.004 },
  },
}

// 模拟世界各类型的最优档（由 rewardOf 公式对上表求期望得出）
export const OPTIMAL = {
  code: 'strong', agentic: 'strong', research: 'mid', write: 'mid',
  summarize: 'fast', qa: 'fast', other: 'fast',
}

// 模拟世界期望奖励：E[r] = p · rewardOf(干净收尾, dt, cost)（失败记 0）
export function worldExpected(type, tier) {
  const w = WORLD[type][tier]
  const { reward } = rewardOf({ costYuan: w.cost }, 'clean-close', w.dt)
  return w.p * reward
}

// 近似最优判定容差：主导档与最优档的期望奖励差 < 0.06 视为合格收敛
export const NEAR_EPS = 0.06

export function nearOptimal(type, tier) {
  return worldExpected(type, tier) >= worldExpected(type, OPTIMAL[type]) - NEAR_EPS
}

// ---- 合成 prompt 池：每条都必须被真实 classify() 分到目标类型 ----
export const PROMPTS = {
  code: [
    '帮我修复这个报错：TypeError: cannot read property of undefined，相关函数是 parseConfig',
    '重构这段代码并优化性能，涉及多个文件：\n```python\ndef solve(x):\n  return x + 1\n```',
    '这个脚本报错了 exception: KeyError，帮我排查一下接口调用',
    'review 一段 Python 函数实现，指出类型问题并重构',
    '调试这个 bug：接口返回 500，traceback 显示 exception in handler',
    '帮我写一个 function 处理 CSV 导入，要求支持报错重试',
  ],
  agentic: [
    '帮我构建一个完整的数据管道并部署上线',
    '请帮我做一个浏览器自动化任务，实现批量操作',
    '帮我搭建一套 CI 流水线，实现自动化构建部署',
    '请实现一个爬虫系统，生成统计报表并定时跑',
    '帮我做一个 Chrome 插件，实现页面内容提取',
    '请帮我构建一套监控告警系统并部署到服务器',
  ],
  research: [
    '查一下最新发布的论文资料，关于 agent 路由',
    '搜索 vector database 的最新对比资料',
    '调研一下几家云厂商的 GPU 价格最新资料',
    '帮我检索 MCP 协议相关的技术文档',
  ],
  write: [
    '帮我起草一封商务合作邮件，语气正式',
    '写一篇关于 AI 网关的 blog 文章开头',
    '润色这段产品文案，让它更有吸引力',
    '翻译这段技术文档成英文',
    '写一份季度技术报告的框架',
  ],
  summarize: [
    '帮我总结这篇文章的要点',
    '概括一下这段会议记录，列出摘要',
    'summarize the key points of this document',
    '把这份调研材料总结成三句话',
  ],
  qa: [
    '什么是 MCP 协议？',
    '为什么调用会返回 401？',
    'LinUCB 和 UCB 有什么区别？如何选择？',
    '怎么在 macOS 上配置 launchd 自启？',
    '能否解释一下 EXP3 算法的原理？',
  ],
  other: [
    '你好',
    '谢谢，测试一下连通性',
    'hello world',
    '短句测试',
  ],
}

// 流量分布（模拟真实使用：问答/闲聊占比高）
export const TRAFFIC_MIX = [
  ['qa', 0.22], ['other', 0.15], ['summarize', 0.14], ['code', 0.20],
  ['agentic', 0.15], ['research', 0.07], ['write', 0.07],
]

// 可复现的 xorshift RNG
export function makeRng(seed = Date.now() % 2147483647) {
  let s = seed || 1
  return () => {
    s ^= s << 13; s ^= s >>> 17; s ^= s << 5
    s &= 0x7fffffff
    return (s - 1) / 2147483646
  }
}

// 校验 prompt 池分类正确性（仿真有效性的前提）
export function validatePrompts() {
  const wrong = []
  for (const [type, prompts] of Object.entries(PROMPTS)) {
    for (const p of prompts) {
      const c = classify(p)
      if (c.type !== type) wrong.push({ type, got: c.type, text: p.slice(0, 30) })
    }
  }
  return wrong
}

const TIERS = ['fast', 'mid', 'strong']

// 内存态存储：仿真不落盘（快 + 不污染真实目录）
export class NoopStore {
  loadState() { return null }
  saveState() {}
  appendJsonl() {}
  loadJsonl() { return [] }
}

// 跑仿真。返回逐轮记录 + 汇总。
// 默认使用内存态（不落盘）：仿真快、且不污染真实 AGENTIC_ROUTER_HOME。
export function simulate({ turns = 2000, seed = 1, router, exploration } = {}) {
  const rng = makeRng(seed)
  // Router.decide 内部 metaPick 用全局 Math.random——仿真期间替换为种子 RNG
  const origRandom = Math.random
  Math.random = rng

  const r = router || new Router({ mode: 'active', store: new NoopStore(), exploration, modelTiers: {
    providers: [{ provider: 'sim' }],
    tiers: { fast: ['sim/fast-m'], mid: ['sim/mid-m'], strong: ['sim/strong-m'] },
  } })

  const history = []
  try {
    const pickType = () => {
      let x = rng(), acc = 0
      for (const [t, w] of TRAFFIC_MIX) { acc += w; if (x < acc) return t }
      return 'qa'
    }
    for (let i = 0; i < turns; i++) {
      const type = pickType()
      const pool = PROMPTS[type]
      const text = pool[Math.floor(rng() * pool.length)]
      const d = r.decide(text, 'sim', TIERS)
      // 模拟世界按"实际路由档位"给出结果
      const w = WORLD[type][d.used]
      const success = rng() < w.p
      const dtMs = Math.round(w.dt * (0.7 + 0.6 * rng()))
      const costYuan = w.cost * (0.8 + 0.4 * rng())
      const res = r.settle(d.id, { success, dtMs, costYuan })
      history.push({ turn: i + 1, type, tier: d.used, expert: d.chosen, reward: res ? res.reward : 0 })
    }
  } finally {
    Math.random = origRandom
  }
  return summarize(history, r)
}

export function summarize(history, router) {
  const byType = {}
  const lastQ = history.slice(Math.floor(history.length * 3 / 4))
  const firstQ = history.slice(0, Math.max(1, Math.floor(history.length / 4)))
  for (const h of history) {
    byType[h.type] = byType[h.type] || { total: 0, tiers: { fast: 0, mid: 0, strong: 0 }, rewardSum: 0 }
    byType[h.type].total++
    byType[h.type].tiers[h.tier]++
    byType[h.type].rewardSum += h.reward
  }
  const lateShares = {}
  for (const [type, agg] of Object.entries(byType)) {
    const late = lastQ.filter((h) => h.type === type)
    if (!late.length) continue
    lateShares[type] = {
      counts: TIERS.map((t) => late.filter((h) => h.tier === t).length),
      optimal: OPTIMAL[type],
    }
  }
  const avg = (arr) => arr.reduce((a, b) => a + b.reward, 0) / Math.max(1, arr.length)
  return {
    history, byType, lateShares, router,
    earlyAvgReward: Math.round(avg(firstQ) * 1000) / 1000,
    lateAvgReward: Math.round(avg(lastQ) * 1000) / 1000,
    exp3Weights: router.state.exp3.weights,
  }
}

// ---- CLI ----
function printReport(s) {
  const pct = (n, d) => (100 * n / Math.max(1, d)).toFixed(0).padStart(3) + '%'
  console.log(`\n=== 仿真回放报告（${s.history.length} 轮）===`)
  console.log(`平均奖励: 前1/4 ${s.earlyAvgReward} → 后1/4 ${s.lateAvgReward}（学习增益 ${s.lateAvgReward - s.earlyAvgReward >= 0 ? '+' : ''}${(s.lateAvgReward - s.earlyAvgReward).toFixed(3)}）\n`)
  console.log('各类型收敛情况（后1/4流量的档位分布 vs 模拟世界最优档）:')
  console.log('  类型        fast  mid strong  最优档  收敛?')
  for (const [type, ls] of Object.entries(s.lateShares)) {
    const [f, m, st] = ls.counts
    const total = f + m + st
    const best = Math.max(f, m, st)
    const bestTier = ['fast', 'mid', 'strong'][[f, m, st].indexOf(best)]
    const ok = bestTier === ls.optimal
    const near = nearOptimal(type, bestTier)
    const mark = ok ? '✓' : (near ? '≈ (' + Math.round(worldExpected(type, bestTier) * 100) + ')' : '✗ → ' + bestTier)
    console.log(`  ${type.padEnd(10)} ${pct(f, total)} ${pct(m, total)} ${pct(st, total)}   ${ls.optimal.padEnd(5)}  ${mark}`)
  }
  console.log(`\nEXP3 专家权重: ${JSON.stringify(Object.fromEntries(Object.entries(s.exp3Weights).map(([k, v]) => [k, Math.round(v * 100) / 100])))}`)
  console.log(`UCB 臂数: ${Object.keys(s.router.state.ucb.arms).length}  Lin样本: ${s.router.state.lin.total}  kNN经验: ${s.router.state.knn.pool.length}`)
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const turns = Number((process.argv.find((a) => a.startsWith('--turns=')) || '').split('=')[1] || 2000)
  const seed = Number(process.env.SEED || 1)
  const wrong = validatePrompts()
  if (wrong.length) {
    console.error('prompt 池分类错误（仿真无效）:', wrong)
    process.exit(1)
  }
  printReport(simulate({ turns, seed }))
}
