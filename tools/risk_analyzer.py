"""Rule-based risk keyword detection and categorization."""
from utils.text_utils import split_sentences

_RISK_CATEGORIES = {
    "market_risk": ["市场竞争", "需求下滑", "价格战", "行业周期", "市场波动", "订单减少"],
    "financial_risk": ["负债", "现金流紧张", "偿债", "商誉减值", "应收账款", "融资风险"],
    "operation_risk": ["产能", "供应链", "生产事故", "管理层变动", "经营不善", "库存积压"],
    "policy_risk": ["监管", "政策", "补贴退坡", "关税", "合规", "政策变化"],
    "technology_risk": ["技术路线", "技术迭代", "研发失败", "专利", "技术壁垒", "被替代"],
}

_GENERIC_RISK_HINTS = ["风险", "不确定性", "警示"]


def detect_risks(text: str) -> dict:
    """Classify risk-related sentences into 5 categories using keyword rules.

    Returns {category: [sentences...]} plus an "uncategorized" bucket for
    sentences that mention risk generically without matching a category.
    """
    result = {cat: [] for cat in _RISK_CATEGORIES}
    result["uncategorized"] = []

    if not text:
        return result

    for sentence in split_sentences(text):
        matched = False
        for cat, keywords in _RISK_CATEGORIES.items():
            if any(kw in sentence for kw in keywords):
                result[cat].append(sentence)
                matched = True
        if not matched and any(h in sentence for h in _GENERIC_RISK_HINTS):
            result["uncategorized"].append(sentence)

    return result
