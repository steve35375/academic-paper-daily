#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Academic Paper Daily - multi-database literature search and email delivery.

Supported sources:
  - PubMed
  - arXiv
  - OpenAlex

The program intentionally uses HTTP APIs directly so that it can run on a
standard GitHub Actions Python runner without a local server.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
import yaml


APP_NAME = "AcademicPaperDaily/1.0"
DEFAULT_TIMEOUT = 30
UA = f"{APP_NAME} (+https://github.com/)"
LOG = logging.getLogger(APP_NAME)


@dataclass
class Paper:
    """Normalized paper object shared by all source adapters."""

    uid: str
    title: str
    authors: list[str]
    publication: str = ""
    published_date: str = ""
    doi: str = ""
    pmid: str = ""
    arxiv_id: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""
    impact_factor: str = ""
    matched_query: str = ""

    @property
    def dedup_id(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower().strip()}"
        if self.pmid:
            return f"pmid:{self.pmid.strip()}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower().strip()}"
        return f"uid:{self.uid.lower().strip()}"

    @property
    def journal_key(self) -> str:
        return normalize_text(self.publication)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def html_text(value: str) -> str:
    return html.escape(value or "", quote=True)


def iso_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def time_window(cfg: dict[str, Any]) -> tuple[datetime, datetime]:
    """Resolve the configured search interval.

    Mode:
      - hours: rolling N-hour window ending now.
      - days: rolling N-day window ending now.
      - start/end: explicit ISO 8601 datetimes (UTC recommended).
    """
    search_cfg = cfg["search"]
    now = iso_now()

    if search_cfg.get("start"):
        start = parse_iso_date(str(search_cfg["start"]))
        end = parse_iso_date(str(search_cfg.get("end") or now.isoformat()))
        return start, end

    hours = float(search_cfg.get("hours", 24))
    if hours <= 0:
        raise ValueError("search.hours must be > 0")
    return now - timedelta(hours=hours), now


def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"GET JSON failed: {url}") from last_exc


def request_text(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"GET text failed: {url}") from last_exc


# --------------------------- Boolean filtering ---------------------------

TOKEN_RE = re.compile(
    r'"[^"]+"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+',
    flags=re.IGNORECASE,
)


def tokenize_boolean(expr: str) -> list[str]:
    return TOKEN_RE.findall(expr or "")


def boolean_terms(expr: str) -> list[str]:
    tokens = tokenize_boolean(expr)
    out: list[str] = []
    for tok in tokens:
        upper = tok.upper()
        if upper in {"AND", "OR", "NOT", "(", ")"}:
            continue
        if tok.startswith('"') and tok.endswith('"'):
            tok = tok[1:-1]
        if tok:
            out.append(tok)
    return out


def parse_boolean(expr: str):
    """Parse a small, predictable boolean grammar.

    expression := or_expr
    or_expr    := and_expr (OR and_expr)*
    and_expr   := unary (AND unary)*
    unary      := NOT unary | '(' expression ')' | TERM
    """
    tokens = tokenize_boolean(expr)
    if not tokens:
        return ("TERM", "")

    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def consume() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_expr():
        return parse_or()

    def parse_or():
        node = parse_and()
        while peek() and peek().upper() == "OR":
            consume()
            node = ("OR", node, parse_and())
        return node

    def parse_and():
        node = parse_unary()
        while peek() and peek().upper() == "AND":
            consume()
            node = ("AND", node, parse_unary())
        return node

    def parse_unary():
        tok = peek()
        if tok and tok.upper() == "NOT":
            consume()
            return ("NOT", parse_unary())
        if tok == "(":
            consume()
            node = parse_expr()
            if peek() == ")":
                consume()
            return node
        if tok is None:
            return ("TERM", "")
        term = consume()
        if term.startswith('"') and term.endswith('"'):
            term = term[1:-1]
        return ("TERM", normalize_text(term))

    return parse_expr()


def boolean_match(expr: str, text: str) -> bool:
    """Evaluate the configured expression against normalized title+abstract."""
    haystack = normalize_text(text)
    tree = parse_boolean(expr)

    def eval_node(node) -> bool:
        kind = node[0]
        if kind == "TERM":
            term = normalize_text(node[1])
            return bool(term) and term in haystack
        if kind == "AND":
            return eval_node(node[1]) and eval_node(node[2])
        if kind == "OR":
            return eval_node(node[1]) or eval_node(node[2])
        if kind == "NOT":
            return not eval_node(node[1])
        return False

    return eval_node(tree)


