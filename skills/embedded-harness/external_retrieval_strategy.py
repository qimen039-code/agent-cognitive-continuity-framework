#!/usr/bin/env python3
"""Build a task-local, anchor-preserving external retrieval receipt.

This module plans and reduces retrieval attempts. It never performs network
access, writes memory, or treats a provider miss as proof that an entity does
not exist.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_POLICY = SCRIPT_DIR / "embedded_harness_policy.json"

URL_RE = re.compile(r"https?://[^\s<>\"'\[\]()]+", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
RFC_RE = re.compile(r"\bRFC\s*-?\s*(?P<number>\d{3,5})\b", re.IGNORECASE)
ARXIV_RE = re.compile(
    r"\b(?:arXiv\s*:\s*)?(?P<identifier>\d{4}\.\d{4,5}(?:v\d+)?)\b",
    re.IGNORECASE,
)
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
STANDARD_RE = re.compile(
    r"\b(?:ISO(?:/IEC)?|IEC|IEEE)\s+\d+(?:[-:]\d+)*(?::\d{4})?\b",
    re.IGNORECASE,
)
SCOPED_PACKAGE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])@[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
OWNER_REPO_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))"
    r"/(?P<repo>[A-Za-z0-9_.-]{1,100})"
    r"(?![A-Za-z0-9_.-])"
)
QUOTED_RE = re.compile(r'"([^"\r\n]{2,200})"|“([^”\r\n]{2,200})”')
DOI_OR_VERSION_RE = re.compile(
    r"\b(?:v\d+(?:\.\d+){1,3}|"
    r"(?:version|release|sdk|node|python|npm|package|plugin|model)\s*:?\s*"
    r"v?\d+(?:\.\d+){1,3})\b",
    re.IGNORECASE,
)
SEMVER_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,2}\b", re.IGNORECASE)
IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+\b")
TITLE_CASE_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9-]*\s+){1,5}[A-Z][A-Za-z0-9-]*\b"
)
ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,11}\b")
ENTITY_TOKEN_RE = re.compile(
    r"\b(?:[A-Z]{2,12}|[A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Z][A-Za-z0-9]*\d+[A-Za-z0-9]*)\b"
)
ENGLISH_PHRASE_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z][A-Za-z0-9-]*){1,5}\b"
)

LEADING_SEARCH_WORDS = {
    "search",
    "find",
    "locate",
    "lookup",
    "look up",
    "check",
    "browse",
}
FACET_NOISE = {"open source", "ai framework", "github repository"}
MARKER_ACRONYMS = {
    "AI",
    "API",
    "CEO",
    "CVE",
    "DOI",
    "HF",
    "HTTP",
    "HTTPS",
    "ID",
    "ISBN",
    "NPM",
    "PMID",
    "RFC",
    "URL",
}
ENTITY_NOISE = {
    "Check",
    "Compare",
    "Find",
    "Latest",
    "Lookup",
    "Official",
    "Search",
    "Verify",
}
CJK_ENTITY_NOISE = {
    "当前",
    "最新",
    "官方",
    "价格",
    "版本",
    "状态",
    "支持",
    "负责人",
    "总干事",
    "任命",
    "现在",
    "目前",
    "现任",
}
CJK_RE = re.compile(r"[\u3400-\u9fff]")
CURRENTNESS_OR_REVISION_RE = re.compile(
    r"(?i)\b(?:current|currently|now|latest|version|release|status)\b|"
    r"现在|现任|目前|当前|最新|版本|发布|状态"
)
CJK_ENTITY_BEFORE_CLAIM_RE = re.compile(
    r"(?:^|[\n,，;；])\s*(?:请)?(?:查找|搜索|检索|核对|查看|确认)?\s*"
    r"(?P<entity>[\u3400-\u9fff·]{2,30}?)\s*(?:的)?\s*"
    r"(?=(?:当前|最新|官方|价格|版本|状态|支持|负责人|总干事|任命))"
)
PYPI_PACKAGE_RE = re.compile(
    r"(?i)(?:PyPI|python\s*(?:package|包)|pip\s*(?:package|包)?)"
    r"\s*(?:包|package)?\s*[:：]?\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
)
NPM_PACKAGE_RE = re.compile(
    r"(?i)(?:npm\s*(?:package|包)?|node(?:\.js)?\s*(?:package|包))"
    r"\s*[:：]?\s*(?P<name>@?[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?)"
)
CLAUSE_BOUNDARY_RE = re.compile(
    r"[\n,，;；、]|\b(?:and|versus|vs\.?)\b|(?:以及|并且|同时|与|和)",
    re.IGNORECASE,
)
SOURCE_CONTEXT_PATTERNS = {
    "github": re.compile(r"(?i)github(?:\.com)?|repo(?:sitory)?|仓库|开源"),
    "huggingface": re.compile(r"(?i)hugging\s*face|huggingface|\bHF\b"),
    "npm": re.compile(r"(?i)\bnpm\b|node(?:\.js)?\s*(?:package|包)"),
    "pypi": re.compile(r"(?i)\bPyPI\b|python\s*(?:package|包)|pip\s*(?:package|包)?"),
}
RFC_CONTEXT_FACETS = {
    "CONNECT",
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
    "TRACE",
}
CVE_CONTEXT_FACETS = {"CISA", "CNA", "CVSS", "NVD"}


def _load_contract(policy_path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    matrix = policy.get("search_and_learning_decision_matrix") or {}
    contract = matrix.get("external_retrieval_contract") or {}
    if contract.get("schema") != "cbh.external_retrieval_contract.v1":
        raise ValueError("missing or unsupported external retrieval contract")
    return dict(contract)


def _add_anchor(
    anchors: list[dict[str, Any]],
    seen: set[str],
    *,
    kind: str,
    raw_text: str,
    provider_hint: str = "none",
    provider_confidence: str = "none",
) -> None:
    raw = raw_text.strip().strip(".,;:!?，。；：！？")
    folded = raw.casefold()
    if not raw or folded in seen:
        return
    seen.add(folded)
    anchors.append(
        {
            "type": kind,
            "raw_text": raw,
            "preserve_verbatim": True,
            "provider_hint": provider_hint,
            "provider_confidence": provider_confidence,
        }
    )


def _overlaps(span: tuple[int, int], protected: Iterable[tuple[int, int]]) -> bool:
    return any(span[0] < end and span[1] > start for start, end in protected)


def _is_child_anchor(raw: str, anchors: Iterable[dict[str, Any]]) -> bool:
    folded = raw.casefold()
    return any(
        folded != str(anchor["raw_text"]).casefold()
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(folded)}(?![A-Za-z0-9_])", str(anchor["raw_text"]).casefold())
        for anchor in anchors
    )


def _local_context(text: str, span: tuple[int, int]) -> str:
    start, end = _local_clause_bounds(text, span)
    return text[start:end]


def _local_clause_bounds(text: str, span: tuple[int, int]) -> tuple[int, int]:
    before = list(CLAUSE_BOUNDARY_RE.finditer(text, 0, span[0]))
    start = before[-1].end() if before else 0
    after = CLAUSE_BOUNDARY_RE.search(text, span[1])
    end = after.start() if after else len(text)
    return start, end


def _local_provider_hint(text: str, span: tuple[int, int]) -> str:
    before = list(CLAUSE_BOUNDARY_RE.finditer(text, 0, span[0]))
    segment_start = before[-1].end() if before else 0
    after = CLAUSE_BOUNDARY_RE.search(text, span[1])
    segment_end = after.start() if after else len(text)
    segment = text[segment_start:segment_end]
    anchor_offset = span[0] - segment_start
    candidates: list[tuple[int, str]] = []
    for provider, pattern in SOURCE_CONTEXT_PATTERNS.items():
        for match in pattern.finditer(segment):
            candidates.append((abs(match.end() - anchor_offset), provider))
    return min(candidates)[1] if candidates else "source_registry_unknown"


def extract_exact_anchors(text: str) -> list[dict[str, Any]]:
    """Extract conservative exact anchors without normalizing their text."""

    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    protected_spans: list[tuple[int, int]] = []
    folded_text = text.casefold()
    github_context = bool(re.search(r"(?i)github(?:\.com)?|repo(?:sitory)?|仓库|开源", text))
    npm_context = bool(re.search(r"(?i)\bnpm\b|node(?:\.js)?\s*(?:package|包)", text))
    pypi_context = bool(re.search(r"(?i)\bPyPI\b|python\s*(?:package|包)|pip\s*(?:package|包)?", text))
    project_context = bool(
        re.search(r"(?i)\bproject\b|\bframework\b|\blibrary\b|\btool\b|项目|框架|工具", text)
    )

    for match in URL_RE.finditer(text):
        raw = match.group(0).rstrip(".,;:!?，。；：！？")
        protected_spans.append(match.span())
        host = urlparse(raw).netloc.casefold()
        provider = (
            "github"
            if host in {"github.com", "www.github.com"}
            else "huggingface"
            if host in {"huggingface.co", "www.huggingface.co"}
            else "direct_url"
        )
        _add_anchor(
            anchors,
            seen,
            kind="url",
            raw_text=raw,
            provider_hint=provider,
            provider_confidence="explicit_url_host",
        )
        parsed = urlparse(raw)
        if parsed.netloc.casefold() in {"github.com", "www.github.com"}:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                _add_anchor(
                    anchors,
                    seen,
                    kind="owner_repo_slug",
                    raw_text=f"{parts[0]}/{parts[1]}",
                    provider_hint="github",
                    provider_confidence="explicit_url_host",
                )

    text_without_urls = list(text)
    for start, end in protected_spans:
        text_without_urls[start:end] = " " * (end - start)
    non_url_text = "".join(text_without_urls)

    for match in QUOTED_RE.finditer(non_url_text):
        raw = match.group(1) or match.group(2)
        local_context = _local_context(non_url_text, match.span())
        local_project_context = bool(
            re.search(r"(?i)\bproject\b|\bframework\b|\blibrary\b|\btool\b|项目|框架|工具", local_context)
        )
        kind = "project_name" if local_project_context else "named_entity"
        if not CJK_RE.search(raw) and not TITLE_CASE_RE.fullmatch(raw.strip()):
            kind = "quoted_phrase"
        local_provider = _local_provider_hint(non_url_text, match.span())
        provider = "github" if local_provider == "github" and kind == "project_name" else "official_source_discovery"
        confidence = "explicit_source_context" if provider == "github" else "semantic_entity_hint"
        _add_anchor(
            anchors,
            seen,
            kind=kind,
            raw_text=raw,
            provider_hint=provider,
            provider_confidence=confidence,
        )

    for match in DOI_RE.finditer(non_url_text):
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="doi",
            raw_text=match.group(0),
            provider_hint="doi_resolver",
            provider_confidence="identifier_syntax",
        )

    for match in RFC_RE.finditer(non_url_text):
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="rfc",
            raw_text=match.group(0),
            provider_hint="rfc_editor",
            provider_confidence="identifier_syntax",
        )

    for match in ARXIV_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        if "arxiv" not in match.group(0).casefold() and "arxiv" not in folded_text:
            continue
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="arxiv_id",
            raw_text=match.group(0),
            provider_hint="arxiv",
            provider_confidence="identifier_syntax",
        )

    for match in CVE_RE.finditer(non_url_text):
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="cve_id",
            raw_text=match.group(0),
            provider_hint="cve_record",
            provider_confidence="identifier_syntax",
        )

    for match in STANDARD_RE.finditer(non_url_text):
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="standard_id",
            raw_text=match.group(0),
            provider_hint="standards_registry",
            provider_confidence="identifier_syntax",
        )

    for match in SCOPED_PACKAGE_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        protected_spans.append(match.span())
        _add_anchor(
            anchors,
            seen,
            kind="package_id",
            raw_text=match.group(0),
            provider_hint="npm" if npm_context or match.group(0).startswith("@") else "source_registry_unknown",
            provider_confidence="explicit_source_context" if npm_context else "identifier_syntax",
        )

    for matcher, provider in ((PYPI_PACKAGE_RE, "pypi"), (NPM_PACKAGE_RE, "npm")):
        for match in matcher.finditer(non_url_text):
            raw = match.group("name")
            _add_anchor(
                anchors,
                seen,
                kind="package_id",
                raw_text=raw,
                provider_hint=provider,
                provider_confidence="explicit_source_context",
            )

    for match in OWNER_REPO_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        protected_spans.append(match.span())
        raw = f"{match.group('owner')}/{match.group('repo')}"
        local_provider = _local_provider_hint(non_url_text, match.span())
        if local_provider == "huggingface":
            kind, provider = "model_or_dataset_id", "huggingface"
        elif local_provider == "npm":
            kind, provider = "package_id", "npm"
        elif local_provider == "github":
            kind, provider = "owner_repo_slug", "github"
        else:
            kind, provider = "namespaced_identifier", "source_registry_unknown"
        _add_anchor(
            anchors,
            seen,
            kind=kind,
            raw_text=raw,
            provider_hint=provider,
            provider_confidence=(
                "explicit_source_context" if provider != "source_registry_unknown" else "none"
            ),
        )

    for match in SEMVER_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        _add_anchor(
            anchors,
            seen,
            kind="version_or_release",
            raw_text=match.group(0),
            provider_hint="official_source_discovery",
            provider_confidence="identifier_syntax",
        )

    for match in DOI_OR_VERSION_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        _add_anchor(
            anchors,
            seen,
            kind="version_or_release",
            raw_text=match.group(0),
            provider_hint="official_source_discovery",
            provider_confidence="task_context_required",
        )

    for match in IDENTIFIER_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans) or _is_child_anchor(match.group(0), anchors):
            continue
        kind = "error_literal" if re.search(r"ERR|ERROR|EXCEPTION|FAIL", match.group(0), re.IGNORECASE) else "identifier"
        _add_anchor(
            anchors,
            seen,
            kind=kind,
            raw_text=match.group(0),
            provider_hint="official_source_discovery",
            provider_confidence="identifier_syntax",
        )

    for match in TITLE_CASE_RE.finditer(non_url_text):
        if _overlaps(match.span(), protected_spans):
            continue
        raw = match.group(0).strip()
        words = raw.split()
        if words and words[0].casefold() in LEADING_SEARCH_WORDS and len(words) > 2:
            raw = " ".join(words[1:])
        local_context = _local_context(non_url_text, match.span())
        local_project_context = bool(
            re.search(r"(?i)\bproject\b|\bframework\b|\blibrary\b|\btool\b|项目|框架|工具", local_context)
        )
        kind = "project_name" if local_project_context else "named_entity"
        local_provider = _local_provider_hint(non_url_text, match.span())
        provider = "github" if local_provider == "github" and kind == "project_name" else "official_source_discovery"
        _add_anchor(
            anchors,
            seen,
            kind=kind,
            raw_text=raw,
            provider_hint=provider,
            provider_confidence="explicit_source_context" if provider == "github" else "semantic_entity_hint",
        )

    for match in ENTITY_TOKEN_RE.finditer(non_url_text):
        raw = match.group(0)
        if (
            raw in MARKER_ACRONYMS
            or raw in ENTITY_NOISE
            or _overlaps(match.span(), protected_spans)
            or _is_child_anchor(raw, anchors)
        ):
            continue
        _add_anchor(
            anchors,
            seen,
            kind="named_entity",
            raw_text=raw,
            provider_hint="official_source_discovery",
            provider_confidence="semantic_entity_hint",
        )

    for match in CJK_ENTITY_BEFORE_CLAIM_RE.finditer(non_url_text):
        raw = match.group("entity")
        raw = re.sub(r"^(?:查找|搜索|检索|核对|查看|确认)", "", raw).strip()
        if (
            raw
            and raw not in CJK_ENTITY_NOISE
            and not _is_child_anchor(raw, anchors)
        ):
            _add_anchor(
                anchors,
                seen,
                kind="named_entity",
                raw_text=raw,
                provider_hint="official_source_discovery",
                provider_confidence="semantic_entity_hint",
            )

    for match in ACRONYM_RE.finditer(non_url_text):
        raw = match.group(0)
        if raw not in MARKER_ACRONYMS and not _is_child_anchor(raw, anchors):
            _add_anchor(anchors, seen, kind="acronym", raw_text=raw)

    provider_markers = {
        "github": "github",
        "hugging face": "huggingface",
        "pypi": "pypi",
        "arxiv": "arxiv",
    }
    provider_hints = {str(anchor.get("provider_hint")) for anchor in anchors}
    anchors = [
        anchor
        for anchor in anchors
        if not (
            str(anchor["raw_text"]).casefold() in provider_markers
            and provider_markers[str(anchor["raw_text"]).casefold()] in provider_hints
            and any(
                other is not anchor
                and str(other.get("provider_hint"))
                == provider_markers[str(anchor["raw_text"]).casefold()]
                for other in anchors
            )
        )
    ]
    anchor_types = {str(anchor["type"]) for anchor in anchors}
    contextual_facets: set[str] = set()
    if anchor_types.intersection({"rfc", "standard_id"}):
        contextual_facets.update(RFC_CONTEXT_FACETS)
    if "cve_id" in anchor_types:
        contextual_facets.update(CVE_CONTEXT_FACETS)
    anchors = [
        anchor
        for anchor in anchors
        if not (
            str(anchor["raw_text"]) in contextual_facets
            and str(anchor["type"]) in {"acronym", "named_entity", "identifier"}
        )
    ]

    for index, anchor in enumerate(anchors, start=1):
        anchor["target_id"] = f"anchor-{index:03d}"

    return anchors


def extract_semantic_facets(
    text: str, exact_anchors: Iterable[dict[str, Any]]
) -> list[str]:
    """Return task-local facet hints; the agent remains the semantic reviewer."""

    exact = {str(anchor["raw_text"]).casefold() for anchor in exact_anchors}
    facets: list[str] = []
    seen: set[str] = set()
    segments = re.split(r"[、,，;；\n]|\b(?:and|plus|with)\b|和|与|以及", text)
    for segment in segments:
        working = segment
        for anchor in exact_anchors:
            working = working.replace(str(anchor["raw_text"]), " ")
        working = re.sub(
            r"(?i)^\s*(?:search|find|locate|lookup|look up|check|browse|verify|compare)\s+",
            "",
            working,
        )
        working = re.sub(r"^\s*(?:按自然搜索)?(?:查找|搜索|检索|核对|查看|确认|验证|比较|对比)\s*", "", working)
        working = re.sub(
            r"(?i)\b(?:GitHub|Hugging\s*Face|PyPI|npm|arXiv)\b",
            " ",
            working,
        )
        working = re.sub(r"[\"'“”]", " ", working)
        for match in ENGLISH_PHRASE_RE.finditer(working):
            raw = match.group(0).strip()
            folded = raw.casefold()
            words = folded.split()
            if folded in exact or folded in FACET_NOISE:
                continue
            if words and words[0] in LEADING_SEARCH_WORDS:
                continue
            if folded not in seen:
                seen.add(folded)
                facets.append(raw)
        if CJK_RE.search(working):
            raw = re.sub(r"\s+", " ", working).strip(" \t:：的")
            folded = raw.casefold()
            if raw in {"包", "仓库", "项目", "框架", "模型", "数据集"}:
                continue
            if len(raw) >= 2 and folded not in exact and folded not in seen:
                seen.add(folded)
                facets.append(raw)
    anchor_types = {str(anchor["type"]) for anchor in exact_anchors}
    contextual_facets: set[str] = set()
    if anchor_types.intersection({"rfc", "standard_id"}):
        contextual_facets.update(RFC_CONTEXT_FACETS)
    if "cve_id" in anchor_types:
        contextual_facets.update(CVE_CONTEXT_FACETS)
    for raw in ACRONYM_RE.findall(text):
        folded = raw.casefold()
        if (
            raw in contextual_facets
            and folded not in exact
            and folded not in seen
            and not any(folded in facet.casefold() for facet in facets)
        ):
            seen.add(folded)
            facets.append(raw)
    return facets


def _bind_facets_to_anchors(
    text: str,
    facets: Iterable[str],
    exact_anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchor_spans: dict[str, list[tuple[int, int]]] = {}
    for anchor in exact_anchors:
        raw = str(anchor["raw_text"])
        anchor_spans[str(anchor["target_id"])] = [
            match.span()
            for match in re.finditer(re.escape(raw), text, re.IGNORECASE)
        ]

    bindings: list[dict[str, Any]] = []
    all_target_ids = [str(anchor["target_id"]) for anchor in exact_anchors]
    for facet in facets:
        local_target_ids: list[str] = []
        facet_matches = list(
            re.finditer(re.escape(str(facet)), text, re.IGNORECASE)
        )
        for facet_match in facet_matches:
            clause_start, clause_end = _local_clause_bounds(text, facet_match.span())
            for target_id, spans in anchor_spans.items():
                if any(
                    anchor_start >= clause_start and anchor_end <= clause_end
                    for anchor_start, anchor_end in spans
                ) and target_id not in local_target_ids:
                    local_target_ids.append(target_id)

        if local_target_ids:
            linked_target_ids = local_target_ids
            association_status = (
                "local_clause_unique"
                if len(local_target_ids) == 1
                else "local_clause_multiple"
            )
        elif len(all_target_ids) == 1:
            linked_target_ids = all_target_ids
            association_status = "single_anchor_default"
        elif all_target_ids:
            linked_target_ids = all_target_ids
            association_status = "ambiguous_all_anchors"
        else:
            linked_target_ids = []
            association_status = "no_exact_anchor"
        bindings.append(
            {
                "facet": str(facet),
                "linked_target_ids": linked_target_ids,
                "association_status": association_status,
            }
        )
    return bindings


def _query(
    query_id: str,
    *,
    query_text: str,
    query_type: str,
    execution_group: str,
    mode: str,
    provider_hint: str,
    activation_condition: str,
    anchor: str | None = None,
    direct_url: str | None = None,
    target_id: str | None = None,
    source_route_id: str | None = None,
    authority_boundary: str | None = None,
    provider_confidence: str | None = None,
    expected_canonical_url: str | None = None,
    expected_canonical_urls: Iterable[str] = (),
    expected_canonical_urls_by_route: dict[str, Iterable[str]] | None = None,
    allowed_source_route_ids: Iterable[str] = (),
    linked_target_ids: Iterable[str] = (),
    association_status: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query_id": query_id,
        "query_text": query_text,
        "query_type": query_type,
        "execution_group": execution_group,
        "mode": mode,
        "provider_hint": provider_hint,
        "activation_condition": activation_condition,
        "result_budget_scope": "per_query_soft_target_expand_for_coverage",
    }
    if anchor is not None:
        result["exact_anchor"] = anchor
        result["anchor_preserved"] = anchor in query_text
    if direct_url is not None:
        result["direct_url"] = direct_url
    if target_id is not None:
        result["target_id"] = target_id
    if source_route_id is not None:
        result["source_route_id"] = source_route_id
    if authority_boundary is not None:
        result["authority_boundary"] = authority_boundary
    if provider_confidence is not None:
        result["provider_confidence"] = provider_confidence
    if expected_canonical_url is not None:
        result["expected_canonical_url"] = expected_canonical_url
    expected_urls = list(dict.fromkeys(str(item) for item in expected_canonical_urls if str(item)))
    if expected_urls:
        result["expected_canonical_urls"] = expected_urls
    expected_by_route = {
        str(route_id): list(
            dict.fromkeys(str(item) for item in route_urls if str(item))
        )
        for route_id, route_urls in (expected_canonical_urls_by_route or {}).items()
    }
    if expected_by_route:
        result["expected_canonical_urls_by_route"] = expected_by_route
    allowed_routes = list(dict.fromkeys(str(item) for item in allowed_source_route_ids if str(item)))
    if allowed_routes:
        result["allowed_source_route_ids"] = allowed_routes
    linked_targets = list(dict.fromkeys(str(item) for item in linked_target_ids if str(item)))
    if linked_targets:
        result["linked_target_ids"] = linked_targets
    if association_status is not None:
        result["association_status"] = association_status
    return result


def _matching_source_routes(
    anchor: dict[str, Any], contract: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    kind = str(anchor["type"])
    provider = str(anchor.get("provider_hint") or "none")
    matches: list[tuple[str, dict[str, Any]]] = []
    for route_id, raw_route in (contract.get("source_native_routes") or {}).items():
        route = dict(raw_route)
        if kind not in {str(item) for item in route.get("anchor_types") or []}:
            continue
        provider_hints = {str(item) for item in route.get("provider_hints") or []}
        if provider in provider_hints:
            matches.append((str(route_id), route))
    return matches


def _render_source_route(template: str, raw: str) -> str:
    number_match = re.search(r"\d+", raw)
    identifier = re.sub(r"(?i)^arXiv\s*:\s*", "", raw)
    values = {
        "raw": raw,
        "quoted": f'"{raw}"',
        "raw_url": quote(raw, safe="/@:-._"),
        "number": number_match.group(0) if number_match else "",
        "identifier": identifier,
    }
    return template.format(**values)


def _parse_timezone_aware_iso8601(value: str) -> datetime | None:
    if not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _build_query_plan(
    task_text: str,
    exact_anchors: list[dict[str, Any]],
    facet_bindings: list[dict[str, Any]],
    modes: list[str],
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    sequence = 1

    for anchor in exact_anchors:
        raw = str(anchor["raw_text"])
        kind = str(anchor["type"])
        target_id = str(anchor["target_id"])
        provider_confidence = str(anchor.get("provider_confidence") or "none")
        if kind == "url":
            plan.append(
                _query(
                    f"q-{sequence:03d}",
                    query_text=raw,
                    query_type="direct_candidate_verification",
                    execution_group="anchor_first_standalone",
                    mode="direct_candidate_verification",
                    provider_hint=str(anchor["provider_hint"]),
                    activation_condition="immediate",
                    anchor=raw,
                    direct_url=raw,
                    target_id=target_id,
                    provider_confidence=provider_confidence,
                )
            )
            sequence += 1
            continue

        quoted = (
            f'"{raw}"'
            if kind
            in {
                "project_name",
                "quoted_phrase",
                "named_entity",
                "namespaced_identifier",
            }
            else raw
        )
        source_routes = _matching_source_routes(anchor, contract)
        native_first = kind in {
            "doi",
            "rfc",
            "arxiv_id",
            "cve_id",
            "standard_id",
            "package_id",
            "model_or_dataset_id",
        }

        def append_literal_query() -> None:
            nonlocal sequence
            plan.append(
                _query(
                    f"q-{sequence:03d}",
                    query_text=quoted,
                    query_type="literal_anchor",
                    execution_group="anchor_first_standalone",
                    mode="general_web_cross_check",
                    provider_hint="general_web",
                    activation_condition=(
                        "source_native_weak_empty_conflicting_or_unavailable"
                        if native_first
                        else "immediate"
                    ),
                    anchor=raw,
                    target_id=target_id,
                    provider_confidence=provider_confidence,
                )
            )
            sequence += 1

        def append_source_routes() -> None:
            nonlocal sequence
            for route_id, route in source_routes:
                route_query = _render_source_route(
                    str(route["query_template"]), raw
                )
                direct_template = str(route.get("direct_url_template") or "")
                direct_url = (
                    _render_source_route(direct_template, raw)
                    if direct_template
                    else None
                )
                plan.append(
                    _query(
                        f"q-{sequence:03d}",
                        query_text=route_query,
                        query_type="source_native_fallback",
                        execution_group="source_native_fallback",
                        mode=str(route["mode"]),
                        provider_hint=str(anchor["provider_hint"]),
                        activation_condition=str(route["activation_condition"]),
                        anchor=raw,
                        target_id=target_id,
                        source_route_id=route_id,
                        authority_boundary=str(route["authority_boundary"]),
                        provider_confidence=provider_confidence,
                        expected_canonical_url=direct_url,
                    )
                )
                sequence += 1
                if direct_url:
                    plan.append(
                        _query(
                            f"q-{sequence:03d}",
                            query_text=direct_url,
                            query_type="direct_candidate_verification",
                            execution_group="candidate_verification",
                            mode="direct_candidate_verification",
                            provider_hint=str(anchor["provider_hint"]),
                            activation_condition="source_native_candidate_found_or_known_identifier",
                            anchor=raw,
                            direct_url=direct_url,
                            target_id=target_id,
                            source_route_id=route_id,
                            authority_boundary=str(route["authority_boundary"]),
                            provider_confidence=provider_confidence,
                        )
                    )
                    sequence += 1

        if native_first:
            append_source_routes()
            append_literal_query()
        else:
            append_literal_query()
            append_source_routes()

    if task_text.strip():
        original_modes = modes or ["general_web_cross_check"]
        for mode_index, mode in enumerate(original_modes):
            plan.append(
                _query(
                    f"q-{sequence:03d}",
                    query_text=task_text.strip(),
                    query_type="original_query",
                    execution_group="original_query_standalone",
                    mode=mode,
                    provider_hint="route_selected",
                    activation_condition=(
                        "immediate" if mode_index == 0 else "previous_source_weak_empty_conflicting_or_unavailable"
                    ),
                    target_id="task",
                )
            )
            sequence += 1

    anchor_by_id = {
        str(anchor["target_id"]): anchor for anchor in exact_anchors
    }
    for facet_index, binding in enumerate(facet_bindings, start=1):
        facet = str(binding["facet"])
        linked_target_ids = [
            str(item) for item in binding.get("linked_target_ids") or []
        ]
        facet_route_ids: list[str] = []
        facet_expected_urls_by_route: dict[str, list[str]] = {}
        for linked_target_id in linked_target_ids:
            anchor = anchor_by_id.get(linked_target_id)
            if anchor is None:
                continue
            raw = str(anchor["raw_text"])
            for route_id, route in _matching_source_routes(anchor, contract):
                if route_id not in facet_route_ids:
                    facet_route_ids.append(route_id)
                direct_template = str(route.get("direct_url_template") or "")
                if direct_template:
                    direct_url = _render_source_route(direct_template, raw)
                    route_urls = facet_expected_urls_by_route.setdefault(
                        route_id, []
                    )
                    if direct_url not in route_urls:
                        route_urls.append(direct_url)
        if not facet_route_ids:
            facet_route_ids.extend(
                str(item)
                for item in contract.get("generic_facet_source_route_ids") or []
            )
        plan.append(
            _query(
                f"q-{sequence:03d}",
                query_text=facet,
                query_type="semantic_facet",
                execution_group="semantic_expansion",
                mode="general_web_cross_check",
                provider_hint="general_web",
                activation_condition="facet_unresolved_after_anchor_and_original_queries",
                target_id=f"facet-{facet_index:03d}",
                expected_canonical_urls_by_route=facet_expected_urls_by_route,
                allowed_source_route_ids=facet_route_ids,
                linked_target_ids=linked_target_ids,
                association_status=str(binding["association_status"]),
            )
        )
        sequence += 1

    return plan


def _attempt_list(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise ValueError("attempts must be a JSON object or array of objects")
    return [dict(item) for item in parsed]


def _reduce_attempts(
    receipt: dict[str, Any], attempts: list[dict[str, Any]]
) -> None:
    if not attempts:
        return

    normalized: list[dict[str, Any]] = []
    verified_absence_bases = {
        "closed_world_complete_source",
        "authority_direct_negative",
        "known_url_verifiable_not_found",
    }
    attempted_modes: list[str] = []
    attempted_query_ids: set[str] = set()
    plan_by_id = {str(item["query_id"]): item for item in receipt["query_plan"]}
    anchor_by_raw = {
        str(item["raw_text"]): str(item["target_id"])
        for item in receipt["exact_anchors"]
    }
    facet_by_raw = {
        str(item["facet"]): str(item["target_id"])
        for item in receipt["facet_coverage"]
    }
    anchor_info_by_id = {
        str(item["target_id"]): item for item in receipt["exact_anchors"]
    }
    valid_routes_by_target: dict[str, set[str]] = {}
    for candidate in receipt["source_capability_candidates"]:
        valid_routes_by_target.setdefault(str(candidate["target_id"]), set()).add(
            str(candidate["source_route_id"])
        )
    registered_source_route_ids = {
        str(item) for item in receipt.get("registered_source_route_ids") or []
    }
    authority_binding_status_values = {
        str(item) for item in receipt.get("authority_binding_status_values") or []
    }
    max_future_skew_seconds = int(receipt.get("max_future_skew_seconds") or 0)
    max_freshness_window_seconds = int(
        receipt.get("max_freshness_window_seconds") or 0
    )
    evaluated_at = datetime.now(timezone.utc)
    anchor_state = {
        str(item["target_id"]): {
            "hit": False,
            "source_read": False,
            "verified_absent": False,
            "attempted_query_ids": [],
            "canonical_source_refs": [],
        }
        for item in receipt["target_coverage"]
    }
    facet_state = {
        str(item["target_id"]): {
            "hit": False,
            "source_read": False,
            "attempted_query_ids": [],
            "canonical_source_refs": [],
        }
        for item in receipt["facet_coverage"]
    }

    for index, attempt in enumerate(attempts, start=1):
        query_id = str(attempt.get("query_id") or "unknown")
        attempted_query_ids.add(query_id)
        planned_query = plan_by_id.get(query_id) or {}
        mode = str(attempt.get("mode") or planned_query.get("mode") or "unknown")
        attempted_modes.append(mode)
        exact_hits = [str(item) for item in attempt.get("exact_anchor_hits") or []]
        facet_hits = [str(item) for item in attempt.get("covered_facets") or []]
        source_read = bool(attempt.get("source_read"))
        canonical_refs = [
            str(item) for item in attempt.get("canonical_source_refs") or []
        ]
        source_route_id = str(
            attempt.get("source_route_id")
            or planned_query.get("source_route_id")
            or ""
        )
        source_role = str(attempt.get("source_role") or "")
        primary_source_verified = bool(attempt.get("primary_source_verified"))
        conflict_status = str(attempt.get("conflict_status") or "unreviewed")
        checked_at = str(attempt.get("checked_at") or "")
        checked_at_value = _parse_timezone_aware_iso8601(checked_at)
        freshness_basis = str(attempt.get("freshness_basis") or "").strip()
        freshness_window_raw = attempt.get("freshness_window_seconds")
        freshness_window_seconds: float | None = None
        if not isinstance(freshness_window_raw, bool):
            try:
                parsed_window = float(freshness_window_raw)
            except (TypeError, ValueError):
                parsed_window = 0.0
            if 0 < parsed_window <= max_freshness_window_seconds:
                freshness_window_seconds = parsed_window
        authority_binding_status = str(
            attempt.get("authority_binding_status") or "semantic_review_required"
        )
        authority_binding_basis = str(
            attempt.get("authority_binding_basis") or ""
        ).strip()
        supports_target_ids = [
            str(item) for item in attempt.get("supports_target_ids") or []
        ]
        planned_query_known = query_id in plan_by_id
        explicit_target_ids = [str(item) for item in attempt.get("target_ids") or []]
        planned_target = str(planned_query.get("target_id") or "")

        if not receipt["currentness_or_revision_required"]:
            freshness_validation_status = "not_required"
        elif checked_at_value is None:
            freshness_validation_status = "invalid_or_missing_timestamp"
        elif checked_at_value > evaluated_at + timedelta(seconds=max_future_skew_seconds):
            freshness_validation_status = "future_timestamp"
        elif freshness_window_seconds is None:
            freshness_validation_status = "missing_or_invalid_window"
        elif (evaluated_at - checked_at_value).total_seconds() > freshness_window_seconds:
            freshness_validation_status = "outside_declared_window"
        elif not freshness_basis:
            freshness_validation_status = "missing_basis"
        else:
            freshness_validation_status = "valid_within_declared_window"
        if (
            planned_target
            and planned_target != "task"
            and planned_target not in explicit_target_ids
        ):
            explicit_target_ids.append(planned_target)
        for target_id in explicit_target_ids:
            if target_id in anchor_state:
                anchor_state[target_id]["attempted_query_ids"].append(query_id)
            if target_id in facet_state:
                facet_state[target_id]["attempted_query_ids"].append(query_id)

        def expected_urls_for(target_id: str) -> list[str]:
            expected_urls: list[str] = []
            if planned_query.get("direct_url"):
                expected_urls.append(str(planned_query["direct_url"]))
            if planned_query.get("expected_canonical_url"):
                expected_urls.append(str(planned_query["expected_canonical_url"]))
            expected_urls.extend(
                str(item)
                for item in planned_query.get("expected_canonical_urls") or []
            )
            expected_by_route = planned_query.get(
                "expected_canonical_urls_by_route"
            ) or {}
            expected_urls.extend(
                str(item) for item in expected_by_route.get(source_route_id, [])
            )
            anchor_info = anchor_info_by_id.get(target_id) or {}
            if anchor_info.get("type") == "url":
                expected_urls.append(str(anchor_info.get("raw_text") or ""))
            return list(dict.fromkeys(item for item in expected_urls if item))

        normalized_canonical_refs = {ref.rstrip("/") for ref in canonical_refs}

        def canonical_url_matched(target_id: str) -> bool:
            return any(
                expected.rstrip("/") in normalized_canonical_refs
                for expected in expected_urls_for(target_id)
            )

        def evidence_qualified(target_id: str) -> bool:
            if not planned_query_known or planned_target != target_id:
                return False
            if target_id not in supports_target_ids:
                return False
            if not source_read or not canonical_refs or not primary_source_verified:
                return False
            if source_role not in {
                "primary",
                "official_authority",
                "canonical_registry",
                "canonical_target",
            }:
                return False
            if conflict_status not in {
                "not_observed",
                "not_applicable",
                "resolved",
            }:
                return False
            if (
                receipt["currentness_or_revision_required"]
                and freshness_validation_status != "valid_within_declared_window"
            ):
                return False
            if source_route_id and source_route_id not in registered_source_route_ids:
                return False
            valid_routes = valid_routes_by_target.get(target_id, set())
            if valid_routes and source_route_id not in valid_routes:
                return False
            planned_allowed_routes = {
                str(item)
                for item in planned_query.get("allowed_source_route_ids") or []
            }
            if planned_allowed_routes and source_route_id not in planned_allowed_routes:
                return False
            expected_urls = expected_urls_for(target_id)
            if expected_urls:
                if not canonical_url_matched(target_id):
                    return False
            elif not source_route_id:
                return False
            if (
                target_id in facet_state or not expected_urls
            ) and (
                authority_binding_status != "verified_for_target"
                or authority_binding_status not in authority_binding_status_values
                or not authority_binding_basis
            ):
                return False
            return True

        for raw in exact_hits:
            target_id = anchor_by_raw.get(raw)
            if target_id is None or planned_target != target_id or not planned_query_known:
                continue
            anchor_state[target_id]["hit"] = True
            if evidence_qualified(target_id):
                anchor_state[target_id]["source_read"] = True
                anchor_state[target_id]["canonical_source_refs"].extend(canonical_refs)
        for facet in facet_hits:
            target_id = facet_by_raw.get(facet)
            if target_id is None or planned_target != target_id or not planned_query_known:
                continue
            facet_state[target_id]["hit"] = True
            if evidence_qualified(target_id):
                facet_state[target_id]["source_read"] = True
                facet_state[target_id]["canonical_source_refs"].extend(canonical_refs)

        absence_basis = str(attempt.get("absence_basis") or "")
        absence_targets = [str(item) for item in attempt.get("absence_targets") or []]
        if (
            bool(attempt.get("verified_absent"))
            and absence_basis in verified_absence_bases
            and planned_query_known
        ):
            if not absence_targets and planned_target in anchor_state:
                absence_targets = [planned_target]
            for target in absence_targets:
                target_id = anchor_by_raw.get(target, target)
                if (
                    target_id in anchor_state
                    and target_id == planned_target
                    and evidence_qualified(target_id)
                ):
                    anchor_state[target_id]["verified_absent"] = True
                    anchor_state[target_id]["canonical_source_refs"].extend(
                        canonical_refs
                    )

        normalized.append(
            {
                "attempt_id": str(
                    attempt.get("attempt_id") or f"attempt-{index:03d}"
                ),
                "query_id": query_id,
                "mode": mode,
                "provider": str(attempt.get("provider") or "unknown"),
                "provider_status": str(
                    attempt.get("provider_status") or "unknown"
                ),
                "result_count": int(attempt.get("result_count") or 0),
                "exact_anchor_hits": exact_hits,
                "covered_facets": facet_hits,
                "source_read": source_read,
                "canonical_source_refs": canonical_refs,
                "absence_basis": absence_basis or None,
                "target_ids": explicit_target_ids,
                "absence_targets": absence_targets,
                "source_route_id": source_route_id or None,
                "source_role": source_role or None,
                "primary_source_verified": primary_source_verified,
                "supports_target_ids": supports_target_ids,
                "linked_target_ids": list(
                    str(item) for item in planned_query.get("linked_target_ids") or []
                ),
                "facet_association_status": planned_query.get("association_status"),
                "checked_at": checked_at or None,
                "freshness_window_seconds": freshness_window_seconds,
                "freshness_basis": freshness_basis or None,
                "freshness_validation_status": freshness_validation_status,
                "revision": attempt.get("revision"),
                "conflict_status": conflict_status,
                "authority_binding_status": (
                    "not_required_canonical_url_matched"
                    if canonical_url_matched(planned_target)
                    and planned_target not in facet_state
                    else authority_binding_status
                ),
                "authority_binding_basis": authority_binding_basis or None,
                "evidence_binding_status": (
                    "qualified_for_declared_targets"
                    if any(
                        evidence_qualified(target_id)
                        for target_id in supports_target_ids
                    )
                    else "unqualified_or_unbound"
                ),
            }
        )

    receipt["source_ledger_or_citations"] = normalized
    receipt["fallback_state"]["exhausted_modes"] = list(
        dict.fromkeys(attempted_modes)
    )
    receipt["fallback_state"]["exhausted_query_ids"] = sorted(
        attempted_query_ids
    )

    for item in receipt["target_coverage"]:
        state = anchor_state[str(item["target_id"])]
        if state["verified_absent"]:
            status = "verified_absent"
        elif state["source_read"]:
            status = "verified_at_primary_source"
        elif state["hit"]:
            status = "source_read_required"
        else:
            status = "unresolved"
        item.update(
            {
                "status": status,
                "attempted_query_ids": list(
                    dict.fromkeys(state["attempted_query_ids"])
                ),
                "canonical_source_refs": list(
                    dict.fromkeys(state["canonical_source_refs"])
                ),
            }
        )
    for item in receipt["facet_coverage"]:
        state = facet_state[str(item["target_id"])]
        if state["source_read"]:
            status = "verified_at_primary_source"
        elif state["hit"]:
            status = "source_read_required"
        else:
            status = "unresolved"
        item.update(
            {
                "status": status,
                "attempted_query_ids": list(
                    dict.fromkeys(state["attempted_query_ids"])
                ),
                "canonical_source_refs": list(
                    dict.fromkeys(state["canonical_source_refs"])
                ),
            }
        )

    unresolved_anchor_targets = [
        item
        for item in receipt["target_coverage"]
        if item["status"] == "unresolved"
    ]
    unread_anchor_targets = [
        item
        for item in receipt["target_coverage"]
        if item["status"] == "source_read_required"
    ]
    unresolved_facet_targets = [
        item
        for item in receipt["facet_coverage"]
        if item["status"] == "unresolved"
    ]
    unread_facet_targets = [
        item
        for item in receipt["facet_coverage"]
        if item["status"] == "source_read_required"
    ]
    receipt["unresolved_facets"] = [
        str(item["facet"]) for item in unresolved_facet_targets
    ]

    def next_untried(
        target_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        for query in receipt["query_plan"]:
            if str(query["query_id"]) in attempted_query_ids:
                continue
            if (
                target_ids is not None
                and str(query.get("target_id") or "") not in target_ids
            ):
                continue
            return query
        return None

    def set_next(
        status: str, reason: str, query: dict[str, Any] | None
    ) -> None:
        receipt["coverage_status"] = status
        receipt["fallback_state"].update(
            {
                "reason": reason,
                "next_mode": query.get("mode") if query else None,
                "next_query_id": query.get("query_id") if query else None,
            }
        )

    if unread_anchor_targets:
        target_ids = {
            str(item["target_id"]) for item in unread_anchor_targets
        }
        set_next(
            "source_read_required",
            "candidate_found_but_matching_primary_source_not_opened",
            next_untried(target_ids),
        )
        return

    if unresolved_anchor_targets:
        target_ids = {
            str(item["target_id"]) for item in unresolved_anchor_targets
        }
        next_query = next_untried(target_ids)
        set_next(
            "fallback_required" if next_query else "inconclusive",
            (
                "exact_anchor_not_found_in_checked_surface"
                if next_query
                else "exact_anchor_unresolved_after_checked_queries"
            ),
            next_query,
        )
        return

    if unread_facet_targets:
        target_ids = {
            str(item["target_id"]) for item in unread_facet_targets
        }
        set_next(
            "source_read_required",
            "facet_candidate_found_but_matching_primary_source_not_opened",
            next_untried(target_ids),
        )
        return

    if unresolved_facet_targets:
        target_ids = {
            str(item["target_id"]) for item in unresolved_facet_targets
        }
        set_next(
            "semantic_review_required",
            "unresolved_semantic_facets",
            next_untried(target_ids) or next_untried({"task"}),
        )
        return

    if not receipt["target_coverage"] and not receipt["facet_coverage"]:
        set_next(
            "semantic_review_required",
            "no_mechanical_targets_extracted_model_semantic_review_required",
            next_untried({"task"}),
        )
        return

    if receipt["semantic_review_required_hint"]:
        set_next(
            "semantic_review_required",
            "mechanical_extraction_does_not_cover_full_task_semantics",
            next_untried({"task"}),
        )
        return

    anchor_statuses = [
        str(item["status"]) for item in receipt["target_coverage"]
    ]
    if (
        anchor_statuses
        and all(status == "verified_absent" for status in anchor_statuses)
        and not receipt["facet_coverage"]
    ):
        set_next(
            "verified_absent",
            "every_exact_target_has_target_bound_verified_absence",
            None,
        )
        return

    set_next(
        "complete",
        "every_exact_target_and_semantic_facet_has_matching_primary_source_evidence",
        None,
    )


def _needs_semantic_review(
    task_text: str,
    anchors: list[dict[str, Any]],
    facets: list[str],
) -> bool:
    if not anchors and not facets:
        return True
    if any(
        str(anchor.get("provider_hint")) == "source_registry_unknown"
        for anchor in anchors
    ):
        return True
    residual = task_text
    for anchor in anchors:
        residual = residual.replace(str(anchor["raw_text"]), " ")
    for facet in facets:
        residual = residual.replace(str(facet), " ")
    residual = re.sub(
        r"(?i)\b(?:search|find|locate|lookup|look\s+up|check|browse|verify|compare|and|plus|with|GitHub|Hugging\s*Face|PyPI|npm|arXiv|DOI|RFC|CVE)\b",
        " ",
        residual,
    )
    residual = re.sub(
        r"(?:查找|搜索|检索|核对|查看|确认|验证|比较|对比|以及|并且|同时|和|与|关于|对|在|由|的|仓库|开源|项目|框架|模型|数据集|包)",
        " ",
        residual,
    )
    residual = re.sub(r"[\s\"'“”‘’、,，;；:：.!！?？()（）/\\-]+", "", residual)
    return bool(re.search(r"[A-Za-z0-9\u3400-\u9fff]", residual))


def build_external_retrieval_receipt(
    task_text: str,
    *,
    recommended_modes: Iterable[str] = (),
    attempts: Any = None,
    contract: dict[str, Any] | None = None,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    contract = dict(contract or _load_contract(policy_path))
    modes = list(dict.fromkeys(str(mode) for mode in recommended_modes if str(mode)))
    requested = bool(modes)
    anchors = extract_exact_anchors(task_text) if requested else []
    facets = extract_semantic_facets(task_text, anchors) if requested else []
    facet_bindings = (
        _bind_facets_to_anchors(task_text, facets, anchors) if requested else []
    )
    if anchors and facets:
        profile = "exact_anchor_plus_facet_coverage"
    elif anchors:
        profile = "exact_anchor_first"
    elif requested:
        profile = "facet_coverage"
    else:
        profile = "none"

    query_plan = (
        _build_query_plan(task_text, anchors, facet_bindings, modes, contract)
        if requested
        else []
    )
    semantic_review_required_hint = (
        (
            _needs_semantic_review(task_text, anchors, facets)
            or any(
                binding["association_status"]
                in {"ambiguous_all_anchors", "local_clause_multiple"}
                for binding in facet_bindings
            )
        )
        if requested
        else False
    )
    currentness_or_revision_required = bool(
        requested and CURRENTNESS_OR_REVISION_RE.search(task_text)
    )
    target_coverage = [
        {
            "target_id": str(anchor["target_id"]),
            "target_kind": str(anchor["type"]),
            "raw_text": str(anchor["raw_text"]),
            "status": "planned",
            "attempted_query_ids": [],
            "canonical_source_refs": [],
        }
        for anchor in anchors
    ]
    facet_coverage = [
        {
            "target_id": f"facet-{index:03d}",
            "facet": str(binding["facet"]),
            "linked_target_ids": list(binding["linked_target_ids"]),
            "association_status": str(binding["association_status"]),
            "status": "planned",
            "attempted_query_ids": [],
            "canonical_source_refs": [],
        }
        for index, binding in enumerate(facet_bindings, start=1)
    ]
    source_capability_candidates = [
        {
            "target_id": str(query["target_id"]),
            "source_route_id": str(query["source_route_id"]),
            "mode": str(query["mode"]),
            "provider_hint": str(query["provider_hint"]),
            "provider_confidence": str(query.get("provider_confidence") or "none"),
            "activation_condition": str(query["activation_condition"]),
            "authority_boundary": str(query["authority_boundary"]),
        }
        for query in query_plan
        if query.get("source_route_id")
        and query.get("query_type") == "source_native_fallback"
    ]
    if not anchors:
        anchor_status = "not_applicable"
    elif all(
        any(
            query.get("exact_anchor") == anchor["raw_text"]
            and query.get("anchor_preserved") is True
            for query in query_plan
        )
        for anchor in anchors
    ):
        anchor_status = "pass"
    else:
        anchor_status = "fail"
    receipt: dict[str, Any] = {
        "schema": "cbh.external_retrieval_receipt.v1",
        "network_access_performed": False,
        "durable_memory_write_performed": False,
        "execution_owner": "host_model_agent",
        "retrieval_profile": profile,
        "recommended_modes": modes,
        "original_query": task_text,
        "original_query_preserved": bool(task_text and any(
            query["query_type"] == "original_query" and query["query_text"] == task_text.strip()
            for query in query_plan
        )),
        "exact_anchors": anchors,
        "semantic_facets": facets,
        "semantic_review_required_hint": semantic_review_required_hint,
        "currentness_or_revision_required": currentness_or_revision_required,
        "anchor_preservation_status": anchor_status,
        "query_plan": query_plan,
        "source_capability_candidates": source_capability_candidates,
        "source_capability_rule": str(contract["source_capability_rule"]),
        "unknown_source_rule": str(contract["unknown_source_rule"]),
        "registered_source_route_ids": list(
            str(item) for item in (contract.get("source_native_routes") or {}).keys()
        ),
        "generic_facet_source_route_ids": list(
            str(item) for item in contract.get("generic_facet_source_route_ids") or []
        ),
        "authority_binding_status_values": list(
            str(item) for item in contract.get("authority_binding_status_values") or []
        ),
        "currentness_evidence_rule": str(contract["currentness_evidence_rule"]),
        "max_future_skew_seconds": int(contract["max_future_skew_seconds"]),
        "max_freshness_window_seconds": int(
            contract["max_freshness_window_seconds"]
        ),
        "target_coverage": target_coverage,
        "facet_coverage": facet_coverage,
        "merge_policy": str(contract["merge_policy"]),
        "coverage_status": "planned" if requested else "not_requested",
        "fallback_state": {
            "reason": None,
            "next_mode": None,
            "next_query_id": None,
            "exhausted_modes": [],
            "exhausted_query_ids": [],
        },
        "negative_evidence_boundary": str(contract["negative_claim_rule"]),
        "source_ledger_fields": list(contract["source_ledger_fields"]),
        "source_ledger_or_citations": [],
        "unresolved_facets": facets,
        "completion_rule": str(contract["coverage_rule"]),
        "execution_boundary": "agent_required_no_network_or_memory_write_performed",
    }
    _reduce_attempts(receipt, _attempt_list(attempts))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or reduce an anchor-preserving external retrieval receipt."
    )
    parser.add_argument("--task-text", default="")
    parser.add_argument("--task-text-base64", default="")
    parser.add_argument("--mode", action="append", default=[])
    parser.add_argument("--attempt-json", default="")
    parser.add_argument("--attempt-json-base64", default="")
    parser.add_argument("--ascii-output", action="store_true")
    parser.add_argument("--policy-path", default=str(DEFAULT_POLICY))
    args = parser.parse_args(argv)
    task_text = args.task_text
    if args.task_text_base64:
        task_text = base64.b64decode(args.task_text_base64, validate=True).decode(
            "utf-8", errors="strict"
        )
    attempt_json = args.attempt_json
    if args.attempt_json_base64:
        attempt_json = base64.b64decode(
            args.attempt_json_base64, validate=True
        ).decode("utf-8", errors="strict")
    receipt = build_external_retrieval_receipt(
        task_text,
        recommended_modes=args.mode,
        attempts=attempt_json,
        policy_path=Path(args.policy_path),
    )
    print(json.dumps(receipt, ensure_ascii=args.ascii_output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
