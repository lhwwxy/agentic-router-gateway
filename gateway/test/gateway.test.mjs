// e2e：mock 一个 new-api，再起本网关，验证改写模型 / 流式透传 / 奖励结算。

import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { buildTiers, buildTiersFromStatic, availableTiers } from '../catalog.mjs'
import { createGateway, lastUserText, usageFromSseChunk, toUsage } from '../server.mjs'

const MODELS = ['deepseek-v4-flash', 'deepseek-v4', 'deepseek-v4-pro']

function startMockNewApi() {
  const seen = []
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url.startsWith('/v1/models')) {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ data: MODELS.map((id) => ({ id })) }))
      return
    }
    let body = ''
    req.on('data', (c) => { body += c })
    req.on('end', () => {
      const j = JSON.parse(body)
      seen.push({ model: j.model, auth: req.headers.authorization })
      if (j.stream) {
        res.writeHead(200, { 'content-type': 'text/event-stream' })
        res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: '你好' } }] })}\n\n`)
        res.write(`data: ${JSON.stringify({ choices: [{ delta: {} }], usage: { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 2 }, completion_tokens_details: { reasoning_tokens: 3 } } })}\n\n`)
        res.write('data: [DONE]\n\n')
        res.end()
      } else {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({
          model: j.model,
          choices: [{ message: { content: 'ok' } }],
          usage: { prompt_tokens: 100, completion_tokens: 50 },
        }))
      }
    })
  })
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ server, seen, port: server.address().port })))
}

async function withGateway(mode, fn) {
  const mock = await startMockNewApi()
  const home = mkdtempSync(join(tmpdir(), 'ar-gw-'))
  const g = createGateway({
    port: 0, host: '127.0.0.1', mode,
    newapiBaseUrl: `http://127.0.0.1:${mock.port}`,
    newapiKey: 'sk-gw-key', home,
    exploration: 0, // 测试关掉 ε 探索保证确定性
    tiers: { fast: ['deepseek-v4-flash'], mid: ['deepseek-v4'], strong: ['deepseek-v4-pro'] },
  })
  // 静态目录已在 config.tiers，手动同步给 router（生产路径由 Catalog.onRefresh 做）
  g.catalog.tiers = buildTiersFromStatic({ fast: ['deepseek-v4-flash'], mid: ['deepseek-v4'], strong: ['deepseek-v4-pro'] })
  g.router.modelTiers = g.catalog.modelTiers
  await new Promise((r) => g.server.listen(0, '127.0.0.1', r))
  try {
    await fn(mock, g)
  } finally {
    g.server.close()
    mock.server.close()
    rmSync(home, { recursive: true, force: true })
  }
}

test('catalog: buildTiers 按 tierOf 归档', () => {
  const t = buildTiers(MODELS)
  assert.deepEqual(t.tiers.fast, ['newapi/deepseek-v4-flash'])
  assert.deepEqual(t.tiers.mid, ['newapi/deepseek-v4'])
  assert.deepEqual(t.tiers.strong, ['newapi/deepseek-v4-pro'])
  assert.deepEqual(availableTiers(t), ['fast', 'mid', 'strong'])
  assert.deepEqual(availableTiers(buildTiers([])), ['fast', 'mid', 'strong'])
})

test('catalog: 静态配置按声明档位落位', () => {
  const t = buildTiersFromStatic({ fast: 'model-a', strong: ['model-b'] })
  assert.deepEqual(t.tiers.fast, ['newapi/model-a'])
  assert.deepEqual(t.tiers.mid, [])
  assert.deepEqual(t.tiers.strong, ['newapi/model-b'])
})

test('helpers: lastUserText / usageFromSseChunk / toUsage', () => {
  assert.equal(lastUserText([
    { role: 'system', content: 'sys' },
    { role: 'user', content: '第一个问题' },
    { role: 'assistant', content: '答' },
    { role: 'user', content: [{ type: 'text', text: '帮我总结' }] },
  ]), '帮我总结')
  assert.equal(lastUserText([]), '')

  const u = usageFromSseChunk(`data: {"choices":[{}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\ndata: [DONE]\n`)
  assert.equal(u.prompt_tokens, 10)

  const usage = toUsage({ prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 2 }, completion_tokens_details: { reasoning_tokens: 3 } })
  assert.deepEqual(usage, { inputTokens: 10, outputTokens: 5, cacheReadTokens: 2, reasoningTokens: 3 })
})