def query_matches(paper: Paper, queries: list[str], *, local_filter: bool) -> str:
    combined = f"{paper.title}\n{paper.abstract}"
    for query in queries:
        if not local_filter or boolean_match(query, combined):
            return query
    return ""


# --------------------------- Source adapters ---------------------------

class PubMedClient:
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, session: requests.Session, cfg: dict[str, Any], start: datetime, end: datetime):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(self, queries: list[str]) -> list[Paper]:
        source_cfg = self.cfg["sources"]["pubmed"]
        if not source_cfg.get("enabled", True):
            return []

        params_base = {
            "db": "pubmed",
            "retmode": "json",
            "tool": source_cfg.get("tool", "academic_paper_daily"),
            "email": source_cfg.get("email") or os.getenv("NCBI_EMAIL", ""),
        }
        api_key = os.getenv("NCBI_API_KEY", "")
        if api_key:
            params_base["api_key"] = api_key

        # Use PubMed's date field for online/indexing freshness.
        date_filter = (
            f'("{self.start.strftime("%Y/%m/%d %H:%M")}"[edat] : '
            f'"{self.end.strftime("%Y/%m/%d %H:%M")}"[edat])'
        )

        ids: list[str] = []
        for query in queries:
            term = f"({query}) AND {date_filter}"
            params = {**params_base, "term": term, "retmax": int(source_cfg.get("max_results", 50))}
            data = request_json(self.session, f"{self.BASE}/esearch.fcgi", params=params)
            ids.extend(data.get("esearchresult", {}).get("idlist", []))

        ids = list(dict.fromkeys(ids))
        if not ids:
            return []

        # EFetch accepts comma-separated PMIDs. Chunk to keep requests manageable.
        papers: list[Paper] = []
        chunk_size = 150
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i + chunk_size]
            params = {
                **params_base,
                "db": "pubmed",
                "id": ",".join(chunk),
                "retmode": "xml",
            }
            xml_text = request_text(self.session, f"{self.BASE}/efetch.fcgi", params=params)
            papers.extend(self._parse_xml(xml_text, queries))
            time.sleep(float(source_cfg.get("request_delay_seconds", 0.2)))
        return papers

    def _parse_xml(self, xml_text: str, queries: list[str]) -> list[Paper]:
        root = ET.fromstring(xml_text)
        papers: list[Paper] = []
        for article in root.findall(".//PubmedArticle"):
            pmid = (article.findtext(".//PMID") or "").strip()
            title = "".join(article.find(".//ArticleTitle").itertext()) if article.find(".//ArticleTitle") is not None else ""

            authors: list[str] = []
            for author in article.findall(".//AuthorList/Author"):
                collective = author.findtext("CollectiveName")
                if collective:
                    authors.append(collective.strip())
                    continue
                last = author.findtext("LastName") or ""
                initials = author.findtext("Initials") or ""
                name = f"{last} {initials}".strip()
                if name:
                    authors.append(name)

            journal = article.findtext(".//Journal/Title") or ""
            doi = ""
            for aid in article.findall(".//ArticleIdList/ArticleId"):
                if aid.attrib.get("IdType") == "doi":
                    doi = (aid.text or "").strip()
                    break

            abstract_parts = []
            for node in article.findall(".//Abstract/AbstractText"):
                txt = "".join(node.itertext()).strip()
                label = node.attrib.get("Label", "")
                abstract_parts.append(f"{label}: {txt}".strip(": "))
            abstract = " ".join(x for x in abstract_parts if x)

            pub_date = self._pub_date(article)
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

            papers.append(
                Paper(
                    uid=pmid or doi or title,
                    title=title.strip(),
                    authors=authors,
                    publication=journal.strip(),
                    published_date=pub_date,
                    doi=doi,
                    pmid=pmid,
                    abstract=abstract,
                    url=url,
                    source="PubMed",
                )
            )
        return papers

    @staticmethod
    def _pub_date(article: ET.Element) -> str:
        pub_date = article.find(".//Journal/JournalIssue/PubDate")
        if pub_date is not None:
            year = pub_date.findtext("Year") or ""
            month = pub_date.findtext("Month") or ""
            day = pub_date.findtext("Day") or ""
            medline = pub_date.findtext("MedlineDate") or ""
            if year:
                return "-".join(x for x in [year, month, day] if x)
            if medline:
                return medline
        return ""


