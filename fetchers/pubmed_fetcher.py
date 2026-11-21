"""PubMed 文献检索：面向临床 / 重症医学方向的每日文献源。

arXiv 以计算机与数理为主，临床医学文献基本不在其中。本模块走 PubMed 官方
E-utilities（esearch + efetch），无 key 可用；Europe PMC 作为备用源，覆盖同一
批 PubMed 记录并可选纳入 medRxiv / bioRxiv 预印本（SRC:PPR）。

返回契约与 semantic_scholar_fetcher 保持一致，供 main.py 在 paper_source
设为 pubmed 时直接调用。
"""

from __future__ import annotations

import re
import time
from datetime import date
from xml.etree import ElementTree as ET

import requests

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_PAGE_SIZE = 100
USER_AGENT = "Daily-Digest-Assistant/1.0 (pubmed source)"
PREPRINT_SOURCES = {"PPR"}
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── 通用工具 ───────────────────────────────────────────────────

def _phrase(term: str) -> str:
    """压成短语查询；含空格的加引号避免被拆词。"""
    term = " ".join(term.split()).replace('"', " ")
    return f'"{term}"' if " " in term else term


def _matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [kw for kw in keywords if kw and kw.lower() in lowered]


def _ncbi_get(session: requests.Session, path: str, params: dict,
              attempts: int = 3) -> requests.Response:
    """NCBI 无 key 时限 3 请求/秒，本流程每次运行只有寥寥数请求；仍做退避重试。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(f"{NCBI_BASE}/{path}", params=params, timeout=(10, 60))
        except requests.exceptions.RequestException as exc:
            last_error = exc
            print(f"      [PubMed] 第 {attempt + 1}/{attempts} 次请求异常："
                  f"{type(exc).__name__}", flush=True)
            time.sleep(3 * (attempt + 1))
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            last_error = None
            print(f"      [PubMed] HTTP {response.status_code}，"
                  f"第 {attempt + 1}/{attempts} 次重试", flush=True)
            time.sleep(3 * (attempt + 1))
            continue
        response.raise_for_status()
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("PubMed 连续重试仍失败")


# ── NCBI E-utilities（主源）────────────────────────────────────

def build_ncbi_term(
    keywords: list[str],
    start_date: date,
    end_date: date,
    journal_filter: list[str] | None = None,
) -> str | None:
    """关键词限定在 [Title/Abstract]，日期用 Entrez 收录日期 [EDAT]。

    用 EDAT 而不是出版日期：每日推送关心的是"今天新进库的文献"，
    出版日期可能落后数周（在线优先），拿它做日更会反复漏推。
    """
    terms = [f"{_phrase(k)}[Title/Abstract]" for k in keywords if k and k.strip()]
    if not terms:
        return None

    parts = [
        "(" + " OR ".join(terms) + ")",
        f'("{start_date:%Y/%m/%d}"[EDAT] : "{end_date:%Y/%m/%d}"[EDAT])',
    ]

    journals = [j.strip() for j in (journal_filter or []) if j and j.strip()]
    if journals:
        parts.append("(" + " OR ".join(f'"{j}"[Journal]' for j in journals) + ")")

    return " AND ".join(parts)


def _text(node: ET.Element | None) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _parse_pubmed_date(article: ET.Element) -> str:
    """优先取电子发表日期（ArticleDate），回退到 JournalIssue 的 PubDate。"""
    for node in (article.find(".//Article/ArticleDate"),
                 article.find(".//JournalIssue/PubDate")):
        if node is None:
            continue
        year = _text(node.find("Year"))
        if not year:
            medline = _text(node.find("MedlineDate"))
            year_match = re.search(r"(\d{4})", medline)
            if not year_match:
                continue
            year = year_match.group(1)
        month_raw = _text(node.find("Month")).strip().lower()
        if month_raw.isdigit():
            month = int(month_raw)
        else:
            month = MONTHS.get(month_raw[:3], 1)
        day_raw = _text(node.find("Day")).strip()
        day = int(day_raw) if day_raw.isdigit() else 1
        return f"{int(year):04d}-{month:02d}-{day:02d}"
    return ""


def _parse_pubmed_article(article: ET.Element, keywords: list[str]) -> dict | None:
    title = " ".join(_text(article.find(".//Article/ArticleTitle")).split())
    abstract = " ".join(
        _text(node) for node in article.findall(".//Abstract/AbstractText")
    ).strip()
    if not title or not abstract:
        return None

    pmid = _text(article.find(".//MedlineCitation/PMID"))
    doi = ""
    pmcid = ""
    for article_id in article.findall(".//ArticleIdList/ArticleId"):
        id_type = (article_id.get("IdType") or "").lower()
        if id_type == "doi" and not doi:
            doi = _text(article_id)
        elif id_type in {"pmc", "pmcid"} and not pmcid:
            pmcid = _text(article_id)

    if pmid:
        paper_id = f"PMID:{pmid}"
    elif doi:
        paper_id = f"DOI:{doi}"
    else:
        return None

    journal = _text(article.find(".//Journal/Title"))
    pub_types = [_text(pt) for pt in article.findall(".//PublicationTypeList/PublicationType")]

    authors: list[str] = []
    for author in article.findall(".//AuthorList/Author")[:5]:
        last = _text(author.find("LastName"))
        initials = _text(author.find("Initials"))
        collective = _text(author.find("CollectiveName"))
        if last:
            authors.append(f"{last} {initials}".strip())
        elif collective:
            authors.append(collective)

    # 临床场景优先给 PubMed 链接：doi.org 常跳到出版社付费墙，医院网络也常封
    # 出版社域名；PubMed 页面免费显示摘要与 MeSH，更适合每天快速过一遍
    url = (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid
           else f"https://doi.org/{doi}")

    return {
        "id": paper_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "url": url,
        "pdf_url": (f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None),
        "published": _parse_pubmed_date(article),
        "categories": [journal] if journal else [],
        "matched_keywords": _matched_keywords(title + " " + abstract, keywords),
        "source": "PubMed",
        "link_label": "PubMed",
        "journal": journal,
        "pub_types": pub_types,
        "cited_by": None,
    }


def fetch_ncbi_papers(
    keywords: list[str],
    candidate_pool: int,
    start_date: date,
    end_date: date,
    journal_filter: list[str] | None = None,
) -> list[dict]:
    """esearch 拿 PMID 列表，再 efetch 取题录与摘要。"""
    term = build_ncbi_term(keywords, start_date, end_date, journal_filter)
    if not term or candidate_pool <= 0:
        return []

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        search_params = {
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": min(candidate_pool, 200),
            "sort": "pub_date",
            "tool": "daily-digest-assistant",
        }
        search = _ncbi_get(session, "esearch.fcgi", search_params).json()
        result = search.get("esearchresult") or {}
        if result.get("ERROR"):
            raise RuntimeError(f"PubMed 检索式被拒绝：{result['ERROR']}")
        ids = result.get("idlist") or []
        print(f"      [PubMed] 命中 {result.get('count', 0)} 篇，取 {len(ids)} 篇进入评分",
              flush=True)
        if not ids:
            return []

        time.sleep(0.4)  # 遵守无 key 时 3 请求/秒的限制
        fetch = _ncbi_get(session, "efetch.fcgi", {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
            "rettype": "abstract",
            "tool": "daily-digest-assistant",
        })

    try:
        root = ET.fromstring(fetch.content)
    except ET.ParseError as exc:
        raise RuntimeError(f"PubMed 返回的 XML 无法解析：{exc}") from exc

    papers: list[dict] = []
    seen: set[str] = set()
    for article in root.findall("PubmedArticle"):
        paper = _parse_pubmed_article(article, keywords)
        if not paper or paper["id"] in seen:
            continue
        if not paper["matched_keywords"]:
            continue
        seen.add(paper["id"])
        papers.append(paper)
    return papers


# ── Europe PMC（备用源，含预印本）──────────────────────────────

def build_epmc_query(
    keywords: list[str],
    start_date: date,
    end_date: date,
    include_preprints: bool = False,
    journal_filter: list[str] | None = None,
) -> str | None:
    """关键词限定 TITLE_ABS：Europe PMC 默认连开放获取全文一起搜，
    不限定字段会把正文里顺带提一句的无关文献（牲畜、肿瘤等）带进候选。"""
    terms = [f"TITLE_ABS:{_phrase(k)}" for k in keywords if k and k.strip()]
    if not terms:
        return None

    sources = ["MED"] + (["PPR"] if include_preprints else [])
    parts = [
        "(" + " OR ".join(terms) + ")",
        f"(FIRST_PDATE:[{start_date.isoformat()} TO {end_date.isoformat()}])",
        "(" + " OR ".join(f"SRC:{s}" for s in sources) + ")",
    ]

    journals = [j.strip() for j in (journal_filter or []) if j and j.strip()]
    if journals:
        parts.append("(" + " OR ".join(f'JOURNAL:"{j}"' for j in journals) + ")")

    return " AND ".join(parts)


def _epmc_request(session: requests.Session, params: dict, attempts: int = 3) -> dict:
    """Europe PMC 偶发 500/503 与读超时，退避重试后仍失败则由调用方降级。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(EPMC_URL, params=params, timeout=(10, 90))
        except requests.exceptions.RequestException as exc:
            last_error = exc
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            last_error = None
            time.sleep(5 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()

    if last_error is not None:
        raise last_error
    raise RuntimeError("Europe PMC 连续重试仍失败")


def _epmc_to_paper(item: dict, keywords: list[str]) -> dict | None:
    title = " ".join((item.get("title") or "").split())
    abstract = " ".join((item.get("abstractText") or "").split())
    if not title or not abstract:
        return None

    published = (
        item.get("firstPublicationDate")
        or item.get("electronicPublicationDate")
        or (item.get("journalInfo") or {}).get("printPublicationDate")
        or ""
    )[:10]
    if not published:
        return None

    pmid = (item.get("pmid") or "").strip()
    doi = (item.get("doi") or "").strip()
    pmcid = (item.get("pmcid") or "").strip()
    record_id = str(item.get("id") or "")

    paper_id = (f"PMID:{pmid}" if pmid else
                (f"DOI:{doi}" if doi else f"EPMC:{record_id}"))

    if pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        url = f"https://europepmc.org/article/{item.get('source', 'MED')}/{record_id}"

    is_preprint = (item.get("source") or "") in PREPRINT_SOURCES
    authors = [
        a.strip()
        for a in (item.get("authorString") or "").rstrip(".").split(",")
        if a.strip()
    ][:5]

    return {
        "id": paper_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "url": url,
        "pdf_url": f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None,
        "published": published,
        "categories": [item.get("journalTitle")] if item.get("journalTitle") else [],
        "matched_keywords": _matched_keywords(title + " " + abstract, keywords),
        "source": "Preprint" if is_preprint else "PubMed",
        "link_label": "预印本" if is_preprint else "PubMed",
        "journal": item.get("journalTitle") or "",
        "pub_types": (item.get("pubTypeList") or {}).get("pubType") or [],
        "cited_by": item.get("citedByCount"),
    }


def fetch_epmc_papers(
    keywords: list[str],
    candidate_pool: int,
    start_date: date,
    end_date: date,
    include_preprints: bool = False,
    journal_filter: list[str] | None = None,
) -> list[dict]:
    query = build_epmc_query(
        keywords, start_date, end_date,
        include_preprints=include_preprints,
        journal_filter=journal_filter,
    )
    if not query or candidate_pool <= 0:
        return []

    papers: list[dict] = []
    seen: set[str] = set()
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": min(EPMC_PAGE_SIZE, candidate_pool),
        "sort": "P_PDATE_D desc",
        "cursorMark": "*",
    }

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        for _ in range(6):
            data = _epmc_request(session, params)
            results = (data.get("resultList") or {}).get("result") or []
            for item in results:
                paper = _epmc_to_paper(item, keywords)
                if not paper or paper["id"] in seen or not paper["matched_keywords"]:
                    continue
                seen.add(paper["id"])
                papers.append(paper)
                if len(papers) >= candidate_pool:
                    return papers

            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == params["cursorMark"]:
                break
            params["cursorMark"] = next_cursor
            time.sleep(0.5)

    return papers


