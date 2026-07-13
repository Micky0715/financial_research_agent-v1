# 来源权威性分级报告（source authority tiers）

对每个 eval topic 最新一次 run 的入选来源做 tier 分级（tools/source_tier.py）。
**分级是启发式的**：tier1 不代表内容绝对正确，tier3 不代表内容一定差；分级刻画的是来源性质。

| topic | 来源数 | tier1 | tier2 | tier3 | unknown | tier1+2 占比 | authority_score(eval) |
|---|---|---|---|---|---|---|---|
| 宁德时代投资分析 | 5 | 0 | 1 | 4 | 0 | 0.20 | 0.62 |
| 比亚迪投资价值分析 | 5 | 1 | 2 | 2 | 0 | 0.60 | 0.76 |
| 贵州茅台财务与估值分析 | 5 | 0 | 4 | 1 | 0 | 0.80 | 0.7 |
| 中芯国际投资风险分析 | 5 | 1 | 0 | 4 | 0 | 0.20 | 0.68 |
| 隆基绿能行业地位分析 | 5 | 1 | 0 | 4 | 0 | 0.20 | 0.76 |
| AI机器人行业研报 | 5 | 0 | 0 | 5 | 0 | 0.00 | 0.3 |
| 半导体国产替代行业研究 | 5 | 0 | 1 | 4 | 0 | 0.20 | 0.7 |
| 光伏行业投资风险分析 | 5 | 0 | 1 | 4 | 0 | 0.20 | 0.54 |
| 低空经济行业研报 | 5 | 2 | 3 | 0 | 0 | 1.00 | 0.82 |
| 新能源汽车产业链分析 | 5 | 2 | 2 | 1 | 0 | 0.80 | 0.82 |

## 总体分布

- 来源总数: 50
- tier1: 7（14.0%）
- tier2: 14（28.0%）
- tier3: 29（58.0%）
- unknown: 0（0.0%）
- **overall tier1_or_tier2_ratio: 0.420**

## authority_score 与 tier 分布的关系

evaluation 里的 authority_score 由 tier1=1.0 / tier2=0.7 / 白名单=0.5 / 其他=0.3 加权平均而来，
所以 tier1+2 占比高的 topic authority_score 必然高——两者是同一信号的两种呈现，不是独立验证。

## 低权威来源占比较高的 case

- **宁德时代投资分析**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, news.qq.com, xueqiu.com
- **中芯国际投资风险分析**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, news.qq.com
- **隆基绿能行业地位分析**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, xueqiu.com
- **AI机器人行业研报**（tier1+2 占比 0.00）：tier3 域名 sohu.com, robotsci.com.cn, 36kr.com, qianzhan.com, bg.qianzhan.com
- **半导体国产替代行业研究**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, xueqiu.com, xueqiu.com
- **光伏行业投资风险分析**（tier1+2 占比 0.20）：tier3 域名 xueqiu.com, xueqiu.com, time-weekly.com, baogao.chinabaogao.com

典型模式：行业类主题（市场空间/产业链分析）的优质内容多在行业媒体和自媒体上，
官方公告类 tier1 来源天然少——低 tier 占比不一定是检索质量问题，需结合内容相关性判断。
