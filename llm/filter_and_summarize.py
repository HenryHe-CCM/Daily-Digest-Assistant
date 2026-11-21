"""
filter_and_summarize.py

用 LLM 对候选论文进行：
1. 相关性筛选：从 candidate_pool 中选出最相关的 top_n 篇
2. 中文一句话总结（summary_zh）：20-40 字，用于卡片摘要
3. 中文详细解读（detail_zh）：包括论文做了什么、核心方法、主要结论，
   用于邮件卡片展开区域

整体逻辑：
- 将所有候选论文标题+摘要打包成一个 prompt，让 LLM 一次性返回
  评分 + summary_zh + detail_zh（减少 API 调用次数）
- 关键词自动衍生三级优先级（_build_keyword_tiers），无需用户手动配置复合词
- temperature=0 保证每次输出确定，加 paper ID 次级排序保证同分稳定
- 支持多种 LLM 后端，通过 PROVIDER_REGISTRY 注册表驱动：
  Anthropic SDK（claude、minimax）和 OpenAI 兼容协议（openai、deepseek 等）
  均统一在注册表中配置，config.yml 填写 llm_provider 名称即可切换
"""

import json
import os
import time
from itertools import combinations


def filter_and_summarize_papers(
    papers: list[dict],
    keywords: list[str],
    top_n: int,
    llm_provider: str,
    api_key: str,
    min_score: int = 6,
    custom_llm: dict | None = None,
    prompt_profile: str = "ai",
    min_papers: int = 0,
) -> list[dict]:
    """
    筛选论文并生成中文总结。

    整体逻辑：
    - 构造批量 prompt，包含所有候选论文的 id / title / abstract
    - 要求 LLM 输出 JSON 列表：[{id, score, summary_zh, detail_zh}, ...]
    - 按 score 降序、paper ID 字典序次级排序（保证结果稳定）
    - 过滤掉 score < min_score 的论文，不强制凑满 top_n
    - 若全部低于门槛，兜底返回最高分 1 篇

    Args:
        papers: arxiv_fetcher 返回的候选论文列表
        keywords: 用于告知 LLM 关注方向，自动衍生优先级
        top_n: 最终保留篇数上限
        llm_provider: config.yml 中的 llm_provider 名称（见 PROVIDER_REGISTRY）
        api_key: 对应的 API key
        min_score: 最低相关性门槛，低于此分不推送，默认 6

    Returns:
        筛选后的论文列表（已附加 summary_zh / detail_zh 字段），按相关性排序
    """
    if not papers:
        return []

    prompt = _build_paper_prompt(papers, keywords, top_n, profile=prompt_profile)
    raw = ""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = _call_llm(prompt, llm_provider, api_key, custom_llm)
            break
        except Exception as e:
            last_error = e
            print(f"      [LLM] 第 {attempt + 1}/2 次调用失败: {type(e).__name__}: {e}",
                  flush=True)
            message = str(e).lower()
            if "401" in message or "authentication" in message or "invalid" in message:
                print("       ↳ 密钥被拒：确认 LLM_API_KEY 是 platform.deepseek.com 签发的"
                      " key，复制时没带入空格或换行，且账户有余额", flush=True)
            if attempt == 0:
                time.sleep(5)
    if not raw:
        # 不再用占位文案凑数发信：模型不可用就当本次推送失败，让 workflow 变红
        raise RuntimeError(f"模型调用失败，已中止本次推送：{last_error}") from last_error
    results = _parse_json_response(raw)
    print(f"      [LLM] 论文响应字符数={len(raw)}, 解析结果数={len(results)}", flush=True)
    if not results:
        raise RuntimeError("模型返回的内容无法解析为 JSON，已中止本次推送（避免发送空摘要邮件）")

    # 建立 id → paper 映射
    id_map = {p["id"]: p for p in papers}
    all_scored = []
    for item in results:
        pid = item.get("id")
        if pid in id_map:
            paper = id_map[pid].copy()
            paper["summary_zh"] = item.get("summary_zh", "")
            paper["detail_zh"]  = item.get("detail_zh", "")
            paper["score"]      = item.get("score", 0)
            all_scored.append(paper)

    print(f"      [LLM] 匹配论文数={len(all_scored)}", flush=True)

    # 稳定排序：score 降序，同分按 paper ID 字典序
    all_scored.sort(key=lambda x: (-x.get("score", 0), x.get("id", "")))

    # 按 min_score 门槛过滤，不强制凑满 top_n
    output = [p for p in all_scored if p.get("score", 0) >= min_score][:top_n]

    # 保持每日推送的篇数下限：门槛卡得太狠时，用剩余候选里评分最高的补齐
    if 0 < len(output) < min_papers:
        picked = {p["id"] for p in output}
        for paper in all_scored:
            if len(output) >= min_papers:
                break
            if paper["id"] not in picked:
                output.append(paper)
        print(f"      [LLM] 达标篇数不足，已从小分候选补至 {len(output)} 篇", flush=True)

    # 兜底：全部低于门槛时返回最高分 1 篇，避免空邮件
    if not output and all_scored:
        output = all_scored[:1]

    # 若 LLM 解析完全失败
    if not output:
        # 全部低分或模型没给出任何可用结果：同样视为失败，不发空邮件
        raise RuntimeError(
            f"模型未返回可用结果（评分均低于 min_score={min_score} 或 id 全部对不上），已中止推送"
        )

    return output


