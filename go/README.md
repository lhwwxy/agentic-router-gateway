# agentic-router（Go / Higress Wasm 插件）

Higress 的学习型路由 Wasm 插件。核心算法在 `core/`（纯标准库，可直接 `go test`），插件入口在 `main.go`。

## 行为

1. **请求体阶段**：提取最后一条 user 消息 → 分类 + 四专家决策 → `active` 模式改写 JSON body 的 `model` 字段，并加 `x-agentic-router-tier` / `x-agentic-router-expert` 头（供日志/统计）
2. **响应阶段**：按 HTTP 状态（成功/失败）+ 延迟 + 非流式响应的 `usage` 成本结算奖励，喂飞轮
3. **状态**：飞轮经 Wasm shared data（key `agentic_router_state`）在同实例多 worker 间共享（last-writer-wins）

## 配置

```yaml
shadow: true                 # 默认 true：只记录不改写
crossProvider: true
tierModels:
  fast: "qwen-flash"
  mid: "qwen-plus"
  strong: "qwen-max"
prices:                      # 可选：元/百万 token（tier → {in, out}）
  fast: { in: 0.5, out: 2 }
```

配合 `ai-proxy`（按 model 转发到对应 provider）或 Higress 的 model 路由能力使用：本插件负责"选档"，转发执行交给现有插件。

## 构建

需要 Go 1.21+ 与 TinyGo：

```bash
cd go
go test ./core/...                            # 核心对拍测试
tinygo build -o plugin.wasm -target=wasi ./   # 产出 Wasm 插件
```

依赖已锁定在 `go.sum`（higress wasm-go v1.4.3 / proxy-wasm-go-sdk v1.0.0）。国内网络环境请配 `GOPROXY=https://goproxy.cn,direct`。如 API 有变动（wrapper 回调签名等），以你所用 Higress 版本的 [plugins/wasm-go](https://github.com/higress-group/higress/tree/main/plugins/wasm-go) 文档为准。

## 安装

- **插件市场/独立路由配置**：将 `plugin.wasm` 打成 OCI 镜像或直接在控制台"插件配置 → 自定义 Wasm 插件"上传
- **控制台配置**：按上面的 YAML 填入插件配置

## 已知限制

- **流式（SSE）响应**拿不到聚合 usage：成本记 0，延迟/状态信号仍有效
- **shared data**：多 worker last-writer-wins；重实例/多副本场景建议接 Redis 持久化（roadmap）
- **pending 决策**存 worker 内存：网关重启丢失未结算请求的奖励回传（可接受）

## 上游合并

见 [../UPSTREAM.md](../UPSTREAM.md)：目标是合入 `higress/plugins/wasm-go/extensions/agentic-router/`。