# ── 对外入口：NCBI 优先，失败降级 Europe PMC ──────────────────

def fetch_pubmed_papers(
    keywords: list[str],
    candidate_pool: int,
    start_date: date,
    end_date: date,
    include_preprints: bool = False,
    journal_filter: list[str] | None = None,
) -> list[dict]:
    """返回统一结构的候选文献；主源失败自动降级，两个都失败才抛错。"""
    errors: list[str] = []

    try:
        papers = fetch_ncbi_papers(
            keywords, candidate_pool, start_date, end_date, journal_filter,
        )
        if papers:
            print(f"      [PubMed] 主源 NCBI 返回 {len(papers)} 篇", flush=True)
            return papers
        print("      [PubMed] NCBI 零结果，尝试备用源 Europe PMC", flush=True)
    except Exception as exc:
        errors.append(f"NCBI: {type(exc).__name__}: {exc}")
        print(f"      [PubMed] 主源 NCBI 失败（{type(exc).__name__}），降级 Europe PMC",
              flush=True)

    try:
        papers = fetch_epmc_papers(
            keywords, candidate_pool, start_date, end_date,
            include_preprints=include_preprints, journal_filter=journal_filter,
        )
        if papers:
            print(f"      [PubMed] 备用源 Europe PMC 返回 {len(papers)} 篇", flush=True)
            return papers
        errors.append("Europe PMC: 零结果")
    except Exception as exc:
        errors.append(f"Europe PMC: {type(exc).__name__}: {exc}")

    if errors:
        raise RuntimeError("；".join(errors))
    return []