# ── Prompt builders ────────────────────────────────────────────

def _build_keyword_tiers(keywords: list[str]) -> tuple[list[str], list[str]]:
    """
    从用户配置的基础关键词自动衍生两级优先级，用于 prompt 评分标准。

    整体逻辑：
    - tier1（高优先级）：多词关键词（本身含空格）+ 单词关键词的两两组合复合短语
    - tier2（中优先级）：所有单词关键词

    例：keywords=["openclaw","agent","skill"]
        → tier1=["openclaw","agent skill","agent openclaw","skill openclaw"]
          tier2=["openclaw","agent","skill"]

    Args:
        keywords: config.yml 中的 keywords 列表

    Returns:
        (tier1_phrases, tier2_words)
    """
    multi  = [k for k in keywords if len(k.split()) > 1]
    singles = [k for k in keywords if len(k.split()) == 1]
    combos = [f"{a} {b}" for a, b in combinations(singles, 2)]
    tier1  = multi + combos + singles  # 专有词也进 tier1
    tier2  = singles
    return tier1, tier2


def _build_paper_prompt(papers: list[dict], keywords: list[str], top_n: int,
                        profile: str = "ai") -> str:
    tier1, tier2 = _build_keyword_tiers(keywords)
    tier1_str = "、".join(f'"{p}"' for p in tier1)
    tier2_str = "、".join(f'"{w}"' for w in tier2)
    all_kw_str = "、".join(keywords)

    items = []
    for p in papers:
        items.append(
            f'ID: {p["id"]}\n标题: {p["title"]}\n摘要: {p["abstract"][:400]}'
        )
    papers_text = "\n\n---\n\n".join(items)

    if profile == "clinical":
        clinical_items = []
        for p in papers:
            meta = []
            if p.get("journal"):
                meta.append(p["journal"])
            pub_types = p.get("pub_types") or []
            if pub_types:
                meta.append("/".join(pub_types[:3]))
            if p.get("published"):
                meta.append(str(p["published"]))
            head = " | ".join(meta)
            # 临床摘要多为结构式（Background/Methods/Results/Conclusions），结果与数字
            # 集中在后半段，截断会让模型误判"摘要未报告"——这里给全文。
            # 44 篇候选约 2.1 万 token，在 64k 上下文内仍有余量。
            clinical_items.append(
                f'ID: {p["id"]}\n标题: {p["title"]}\n'
                + (f'期刊/类型/日期: {head}\n' if head else "")
                + f'摘要: {p["abstract"]}'
            )
        clinical_text = "\n\n---\n\n".join(clinical_items)

        return f"""你是一位临床医学文献筛选助手，方向是重症医学（ICU / 急诊危重症）。
关注方向：{all_kw_str}

评分标准（严格按此执行，score 为 1-10 整数）：
- score=9-10：研究对象是重症或危重患者（ICU、抢救室、机械通气、休克），且主题直接命中 {tier1_str}
- score=7-8：主题明确命中 {tier2_str}，但研究人群或场景只是部分相关（如普通病房、择期手术、儿科）
- score=4-6：顺带提及相关词，或为纯机制 / 动物 / 体外研究，对床旁决策的转化价值有限
- score=1-3：几乎无关，仅因关键词宽泛被检索召回

筛选规则：
1. 纯体外实验或动物实验、且未报告临床结局的，score 不得超过 6
2. 关键词只出现在参考文献、勘误、致谢或基金信息里的，score 不得超过 3
3. 只输出评分最高的 {top_n} 篇，其余不输出

下面是从 PubMed 检索到的 {len(papers)} 篇候选文献。请为每篇在心里打分，然后**只输出**评分最高的 {top_n} 篇，并生成中文总结：
1. summary_zh：一句中文总结（30-50字），**必须先给结果或结论，再交代研究是什么**。
   - 正确：`脓毒症合并 AKI 患者中，CatBoost 预测谵妄的验证集 AUC 0.742，糖尿病与肺炎权重最高`
   - 正确：`SDD 与益生菌均未降低 ICU 死亡率，但 SDD 使肺炎风险下降约一半（RR 0.50）`
   - 错误：`比较选择性消化道去污与益生菌对 ICU 感染的影响`（只描述做了什么，没有结果）
   - 错误：`本研究探讨了肠内营养时机与谵妄的关系`（空话，且犯了这个项目明令禁止的句式）
2. detail_zh：详细中文解读（120-200字），按以下三点组织：
   - 研究设计与人群：RCT / 队列 / 病例对照 / 回顾性 / Meta 分析，样本量，纳入人群
   - 干预或暴露与主要结局：做了什么、对照是什么、主要终点结果（必须抄上效应量、置信区间或 p 值）
   - 结论与局限：作者结论，以及摘要中可见的局限（单中心、样本量小、观察性设计等）

硬性要求：
- 只能使用摘要中出现的信息，摘要没写的内容不要补；确实没有就给方向性结论，再注明"摘要未报告效应量"
- **结果和数字优先**：摘要里出现的 HR / OR / RR / 百分比 / 样本量 / p 值，必须至少引用一个到 detail_zh 里
- 数字必须与摘要一致，不得四舍五入或换算单位
- 不使用"显著改善""效果良好"这类空话，要指出改善的具体指标和幅度

**输出格式**（严格 JSON 数组，不要有其他文字，只输出 {top_n} 篇）：
[
  {{
    "id": "文献ID",
    "score": 相关性评分(1-10的整数),
    "summary_zh": "一句中文总结",
    "detail_zh": "详细中文解读"
  }},
  ...
]

候选文献：
{clinical_text}
"""

    return f"""你是一位 AI 研究领域的论文筛选助手。
关注方向：{all_kw_str}

评分标准（严格按此执行，score 为 1-10 整数）：
- score=9-10：标题或摘要核心内容直接涉及高优先级组合 {tier1_str}
- score=7-8：摘要明确以 {tier2_str} 为核心研究主题（不是顺带提及）
- score=4-6：论文泛泛使用上述词汇，但并非核心主题
- score=1-3：几乎无关，仅因关键词宽泛被搜索引擎召回

筛选规则：
1. 仅在 abstract 末尾 "future work" 中提到关键词的，score 不得超过 3
2. 只输出评分最高的 {top_n} 篇，其余不输出

下面是从 arxiv 搜索到的 {len(papers)} 篇候选论文。
请为每篇在心里打分，然后**只输出**评分最高的 {top_n} 篇，并生成中文总结：
1. summary_zh：一句精炼的中文总结（20-40字），用于卡片摘要
2. detail_zh：详细中文解读（100-150字），包含三点：
   - 论文做了什么（研究问题/方法）
   - 核心创新点
   - 主要结论或实验结果

**输出格式**（严格 JSON 数组，不要有其他文字，只输出 {top_n} 篇）：
[
  {{
    "id": "论文ID",
    "score": 相关性评分(1-10的整数),
    "summary_zh": "一句中文总结（仅高分论文填写，低分填空字符串）",
    "detail_zh": "详细中文解读（仅高分论文填写，低分填空字符串）"
  }},
  ...
]

候选论文：
{papers_text}
"""


