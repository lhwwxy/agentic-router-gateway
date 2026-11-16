// @agentic-router/core —— 学习型智能路由核心（框架无关参考实现）
//
// 三个纯模块（分类器 / 专家策略 / 元选择器）+ 存储 + 编排 Router。
// 适配层（LiteLLM Python hook、Higress Go wasm 插件）必须与本实现行为一致。

export { classify, featuresOf } from './classify.js'
export {
  tierOf, tierTarget, tierHasAny, resolveTier,
  recommendRule, recommendUcb, recommendLin, recommendKnn,
  rewardOf, applyReward, correctReward,
  DEFAULT_PRICES, turnCostYuan, resolvePrice, mergePrices,
} from './strategies.js'
export { metaPick, DEFAULT_WEIGHTS } from './meta.js'
export { Store, defaultHome } from './store.js'
export { Router, freshState, emptyTiers, LIN_GRADUATION, KNN_GRADUATION } from './router.js'
