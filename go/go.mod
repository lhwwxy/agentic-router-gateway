module github.com/lhwwxy/agentic-router-gateway/go

go 1.21

require (
	github.com/higress-group/proxy-wasm-go-sdk v1.0.0
	github.com/tidwall/gjson v1.17.3
	github.com/tidwall/sjson v1.2.5
)

require (
	github.com/alibaba/higress/plugins/wasm-go v1.4.3
	github.com/google/uuid v1.3.0 // indirect
	github.com/higress-group/nottinygc v0.0.0-20231101025119-e93c4c2f8520 // indirect
	github.com/magefile/mage v1.14.0 // indirect
	github.com/tidwall/match v1.1.1 // indirect
	github.com/tidwall/pretty v1.2.0 // indirect
	github.com/tidwall/resp v0.1.1 // indirect
)

// wasm-go 是 higress 仓库的独立子 module（module 路径保留 alibaba 前缀），
// 版本随上游发布（v1.4.3）锁定于 go.sum。