# ── 内置 LLM 提供商注册表 ─────────────────────────────────────
#
# 设计思路：
# - 常用提供商内置在此，开箱即用
# - 用户也可在 config.yml 的 custom_llm 中自定义提供商，无需改代码
# - 内置 + 自定义合并成完整的 PROVIDER_REGISTRY
#
# registry 结构：provider_name → (sdk, base_url, model)
#   sdk: "anthropic" 或 "openai"
#   base_url: None 表示使用该 SDK 的官方默认地址

BUILTIN_PROVIDERS = {
    "openai":   ("openai",    None,                                         "gpt-4o-mini"),
    "minimax":  ("anthropic", "https://api.minimaxi.com/anthropic",         "minimax-m2.7"),
    "claude":   ("anthropic", None,                                         "claude-haiku-4-5-20251001"),
    "deepseek": ("openai",    "https://api.deepseek.com/v1",                "deepseek-chat"),
    "zhipu":    ("openai",    "https://open.bigmodel.cn/api/paas/v4",       "glm-4-flash"),
    "moonshot": ("openai",    "https://api.moonshot.cn/v1",                 "moonshot-v1-8k"),
    "qwen":     ("openai",    "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
}


def _build_registry(custom_llm: dict | None) -> dict:
    """
    合并内置提供商和用户自定义配置，生成完整的注册表。

    custom_llm 格式（来自 config.yml）：
        my_api:
          sdk: openai
          base_url: "https://my-api.com/v1"
          model: "gpt-4o"
    """
    registry = dict(BUILTIN_PROVIDERS)
    if not custom_llm:
        return registry

    for name, cfg in custom_llm.items():
        sdk = cfg.get("sdk", "openai")
        base_url = cfg.get("base_url") or None
        model = cfg.get("model")
        if not model:
            raise ValueError(f"自定义 LLM 配置 '{name}' 缺少必填字段 'model'")
        if sdk not in ("openai", "anthropic"):
            raise ValueError(f"自定义 LLM 配置 '{name}' 的 sdk 必须是 'openai' 或 'anthropic'，收到: '{sdk}'")
        registry[name] = (sdk, base_url, model)

    return registry


def _call_llm(prompt: str, provider: str, api_key: str, custom_llm: dict | None = None) -> str:
    """
    统一 LLM 调用入口。

    整体逻辑：
    - 合并内置提供商 + 用户自定义配置生成完整注册表
    - 从注册表查找 (sdk, base_url, model)
    - sdk == "anthropic" → 走 Anthropic SDK（Claude、MiniMax 等）
    - sdk == "openai"    → 走 OpenAI 兼容协议（OpenAI、DeepSeek 等）
    - 新增内置提供商：修改 BUILTIN_PROVIDERS
    - 新增自定义提供商：在 config.yml 的 custom_llm 中配置，无需改代码

    Args:
        prompt: 输入 prompt
        provider: config.yml 中的 llm_provider 名称
        api_key: 对应的 API key
        custom_llm: config.yml 中的 custom_llm 配置块（可选）

    Raises:
        ValueError: provider 不在注册表（内置或自定义）
    """
    registry = _build_registry(custom_llm)
    if provider not in registry:
        supported = ", ".join(registry.keys())
        raise ValueError(f"不支持的 llm_provider: '{provider}'，可选：{supported}")

    sdk, base_url, model = registry[provider]
    if sdk == "anthropic":
        return _call_anthropic(prompt, api_key, base_url, model)
    else:
        return _call_openai_compatible(prompt, api_key, base_url, model)


def _call_anthropic(prompt: str, api_key: str, base_url: str | None, model: str) -> str:
    """
    调用 Anthropic SDK。
    base_url=None 时使用官方地址（Claude），
    传入自定义 base_url 时可对接兼容 Anthropic 协议的第三方（如 MiniMax）。
    temperature=0 保证输出确定性。

    重试机制：遇到可重试错误（529 过载、429 限流、500 服务端错误、连接/超时）
    时最多重试 5 次，指数退避等待。不可重试错误（401 认证、400 参数等）直接抛出。
    """
    import time
    import anthropic

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)

    max_retries = 5
    wait_times = [30, 60, 90, 120, 180]  # 指数退避：30s, 60s, 90s, 120s, 180s

    # 可重试异常类型：529 过载、429 限流、500+ 服务端错误、连接/超时
    retryable_exceptions = (
        anthropic._exceptions.OverloadedError,      # 529
        anthropic._exceptions.RateLimitError,       # 429
        anthropic._exceptions.InternalServerError,   # 500+
        anthropic._exceptions.ServiceUnavailableError,  # 503
        anthropic._exceptions.APIConnectionError,   # 网络连接
        anthropic._exceptions.APITimeoutError,      # 请求超时
    )

    # GLM-5.3 强制思考；线上已出现 8192 tokens 全用于 thinking、正文为空。
    output_budget = 16384 if model == "glm-5.3" else 8192

    for attempt in range(max_retries):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=output_budget,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "\n".join(block.text for block in msg.content if block.type == "text")
            print(f"      [LLM] model={model}, stop_reason={msg.stop_reason}, "
                  f"output_tokens={msg.usage.output_tokens}, max_tokens={output_budget}, "
                  f"content_types={[block.type for block in msg.content]}, text_chars={len(text)}",
                  flush=True)
            return text
        except retryable_exceptions as e:
            if attempt < max_retries - 1:
                wait_time = wait_times[attempt]
                err_type = type(e).__name__
                print(f"      [LLM] API {err_type}，{wait_time}秒后重试 ({attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise


def _call_openai_compatible(prompt: str, api_key: str, base_url: str | None, model: str) -> str:
    """
    调用 OpenAI 兼容协议。
    base_url=None 时使用 OpenAI 官方地址。
    temperature=0 保证输出确定性。

    重试机制：遇到可重试错误时最多重试 5 次，指数退避等待。
    SDK 自带 2 次重试，此处额外补充更长的退避间隔。
    """
    import time
    from openai import OpenAI
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    max_retries = 5
    wait_times = [30, 60, 90, 120, 180]

    # 可重试异常类型：429 限流、500+ 服务端错误、连接/超时
    from openai import APIError as _OpenAIAPIError
    retryable_exceptions = (
        getattr(_OpenAIAPIError, 'RateLimitError', type('NotFound', (), {})),
        getattr(_OpenAIAPIError, 'APIConnectionError', type('NotFound', (), {})),
        getattr(_OpenAIAPIError, 'APITimeoutError', type('NotFound', (), {})),
        getattr(_OpenAIAPIError, 'InternalServerError', type('NotFound', (), {})),
    )
    # 过滤掉占位类
    retryable_exceptions = tuple(e for e in retryable_exceptions if e.__name__ != 'NotFound')

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content
        except retryable_exceptions as e:
            if attempt < max_retries - 1:
                wait_time = wait_times[attempt]
                err_type = type(e).__name__
                print(f"      [LLM] API {err_type}，{wait_time}秒后重试 ({attempt+1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise


# ── JSON 解析 ──────────────────────────────────────────────────

def _parse_json_response(raw: str) -> list[dict]:
    """从 LLM 响应中提取 JSON 数组，容错处理代码块包裹。"""
    import re
    # 去掉 ```json ... ``` 包裹
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 尝试提取第一个 [ ... ] 块
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return []
