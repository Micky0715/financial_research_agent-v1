# 来源权威性分级报告（source authority tiers）

对每个 eval topic 最新一次 run 的入选来源做 tier 分级（tools/source_tier.py）。
**分级是启发式的**：tier1 不代表内容绝对正确，tier3 不代表内容一定差；分级刻画的是来源性质。

| topic | 来源数 | tier1 | tier2 | tier3 | unknown | tier1+2 占比 | authority_score(eval) |
|---|---|---|---|---|---|---|---|
| 宁德时代投资分析 | 5 | 0 | 3 | 2 | 0 | 0.60 | 0.62 |
| 比亚迪投资价值分析 | 5 | 2 | 1 | 2 | 0 | 0.60 | 0.82 |
| 贵州茅台财务与估值分析 | 5 | 0 | 0 | 5 | 0 | 0.00 | 0.7 |
| 中芯国际投资风险分析 | 5 | 0 | 0 | 5 | 0 | 0.00 | 0.62 |
| 隆基绿能行业地位分析 | 5 | 2 | 1 | 2 | 0 | 0.60 | 0.82 |
| AI机器人行业研报 | 2 | 0 | 0 | 2 | 0 | 0.00 | 0.3 |
| 半导体国产替代行业研究 | 5 | 0 | 1 | 4 | 0 | 0.20 | 0.7 |
| 光伏行业投资风险分析 | 5 | 0 | 2 | 3 | 0 | 0.40 | 0.62 |
| 低空经济行业研报 | 5 | 3 | 1 | 1 | 0 | 0.80 | 0.88 |
| 新能源汽车产业链分析 | 5 | 0 | 2 | 3 | 0 | 0.40 | 0.62 |

## 总体分布

- 来源总数: 47
- tier1: 7（14.9%）
- tier2: 11（23.4%）
- tier3: 29（61.7%）
- unknown: 0（0.0%）
- **overall tier1_or_tier2_ratio: 0.383**

## authority_score 与 tier 分布的关系

evaluation 里的 authority_score 由 tier1=1.0 / tier2=0.7 / 白名单=0.5 / 其他=0.3 加权平均而来，
所以 tier1+2 占比高的 topic authority_score 必然高——两者是同一信号的两种呈现，不是独立验证。

## 低权威来源占比较高的 case

- **贵州茅台财务与估值分析**（tier1+2 占比 0.00）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, guba.eastmoney.com, xueqiu.com
- **中芯国际投资风险分析**（tier1+2 占比 0.00）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, xueqiu.com, testtoo1.oss-cn-hangzhou.aliyuncs.com
- **AI机器人行业研报**（tier1+2 占比 0.00）：tier3 域名 robot.ofweek.com, reportify.cn
- **半导体国产替代行业研究**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, xueqiu.com
- **光伏行业投资风险分析**（tier1+2 占比 0.40）：tier3 域名 xueqiu.com, xueqiu.com, baogao.chinabaogao.com
- **新能源汽车产业链分析**（tier1+2 占比 0.40）：tier3 域名 xueqiu.com, xueqiu.com, m.pedaily.cn

典型模式：行业类主题（市场空间/产业链分析）的优质内容多在行业媒体和自媒体上，
官方公告类 tier1 来源天然少——低 tier 占比不一定是检索质量问题，需结合内容相关性判断。