class ArxivClient:
    API = "https://export.arxiv.org/api/query"

    NS = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    def __init__(self, session: requests.Session, cfg: dict[str, Any], start: datetime, end: datetime):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(self, queries: list[str]) -> list[Paper]:
        source_cfg = self.cfg["sources"]["arxiv"]
        if not source_cfg.get("enabled", True):
            return []

        max_results = int(source_cfg.get("max_results", 50))
        papers: list[Paper] = []
        # arXiv supports search syntax; execute each configured boolean query.
        for query in queries:
            search_query = self._to_arxiv_query(query)
            params = {
                "search_query": search_query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            xml_text = request_text(self.session, self.API, params=params)
            root = ET.fromstring(xml_text)
            for entry in root.findall("atom:entry", self.NS):
                paper = self._parse_entry(entry)
                if not paper:
                    continue
                if self.start <= parse_iso_date(paper.published_date) <= self.end:
                    paper.matched_query = query
                    if boolean_match(query, f"{paper.title}\n{paper.abstract}"):
                        papers.append(paper)
            time.sleep(float(source_cfg.get("request_delay_seconds", 3)))
        return self._unique(papers)

    @staticmethod
    def _to_arxiv_query(query: str) -> str:
        # arXiv's API accepts title/abstract search clauses and boolean operators.
        # Convert bare phrases into all-fields search syntax. Existing fielded
        # clauses are kept untouched.
        tokens = tokenize_boolean(query)
        out = []
        for tok in tokens:
            upper = tok.upper()
            if upper in {"AND", "OR", "NOT"} or tok in {"(", ")"}:
                out.append(upper if upper in {"AND", "OR", "NOT"} else tok)
            elif ":" in tok and tok.split(":", 1)[0] in {"ti", "au", "abs", "cat", "all"}:
                out.append(tok)
            else:
                raw = tok.strip('"')
                out.append(f'all:"{raw}"' if " " not in raw else f'all:"{raw}"')
        return " ".join(out)

    def _parse_entry(self, entry: ET.Element) -> Paper | None:
        id_url = (entry.findtext("atom:id", default="", namespaces=self.NS)).strip()
        match = re.search(r"arxiv\.org/abs/([^/]+)$", id_url)
        arxiv_id = match.group(1) if match else id_url.rsplit("/", 1)[-1]
        title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=self.NS)).strip()
        abstract = re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=self.NS)).strip()
        authors = [
            (a.findtext("atom:name", default="", namespaces=self.NS)).strip()
            for a in entry.findall("atom:author", self.NS)
        ]
        published = entry.findtext("atom:published", default="", namespaces=self.NS).strip()
        category = entry.find("atom:category", self.NS)
        cat_name = category.attrib.get("term", "") if category is not None else ""

        if not arxiv_id:
            return None

        return Paper(
            uid=arxiv_id,
            title=title,
            authors=[a for a in authors if a],
            publication=f"arXiv [{cat_name}]" if cat_name else "arXiv",
            published_date=published,
            arxiv_id=arxiv_id,
            abstract=abstract,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            source="arXiv",
        )

    @staticmethod
    def _unique(papers: Iterable[Paper]) -> list[Paper]:
        out = []
        seen = set()
        for p in papers:
            if p.dedup_id not in seen:
                out.append(p)
                seen.add(p.dedup_id)
        return out


