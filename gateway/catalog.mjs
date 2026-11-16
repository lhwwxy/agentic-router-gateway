// new-api 模型目录自动发现：/v1/models → 按 tierOf 分三档。
// 目录为空（探测失败/未配置）时路由器照常记录决策但不切换（no-candidate）。

import { tierOf } from '../js/src/index.js'

export const PROVIDER = 'newapi'

// models: string[] → modelTiers 结构（条目形如 "newapi/<model>"）
export function buildTiers(models) {
  const tiers = { fast: [], mid: [], strong: [] }
  for (const m of models || []) {
    if (typeof m !== 'string' || !m) continue
    const t = tierOf(m)
    if (t in tiers) tiers[t].push(PROVIDER + '/' + m)
  }
  return { providers: [{ provider: PROVIDER }], tiers }
}

// 当前有候选模型的档位（不探索"幽灵档"）
export function availableTiers(modelTiers) {
  const tiers = (modelTiers && modelTiers.tiers) || {}
  const has = ['fast', 'mid', 'strong'].filter((t) => Array.isArray(tiers[t]) && tiers[t].length > 0)
  return has.length ? has : ['fast', 'mid', 'strong']
}

// 静态配置覆盖：{"fast": ["m1"], "mid": ["m2"], "strong": ["m3"]}
export function buildTiersFromStatic(staticTiers) {
  const models = {}
  for (const [tier, list] of Object.entries(staticTiers || {})) {
    for (const m of Array.isArray(list) ? list : [list]) {
      models[m] = tier
    }
  }
  // 直接按声明的档位落位（信任用户配置，不用 tierOf 猜）
  const tiers = { fast: [], mid: [], strong: [] }
  for (const [m, tier] of Object.entries(models)) {
    if (tier in tiers) tiers[tier].push(PROVIDER + '/' + m)
  }
  return { providers: [{ provider: PROVIDER }], tiers }
}

// 从 new-api 拉取可用模型清单（OpenAI 兼容 /v1/models）
export async function fetchModels(baseUrl, apiKey) {
  const res = await fetch(baseUrl.replace(/\/$/, '') + '/v1/models', {
    headers: { authorization: 'Bearer ' + apiKey },
    signal: AbortSignal.timeout(10000),
  })
  if (!res.ok) throw new Error('new-api /v1/models ' + res.status)
  const j = await res.json()
  const ids = ((j && j.data) || []).map((x) => x && x.id).filter((x) => typeof x === 'string' && x)
  if (!ids.length) throw new Error('new-api /v1/models 返回空模型列表')
  return ids
}

// 目录管理：启动即探测 + TTL 轮询刷新（provider/模型增删自愈）
export class Catalog {
  constructor({ baseUrl, apiKey, staticTiers, ttlMs = 600000, onRefresh }) {
    this.baseUrl = baseUrl
    this.apiKey = apiKey
    this.staticTiers = staticTiers
    this.ttlMs = ttlMs
    this.onRefresh = onRefresh || (() => {})
    this.tiers = staticTiers ? buildTiersFromStatic(staticTiers) : buildTiers([])
    this.ts = 0
    this.probing = false
    this.lastError = null
    this.timer = null
  }

  start() {
    this.probe()
    if (this.ttlMs > 0) {
      this.timer = setInterval(() => this.probe(), this.ttlMs)
      this.timer.unref?.()
    }
    return this
  }

  stop() {
    if (this.timer) clearInterval(this.timer)
  }

  get modelTiers() { return this.tiers }

  async probe() {
    if (this.probing || !this.baseUrl || !this.apiKey) return
    this.probing = true
    try {
      const models = await fetchModels(this.baseUrl, this.apiKey)
      this.tiers = buildTiers(models)
      this.ts = Date.now()
      this.lastError = null
      this.onRefresh(this.tiers, models)
    } catch (e) {
      // 静态目录兜底；无静态配置且从未成功过 → 空表（只记录不切换）
      this.lastError = String(e && e.message || e)
    } finally {
      this.probing = false
    }
  }
}
