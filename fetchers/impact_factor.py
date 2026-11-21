"""期刊影响因子查询：走 OpenAlex 公开接口。

**口径声明（重要）**：Clarivate 的 JCR 影响因子是商业数据，没有公开 API，
本模块取 OpenAlex 的 ``summary_stats.2yr_mean_citedness``——同一本期刊近两年
发表论文的平均被引次数。它与 JIF 的定义思路一致、数值相近但不相等，
界面上必须标注来源，不能冒充官方 JCR 影响因子。

查询按 ISSN 批量进行（一次最多 50 个），结果落本地缓存，进程内也做 memo。
"""

from __future__ import annotations

import json
import os
import time

import requests

OPENALEX_URL = "https://api.openalex.org/sources"
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", ".if-cache.json")
BATCH = 40
USER_AGENT = "Daily-Digest-Assistant/1.0 (impact-factor lookup)"


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass  # 缓存写不了不影响主流程（CI 上每次都是冷启动）


def _fetch_batch(issns: list[str], session: requests.Session) -> dict[str, dict]:
    params = {
        "filter": "issn:" + "|".join(issns),
        "per-page": 200,
        "select": "display_name,issn_l,issn,summary_stats,cited_by_count,works_count",
    }
    response = session.get(OPENALEX_URL, params=params, timeout=(10, 60))
    response.raise_for_status()
    found: dict[str, dict] = {}
    for item in response.json().get("results", []):
        stats = item.get("summary_stats") or {}
        impact = stats.get("2yr_mean_citedness")
        record = {
            "name": item.get("display_name") or "",
            "impact_factor": round(float(impact), 1) if impact else None,
            "h_index": stats.get("h_index"),
            "works_count": item.get("works_count"),
        }
        keys = set(item.get("issn") or [])
        if item.get("issn_l"):
            keys.add(item["issn_l"])
        for issn in keys:
            found[issn] = record
    return found


def get_impact_factors(issns: list[str], use_cache: bool = True) -> dict[str, dict]:
    """按 ISSN 查期刊指标。返回 {issn: {name, impact_factor, h_index, works_count}}。

    查不到的 ISSN 不会出现在返回值里，调用方需自行降级显示。
    """
    wanted = []
    for issn in issns:
        issn = (issn or "").strip()
        if issn and issn not in wanted:
            wanted.append(issn)
    if not wanted:
        return {}

    cache = _load_cache() if use_cache else {}
    result = {issn: cache[issn] for issn in wanted if issn in cache}
    todo = [issn for issn in wanted if issn not in result]
    if not todo:
        return result

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        for start in range(0, len(todo), BATCH):
            chunk = todo[start:start + BATCH]
            for attempt in range(3):
                try:
                    result.update(_fetch_batch(chunk, session))
                    break
                except requests.exceptions.RequestException as exc:
                    if attempt == 2:
                        print(f"      [期刊指标] 查询失败（{type(exc).__name__}），"
                              f"本批 {len(chunk)} 个 ISSN 降级为无指标", flush=True)
                    else:
                        time.sleep(3 * (attempt + 1))
            time.sleep(0.3)

    if use_cache:
        cache.update(result)
        _save_cache(cache)
    return result