class OpenAlexClient:
    API = "https://api.openalex.org/works"

    def __init__(self, session: requests.Session, cfg: dict[str, Any], start: datetime, end: datetime):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(self, queries: list[str]) -> list[Paper]:
        source_cfg = self.cfg["sources"]["openalex"]
        if not source_cfg.get("enabled", True):
            return []

        params_base = {
            "filter": (
                f"from_publication_date:{self.start.date().isoformat()},"
                f"to_publication_date:{self.end.date().isoformat()}"
            ),
            "per-page": int(source_cfg.get("max_results", 50)),
        }

        api_key = os.getenv("OPENALEX_API_KEY", "")
        if api_key:
            params_base["api_key"] = api_key

        mailto = os.getenv("OPENALEX_MAILTO") or source_cfg.get("mailto")
        if mailto:
            params_base["mailto"] = mailto

        papers: list[Paper] = []
        for query in queries:
            # OpenAlex search is used for candidate discovery; exact boolean logic
            # is applied locally against title + abstract.
            candidate_terms = boolean_terms(query)
            broad = " ".join(candidate_terms[:12])
            if not broad:
                continue

            params = {**params_base, "search": broad}
            data = request_json(self.session, self.API, params=params)
            for item in data.get("results", []):
                paper = self._parse_work(item)
                if not paper:
                    continue
                if boolean_match(query, f"{paper.title}\n{paper.abstract}"):
                    paper.matched_query = query
                    papers.append(paper)
        return self._unique(papers)

    @staticmethod
    def _parse_work(item: dict[str, Any]) -> Paper | None:
        title = item.get("display_name") or ""
        abstract = OpenAlexClient._abstract_from_inverted_index(item.get("abstract_inverted_index"))
        authors = []
        for authorship in item.get("authorships") or []:
            name = ((authorship.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)

        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        journal = source.get("display_name") or ""
        doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()
        openalex_id = item.get("id") or title

        best_link = (
            primary.get("landing_page_url")
            or primary.get("pdf_url")
            or item.get("doi")
            or openalex_id
        )

        return Paper(
            uid=openalex_id,
            title=title.strip(),
            authors=authors,
            publication=journal,
            published_date=item.get("publication_date") or "",
            doi=doi,
            abstract=abstract,
            url=best_link,
            source="OpenAlex",
        )

    @staticmethod
    def _abstract_from_inverted_index(inv: dict[str, list[int]] | None) -> str:
        if not inv:
            return ""
        tokens: list[tuple[int, str]] = []
        for word, positions in inv.items():
            for pos in positions:
                tokens.append((pos, word))
        tokens.sort(key=lambda x: x[0])
        return " ".join(word for _, word in tokens)

    @staticmethod
    def _unique(papers: Iterable[Paper]) -> list[Paper]:
        out = []
        seen = set()
        for p in papers:
            if p.dedup_id not in seen:
                out.append(p)
                seen.add(p.dedup_id)
        return out


# --------------------------- Deduplication / metrics ---------------------------

class SentStore:
    """Persistent sent-ID store, normally committed back to the repository."""

    def __init__(self, path: Path, max_entries: int = 100000):
        self.path = path
        self.max_entries = max_entries
        self.ids: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.ids = {str(k): str(v) for k, v in data.items()}
            elif isinstance(data, list):
                self.ids = {str(x): "" for x in data}
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Could not read sent store %s: %s", self.path, exc)

    def contains(self, paper: Paper) -> bool:
        return paper.dedup_id in self.ids

    def mark_many(self, papers: Iterable[Paper]) -> None:
        timestamp = iso_now().isoformat()
        for paper in papers:
            self.ids[paper.dedup_id] = timestamp
        if len(self.ids) > self.max_entries:
            # Drop oldest entries by timestamp where possible.
            ordered = sorted(self.ids.items(), key=lambda kv: kv[1] or "")
            self.ids = dict(ordered[-self.max_entries:])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.ids, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)


