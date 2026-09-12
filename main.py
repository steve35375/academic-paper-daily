#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Academic Paper Daily
=====================

Multi-database academic literature search and HTML email delivery system.

Supported sources:
    - PubMed
    - arXiv
    - OpenAlex

Core features:
    - Boolean query support: AND / OR / NOT / ()
    - Rolling time-window search
    - Persistent deduplication by DOI / PMID / arXiv ID
    - Persistent runtime state
    - Adaptive search window for delayed GitHub Actions runs
    - HTML email delivery through SMTP
    - Optional journal impact-factor mapping

The program is designed to run locally and on GitHub Actions.
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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
import yaml


# ============================================================================
# Global constants
# ============================================================================

APP_NAME = "AcademicPaperDaily/1.1"
DEFAULT_TIMEOUT = 30

DEFAULT_SEARCH_HOURS = 24
DEFAULT_DELAY_TOLERANCE_HOURS = 12
DEFAULT_MAX_SEARCH_HOURS = 48

DEFAULT_RUNTIME_STATE_FILE = "data/runtime_state.json"
DEFAULT_SENT_IDS_FILE = "data/sent_ids.json"

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SCHEDULE_HOUR = 8
DEFAULT_SCHEDULE_MINUTE = 15

UA = f"{APP_NAME} (+https://github.com/)"

LOG = logging.getLogger(APP_NAME)


# ============================================================================
# Data model
# ============================================================================

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
        """Return the strongest stable identifier available."""
        if self.doi:
            return f"doi:{self.doi.lower().strip()}"

        if self.pmid:
            return f"pmid:{self.pmid.strip()}"

        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id.lower().strip()}"

        return f"uid:{self.uid.lower().strip()}"

    @property
    def journal_key(self) -> str:
        """Normalized publication/journal name."""
        return normalize_text(self.publication)


# ============================================================================
# General helpers
# ============================================================================

def normalize_text(value: str) -> str:
    """Normalize text for searching/comparison."""
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def html_text(value: str) -> str:
    """Escape text for HTML."""
    return html.escape(value or "", quote=True)


