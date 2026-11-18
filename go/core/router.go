package core

import (
	"encoding/json"
	"math/rand"
	"sync"
	"time"
)

var defaultRNG = func() float64 { return rand.Float64() }

func randInt(n int) int { return rand.Intn(n) }

// 编排层：决策 → 结算（对应 js/src/router.js）。decisions 由本结构持有，
// 专家状态 State 可整体序列化后经宿主（Wasm shared data / Redis）持久化。

const (
	LinGraduation = 50   // LinUCB 毕业样本线
	KnnGraduation = 30   // k-NN 毕业经验线
	DecisionWindow = 200
	// ExplorationEps 是档位级 ε 探索率（0 关闭）
	ExplorationEps = 0.08
)

// Decision 一条路由决策。
type Decision struct {
	ID         int            `json:"id"`
	TS         int64          `json:"ts"`
	Type       string         `json:"type"`
	Complexity float64        `json:"complexity"`
	Len        int            `json:"len"`
	Mode       string         `json:"mode"`
	Chosen     string         `json:"chosen"`
	PChosen    float64        `json:"pChosen"`
	Pool       []string       `json:"pool"`
	Tier       string         `json:"tier"`
	Used       string         `json:"used"`
	Provider   string         `json:"provider"`
	Model      string         `json:"model"`
	Note       string         `json:"note"`
	Reason     string         `json:"reason"`
	Settled    bool           `json:"settled"`
	Reward     float64        `json:"reward,omitempty"`
	LinLr      float64        `json:"linLr,omitempty"`
	// Train 决策时快照：mode=active 且有改写意图才可能训练飞轮。
	// shadow 决策或改写被拒（MarkNoTrain）时为假——请求由原模型服务，
	// 其结果不能归因于路由档位。
	Train      bool           `json:"train,omitempty"`
}

// SettleResult 结算结果。Trained=false 表示仅结算观测（shadow/被拒改写）。
type SettleResult struct {
	Reward    float64
	Breakdown Breakdown
	Tier      string
	Trained   bool
}

type decisionRing struct {
	items []*Decision
}

func (r *decisionRing) push(d *Decision) {
	r.items = append(r.items, d)
	if len(r.items) > DecisionWindow {
		r.items = r.items[len(r.items)-DecisionWindow:]
	}
}

func (r *decisionRing) find(id int, settled bool) *Decision {
	for _, d := range r.items {
		if d.ID == id && d.Settled == settled {
			return d
		}
	}
	return nil
}

// Router 线程安全的路由器（宿主回调可能并发）。
type Router struct {
	mu            sync.Mutex
	State         *State
	ModelTiers    ModelTiers
	CrossProvider bool
	// Exploration 是档位级 ε 探索率（0 关闭，默认 ExplorationEps）
	Exploration   float64
	rng           func() float64
	decisions     decisionRing
}

// NewRouter 创建路由器；state/modelTiers 为空时使用全新/空目录（只记录不切换）。
func NewRouter(state *State, mt ModelTiers, crossProvider bool, rng func() float64) *Router {
	if state == nil {
		state = FreshState()
	}
	if rng == nil {
		rng = defaultRNG
	}
	return &Router{State: state, ModelTiers: mt, CrossProvider: crossProvider,
		Exploration: ExplorationEps, rng: rng}
}

