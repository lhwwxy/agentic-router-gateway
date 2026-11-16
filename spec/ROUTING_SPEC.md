# 路由规范（跨语言行为规范）

本文件是 js / python / go 三个实现共同遵守的**唯一事实源**。任何公式或阈值变更必须：

1. 先改本文件；
2. 同步修改三份 golden 测试（`js/test/core.test.js`、`python/tests/test_core.py`、`go/core/core_test.go`）；
3. 三个实现的 CI 必须同时绿。

## 1. 分类器（`classify`）

输入：本轮用户文本（最后一条 user 消息）。输出：`{type, complexity}`。

类型判定按顺序短路（对 lowercased 文本做正则搜索）：

| 顺序 | 类型 | 正则（示意） |
|---|---|---|
| 1 | qa | `(请|麻烦|帮我).{0,6}(回答|告诉|说)` |
| 2 | code | `def |function |class |import |require(|```|\.py|...|报错|代码|函数|接口|脚本` |
| 3 | write | `(写|创作|起草|润色|翻译|translate|write|blog|article|文案|邮件|报告)` |
| 4 | summarize | `(总结|概括|summar|摘要|要点)` |
| 5 | research | `(查|搜索|检索|research|调研|对比|最新|资料|文档)` |
| 6 | agentic | `(帮我|请|做|搭建|构建|部署|实现|写一个|生成|build|setup|deploy|implement|create)` |
| 7 | qa | `[?？]|什么|为什么|怎么|如何|how|what|why|能否|是否` |
| - | other | 以上皆不中 |

复杂度：

```
score = PRIOR[type]                       // code/agentic 0.3, research 0.2, write 0.15, summarize 0.05, qa/other 0
      + min(len/2000, 1) × 0.2            // 长度项
      + 0.25 if 含```                     // 代码块
      + 0.25 if COMPLEX_RE                // 复杂关键词
      − 0.25 if SIMPLE_RE and !COMPLEX_RE // 简单关键词（与复杂同现不罚）
complexity = clamp(0..1) 四舍五入两位小数   // JS Math.round 语义（向 +∞）
```

特征向量（LinUCB / k-NN 共用，9 维）：

```
f = [1, complexity, min(len,4000)/4000, one-hot(type ∈ [code,write,summarize,research,agentic,qa])]
```

## 2. 模型档位（`tierOf`）

- `fast`：模型名含 `flash|lite|mini|small|fast|air|nano|tiny|light`
- `strong`：含 `pro|max|ultra|plus|large|premium|reason|thinking`
- 其余 `mid`

## 3. 档位解析（`resolveTier`）

输入 `tier`、当前 `provider`、模型目录 `modelTiers`（`providers` 顺序即跨厂商择优顺序，档位条目形如 `provider/model`）：

1. 同 provider 同档 → 直接用
2. `crossProvider`（默认开）时按 providers 顺序取其它 provider 同档
3. `mid` 仍无：先（同/跨）`strong` 再 `fast` 回退
4. 全无 → `{target: null, note: 'no-candidate'}`（照常记录决策但不切换）

返回 `{target, provider, used, note}`；`used` 是实际生效档位（奖励按它记账）。

## 4. 四专家

所有专家只在 `availableTiers`（当前有候选模型的档位）中推荐，不探索"幽灵档"。

| 专家 | 公式 | 冷启动 | 毕业线 |
|---|---|---|---|
| rule | `complexity ≥ 0.7 → strong; ≤ 0.3 → fast; else mid` | 即可用 | — |
| UCB | `mean + 1.2·√(ln(N+1)/n)`，臂 = `type:tier`；n=0 上界∞，多∞平局按 `total % len(infs)` 轮转 | 立即探索 | — |
| LinUCB | 每档 `w·f + bonus`，`bonus = 0.8·√(ln(total+1)/tn)`（tn=0 时 0.3）；梯度 `w += lr·(r − w·f)·f`，`lr = 1/(1+tn)` | dot=0 纯 bonus | total ≥ 50 进入 EXP3 臂池 |
| k-NN | 欧氏距离 k=5 邻居，按邻居平均奖励选档；池上限 400（FIFO） | 池 <5 返回空档 | 池 ≥ 30 进入 EXP3 臂池 |

## 5. EXP3 元选择器（`metaPick`）

臂池 `[ucb, (linucb), (knn), rule]`（括号内需已毕业且给出有效档位）。

初始权重 `{ucb: 1, linucb: 1, knn: 1, rule: 2.5}`（规则表冷启动占优）。采样分布带 **γ 混合**（v0.2，默认 γ=0.05）：

```
p_n = (1−γ)·w_n/Σw + γ/K        # K = 池内专家数
```

