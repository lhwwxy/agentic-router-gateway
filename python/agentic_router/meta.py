"""EXP3 元选择器：把 {UCB, LinUCB, k-NN, rule} 当臂，按权重软最大采样。

v0.2：加入标准 EXP3 的 γ 混合（采样分布 p_n = (1−γ)·w_n/Σw + γ/K），
保证每个池内专家至少 γ/K 的采样概率。没有它，早期复合增长的专家权重会
指数爆炸（仿真复现过 2e141 量级），其余专家概率永久归零、无法恢复，
元选择器锁死在先发优势者上。重要性加权配合 γ 混合后，被冷落的专家一旦
被采到且给出高奖励，小 p 带来的大回血可使博弈恢复均衡。
"""

import random

DEFAULT_WEIGHTS = {"ucb": 1.0, "linucb": 1.0, "knn": 1.0, "rule": 2.5}

GAMMA = 0.05  # γ 混合率：每位专家保底 1.25% 采样概率


def meta_pick(recs, exp3, rng=None):
    """返回 {"chosen", "ps", "pool", "byName"}。

    exp3 需含 weights、linGraduated、knnGraduated（与 js metaPick 一致）。
    """
    rng = rng or random
    weights = exp3["weights"]
    pool = ["ucb"]
    by_name = {"ucb": recs["ucb"], "rule": recs["rule"]}
    if exp3.get("linGraduated") and recs.get("lin") and recs["lin"].get("tier"):
        pool.append("linucb")
        by_name["linucb"] = recs["lin"]
    if exp3.get("knnGraduated") and recs.get("knn") and recs["knn"].get("tier"):
        pool.append("knn")
        by_name["knn"] = recs["knn"]
    pool.append("rule")

    total = sum(weights.get(n, 0) for n in pool)
    k = len(pool)
    gamma = exp3.get("gamma", GAMMA)
    ps = {}
    for n in pool:
        raw = (weights.get(n, 0) / total) if total > 0 else 1 / k
        ps[n] = (1 - gamma) * raw + gamma / k
    r = rng.random()
    chosen = "rule"
    for n in pool:
        if r < ps[n]:
            chosen = n
            break
        r -= ps[n]
    return {"chosen": chosen, "ps": ps, "pool": pool, "byName": by_name}