def load_impact_factors(path: Path) -> dict[str, str]:
    """Optional journal -> JCR impact factor mapping.

    The mapping is intentionally user-maintained because Clarivate Journal
    Impact Factor data is licensed/proprietary and is not included in these
    public APIs.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {normalize_text(k): str(v) for k, v in (data or {}).items()}
    except (OSError, yaml.YAMLError) as exc:
        LOG.warning("Cannot read impact factor file %s: %s", path, exc)
        return {}


def apply_impact_factors(papers: list[Paper], mapping: dict[str, str]) -> None:
    for paper in papers:
        if paper.journal_key in mapping:
            paper.impact_factor = mapping[paper.journal_key]


def merge_cross_source(papers: list[Paper]) -> list[Paper]:
    """Merge duplicates returned by different databases using DOI first."""
    merged: dict[str, Paper] = {}
    for p in papers:
        key = p.dedup_id
        existing = merged.get(key)
        if not existing:
            merged[key] = p
            continue

        # Prefer records with a richer abstract/metadata.
        if len(p.abstract) > len(existing.abstract):
            existing.abstract = p.abstract
        if not existing.doi and p.doi:
            existing.doi = p.doi
        if not existing.publication and p.publication:
            existing.publication = p.publication
        if not existing.impact_factor and p.impact_factor:
            existing.impact_factor = p.impact_factor

        sources = {s.strip() for s in existing.source.split(",") if s.strip()}
        sources.update(s.strip() for s in p.source.split(",") if s.strip())
        existing.source = ", ".join(sorted(sources))
    return list(merged.values())


# --------------------------- Email ---------------------------

def render_paper_card(paper: Paper, index: int) -> str:
    authors = ", ".join(paper.authors) if paper.authors else "作者信息未提供"
    abstract = paper.abstract or "摘要未提供"
    abstract = abstract if len(abstract) <= 1800 else abstract[:1800] + "…"

    meta = [
        ("作者", authors),
        ("出版物", paper.publication or "未提供"),
        ("发表日期", paper.published_date or "未提供"),
        ("影响因子", paper.impact_factor or "未配置"),
        ("来源数据库", paper.source or "未知"),
    ]

    rows = "".join(
        f"<tr><td style='padding:5px 10px;color:#666;width:90px;vertical-align:top'>{html_text(k)}</td>"
        f"<td style='padding:5px 10px'>{html_text(v)}</td></tr>"
        for k, v in meta
    )

    link = html.escape(paper.url, quote=True)
    doi_html = ""
    if paper.doi:
        doi_url = f"https://doi.org/{quote_plus(paper.doi)}"
        doi_html = (
            f"<div style='margin-top:8px;font-size:13px'>DOI："
            f"<a href='{html.escape(doi_url, quote=True)}'>{html_text(paper.doi)}</a></div>"
        )

    return f"""
    <div style="margin:0 0 22px;padding:18px;border:1px solid #e5e7eb;border-radius:10px;background:#fff">
      <div style="font-size:18px;line-height:1.45;font-weight:700;margin-bottom:10px">
        {index}. {html_text(paper.title)}
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:14px;line-height:1.5">
        {rows}
      </table>
      <div style="margin-top:12px;font-size:14px;line-height:1.6">
        <strong>摘要：</strong>{html_text(abstract)}
      </div>
      {doi_html}
      <div style="margin-top:12px">
        <a href="{link}" style="display:inline-block;padding:9px 14px;background:#1f6feb;color:#fff;text-decoration:none;border-radius:7px">
          查看原文
        </a>
      </div>
    </div>
    """


def build_email(papers: list[Paper], start: datetime, end: datetime, cfg: dict[str, Any]) -> tuple[str, str]:
    subject_prefix = cfg["email"].get("subject_prefix", "每日学术文献推送")
    subject = f"{subject_prefix}｜{end.astimezone().strftime('%Y-%m-%d')}｜{len(papers)}篇"

    if papers:
        cards = "\n".join(render_paper_card(p, i) for i, p in enumerate(papers, 1))
    else:
        cards = """
        <div style="padding:20px;border:1px solid #e5e7eb;border-radius:10px;background:#fff">
          本次检索时间段内没有发现新的、符合条件且尚未推送的文献。
        </div>
        """

    html_body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif;color:#1f2937">
  <div style="max-width:900px;margin:0 auto">
    <div style="padding:22px 24px;margin-bottom:18px;background:#111827;color:#fff;border-radius:12px">
      <div style="font-size:24px;font-weight:800">每日学术文献推送</div>
      <div style="margin-top:8px;opacity:.85;font-size:14px">
        检索范围：{html_text(start.isoformat())} 至 {html_text(end.isoformat())}
      </div>
      <div style="margin-top:4px;opacity:.85;font-size:14px">本次新文献：{len(papers)} 篇</div>
    </div>
    {cards}
    <div style="font-size:12px;color:#6b7280;text-align:center;margin:20px 0">
      Generated by AcademicPaperDaily · GitHub Actions
    </div>
  </div>
</body>
</html>"""

    return subject, html_body