这保证每位专家至少 γ/K 的采样概率。**为什么必须有**：无 γ 混合时，早期复合增长的专家权重会指数爆炸（仿真复现过 2e141 量级），其余专家采样概率永久归零，元选择器锁死在先发优势者上（哪怕它后来学歪了，也无法被纠正）。重要性加权与 γ 混合配合：被冷落的专家一旦被采到且给出高奖励，`w *= exp(0.3·(r/p)/|pool|)` 中的小 p 带来大回血，博弈可恢复均衡。

重要性加权更新：

```
w[chosen] *= exp(0.3 · (reward / pChosen) / len(pool))
```

**权重防爆保险**：更新后若 `max(w) > 1e50`，全部权重除以 max，并给每个权重加 `1e-6` 地板（比例钳在 1e6 内）。地板必不可少：反复归一化会让小权重下溢到精确 0（`0×任何数=0`，永久不可恢复）；比例钳制也不可过宽（如 1e12 时被冷落专家靠 γ 地板采样翻身需上千轮，主导权振荡周期过长）。

**档位级 ε 探索（默认 ε=0.08，可配 0 关闭）**：EXP3 按实现奖励评判专家，会惩罚付探索成本的 UCB——贪婪专家（k-NN/LinUCB）主导后，没人再探索非首选档位，正确档位的数据永远采不到（仿真复现：`write` 类永久卡在 fast 而最优是 mid）。因此决策时以 ε 概率从可用档位中随机路由一个，保证全臂数据持续流入；奖励照常记在 `used`（实际路由）档位上，探索决策在 `note` 中标注 `[explore]`。

k-NN 冷启动给出空档时回退 rule 的推荐。

## 6. 奖励函数（`rewardOf`）

```
tag = 响应干净收尾 ? 'clean-close' : 'failed'   // failed → reward 0
reward = 1 − min(0.6, 0.2×toolErrors)
         − min(0.4, 0.2×retries)
         − min(0.5, 20×cost)          // cost 单位与宿主一致（网关默认元；LiteLLM 默认取 response_cost）
         − (dt > 90s ? 0.3 : dt > 30s ? 0.1 : 0)
clamp 0..1，两位小数
```

## 7. 飞轮更新（`applyReward`）

一条决策结算后（奖励 r，特征 f，臂 `type:tier`，被选专家 `chosen`，采样概率 `pChosen`，臂池 `pool`）：

- **UCB**：`n++; mean += (r − mean)/n`
- **LinUCB**：`lr = 1/(1+tn)`（记入决策供修正回退）；`dot = w·f`；`w += lr·(r − dot)·f`；`tn++; total++`
- **k-NN**：`pool.push({f, tier, r})`，上限 400
- **EXP3**：`w[chosen] *= exp(0.3·(r/pChosen)/len(pool))`；`total++`

**反馈修正**（`correctReward`，👍/👎 改写奖励为 `r'`，`Δ = r' − r`）：

- UCB：`mean += Δ/n`（n 不变）
- LinUCB：`w += lr·Δ·f`（用当时记录的 lr 精确回退）
- k-NN：追加 `{f, tier, r'}` 经验
- EXP3：`w[chosen] *= exp(0.3·Δ/(pChosen·len(pool)))`

## 8. 决策记录（落盘审计）

```json
{
  "id": 1, "ts": 0, "type": "summarize", "complexity": 0.05, "len": 12,
  "mode": "shadow", "chosen": "rule", "pChosen": 0.71, "pool": ["ucb","rule"],
  "tier": "fast", "used": "fast", "provider": "p1", "model": "lite",
  "note": "", "reason": "rule threshold complexity=0.05", "settled": false
}
```

宿主持久化约定：

- **JS / Python**：`${AGENTIC_ROUTER_HOME:-~/.agentic-router}/` 下 `state.json`（原子写）、`decisions.jsonl`、`rewards.jsonl`
- **Go (Higress Wasm)**：`State` 经 proxywasm shared data（key `agentic_router_state`）跨 worker 共享；decisions 留在 worker 内存（重启丢失未结算 pending，可接受）

## 9. 宿主适配层契约

每个宿主只需实现三个翻译：

| 宿主事件 | 调用 |
|---|---|
| 请求进入（拿到最后一条 user 消息） | `decide(text, provider)` → active 模式改写模型 |
| 响应成功（延迟、用量） | `settle(id, {success: true, dtMs, costYuan})` |
| 响应失败 / 重试 | `settle(id, {success: false})` |
| 人类反馈（若有通道） | `correct(id, newReward)` |

网关盲区（宿主内版本有、网关版没有的信号）：工具调用失败次数、模型重试次数、用户 👍/👎。网关侧以延迟/状态/成本近似。
