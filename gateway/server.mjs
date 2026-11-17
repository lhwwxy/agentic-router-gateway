// agentic-router-gateway —— new-api 前置网关（OpenAI 兼容，零依赖 Node ≥18）
//
//   客户端 ──► 本网关(:8787) ──改写 model──► new-api ──► 各渠道
//
// 请求路径：分类 + 四专家决策 → active 模式改写 body.model → 转发 new-api
// 响应路径：流式透传（顺扫 usage）→ 状态/延迟/成本 → 奖励结算喂飞轮
//
// 启动：
//   NEWAPI_BASE_URL=http://127.0.0.1:3000 \
//   NEWAPI_API_KEY=sk-xxx \
//   MODE=shadow node gateway/server.mjs
// 环境变量与运维细节见 gateway/README.md。

import http from 'node:http'
import { Router, emptyTiers, turnCostYuan } from '../js/src/index.js'
import { Catalog, availableTiers, PROVIDER } from './catalog.mjs'

export function configFromEnv(env = process.env) {
  return {
    port: Number(env.PORT || 8787),
    host: env.HOST || '127.0.0.1',
    mode: env.MODE === 'active' ? 'active' : 'shadow',
    newapiBaseUrl: (env.NEWAPI_BASE_URL || '').replace(/\/$/, ''),
    newapiKey: env.NEWAPI_API_KEY || '',
    tiers: env.TIERS ? JSON.parse(env.TIERS) : null,
    prices: env.PRICES ? JSON.parse(env.PRICES) : null,
    crossProvider: env.CROSS_PROVIDER !== 'false',
    home: env.AGENTIC_ROUTER_HOME,
    ttlMs: Number(env.TIERS_TTL_MS || 600000),
    exploration: env.EXPLORATION !== undefined ? Number(env.EXPLORATION) : undefined,
  }
}

// 提取最后一条非空 user 消息（分类信号）
export function lastUserText(messages) {
  if (!Array.isArray(messages)) return ''
  let last = ''
  for (const m of messages) {
    if (!m || m.role !== 'user') continue
    const c = m.content
    if (typeof c === 'string' && c.trim()) last = c
    else if (Array.isArray(c)) {
      const joined = c.filter((p) => p && p.type === 'text' && typeof p.text === 'string')
        .map((p) => p.text).join('\n')
      if (joined.trim()) last = joined
    }
  }
  return last
}

// OpenAI usage → 核心的 usage 结构
export function toUsage(usage) {
  if (!usage || typeof usage !== 'object') return null
  const details = usage.prompt_tokens_details || {}
  const cDetails = usage.completion_tokens_details || {}
  return {
    inputTokens: usage.prompt_tokens || 0,
    outputTokens: usage.completion_tokens || 0,
    cacheReadTokens: details.cached_tokens || 0,
    reasoningTokens: cDetails.reasoning_tokens || 0,
  }
}

// 从单条 SSE data 行解析 usage（payload 为完整 JSON）
export function usageFromDataLine(line) {
  const l = line.trim()
  if (!l.startsWith('data:')) return null
  const payload = l.slice(5).trim()
  if (!payload || payload === '[DONE]') return null
  try {
    const j = JSON.parse(payload)
    if (j && typeof j === 'object' && j.usage) return j.usage
  } catch { /* 忽略非 JSON 行 */ }
  return null
}

// 从 SSE 分片文本里找 usage（每行需完整；跨分片拆行由调用方缓冲拼接）
export function usageFromSseChunk(chunkText) {
  for (const line of chunkText.split('\n')) {
    const u = usageFromDataLine(line)
    if (u) return u
  }
  return null
}

// 流式 usage 扫描器：跨 chunk 缓冲按行解析（TCP 分片可能把行切开）
export class SseUsageScanner {
  constructor() {
    this.buf = ''
    this.usage = null
    this.decoder = new TextDecoder()
  }

