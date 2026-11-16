// 飞轮持久化（框架无关版）：状态 state.json 原子写，决策/奖励追加 JSONL，
// policy.json 存模式/强制模型等策略开关。
// 默认目录：${AGENTIC_ROUTER_HOME:-~/.agentic-router}。全部本地明文，可导出可审计。

import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync, appendFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { homedir } from 'node:os'

export function defaultHome() {
  if (process.env.AGENTIC_ROUTER_HOME) return process.env.AGENTIC_ROUTER_HOME
  return join(homedir(), '.agentic-router')
}

export class Store {
  constructor(home) {
    this.home = home || defaultHome()
    mkdirSync(this.home, { recursive: true })
  }

  statePath() { return join(this.home, 'state.json') }
  policyPath() { return join(this.home, 'policy.json') }
  jsonlPath(name) { return join(this.home, name) }

  #atomicWrite(p, content) {
    mkdirSync(dirname(p), { recursive: true })
    const tmp = p + '.tmp'
    writeFileSync(tmp, content)
    renameSync(tmp, p)
  }

  loadState() {
    try {
      if (!existsSync(this.statePath())) return null
      const s = JSON.parse(readFileSync(this.statePath(), 'utf8'))
      if (!s || typeof s !== 'object' || !s.ucb) return null
      return s
    } catch {
      return null
    }
  }

  saveState(state) {
    this.#atomicWrite(this.statePath(), JSON.stringify(state))
  }

  loadPolicy() {
    try {
      if (!existsSync(this.policyPath())) return {}
      return JSON.parse(readFileSync(this.policyPath(), 'utf8'))
    } catch {
      return {}
    }
  }

  savePolicy(policy) {
    this.#atomicWrite(this.policyPath(), JSON.stringify(policy, null, 2))
  }

  appendJsonl(name, obj) {
    appendFileSync(this.jsonlPath(name), JSON.stringify(obj) + '\n')
  }

  loadJsonl(name) {
    try {
      if (!existsSync(this.jsonlPath(name))) return []
      return readFileSync(this.jsonlPath(name), 'utf8')
        .split('\n')
        .filter((l) => l.trim())
        .map((l) => JSON.parse(l))
    } catch {
      return []
    }
  }

  resetFlywheel() {
    for (const name of ['decisions.jsonl', 'rewards.jsonl']) {
      const p = this.jsonlPath(name)
      if (existsSync(p)) writeFileSync(p, '')
    }
  }
}
