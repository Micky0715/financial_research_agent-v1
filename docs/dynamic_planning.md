# Planner 动态路由：评估与最小侵入实现

v2 模块5：评估 `normalize_plan()` 强制收敛 5 阶段的利弊，并给出一个在**不破坏
评估集**前提下让执行链路在特定场景真正"重复某个阶段"的最小侵入实现。

## 1. 对现有 normalize_plan() 的评估

**为什么当初要强制收敛**（见 eval/bad_cases.md Bad Case 2）：LLM Planner 输出
过 9 个甚至 17 个任务（隆基绿能 raw=9、半导体国产替代 raw=17），包含重复的
research/browse 阶段，orchestrator 照单执行导致同一阶段跑多遍、耗时成倍增加。
`normalize_plan()` 把"LLM 说什么就执行什么"改成"LLM 输出只作为参考，执行计划
由代码保证"，10/10 case 的 `planner_normalized_task_count` 从此恒为 5。

**代价**：链路失去了合理的动态性。最典型的场景——browse 阶段发现来源不足时，
正确的做法是**带着新的检索策略回到 research 阶段再来一轮**，而不是像现在这样
直接判失败、跳过后续阶段、输出"资料不足"报告。v1 里 `ResearchAgent` 内部虽然
有 fallback query 机制，但它只在第一轮搜索结果数不足时触发；如果搜索结果数够
多、只是**质量差**（browse 阶段才能发现），就没有任何补救路径。

**结论**：固定 5 阶段作为默认骨架应该保留（它挡住的是 LLM 输出的不可控性），
动态性应该加在**由确定性信号触发、有硬性次数上限**的重执行上，而不是把执行
计划的控制权还给 LLM。

## 2. 方案对比

| 方案 | 侵入度 | 风险 | 结论 |
|---|---|---|---|
| A. 放开 normalize_plan，信任 LLM 输出的多阶段计划 | 低（删代码） | 直接回退到 Bad Case 2 的重复执行问题 | 否决 |
| B. LLM 在每个阶段后决定下一步（ReAct 式动态调度） | 高（重写 orchestrator） | 引入新的不可控性；评估集大概率回归 | 否决（超出"最小侵入"） |
| C. **orchestrator 内置确定性重执行规则**：browse 失败且诊断信号表明"是检索问题而非网络问题"时，插入一轮 research(强制 fallback query) -> browse，最多 1 轮 | 低（orchestrator 一个循环 + ResearchAgent 一个参数） | 触发条件只在失败路径上，评估集全通过的 case 行为完全不变 | **采用** |

## 3. 实现（方案 C）

### 触发条件（全部满足才触发）

1. browse 任务失败（`NoUsableSourcesError`）；
2. `browser_metrics.browser_fallback_triggered == True`（候选<5 或可用来源<3，
   即"检索供给不足"，而不是相关性阈值问题——后者已有 relaxed relevance 兜底）；
3. 本次运行还没用过重检索轮次（`MAX_REPLAN_ROUNDS = 1`，硬上限）。

### 执行动作

1. 重跑 research 任务，带参数 `parameters={"force_fallback": True}`——
   `ResearchAgent` 看到该参数时无条件追加 site: 权威源 fallback query 轮
   （v1 里这些 query 只在首轮唯一结果数低于阈值时才触发）；
2. 用新的候选集重跑 browse 任务；
3. 无论成败，不再触发第二轮（防止死循环烧 API）。

### 为什么这算"真正的动态路由"而不是换皮的重试

- BaseAgent 的重试是**同输入重放**（v1 里已经对 browse 禁用了这种无意义重试，
  见 Bad Case 4）；这里的重执行是**改变了输入的阶段回跳**——第二轮 research
  会发出第一轮没有发过的 fallback query，browse 拿到的是不同的候选集；
- 触发与否由运行时诊断信号决定，不是固定流程的一部分：10 个评测 topic 全部
  一次通过时，这段逻辑完全不执行，trace 里 `replan_metrics.triggered=false`。

### 不回归保证

- 触发条件挂在**失败路径**上：eval 集 10/10 成功的 case 根本不会进入这段代码；
- 唯一行为变化发生在 v1 里本来就要输出"资料不足"报告的 case 上——对这类 case，
  多一轮定向重检索只可能把结果从"失败"变成"成功"，不会反向；
- trace 新增 `replan_metrics`（triggered / reason / rounds），可观测。

## 4. 已知边界

- 只覆盖"browse 因候选不足失败"这一种场景；analyze/report 阶段失败仍走原有
  重试逻辑（它们的失败通常是 LLM 输出格式问题，重跳阶段没有意义）；
- 重检索轮仍复用 ResearchAgent 现有的 fallback query 模板，没有引入 LLM 动态
  改写 query（那是下一步，需要先有评测手段衡量改写质量）；
- `MAX_REPLAN_ROUNDS` 固定为 1，不可配置为无限。
