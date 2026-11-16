"""EXP3 元选择器：把 {UCB, LinUCB, k-NN, rule} 当臂，按权重软最大采样。"""

import random

DEFAULT_WEIGHTS = {"ucb": 1.0, "linucb": 1.0, "knn": 1.0, "rule": 2.5}


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
    ps = {n: weights.get(n, 0) / total for n in pool}
    r = rng.random()
    chosen = "rule"
    for n in pool:
        if r < ps[n]:
            chosen = n
            break
        r -= ps[n]
    return {"chosen": chosen, "ps": ps, "pool": pool, "byName": by_name}