test('e2e: active 模式改写模型 + 非流式结算（成本入奖励）', async () => {
  await withGateway('active', async (mock, g) => {
    const port = g.server.address().port
    const res = await fetch(`http://127.0.0.1:${port}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer sk-user' },
      body: JSON.stringify({
        model: 'deepseek-v4',
        messages: [{ role: 'user', content: '帮我总结这篇文章的要点' }],
      }),
    })
    assert.equal(res.status, 200)
    assert.ok(['deepseek-v4-flash', 'deepseek-v4', 'deepseek-v4-pro'].includes(mock.seen[0].model))
    // 客户端自己的令牌透传给 new-api
    assert.equal(mock.seen[0].auth, 'Bearer sk-user')
    assert.equal(res.headers.get('x-agentic-router-tier'), 'fast') // summarize→rule→fast

    // 结算已发生：飞轮 +1
    assert.equal(g.router.state.ucb.total, 1)
    const recs = g.router.store.loadJsonl('rewards.jsonl')
    assert.equal(recs.length, 1)
    // 非流式 usage 计入成本：fast 档 in=1,out=4（元/百万）
    // input=100, output=50 → (1*100 + 4*50)/1e6 = 0.0003
    assert.ok(recs[0].breakdown.costYuan > 0)
    assert.ok(Math.abs(recs[0].breakdown.costYuan - 0.0003) < 1e-9)
    // 奖励 = 1 - 20*0.0003 = 0.994 → 两位小数 0.99
    assert.equal(recs[0].reward, 0.99)
  })
})

test('e2e: shadow 模式不改写模型，结算只记观测不训练', async () => {
  await withGateway('shadow', async (mock, g) => {
    const port = g.server.address().port
    const res = await fetch(`http://127.0.0.1:${port}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        model: 'deepseek-v4',
        messages: [{ role: 'user', content: '请帮我重构这个模块并做性能优化' }],
      }),
    })
    assert.equal(res.status, 200)
    assert.equal(mock.seen[0].model, 'deepseek-v4') // 未改写
    assert.equal(res.headers.get('x-agentic-router-tier'), null) // shadow 无头
    // shadow 的结果由原模型服务，不能归因于路由档位：结算 drain 但不喂飞轮
    assert.equal(g.router.state.ucb.total, 0)
    const d = g.router.state.decisions.find((x) => !x.train && x.settled)
    assert.ok(d, 'shadow decision should settle (drain) without training')
  })
})

test('e2e: 流式透传 + 末帧 usage 结算', async () => {
  await withGateway('active', async (mock, g) => {
    const port = g.server.address().port
    const res = await fetch(`http://127.0.0.1:${port}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        model: 'deepseek-v4',
        stream: true,
        messages: [{ role: 'user', content: '你好' }],
      }),
    })
    assert.equal(res.status, 200)
    const text = await res.text()
    assert.ok(text.includes('data: [DONE]'))
    assert.ok(text.includes('你好'))
    assert.equal(g.router.state.ucb.total, 1)
    const recs = g.router.store.loadJsonl('rewards.jsonl')
    // 流式 usage：input=10(+2缓存)=12, output=5(+3推理)=8
    // fast 档 in=1,out=4 → (1*12 + 4*8)/1e6 = 44e-6 → 罚分 20*44e-6=0.00088
    // → 奖励 0.99912 → 两位小数 = 1
    assert.ok(Math.abs(recs[0].breakdown.costYuan - 0.000044) < 1e-12)
    assert.equal(recs[0].reward, 1)
  })
})

test('e2e: /healthz 与 /v1/models 透传', async () => {
  await withGateway('shadow', async (mock, g) => {
    const port = g.server.address().port
    const h = await (await fetch(`http://127.0.0.1:${port}/healthz`)).json()
    assert.equal(h.mode, 'shadow')
    assert.deepEqual([h.catalog.fast, h.catalog.mid, h.catalog.strong], [1, 1, 1])

    const m = await (await fetch(`http://127.0.0.1:${port}/v1/models`)).json()
    assert.deepEqual(m.data.map((x) => x.id), MODELS)
  })
})