  push(chunk) {
    this.buf += this.decoder.decode(chunk, { stream: true })
    let idx
    while ((idx = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, idx)
      this.buf = this.buf.slice(idx + 1)
      const u = usageFromDataLine(line)
      if (u) this.usage = u
    }
    // 单行超长保护（>1MB 的行放弃，正常 SSE 不会出现）
    if (this.buf.length > 1048576) this.buf = ''
  }
}

export function createGateway(config = {}) {
  const cfg = {
    port: 8787, host: '127.0.0.1', mode: 'shadow',
    newapiBaseUrl: '', newapiKey: '', tiers: null, prices: null,
    crossProvider: true, home: undefined, ttlMs: 600000, ...config,
  }
  if (!cfg.newapiBaseUrl) throw new Error('newapiBaseUrl is required')

  const router = new Router({
    home: cfg.home, mode: cfg.mode, crossProvider: cfg.crossProvider,
    modelTiers: emptyTiers(), prices: cfg.prices,
    exploration: cfg.exploration,
  })
  const catalog = new Catalog({
    baseUrl: cfg.newapiBaseUrl, apiKey: cfg.newapiKey, staticTiers: cfg.tiers,
    ttlMs: cfg.ttlMs, onRefresh: (tiers) => { router.modelTiers = tiers },
  })

  function upstreamHeaders(req) {
    // 客户端带了自己的 authorization 就透传（保留 new-api 的令牌/额度管理），
    // 否则用网关配置的 NEWAPI_API_KEY
    const auth = req.headers.authorization || ('Bearer ' + cfg.newapiKey)
    const h = {
      authorization: auth,
      'content-type': req.headers['content-type'] || 'application/json',
      accept: req.headers.accept || '*/*',
    }
    for (const k of ['x-request-id', 'user-agent']) {
      if (req.headers[k]) h[k] = req.headers[k]
    }
    return h
  }

  async function readBody(req) {
    const chunks = []
    for await (const c of req) chunks.push(c)
    return Buffer.concat(chunks)
  }

  async function proxyGeneric(req, res, body) {
    const target = cfg.newapiBaseUrl + req.url
    const up = await fetch(target, {
      method: req.method, headers: upstreamHeaders(req),
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
      redirect: 'manual',
    })
    res.writeHead(up.status, Object.fromEntries(up.headers))
    if (up.body) for await (const c of up.body) res.write(c)
    res.end()
  }

  // ---- 核心：chat/completions 路由 + 透传 + 结算 ----
  async function handleChat(req, res) {
    const startMs = Date.now()
    let bodyText
    try {
      bodyText = (await readBody(req)).toString('utf8')
    } catch {
      res.writeHead(400, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ error: { message: 'bad request body' } }))
      return
    }

    let j = null
    try { j = JSON.parse(bodyText) } catch { j = null }

    let decision = null
    let actualModel = j && typeof j.model === 'string' ? j.model : ''
    if (j && j.messages) {
      const text = lastUserText(j.messages)
      if (text) {
        decision = router.decide(text, PROVIDER, availableTiers(catalog.modelTiers))
        if (router.mode === 'active' && decision.model) {
          j.model = decision.model
          actualModel = decision.model
          bodyText = JSON.stringify(j)
        }
      }
    }
    const routedHeaders = decision && router.mode === 'active' && decision.model
      ? { 'x-agentic-router-tier': String(decision.used), 'x-agentic-router-expert': String(decision.chosen) }
      : {}

    const up = await fetch(cfg.newapiBaseUrl + '/v1/chat/completions', {
      method: 'POST', headers: upstreamHeaders(req), body: bodyText,
      signal: AbortSignal.timeout(600000),
    }).catch((e) => e)

    if (up instanceof Error) {
      // shadow 下请求由原模型服务，结果不能归因于路由档位，只记录不训练
      if (decision && router.mode === 'active') router.settle(decision.id, { success: false, dtMs: Date.now() - startMs })
      res.writeHead(502, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ error: { message: 'upstream unreachable: ' + up.message } }))
      return
    }

    const tier = decision ? decision.used : null
    // usage 统一按 OpenAI 格式（prompt_tokens/completion_tokens）传入，这里转换
    const settle = (rawUsage) => {
      if (!decision || router.mode !== 'active') return // shadow 只观察不训练
      const usage = toUsage(rawUsage)
      const costYuan = usage && actualModel
        ? turnCostYuan(usage, PROVIDER, actualModel, tier, router.prices)
        : 0
      router.settle(decision.id, {
        success: up.status >= 200 && up.status < 400,
        dtMs: Date.now() - startMs,
        costYuan,
      })
    }

    // 头部透传（去掉 hop-by-hop 与 content-length，流式自行计算）
    const headers = {}
    for (const [k, v] of up.headers) {
      const lk = k.toLowerCase()
      if (['content-length', 'transfer-encoding', 'connection', 'content-encoding'].includes(lk)) continue
      headers[k] = v
    }
    Object.assign(headers, routedHeaders)

    const streaming = !!(j && j.stream)
    if (!streaming) {
      const text = await up.text()
      let rawUsage = null
      try {
        const rj = JSON.parse(text)
        rawUsage = rj.usage || null
        if (!actualModel && rj.model) actualModel = rj.model
      } catch { /* 非 JSON 响应照常透传 */ }
      if (!headers['content-type']) headers['content-type'] = 'application/json'
      res.writeHead(up.status, headers)
      res.end(text)
      settle(rawUsage)
      return
    }

    // 流式：逐分片透传（不整包缓冲，SSE 首字延迟不受影响），
    // 跨分片按行缓冲扫描 usage
    res.writeHead(up.status, headers)
    const scanner = new SseUsageScanner()
    try {
      for await (const c of up.body) {
        res.write(c)
        scanner.push(c)
      }
      res.end()
    } catch {
      res.destroy()
    }
    settle(scanner.usage)
  }

  const server = http.createServer((req, res) => {
    const url = req.url.split('?')[0]
    if (req.method === 'GET' && (url === '/healthz' || url === '/agentic-router/stats')) {
      const s = router.state
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({
        mode: s.mode,
        decisions: s.seq,
        ucbArms: Object.keys(s.ucb.arms).length,
        linSamples: s.lin.total,
        knnPool: s.knn.pool.length,
        exp3Weights: s.exp3.weights,
        catalog: {
          fast: s ? (catalog.modelTiers.tiers.fast || []).length : 0,
          mid: (catalog.modelTiers.tiers.mid || []).length,
          strong: (catalog.modelTiers.tiers.strong || []).length,
          lastError: catalog.lastError,
        },
      }))
      return
    }
    if (req.method === 'POST' && url === '/v1/chat/completions') {
      handleChat(req, res).catch((e) => {
        if (!res.headersSent) {
          res.writeHead(500, { 'content-type': 'application/json' })
          res.end(JSON.stringify({ error: { message: String(e && e.message || e) } }))
        } else res.destroy()
      })
      return
    }
    // 其余 /v1/* 透传（embeddings / models / audio 等）
    readBody(req).then((b) => proxyGeneric(req, res, b)).catch(() => {
      res.writeHead(500); res.end()
    })
  })

  return { server, router, catalog, config: cfg }
}

export function start(config = {}) {
  const cfg = { ...configFromEnv(), ...config }
  const g = createGateway(cfg)
  g.catalog.start()
  return new Promise((resolve) => {
    g.server.listen(cfg.port, cfg.host, () => {
      console.log(`agentic-router-gateway listening on http://${cfg.host}:${cfg.port}`)
      console.log(`  mode=${cfg.router?.mode || g.router.mode} upstream=${cfg.newapiBaseUrl}`)
      resolve(g)
    })
  })
}

// CLI 直跑
if (import.meta.url === `file://${process.argv[1]}`) {
  start()
}
