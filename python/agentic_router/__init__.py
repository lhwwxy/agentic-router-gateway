"""agentic-router: 学习型智能路由核心 + LiteLLM 网关适配。

四专家（规则 / UCB / LinUCB / k-NN）+ EXP3 元选择器 + 质量奖励飞轮。
与 js/（参考实现）和 go/（Higress 移植）行为对拍，规范见 spec/ROUTING_SPEC.md。
"""

from .classify import classify, features_of
from .meta import DEFAULT_WEIGHTS, meta_pick
from .router import (
    DECISION_WINDOW, KNN_GRADUATION, LIN_GRADUATION, Router, empty_tiers,
    fresh_state,
)
from .store import Store, default_home
from .strategies import (
    DEFAULT_PRICES, apply_reward, correct_reward, merge_prices, recommend_knn,
    recommend_lin, recommend_rule, recommend_ucb, resolve_price, resolve_tier,
    resolve_tier as resolveTier, reward_of, tier_of, tier_target, turn_cost_yuan,
)

__version__ = "0.1.0"
__all__ = [
    "classify", "features_of", "meta_pick", "DEFAULT_WEIGHTS",
    "Router", "fresh_state", "empty_tiers", "Store", "default_home",
    "LIN_GRADUATION", "KNN_GRADUATION", "DECISION_WINDOW",
    "recommend_rule", "recommend_ucb", "recommend_lin", "recommend_knn",
    "resolve_tier", "resolveTier", "tier_of", "tier_target",
    "reward_of", "apply_reward", "correct_reward",
    "DEFAULT_PRICES", "merge_prices", "resolve_price", "turn_cost_yuan",
]