def send_email(
    subject: str,
    html_body: str,
    cfg: dict[str, Any],
) -> None:
    email_cfg = cfg["email"]
    host = os.getenv("SMTP_HOST") or email_cfg.get("smtp_host")
    port = int(os.getenv("SMTP_PORT") or email_cfg.get("smtp_port", 465))
    username = os.getenv("SMTP_USERNAME") or email_cfg.get("username")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("MAIL_FROM") or email_cfg.get("from") or username
    recipients_cfg = os.getenv("MAIL_TO") or email_cfg.get("to", [])

    if isinstance(recipients_cfg, str):
        recipients = [x.strip() for x in recipients_cfg.split(",") if x.strip()]
    else:
        recipients = [str(x).strip() for x in recipients_cfg if str(x).strip()]

    if not all([host, username, password, sender, recipients]):
        raise RuntimeError(
            "SMTP configuration incomplete. Set SMTP_HOST/SMTP_PORT/SMTP_USERNAME/"
            "SMTP_PASSWORD/MAIL_FROM/MAIL_TO or their config.yaml equivalents."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((email_cfg.get("from_name", "Academic Paper Daily"), sender))
    msg["To"] = ", ".join(recipients)
    msg.set_content("请使用支持HTML的邮件客户端查看本邮件。")
    msg.add_alternative(html_body, subtype="html")

    security = (email_cfg.get("security") or "ssl").lower()
    if security == "starttls":
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)


# --------------------------- Configuration / main ---------------------------

def setup_logging(cfg: dict[str, Any]) -> None:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    log_file = Path(log_cfg.get("file", "logs/run.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    if not cfg.get("search", {}).get("queries"):
        raise ValueError("config.yaml must contain search.queries")
    if "sources" not in cfg:
        raise ValueError("config.yaml must contain sources")
    if "email" not in cfg:
        raise ValueError("config.yaml must contain email")

    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily academic literature search and email system")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg)

    start, end = time_window(cfg)
    LOG.info("Search window: %s -> %s", start.isoformat(), end.isoformat())

    sent_store = SentStore(
        Path(cfg.get("state", {}).get("sent_ids_file", "data/sent_ids.json")),
        max_entries=int(cfg.get("state", {}).get("max_entries", 100000)),
    )
    impact_factors = load_impact_factors(
        Path(cfg.get("impact_factor_file", "impact_factors.yaml"))
    )

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.get("user_agent", UA)})

    queries = [str(q).strip() for q in cfg["search"]["queries"] if str(q).strip()]
    all_papers: list[Paper] = []

    clients = [
        ("PubMed", PubMedClient(session, cfg, start, end)),
        ("arXiv", ArxivClient(session, cfg, start, end)),
        ("OpenAlex", OpenAlexClient(session, cfg, start, end)),
    ]

    for name, client in clients:
        try:
            papers = client.search(queries)
            LOG.info("%s returned %d candidates", name, len(papers))
            all_papers.extend(papers)
        except Exception:
            LOG.exception("%s search failed; continuing with remaining sources", name)

    all_papers = merge_cross_source(all_papers)
    apply_impact_factors(all_papers, impact_factors)

    # Keep only papers matching at least one configured expression.
    filtered = []
    for paper in all_papers:
        matched = query_matches(paper, queries, local_filter=True)
        if matched:
            paper.matched_query = paper.matched_query or matched
            filtered.append(paper)

    new_papers = [p for p in filtered if not sent_store.contains(p)]
    new_papers.sort(key=lambda p: (p.published_date or "", p.title), reverse=True)

    LOG.info(
        "Total=%d, matching=%d, already_sent=%d, new=%d",
        len(all_papers),
        len(filtered),
        len(filtered) - len(new_papers),
        len(new_papers),
    )

    email_cfg = cfg["email"]
    if not new_papers and not email_cfg.get("send_when_empty", True):
        LOG.info("No new papers and send_when_empty=false; exiting.")
        return 0

    subject, body = build_email(new_papers, start, end, cfg)
    send_email(subject, body, cfg)
    LOG.info("Email sent successfully to %s", email_cfg.get("to"))

    if new_papers:
        sent_store.mark_many(new_papers)
        sent_store.save()
        LOG.info("Persistent sent store updated: %s", sent_store.path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