// Decide 输入本轮用户文本，返回路由决策（含目标模型）。
func (r *Router) Decide(text, provider string, availableTiers []string) *Decision {
	r.mu.Lock()
	defer r.mu.Unlock()
	cls := Classify(text)
	f := FeaturesOf(cls, text)
	tiers := availableTiers
	if len(tiers) == 0 {
		tiers = []string{"fast", "mid", "strong"}
	}

	recs := map[string]Recommendation{
		"rule": RecommendRule(cls),
		"ucb":  RecommendUcb(cls, &r.State.UCB, tiers),
		"lin":  RecommendLin(cls, f, &r.State.Lin, tiers),
		"knn":  RecommendKnn(cls, f, &r.State.KNN, tiers),
	}

	r.State.Exp3.LinGraduated = r.State.Lin.Total >= LinGraduation
	r.State.Exp3.KnnGraduated = len(r.State.KNN.Pool) >= KnnGraduation
	meta := MetaPick(recs, &r.State.Exp3, r.rng())
	chosenRec := meta.Rec
	if chosenRec.Tier == "" {
		chosenRec = recs["rule"] // k-NN 冷启动回退规则表
	}
	tier := chosenRec.Tier
	explored := false
	// 档位级 ε 探索（v0.2）：EXP3 按实现奖励评判专家，会惩罚付探索成本的 UCB；
	// 贪婪专家主导后没人再探索非首选档，正确档位的数据永远采不到。
	// 以 ε 概率随机路由一个可用档位，保证全臂数据持续流入（reward 记在 used 档）。
	if r.Exploration > 0 && len(tiers) > 1 && r.rng() < r.Exploration {
		tier = tiers[randInt(len(tiers))]
		explored = true
	}
	resolved := ResolveTier(tier, provider, r.ModelTiers, r.CrossProvider)
	if explored {
		resolved.Note += " [explore]"
	}

	r.State.Seq++
	d := &Decision{
		ID: r.State.Seq, TS: time.Now().UnixMilli(),
		Type: cls.Type, Complexity: cls.Complexity, Len: len([]rune(text)),
		Mode: r.State.Mode, Chosen: meta.Chosen,
		PChosen: meta.Ps[meta.Chosen], Pool: meta.Pool,
		Tier: tier, Used: resolved.Used,
		Provider: resolved.Provider, Model: resolved.Target,
		Note: resolved.Note, Reason: chosenRec.Reason,
		// 决策时模式快照 + 改写意图：shadow 决策永不训练
		Train: r.State.Mode == "active" && resolved.Target != "",
	}
	r.decisions.push(d)
	return d
}

// MarkNoTrain 撤销改写（宿主侧替换 body 失败等）：该决策的结果
// 由原模型服务，结算时只记观测不喂飞轮。
func (r *Router) MarkNoTrain(decisionID int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if d := r.decisions.find(decisionID, false); d != nil {
		d.Train = false
	}
}

// Settle 结果回传：计算奖励并喂飞轮。train=false（shadow 决策或改写被拒）时
// 只清 pending 记录观测，不喂飞轮——请求由原模型服务，结果不能归因路由档位。
func (r *Router) Settle(decisionID int, o Outcome) *SettleResult {
	r.mu.Lock()
	defer r.mu.Unlock()
	d := r.decisions.find(decisionID, false)
	if d == nil {
		return nil
	}
	d.Settled = true
	reward, bd := RewardOf(o, o.Success)
	d.Reward = reward
	if !d.Train {
		return &SettleResult{Reward: reward, Breakdown: bd, Tier: d.Used, Trained: false}
	}
	rec := &RewardRec{
		ClsType: d.Type, UcbTier: d.Used, LinTier: d.Used,
		F: rebuildFeatures(d), Reward: reward,
		Chosen: d.Chosen, PChosen: d.PChosen, Pool: d.Pool,
	}
	ApplyReward(r.State, rec)
	d.LinLr = rec.LinLr
	return &SettleResult{Reward: reward, Breakdown: bd, Tier: d.Used, Trained: true}
}

// Correct 人类反馈修正（👍/👎）。
func (r *Router) Correct(decisionID int, newReward float64) float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	d := r.decisions.find(decisionID, true)
	if d == nil {
		return 0
	}
	rec := &RewardRec{
		ClsType: d.Type, UcbTier: d.Used, LinTier: d.Used,
		F: rebuildFeatures(d), Reward: d.Reward,
		Chosen: d.Chosen, PChosen: d.PChosen, Pool: d.Pool, LinLr: d.LinLr,
	}
	return CorrectReward(r.State, rec, newReward)
}

// SetMode 切换 shadow / active。
func (r *Router) SetMode(mode string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if mode == "active" || mode == "shadow" {
		r.State.Mode = mode
	}
}

// Mode 返回当前路由模式（shadow/active）。
func (r *Router) Mode() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.State.Mode
}

// MarshalState 在锁内序列化状态（宿主持久化用，避免与并发的 Decide/Settle 竞态）。
func (r *Router) MarshalState() []byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	b, err := json.Marshal(r.State)
	if err != nil {
		return nil
	}
	return b
}

func rebuildFeatures(d *Decision) []float64 {
	n := d.Len
	if n > 4000 {
		n = 4000
	}
	f := []float64{1, d.Complexity, float64(n) / 4000}
	for _, ty := range featureTypes {
		if d.Type == ty {
			f = append(f, 1)
		} else {
			f = append(f, 0)
		}
	}
	return f
}
