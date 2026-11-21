"""证据等级判定与最终排序。

日报的排序要求是「按影响因子、研究水平高低」，两个维度都不该由模型自由发挥，
否则同一批文献每天排在什么位置会飘。这里用确定性的规则：

- 研究水平：由 PubMed 的 PublicationType 加摘要关键词归类为 1-5 级
- 影响因子：由 OpenAlex 的 2 年平均被引提供（见 fetchers/impact_factor.py）

最后按加权分数排序，模型给出的相关性评分只用于同分时的次级排序。
"""

from __future__ import annotations

import math

# PubMed PublicationType → 证据等级（5 最高）
TYPE_LEVELS = {
    "meta-analysis": (5, "系统评价/Meta"),
    "systematic review": (5, "系统评价/Meta"),
    "practice guideline": (5, "指南"),
    "guideline": (5, "指南"),
    "randomized controlled trial": (4, "RCT"),
    "clinical trial": (3, "临床试验"),
    "observational study": (3, "队列/观察"),
    "comparative study": (3, "对照研究"),
    "case reports": (1, "病例报告"),
    "review": (2, "综述"),
    "editorial": (1, "评论"),
    "comment": (1, "评论"),
    "letter": (1, "通信"),
    "news": (1, "新闻"),
}

# 摘要里的研究设计线索（PubMed 常只给 "Journal Article"，需要从摘要判断）
DESIGN_MARKERS = {
    "队列/观察": ("cohort", "observational", "retrospective", "prospective",
                  "case-control", "cross-sectional", "registry", "nationwide"),
    "动物/体外": ("mice", "mouse", "murine", "rats", "rat ", "porcine", "rabbit",
                  "in vitro", "cell culture", "organoid", "zebrafish"),
}

# 综述的自我陈述：部分期刊不把综述标成 Review，只能从摘要句式识别
REVIEW_PHRASES = (
    "this review", "we review", "in this review", "this narrative review",
    "this article reviews", "we summarize", "this overview", "we discuss",
    "current evidence", "literature review",
)

IF_CAP = 20.0  # 影响因子归一化上限：重症领域顶刊（ICM/AJRCCM）在 20-30 区间


def classify_evidence(pub_types: list[str], title: str, abstract: str) -> tuple[int, str]:
    """返回 (等级 1-5, 中文标签)。规则优先，宁可保守也不夸大。

    判定顺序很关键：先认 PubMed 明确的文献类型，再看摘要里的设计线索。
    反过来会把叙述性综述误判成原始研究——综述里常出现 "observational data"
    "cohort studies have shown" 这类表述。
    """
    types = [str(t).strip().lower() for t in (pub_types or [])]
    text = f"{title} {abstract}".lower()

    # 1) 文献类型里已经明确是高质量研究的，直接采信
    best_high: tuple[int, str] | None = None
    for pub_type in types:
        if pub_type in TYPE_LEVELS:
            level, label = TYPE_LEVELS[pub_type]
            if level >= 3 and (best_high is None or level > best_high[0]):
                best_high = (level, label)
    if best_high:
        return best_high

    # 2) 动物 / 体外研究：没有临床试验标签时降为最低档
    animal = any(marker in text for marker in DESIGN_MARKERS["动物/体外"])
    clinical_trial = any("trial" in t for t in types)
    if animal and not clinical_trial:
        return 1, "动物/体外"

    # 3) 综述类（含叙述性综述）：必须先于摘要设计线索判定
    if any(TYPE_LEVELS.get(t, (0, ""))[0] == 2 for t in types):
        return 2, "综述"
    if any(phrase in text for phrase in REVIEW_PHRASES):
        return 2, "综述"

    # 4) 类型只写了 Journal Article 时，才从摘要推断研究设计
    if any(marker in text for marker in DESIGN_MARKERS["队列/观察"]):
        return 3, "队列/观察"

    # 5) 病例报告 / 评论 / 通信
    for pub_type in types:
        if TYPE_LEVELS.get(pub_type, (0, ""))[0] == 1:
            return 1, TYPE_LEVELS[pub_type][1]

    return 2, "其他研究"


def composite_score(evidence_level: int, impact_factor: float | None,
                    weights: dict | None = None) -> float:
    """加权分数：证据等级与影响因子各占一半，影响因子取对数后归一化。"""
    weights = weights or {}
    w_evidence = float(weights.get("evidence", 0.5))
    w_if = float(weights.get("impact_factor", 0.5))

    evidence_norm = max(0.0, min(1.0, (evidence_level - 1) / 4.0))
    if impact_factor and impact_factor > 0:
        if_norm = math.log1p(min(impact_factor, IF_CAP)) / math.log1p(IF_CAP)
    else:
        if_norm = 0.0

    return w_evidence * evidence_norm + w_if * if_norm


def rank_papers(papers: list[dict], weights: dict | None = None) -> list[dict]:
    """就地补上 evidence_level / evidence_label / impact_factor / rank_score 并排序。

    影响因子缺失的文献不淘汰，只是归一化项记 0，仍按证据等级参与排序。
    """
    for paper in papers:
        level, label = classify_evidence(
            paper.get("pub_types") or [],
            paper.get("title") or "",
            paper.get("abstract") or "",
        )
        paper["evidence_level"] = level
        paper["evidence_label"] = label
        paper["rank_score"] = composite_score(level, paper.get("impact_factor"), weights)

    papers.sort(key=lambda p: (
        -p.get("rank_score", 0.0),
        -p.get("evidence_level", 0),
        -(p.get("impact_factor") or 0.0),
        -p.get("score", 0),          # 模型相关性评分：仅作同分次级排序
        p.get("id", ""),
    ))
    for index, paper in enumerate(papers, 1):
        paper["rank"] = index
    return papers