def iso_now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def parse_iso_date(value: str) -> datetime:
    """Parse an ISO datetime and ensure it is timezone-aware."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def format_datetime(dt: datetime) -> str:
    """Format datetime in Beijing time for human-readable email output."""
    try:
        from zoneinfo import ZoneInfo

        local_dt = dt.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    except Exception:
        local_dt = dt.astimezone(timezone(timedelta(hours=8)))

    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")


# ============================================================================
# Adaptive search window
# ============================================================================

class RuntimeState:
    """
    Persistent execution state.

    Example:
    {
        "last_successful_run": "2026-09-12T03:50:21+00:00",
        "last_email_sent": "2026-09-12T03:50:58+00:00",
        "last_new_papers": 8
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            LOG.info("Runtime state file does not exist yet: %s", self.path)
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))

            if isinstance(raw, dict):
                self.data = raw
            else:
                LOG.warning(
                    "Runtime state file has invalid format: %s",
                    self.path,
                )

        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning(
                "Could not read runtime state file %s: %s",
                self.path,
                exc,
            )

    @property
    def last_successful_run(self) -> datetime | None:
        value = self.data.get("last_successful_run")

        if not value:
            return None

        try:
            return parse_iso_date(str(value))
        except ValueError:
            LOG.warning(
                "Invalid last_successful_run value: %s",
                value,
            )
            return None

    def update(
        self,
        *,
        successful_run: datetime,
        email_sent: datetime,
        new_papers: int,
        total_candidates: int,
        search_hours: float,
    ) -> None:
        self.data = {
            "last_successful_run": successful_run.isoformat(),
            "last_email_sent": email_sent.isoformat(),
            "last_new_papers": int(new_papers),
            "last_total_candidates": int(total_candidates),
            "last_search_hours": round(float(search_hours), 2),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        tmp.write_text(
            json.dumps(
                self.data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        tmp.replace(self.path)


def calculate_adaptive_window(
    *,
    now: datetime,
    runtime_state: RuntimeState,
    cfg: dict[str, Any],
) -> tuple[datetime, datetime, float]:
    """
    Calculate a robust search window.

    Normal operation:
        at least 24 hours

    Delayed operation:
        if the interval since the previous successful run is longer than
        the configured normal interval, expand the search window.

    Example:
        previous successful run: 08:15
        current run:            11:50

        elapsed = 27h35m

        normal = 24h
        delay tolerance = 12h
        max = 48h

        effective window = 27h35m
    """

    search_cfg = cfg.get("search", {})

    base_hours = float(
        search_cfg.get(
            "hours",
            DEFAULT_SEARCH_HOURS,
        )
    )

    delay_tolerance = float(
        search_cfg.get(
            "delay_tolerance_hours",
            DEFAULT_DELAY_TOLERANCE_HOURS,
        )
    )

    max_hours = float(
        search_cfg.get(
            "max_hours",
            DEFAULT_MAX_SEARCH_HOURS,
        )
    )

    if base_hours <= 0:
        raise ValueError("search.hours must be > 0")

    if delay_tolerance < 0:
        raise ValueError(
            "search.delay_tolerance_hours must be >= 0"
        )

    if max_hours <= 0:
        raise ValueError("search.max_hours must be > 0")

    if max_hours < base_hours:
        LOG.warning(
            "search.max_hours (%.2f) is smaller than search.hours (%.2f). "
            "Using base_hours as maximum.",
            max_hours,
            base_hours,
        )
        max_hours = base_hours

    last_run = runtime_state.last_successful_run

    # First ever execution.
    if last_run is None:
        effective_hours = min(base_hours, max_hours)

        start = now - timedelta(hours=effective_hours)

        LOG.info(
            "No previous successful run found. "
            "Using default search window: %.2f hours",
            effective_hours,
        )

        return start, now, effective_hours

    elapsed_hours = (
        now - last_run
    ).total_seconds() / 3600.0

    if elapsed_hours < 0:
        LOG.warning(
            "Runtime state is in the future. "
            "Falling back to default window."
        )

        effective_hours = min(base_hours, max_hours)

    elif elapsed_hours <= base_hours:
        # Keep the normal minimum search window.
        effective_hours = base_hours

    else:
        # The next run was delayed or skipped.
        # Use elapsed interval + a safety buffer.
        effective_hours = elapsed_hours + delay_tolerance

        LOG.warning(
            "Previous successful run was %.2f hours ago. "
            "Adaptive search window expanded to %.2f hours.",
            elapsed_hours,
            effective_hours,
        )

        effective_hours = min(
            effective_hours,
            max_hours,
        )

    start = now - timedelta(hours=effective_hours)

    LOG.info(
        "Adaptive search window: %.2f hours",
        effective_hours,
    )

    return start, now, effective_hours


# ============================================================================
# HTTP helpers
# ============================================================================

def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> dict[str, Any]:
    """GET JSON with basic exponential retry."""

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=timeout,
            )

            response.raise_for_status()

            return response.json()

        except (requests.RequestException, ValueError) as exc:
            last_exc = exc

            if attempt < retries:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(
        f"GET JSON failed: {url}"
    ) from last_exc


def request_text(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
) -> str:
    """GET text/XML with basic exponential retry."""

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=timeout,
            )

            response.raise_for_status()

            return response.text

        except requests.RequestException as exc:
            last_exc = exc

            if attempt < retries:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(
        f"GET text failed: {url}"
    ) from last_exc


# ============================================================================
# Boolean query engine
# ============================================================================

TOKEN_RE = re.compile(
    r'"[^"]+"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+',
    flags=re.IGNORECASE,
)


def tokenize_boolean(expr: str) -> list[str]:
    return TOKEN_RE.findall(expr or "")


def boolean_terms(expr: str) -> list[str]:
    """Extract ordinary search terms from a boolean expression."""

    tokens = tokenize_boolean(expr)

    output: list[str] = []

    for token in tokens:
        upper = token.upper()

        if upper in {"AND", "OR", "NOT", "(", ")"}:
            continue

        if token.startswith('"') and token.endswith('"'):
            token = token[1:-1]

        if token:
            output.append(token)

    return output


def parse_boolean(expr: str):
    """
    Parse a simple boolean grammar:

        expression := OR-expression

        OR-expression :=
            AND-expression (OR AND-expression)*

        AND-expression :=
            unary (AND unary)*

        unary :=
            NOT unary
            | '(' expression ')'
            | TERM
    """

    tokens = tokenize_boolean(expr)

    if not tokens:
        return ("TERM", "")

    pos = 0

    def peek() -> str | None:
        if pos < len(tokens):
            return tokens[pos]

        return None

    def consume() -> str:
        nonlocal pos

        token = tokens[pos]
        pos += 1

        return token

    def parse_expr():
        return parse_or()

    def parse_or():
        node = parse_and()

        while peek() and peek().upper() == "OR":
            consume()
            node = (
                "OR",
                node,
                parse_and(),
            )

        return node

    def parse_and():
        node = parse_unary()

        while peek() and peek().upper() == "AND":
            consume()
            node = (
                "AND",
                node,
                parse_unary(),
            )

        return node

    def parse_unary():
        token = peek()

        if token and token.upper() == "NOT":
            consume()

            return (
                "NOT",
                parse_unary(),
            )

        if token == "(":
            consume()

            node = parse_expr()

            if peek() == ")":
                consume()

            return node

        if token is None:
            return ("TERM", "")

        term = consume()

        if term.startswith('"') and term.endswith('"'):
            term = term[1:-1]

        return (
            "TERM",
            normalize_text(term),
        )

    return parse_expr()


def boolean_match(
    expr: str,
    text: str,
) -> bool:
    """Evaluate boolean expression against supplied text."""

    haystack = normalize_text(text)

    tree = parse_boolean(expr)

    def evaluate(node) -> bool:
        kind = node[0]

        if kind == "TERM":
            term = normalize_text(node[1])

            return (
                bool(term)
                and term in haystack
            )

        if kind == "AND":
            return (
                evaluate(node[1])
                and evaluate(node[2])
            )

        if kind == "OR":
            return (
                evaluate(node[1])
                or evaluate(node[2])
            )

        if kind == "NOT":
            return not evaluate(node[1])

        return False

    return evaluate(tree)


def query_matches(
    paper: Paper,
    queries: list[str],
) -> str:
    """Return the first matching query or empty string."""

    combined = (
        f"{paper.title}\n"
        f"{paper.abstract}"
    )

    for query in queries:
        if boolean_match(
            query,
            combined,
        ):
            return query

    return ""


# ============================================================================
# PubMed
# ============================================================================

class PubMedClient:
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(
        self,
        session: requests.Session,
        cfg: dict[str, Any],
        start: datetime,
        end: datetime,
    ):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(
        self,
        queries: list[str],
    ) -> list[Paper]:

        source_cfg = self.cfg["sources"]["pubmed"]

        if not source_cfg.get("enabled", True):
            return []

        params_base = {
            "db": "pubmed",
            "retmode": "json",
            "tool": source_cfg.get(
                "tool",
                "academic_paper_daily",
            ),
            "email": (
                source_cfg.get("email")
                or os.getenv("NCBI_EMAIL", "")
            ),
        }

        api_key = os.getenv("NCBI_API_KEY", "")

        if api_key:
            params_base["api_key"] = api_key

        # Use PubMed's EDate field as an online/indexing freshness filter.
        date_filter = (
            f'("{self.start.strftime("%Y/%m/%d %H:%M")}"[edat] : '
            f'"{self.end.strftime("%Y/%m/%d %H:%M")}"[edat])'
        )

        ids: list[str] = []

        for query in queries:
            term = (
                f"({query}) AND "
                f"{date_filter}"
            )

            params = {
                **params_base,
                "term": term,
                "retmax": int(
                    source_cfg.get(
                        "max_results",
                        50,
                    )
                ),
            }

            data = request_json(
                self.session,
                f"{self.BASE}/esearch.fcgi",
                params=params,
            )

            ids.extend(
                data.get(
                    "esearchresult",
                    {},
                ).get(
                    "idlist",
                    [],
                )
            )

        ids = list(
            dict.fromkeys(ids)
        )

        if not ids:
            return []

        papers: list[Paper] = []

        chunk_size = 150

        for i in range(
            0,
            len(ids),
            chunk_size,
        ):
            chunk = ids[
                i:i + chunk_size
            ]

            params = {
                **params_base,
                "db": "pubmed",
                "id": ",".join(chunk),
                "retmode": "xml",
            }

            xml_text = request_text(
                self.session,
                f"{self.BASE}/efetch.fcgi",
                params=params,
            )

            papers.extend(
                self._parse_xml(
                    xml_text,
                    queries,
                )
            )

            time.sleep(
                float(
                    source_cfg.get(
                        "request_delay_seconds",
                        0.2,
                    )
                )
            )

        return papers

    def _parse_xml(
        self,
        xml_text: str,
        queries: list[str],
    ) -> list[Paper]:

        root = ET.fromstring(xml_text)

        papers: list[Paper] = []

        for article in root.findall(
            ".//PubmedArticle"
        ):
            pmid = (
                article.findtext(".//PMID")
                or ""
            ).strip()

            title_node = article.find(
                ".//ArticleTitle"
            )

            title = (
                "".join(
                    title_node.itertext()
                )
                if title_node is not None
                else ""
            )

            authors: list[str] = []

            for author in article.findall(
                ".//AuthorList/Author"
            ):
                collective = author.findtext(
                    "CollectiveName"
                )

                if collective:
                    authors.append(
                        collective.strip()
                    )

                    continue

                last = (
                    author.findtext("LastName")
                    or ""
                )

                initials = (
                    author.findtext("Initials")
                    or ""
                )

                name = (
                    f"{last} {initials}"
                    .strip()
                )

                if name:
                    authors.append(name)

            journal = (
                article.findtext(
                    ".//Journal/Title"
                )
                or ""
            )

            doi = ""

            for article_id in article.findall(
                ".//ArticleIdList/ArticleId"
            ):
                if (
                    article_id.attrib.get(
                        "IdType"
                    )
                    == "doi"
                ):
                    doi = (
                        article_id.text
                        or ""
                    ).strip()

                    break

            abstract_parts: list[str] = []

            for node in article.findall(
                ".//Abstract/AbstractText"
            ):
                txt = "".join(
                    node.itertext()
                ).strip()

                label = node.attrib.get(
                    "Label",
                    "",
                )

                if label:
                    txt = (
                        f"{label}: {txt}"
                    )

                if txt:
                    abstract_parts.append(
                        txt
                    )

            abstract = " ".join(
                abstract_parts
            )

            pub_date = self._pub_date(
                article
            )

            url = (
                f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                if pmid
                else ""
            )

            papers.append(
                Paper(
                    uid=(
                        pmid
                        or doi
                        or title
                    ),
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
    def _pub_date(
        article: ET.Element,
    ) -> str:

        pub_date = article.find(
            ".//Journal/JournalIssue/PubDate"
        )

        if pub_date is None:
            return ""

        year = (
            pub_date.findtext("Year")
            or ""
        )

        month = (
            pub_date.findtext("Month")
            or ""
        )

        day = (
            pub_date.findtext("Day")
            or ""
        )

        medline = (
            pub_date.findtext(
                "MedlineDate"
            )
            or ""
        )

        if year:
            return "-".join(
                x
                for x in [
                    year,
                    month,
                    day,
                ]
                if x
            )

        if medline:
            return medline

        return ""


# ============================================================================
# arXiv
# ============================================================================

class ArxivClient:
    API = "https://export.arxiv.org/api/query"

    NS = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    def __init__(
        self,
        session: requests.Session,
        cfg: dict[str, Any],
        start: datetime,
        end: datetime,
    ):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(
        self,
        queries: list[str],
    ) -> list[Paper]:

        source_cfg = self.cfg["sources"]["arxiv"]

        if not source_cfg.get("enabled", True):
            return []

        max_results = int(
            source_cfg.get(
                "max_results",
                50,
            )
        )

        papers: list[Paper] = []

        for query in queries:

            search_query = (
                self._to_arxiv_query(
                    query
                )
            )

            params = {
                "search_query": search_query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }

            xml_text = request_text(
                self.session,
                self.API,
                params=params,
            )

            root = ET.fromstring(
                xml_text
            )

            for entry in root.findall(
                "atom:entry",
                self.NS,
            ):
                paper = (
                    self._parse_entry(entry)
                )

                if not paper:
                    continue

                try:
                    published_at = (
                        parse_iso_date(
                            paper.published_date
                        )
                    )
                except ValueError:
                    continue

                if not (
                    self.start
                    <= published_at
                    <= self.end
                ):
                    continue

                if boolean_match(
                    query,
                    f"{paper.title}\n{paper.abstract}",
                ):
                    paper.matched_query = query
                    papers.append(paper)

            time.sleep(
                float(
                    source_cfg.get(
                        "request_delay_seconds",
                        3,
                    )
                )
            )

        return self._unique(papers)

    @staticmethod
    def _to_arxiv_query(
        query: str,
    ) -> str:

        tokens = tokenize_boolean(query)

        output = []

        for token in tokens:

            upper = token.upper()

            if upper in {
                "AND",
                "OR",
                "NOT",
            }:
                output.append(upper)
                continue

            if token in {
                "(",
                ")",
            }:
                output.append(token)
                continue

            if ":" in token:
                field = token.split(
                    ":",
                    1,
                )[0]

                if field in {
                    "ti",
                    "au",
                    "abs",
                    "cat",
                    "all",
                }:
                    output.append(token)
                    continue

            raw = token.strip('"')

            output.append(
                f'all:"{raw}"'
            )

        return " ".join(output)

    def _parse_entry(
        self,
        entry: ET.Element,
    ) -> Paper | None:

        id_url = (
            entry.findtext(
                "atom:id",
                default="",
                namespaces=self.NS,
            )
            .strip()
        )

        match = re.search(
            r"arxiv\.org/abs/([^/]+)$",
            id_url,
        )

        arxiv_id = (
            match.group(1)
            if match
            else id_url.rsplit(
                "/",
                1,
            )[-1]
        )

        title = re.sub(
            r"\s+",
            " ",
            entry.findtext(
                "atom:title",
                default="",
                namespaces=self.NS,
            ),
        ).strip()

        abstract = re.sub(
            r"\s+",
            " ",
            entry.findtext(
                "atom:summary",
                default="",
                namespaces=self.NS,
            ),
        ).strip()

        authors = [
            (
                a.findtext(
                    "atom:name",
                    default="",
                    namespaces=self.NS,
                )
                or ""
            ).strip()
            for a in entry.findall(
                "atom:author",
                self.NS,
            )
        ]

        published = (
            entry.findtext(
                "atom:published",
                default="",
                namespaces=self.NS,
            )
            .strip()
        )

        category = entry.find(
            "atom:category",
            self.NS,
        )

        category_name = (
            category.attrib.get(
                "term",
                "",
            )
            if category is not None
            else ""
        )

        if not arxiv_id:
            return None

        return Paper(
            uid=arxiv_id,
            title=title,
            authors=[
                a
                for a in authors
                if a
            ],
            publication=(
                f"arXiv [{category_name}]"
                if category_name
                else "arXiv"
            ),
            published_date=published,
            arxiv_id=arxiv_id,
            abstract=abstract,
            url=(
                f"https://arxiv.org/abs/{arxiv_id}"
            ),
            source="arXiv",
        )

    @staticmethod
    def _unique(
        papers: Iterable[Paper],
    ) -> list[Paper]:

        result: list[Paper] = []

        seen: set[str] = set()

        for paper in papers:

            if paper.dedup_id in seen:
                continue

            seen.add(
                paper.dedup_id
            )

            result.append(paper)

        return result


# ============================================================================
# OpenAlex
# ============================================================================

class OpenAlexClient:
    API = "https://api.openalex.org/works"

    def __init__(
        self,
        session: requests.Session,
        cfg: dict[str, Any],
        start: datetime,
        end: datetime,
    ):
        self.session = session
        self.cfg = cfg
        self.start = start
        self.end = end

    def search(
        self,
        queries: list[str],
    ) -> list[Paper]:

        source_cfg = self.cfg["sources"]["openalex"]

        if not source_cfg.get("enabled", True):
            return []

        params_base = {
            "filter": (
                "from_publication_date:"
                f"{self.start.date().isoformat()},"
                "to_publication_date:"
                f"{self.end.date().isoformat()}"
            ),
            "per-page": int(
                source_cfg.get(
                    "max_results",
                    50,
                )
            ),
        }

        api_key = os.getenv(
            "OPENALEX_API_KEY",
            "",
        )

        if api_key:
            params_base["api_key"] = api_key

        mailto = (
            os.getenv(
                "OPENALEX_MAILTO"
            )
            or source_cfg.get(
                "mailto"
            )
        )

        if mailto:
            params_base["mailto"] = mailto

        papers: list[Paper] = []

        for query in queries:

            candidate_terms = (
                boolean_terms(query)
            )

            broad = " ".join(
                candidate_terms[:12]
            )

            if not broad:
                continue

            params = {
                **params_base,
                "search": broad,
            }

            data = request_json(
                self.session,
                self.API,
                params=params,
            )

            for item in data.get(
                "results",
                [],
            ):
                paper = (
                    self._parse_work(item)
                )

                if not paper:
                    continue

                if boolean_match(
                    query,
                    f"{paper.title}\n{paper.abstract}",
                ):
                    paper.matched_query = query
                    papers.append(paper)

        return self._unique(papers)

    @staticmethod
    def _parse_work(
        item: dict[str, Any],
    ) -> Paper | None:

        title = (
            item.get(
                "display_name"
            )
            or ""
        )

        abstract = (
            OpenAlexClient
            ._abstract_from_inverted_index(
                item.get(
                    "abstract_inverted_index"
                )
            )
        )

        authors: list[str] = []

        for authorship in (
            item.get("authorships")
            or []
        ):
            name = (
                (
                    authorship.get(
                        "author"
                    )
                    or {}
                ).get(
                    "display_name"
                )
                or ""
            ).strip()

            if name:
                authors.append(name)

        primary = (
            item.get(
                "primary_location"
            )
            or {}
        )

        source = (
            primary.get(
                "source"
            )
            or {}
        )

        journal = (
            source.get(
                "display_name"
            )
            or ""
        )

        doi = (
            item.get("doi")
            or ""
        ).replace(
            "https://doi.org/",
            "",
        ).strip()

        openalex_id = (
            item.get("id")
            or title
        )

        best_link = (
            primary.get(
                "landing_page_url"
            )
            or primary.get(
                "pdf_url"
            )
            or item.get("doi")
            or openalex_id
        )

        return Paper(
            uid=openalex_id,
            title=title.strip(),
            authors=authors,
            publication=journal,
            published_date=(
                item.get(
                    "publication_date"
                )
                or ""
            ),
            doi=doi,
            abstract=abstract,
            url=best_link,
            source="OpenAlex",
        )

    @staticmethod
    def _abstract_from_inverted_index(
        inverted_index: dict[str, list[int]] | None,
    ) -> str:

        if not inverted_index:
            return ""

        tokens: list[tuple[int, str]] = []

        for word, positions in (
            inverted_index.items()
        ):
            for position in positions:
                tokens.append(
                    (
                        position,
                        word,
                    )
                )

        tokens.sort(
            key=lambda x: x[0]
        )

        return " ".join(
            word
            for _, word in tokens
        )

    @staticmethod
    def _unique(
        papers: Iterable[Paper],
    ) -> list[Paper]:

        result: list[Paper] = []

        seen: set[str] = set()

        for paper in papers:

            if paper.dedup_id in seen:
                continue

            seen.add(
                paper.dedup_id
            )

            result.append(paper)

        return result


# ============================================================================
# Sent paper store
# ============================================================================

class SentStore:
    """
    Persistent sent-paper ID store.

    This file is committed back to GitHub by Actions.
    """

    def __init__(
        self,
        path: Path,
        max_entries: int = 100000,
    ):
        self.path = path
        self.max_entries = max_entries
        self.ids: dict[str, str] = {}

        self.load()

    def load(self) -> None:

        if not self.path.exists():
            LOG.info(
                "Sent-ID store does not exist yet: %s",
                self.path,
            )
            return

        try:

            data = json.loads(
                self.path.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(data, dict):
                self.ids = {
                    str(k): str(v)
                    for k, v
                    in data.items()
                }

            elif isinstance(data, list):
                self.ids = {
                    str(x): ""
                    for x in data
                }

            else:
                LOG.warning(
                    "Invalid sent-ID store format: %s",
                    self.path,
                )

        except (
            OSError,
            json.JSONDecodeError,
        ) as exc:

            LOG.warning(
                "Could not read sent-ID store %s: %s",
                self.path,
                exc,
            )

    def contains(
        self,
        paper: Paper,
    ) -> bool:

        return (
            paper.dedup_id
            in self.ids
        )

    def mark_many(
        self,
        papers: Iterable[Paper],
    ) -> None:

        timestamp = iso_now().isoformat()

        for paper in papers:
            self.ids[
                paper.dedup_id
            ] = timestamp

        if (
            len(self.ids)
            > self.max_entries
        ):
            ordered = sorted(
                self.ids.items(),
                key=lambda kv: (
                    kv[1] or ""
                ),
            )

            self.ids = dict(
                ordered[
                    -self.max_entries:
                ]
            )

    def save(self) -> None:

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        tmp = self.path.with_suffix(
            self.path.suffix + ".tmp"
        )

        tmp.write_text(
            json.dumps(
                self.ids,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        tmp.replace(
            self.path
        )


# ============================================================================
# Optional impact-factor mapping
# ============================================================================

def load_impact_factors(
    path: Path,
) -> dict[str, str]:

    """
    Load manually maintained journal -> impact factor mappings.

    We deliberately do not invent JCR Impact Factors from unrelated metrics.
    """

    if not path.exists():
        return {}

    try:

        data = yaml.safe_load(
            path.read_text(
                encoding="utf-8"
            )
        ) or {}

        return {
            normalize_text(k): str(v)
            for k, v in (
                data or {}
            ).items()
        }

    except (
        OSError,
        yaml.YAMLError,
    ) as exc:

        LOG.warning(
            "Cannot read impact factor file %s: %s",
            path,
            exc,
        )

        return {}


def apply_impact_factors(
    papers: list[Paper],
    mapping: dict[str, str],
) -> None:

    for paper in papers:

        key = paper.journal_key

        if key in mapping:
            paper.impact_factor = (
                mapping[key]
            )


# ============================================================================
# Cross-source merging
# ============================================================================

def merge_cross_source(
    papers: list[Paper],
) -> list[Paper]:

    """
    Merge duplicate records from different databases.

    DOI/PMID/arXiv-based deduplication is preferred.
    """

    merged: dict[str, Paper] = {}

    for paper in papers:

        key = paper.dedup_id

        existing = merged.get(
            key
        )

        if existing is None:
            merged[key] = paper
            continue

        if len(paper.abstract) > len(
            existing.abstract
        ):
            existing.abstract = (
                paper.abstract
            )

        if not existing.doi and paper.doi:
            existing.doi = paper.doi

        if not existing.pmid and paper.pmid:
            existing.pmid = paper.pmid

        if not existing.arxiv_id and paper.arxiv_id:
            existing.arxiv_id = paper.arxiv_id

        if (
            not existing.publication
            and paper.publication
        ):
            existing.publication = (
                paper.publication
            )

        if (
            not existing.impact_factor
            and paper.impact_factor
        ):
            existing.impact_factor = (
                paper.impact_factor
            )

        sources = {
            source.strip()
            for source
            in existing.source.split(",")
            if source.strip()
        }

        sources.update(
            source.strip()
            for source
            in paper.source.split(",")
            if source.strip()
        )

        existing.source = ", ".join(
            sorted(sources)
        )

    return list(
        merged.values()
    )


# ============================================================================
# Email HTML
# ============================================================================

def render_paper_card(
    paper: Paper,
    index: int,
) -> str:

    authors = (
        ", ".join(paper.authors)
        if paper.authors
        else "作者信息未提供"
    )

    abstract = (
        paper.abstract
        or "摘要未提供"
    )

    if len(abstract) > 1800:
        abstract = (
            abstract[:1800]
            + "…"
        )

    metadata = [
        (
            "作者",
            authors,
        ),
        (
            "出版物",
            paper.publication
            or "未提供",
        ),
        (
            "发表日期",
            paper.published_date
            or "未提供",
        ),
        (
            "影响因子",
            paper.impact_factor
            or "未配置",
        ),
        (
            "来源数据库",
            paper.source
            or "未知",
        ),
    ]

    rows = "".join(
        (
            "<tr>"
            "<td style='padding:5px 10px;"
            "color:#666;width:90px;"
            "vertical-align:top'>"
            f"{html_text(key)}"
            "</td>"
            "<td style='padding:5px 10px'>"
            f"{html_text(value)}"
            "</td>"
            "</tr>"
        )
        for key, value
        in metadata
    )

    link = html.escape(
        paper.url,
        quote=True,
    )

    doi_html = ""

    if paper.doi:

        doi_url = (
            f"https://doi.org/"
            f"{quote_plus(paper.doi)}"
        )

        doi_html = (
            "<div style='margin-top:8px;"
            "font-size:13px'>"
            "DOI："
            f"<a href='{html.escape(doi_url, quote=True)}'>"
            f"{html_text(paper.doi)}"
            "</a>"
            "</div>"
        )

    return f"""
    <div style="margin:0 0 22px;
                padding:18px;
                border:1px solid #e5e7eb;
                border-radius:10px;
                background:#fff">

      <div style="font-size:18px;
                  line-height:1.45;
                  font-weight:700;
                  margin-bottom:10px">

        {index}. {html_text(paper.title)}

      </div>

      <table style="border-collapse:collapse;
                    width:100%;
                    font-size:14px;
                    line-height:1.5">

        {rows}

      </table>

      <div style="margin-top:12px;
                  font-size:14px;
                  line-height:1.6">

        <strong>摘要：</strong>
        {html_text(abstract)}

      </div>

      {doi_html}

      <div style="margin-top:12px">

        <a href="{link}"
           style="display:inline-block;
                  padding:9px 14px;
                  background:#1f6feb;
                  color:#fff;
                  text-decoration:none;
                  border-radius:7px">

          查看原文

        </a>

      </div>

    </div>
    """


def build_email(
    papers: list[Paper],
    start: datetime,
    end: datetime,
    effective_hours: float,
    cfg: dict[str, Any],
    *,
    previous_run: datetime | None,
    total_candidates: int,
    already_sent_count: int,
) -> tuple[str, str]:

    email_cfg = cfg["email"]

    local_end = end.astimezone(
        timezone(
            timedelta(
                hours=8
            )
        )
    )

    subject_prefix = email_cfg.get(
        "subject_prefix",
        "每日学术文献推送",
    )

    subject = (
        f"{subject_prefix}"
        f"｜{local_end.strftime('%Y-%m-%d')}"
        f"｜{len(papers)}篇"
    )

    if papers:

        cards = "\n".join(
            render_paper_card(
                paper,
                index,
            )
            for index, paper
            in enumerate(
                papers,
                1,
            )
        )

    else:

        cards = """
        <div style="padding:20px;
                    border:1px solid #e5e7eb;
                    border-radius:10px;
                    background:#fff">

          本次检索时间段内没有发现新的、
          符合条件且尚未推送的文献。

        </div>
        """

    previous_run_html = (
        format_datetime(previous_run)
        if previous_run
        else "首次运行"
    )

    html_body = f"""
<!doctype html>

<html>

<head>
  <meta charset="utf-8">
</head>

<body style="
  margin:0;
  padding:24px;
  background:#f5f7fa;
  font-family:-apple-system,
               BlinkMacSystemFont,
               'Segoe UI',
               'Microsoft YaHei',
               Arial,
               sans-serif;
  color:#1f2937;
">

  <div style="
    max-width:900px;
    margin:0 auto;
  ">

    <div style="
      padding:22px 24px;
      margin-bottom:18px;
      background:#111827;
      color:#fff;
      border-radius:12px;
    ">

      <div style="
        font-size:24px;
        font-weight:800;
      ">
        每日学术文献推送
      </div>

      <div style="
        margin-top:8px;
        opacity:.88;
        font-size:14px;
      ">
        本次实际运行：
        {html_text(format_datetime(end))}
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        上次成功运行：
        {html_text(previous_run_html)}
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        实际检索范围：
        {html_text(format_datetime(start))}
        →
        {html_text(format_datetime(end))}
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        实际检索窗口：
        {effective_hours:.2f} 小时
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        数据库候选论文：
        {total_candidates} 篇
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        已过滤历史推送：
        {already_sent_count} 篇
      </div>

      <div style="
        margin-top:4px;
        opacity:.88;
        font-size:14px;
      ">
        本次新论文：
        {len(papers)} 篇
      </div>

    </div>

    {cards}

    <div style="
      font-size:12px;
      color:#6b7280;
      text-align:center;
      margin:20px 0;
    ">

      Generated by AcademicPaperDaily
      · GitHub Actions

    </div>

  </div>

</body>

</html>
"""

    return subject, html_body


# ============================================================================
# SMTP
# ============================================================================

def send_email(
    subject: str,
    html_body: str,
    cfg: dict[str, Any],
) -> None:

    email_cfg = cfg["email"]

    host = (
        os.getenv("SMTP_HOST")
        or email_cfg.get(
            "smtp_host"
        )
    )

    port = int(
        os.getenv("SMTP_PORT")
        or email_cfg.get(
            "smtp_port",
            465,
        )
    )

    username = (
        os.getenv("SMTP_USERNAME")
        or email_cfg.get(
            "username"
        )
    )

    password = os.getenv(
        "SMTP_PASSWORD"
    )

    sender = (
        os.getenv("MAIL_FROM")
        or email_cfg.get(
            "from"
        )
        or username
    )

    recipients_cfg = (
        os.getenv("MAIL_TO")
        or email_cfg.get(
            "to",
            [],
        )
    )

    if isinstance(
        recipients_cfg,
        str,
    ):
        recipients = [
            item.strip()
            for item
            in recipients_cfg.split(",")
            if item.strip()
        ]
    else:
        recipients = [
            str(item).strip()
            for item
            in recipients_cfg
            if str(item).strip()
        ]

    if not all(
        [
            host,
            username,
            password,
            sender,
            recipients,
        ]
    ):
        raise RuntimeError(
            "SMTP configuration incomplete. "
            "Set SMTP_HOST/SMTP_PORT/"
            "SMTP_USERNAME/SMTP_PASSWORD/"
            "MAIL_FROM/MAIL_TO."
        )

    message = EmailMessage()

    message["Subject"] = subject

    message["From"] = formataddr(
        (
            email_cfg.get(
                "from_name",
                "Academic Paper Daily",
            ),
            sender,
        )
    )

    message["To"] = ", ".join(
        recipients
    )

    message.set_content(
        "请使用支持 HTML 的邮件客户端查看本邮件。"
    )

    message.add_alternative(
        html_body,
        subtype="html",
    )

    security = (
        email_cfg.get(
            "security",
            "ssl",
        )
        .lower()
    )

    if security == "starttls":

        with smtplib.SMTP(
            host,
            port,
            timeout=30,
        ) as smtp:

            smtp.ehlo()

            smtp.starttls()

            smtp.ehlo()

            smtp.login(
                username,
                password,
            )

            smtp.send_message(
                message
            )

    else:

        with smtplib.SMTP_SSL(
            host,
            port,
            timeout=30,
        ) as smtp:

            smtp.login(
                username,
                password,
            )

            smtp.send_message(
                message
            )


# ============================================================================
# Configuration / logging
# ============================================================================

def setup_logging(
    cfg: dict[str, Any],
) -> None:

    log_cfg = cfg.get(
        "logging",
        {},
    )

    level = getattr(
        logging,
        str(
            log_cfg.get(
                "level",
                "INFO",
            )
        ).upper(),
        logging.INFO,
    )

    log_file = Path(
        log_cfg.get(
            "file",
            "logs/run.log",
        )
    )

    log_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logging.basicConfig(
        level=level,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
        handlers=[
            logging.StreamHandler(
                sys.stdout
            ),
            logging.FileHandler(
                log_file,
                encoding="utf-8",
            ),
        ],
    )


def load_config(
    path: Path,
) -> dict[str, Any]:

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:

        cfg = (
            yaml.safe_load(file)
            or {}
        )

    if not cfg.get(
        "search",
        {},
    ).get(
        "queries"
    ):
        raise ValueError(
            "config.yaml must contain search.queries"
        )

    if "sources" not in cfg:
        raise ValueError(
            "config.yaml must contain sources"
        )

    if "email" not in cfg:
        raise ValueError(
            "config.yaml must contain email"
        )

    return cfg


# ============================================================================
# Main
# ============================================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Daily academic literature "
            "search and email system"
        )
    )

    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML",
    )

    args = parser.parse_args()

    config_path = Path(
        args.config
    )

    cfg = load_config(
        config_path
    )

    setup_logging(cfg)

    current_time = iso_now()

    # ------------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------------

    state_cfg = cfg.get(
        "state",
        {},
    )

    sent_ids_file = Path(
        state_cfg.get(
            "sent_ids_file",
            DEFAULT_SENT_IDS_FILE,
        )
    )

    runtime_state_file = Path(
        state_cfg.get(
            "runtime_state_file",
            DEFAULT_RUNTIME_STATE_FILE,
        )
    )

    sent_store = SentStore(
        sent_ids_file,
        max_entries=int(
            state_cfg.get(
                "max_entries",
                100000,
            )
        ),
    )

    runtime_state = RuntimeState(
        runtime_state_file
    )

    previous_run = (
        runtime_state.last_successful_run
    )

    # ------------------------------------------------------------------------
    # Adaptive search window
    # ------------------------------------------------------------------------

    start, end, effective_hours = (
        calculate_adaptive_window(
            now=current_time,
            runtime_state=runtime_state,
            cfg=cfg,
        )
    )

    LOG.info(
        "Search window: %s -> %s",
        start.isoformat(),
        end.isoformat(),
    )

    # ------------------------------------------------------------------------
    # Impact factor mapping
    # ------------------------------------------------------------------------

    impact_factor_mapping = (
        load_impact_factors(
            Path(
                cfg.get(
                    "impact_factor_file",
                    "impact_factors.yaml",
                )
            )
        )
    )

    # ------------------------------------------------------------------------
    # HTTP session
    # ------------------------------------------------------------------------

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": cfg.get(
                "user_agent",
                UA,
            )
        }
    )

    # ------------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------------

    queries = [
        str(query).strip()
        for query
        in cfg["search"]["queries"]
        if str(query).strip()
    ]

    # ------------------------------------------------------------------------
    # Database search
    # ------------------------------------------------------------------------

    all_papers: list[Paper] = []

    clients = [
        (
            "PubMed",
            PubMedClient(
                session,
                cfg,
                start,
                end,
            ),
        ),
        (
            "arXiv",
            ArxivClient(
                session,
                cfg,
                start,
                end,
            ),
        ),
        (
            "OpenAlex",
            OpenAlexClient(
                session,
                cfg,
                start,
                end,
            ),
        ),
    ]

    for name, client in clients:

        try:

            papers = client.search(
                queries
            )

            LOG.info(
                "%s returned %d candidates",
                name,
                len(papers),
            )

            all_papers.extend(
                papers
            )

        except Exception:

            LOG.exception(
                "%s search failed; "
                "continuing with remaining sources",
                name,
            )

    # ------------------------------------------------------------------------
    # Cross-source merge
    # ------------------------------------------------------------------------

    all_papers = merge_cross_source(
        all_papers
    )

    apply_impact_factors(
        all_papers,
        impact_factor_mapping,
    )

    # ------------------------------------------------------------------------
    # Boolean matching
    # ------------------------------------------------------------------------

    matching_papers: list[Paper] = []

    for paper in all_papers:

        matched_query = query_matches(
            paper,
            queries,
        )

        if matched_query:

            paper.matched_query = (
                paper.matched_query
                or matched_query
            )

            matching_papers.append(
                paper
            )

    # ------------------------------------------------------------------------
    # Persistent deduplication
    # ------------------------------------------------------------------------

    new_papers = [
        paper
        for paper
        in matching_papers
        if not sent_store.contains(
            paper
        )
    ]

    already_sent_count = (
        len(matching_papers)
        - len(new_papers)
    )

    new_papers.sort(
        key=lambda paper: (
            paper.published_date
            or "",
            paper.title,
        ),
        reverse=True,
    )

    LOG.info(
        "Total=%d, matching=%d, "
        "already_sent=%d, new=%d",
        len(all_papers),
        len(matching_papers),
        already_sent_count,
        len(new_papers),
    )

    # ------------------------------------------------------------------------
    # Send email
    # ------------------------------------------------------------------------

    email_cfg = cfg["email"]

    if (
        not new_papers
        and not email_cfg.get(
            "send_when_empty",
            True,
        )
    ):
        LOG.info(
            "No new papers and "
            "send_when_empty=false."
        )

        # Even when no email is sent, the search itself succeeded.
        # Update runtime state to prevent the next run's search window
        # from growing unnecessarily.
        runtime_state.update(
            successful_run=current_time,
            email_sent=current_time,
            new_papers=0,
            total_candidates=len(all_papers),
            search_hours=effective_hours,
        )

        runtime_state.save()

        return 0

    subject, body = build_email(
        new_papers,
        start,
        end,
        effective_hours,
        cfg,
        previous_run=previous_run,
        total_candidates=len(all_papers),
        already_sent_count=already_sent_count,
    )

    send_email(
        subject,
        body,
        cfg,
    )

    email_time = iso_now()

    LOG.info(
        "Email sent successfully."
    )

    # ------------------------------------------------------------------------
    # Persist sent paper IDs
    # ------------------------------------------------------------------------

    if new_papers:

        sent_store.mark_many(
            new_papers
        )

        sent_store.save()

        LOG.info(
            "Persistent sent-ID store updated: %s",
            sent_store.path,
        )

    else:

        LOG.info(
            "No new paper IDs to add."
        )

    # ------------------------------------------------------------------------
    # Persist runtime state
    # ------------------------------------------------------------------------

    runtime_state.update(
        successful_run=current_time,
        email_sent=email_time,
        new_papers=len(new_papers),
        total_candidates=len(all_papers),
        search_hours=effective_hours,
    )

    runtime_state.save()

    LOG.info(
        "Runtime state updated: %s",
        runtime_state.path,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
