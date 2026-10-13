from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from external_retrieval_strategy import build_external_retrieval_receipt


class ExternalRetrievalStrategyTests(unittest.TestCase):
    def test_real_project_name_failure_preserves_standalone_exact_query(self) -> None:
        task = '搜索 GitHub 开源项目 "Claim Boundary Harness"'
        receipt = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )

        self.assertIn(
            receipt["retrieval_profile"],
            {"exact_anchor_first", "exact_anchor_plus_facet_coverage"},
        )
        self.assertEqual("pass", receipt["anchor_preservation_status"])
        self.assertEqual(task, receipt["original_query"])
        self.assertTrue(receipt["original_query_preserved"])
        self.assertEqual(
            ["Claim Boundary Harness"],
            [item["raw_text"] for item in receipt["exact_anchors"]],
        )
        exact = [
            item
            for item in receipt["query_plan"]
            if item.get("exact_anchor") == "Claim Boundary Harness"
        ]
        self.assertIn('"Claim Boundary Harness"', [item["query_text"] for item in exact])
        self.assertTrue(
            any(item["execution_group"] == "anchor_first_standalone" for item in exact)
        )
        self.assertTrue(
            any(item["mode"] == "github_open_source_repository_search" for item in exact)
        )

    def test_real_owner_repo_failure_has_web_native_and_direct_url_lanes(self) -> None:
        slug = "qimen039-code/claim-boundary-harness"
        receipt = build_external_retrieval_receipt(
            f"搜索 GitHub 仓库 {slug}",
            recommended_modes=[
                "general_web_cross_check",
                "github_open_source_repository_search",
            ],
        )

        anchor = next(
            item for item in receipt["exact_anchors"] if item["type"] == "owner_repo_slug"
        )
        self.assertEqual(slug, anchor["raw_text"])
        queries = receipt["query_plan"]
        self.assertTrue(any(item["query_text"] == slug for item in queries))
        self.assertTrue(any(item["query_text"] == f"repo:{slug}" for item in queries))
        self.assertTrue(
            any(item.get("direct_url") == f"https://github.com/{slug}" for item in queries)
        )
        self.assertTrue(
            all(
                item.get("anchor_preserved") is True
                for item in queries
                if item.get("exact_anchor") == slug
            )
        )

    def test_natural_feature_search_uses_facets_without_exact_anchor(self) -> None:
        receipt = build_external_retrieval_receipt(
            "按自然搜索查找能做 claim boundary、evidence verification、memory continuity、risk routing 的开源 AI 框架",
            recommended_modes=[
                "general_web_cross_check",
                "github_open_source_repository_search",
            ],
        )

        self.assertEqual("facet_coverage", receipt["retrieval_profile"])
        self.assertEqual([], receipt["exact_anchors"])
        self.assertEqual("not_applicable", receipt["anchor_preservation_status"])
        folded = {item.casefold() for item in receipt["semantic_facets"]}
        self.assertTrue(
            {"claim boundary", "evidence verification", "memory continuity", "risk routing"}.issubset(
                folded
            )
        )

    def test_generic_current_facet_does_not_promote_claim_words_to_entities(self) -> None:
        receipt = build_external_retrieval_receipt(
            "搜索当前官方价格",
            recommended_modes=["official_authority_source_search"],
        )
        self.assertEqual([], receipt["exact_anchors"])
        self.assertTrue(receipt["facet_coverage"])
        self.assertTrue(
            all(
                item["association_status"] == "no_exact_anchor"
                for item in receipt["facet_coverage"]
            )
        )
        facet_query = next(
            item
            for item in receipt["query_plan"]
            if item["query_type"] == "semantic_facet"
        )
        self.assertEqual(
            ["official_entity"], facet_query["allowed_source_route_ids"]
        )

    def test_general_web_miss_requires_github_native_fallback_not_absence(self) -> None:
        slug = "qimen039-code/claim-boundary-harness"
        receipt = build_external_retrieval_receipt(
            f"搜索 GitHub 仓库 {slug}",
            recommended_modes=[
                "general_web_cross_check",
                "github_open_source_repository_search",
            ],
            attempts={
                "query_id": "q-001",
                "mode": "general_web_cross_check",
                "provider": "web",
                "provider_status": "ok",
                "result_count": 10,
                "exact_anchor_hits": [],
                "source_read": False,
            },
        )

        self.assertEqual("fallback_required", receipt["coverage_status"])
        self.assertEqual(
            "exact_anchor_not_found_in_checked_surface",
            receipt["fallback_state"]["reason"],
        )
        self.assertEqual(
            "github_open_source_repository_search",
            receipt["fallback_state"]["next_mode"],
        )
        self.assertIn("never proof of absence", receipt["negative_evidence_boundary"])

    def test_unqualified_absence_flag_cannot_create_verified_absent(self) -> None:
        receipt = build_external_retrieval_receipt(
            '搜索 "Claim Boundary Harness"',
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-001",
                "mode": "general_web_cross_check",
                "provider_status": "ok",
                "result_count": 0,
                "exact_anchor_hits": [],
                "verified_absent": True,
                "absence_basis": "top_k_empty",
            },
        )

        self.assertNotEqual("verified_absent", receipt["coverage_status"])

    def test_multiple_exact_queries_never_merge_with_semantic_expansion(self) -> None:
        name = "Claim Boundary Harness"
        slug = "qimen039-code/claim-boundary-harness"
        receipt = build_external_retrieval_receipt(
            f'同时搜索 GitHub："{name}"；{slug}；model-facing claim verification、memory continuity、risk routing',
            recommended_modes=[
                "general_web_cross_check",
                "github_open_source_repository_search",
            ],
        )

        raw_anchors = {item["raw_text"] for item in receipt["exact_anchors"]}
        self.assertTrue({name, slug}.issubset(raw_anchors))
        exact_queries = [
            item for item in receipt["query_plan"] if item.get("exact_anchor") in {name, slug}
        ]
        self.assertTrue(exact_queries)
        self.assertTrue(
            all(item["execution_group"] != "semantic_expansion" for item in exact_queries)
        )
        self.assertTrue(
            all(item["result_budget_scope"].startswith("per_query") for item in exact_queries)
        )
        self.assertIn("Never merge an exact-anchor query", receipt["merge_policy"])

    def test_named_entity_uses_official_route_without_github_guess(self) -> None:
        receipt = build_external_retrieval_receipt(
            "查找 OpenAI 当前 CEO 和官方任命公告",
            recommended_modes=[
                "official_authority_source_search",
                "general_web_cross_check",
            ],
        )

        anchor = next(item for item in receipt["exact_anchors"] if item["raw_text"] == "OpenAI")
        self.assertEqual("named_entity", anchor["type"])
        self.assertEqual("official_source_discovery", anchor["provider_hint"])
        self.assertIn(
            "official_entity",
            {item["source_route_id"] for item in receipt["source_capability_candidates"]},
        )
        self.assertFalse(
            any("github" in str(item.get("direct_url", "")).casefold() for item in receipt["query_plan"])
        )

    def test_people_organizations_products_and_laws_are_not_projects_by_shape(self) -> None:
        for entity in (
            "Sam Altman",
            "World Health Organization",
            "Apple Vision Pro",
            "EU AI Act",
        ):
            with self.subTest(entity=entity):
                receipt = build_external_retrieval_receipt(
                    f"查找 {entity} 当前官方信息",
                    recommended_modes=["official_authority_source_search"],
                )
                anchor = next(
                    item for item in receipt["exact_anchors"] if item["raw_text"] == entity
                )
                self.assertEqual("named_entity", anchor["type"])
                self.assertFalse(
                    any(
                        item["mode"] == "github_open_source_repository_search"
                        for item in receipt["query_plan"]
                    )
                )

    def test_identifier_families_use_their_native_primary_surfaces(self) -> None:
        cases = [
            ("核对 DOI 10.1145/290941.291025", "doi", "https://doi.org/10.1145/290941.291025"),
            ("核对 RFC 9110 当前状态和勘误", "rfc", "https://www.rfc-editor.org/rfc/rfc9110.html"),
            ("查找 CVE-2024-3094 官方记录", "cve_id", "https://www.cve.org/CVERecord?id=CVE-2024-3094"),
            ("查找 arXiv 2401.15884", "arxiv_id", "https://arxiv.org/abs/2401.15884"),
        ]
        for task, kind, direct_url in cases:
            with self.subTest(task=task):
                receipt = build_external_retrieval_receipt(
                    task, recommended_modes=["general_web_cross_check"]
                )
                self.assertIn(kind, {item["type"] for item in receipt["exact_anchors"]})
                self.assertTrue(
                    any(item.get("direct_url") == direct_url for item in receipt["query_plan"])
                )
                target_id = next(
                    item["target_id"]
                    for item in receipt["exact_anchors"]
                    if item["type"] == kind
                )
                first_target_query = next(
                    item
                    for item in receipt["query_plan"]
                    if item.get("target_id") == target_id
                )
                self.assertEqual("source_native_fallback", first_target_query["query_type"])
                marker = {"doi": "DOI", "rfc": "RFC", "cve_id": "CVE"}.get(kind)
                if marker:
                    self.assertNotIn(
                        marker,
                        [
                            item["raw_text"]
                            for item in receipt["exact_anchors"]
                            if item["type"] == "acronym"
                        ],
                    )

    def test_package_and_model_registries_do_not_collapse_to_github(self) -> None:
        cases = [
            (
                "查找 PyPI 包 pydantic-ai 2.8.2",
                "pydantic-ai",
                "pypi",
                "https://pypi.org/project/pydantic-ai/",
            ),
            (
                "查找 npm 包 @openai/codex 最新版本",
                "@openai/codex",
                "npm",
                "https://www.npmjs.com/package/@openai/codex",
            ),
            (
                "查找 Hugging Face 模型 meta-llama/Llama-3.1-8B-Instruct",
                "meta-llama/Llama-3.1-8B-Instruct",
                "huggingface",
                "https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct",
            ),
        ]
        for task, raw, provider, direct_url in cases:
            with self.subTest(task=task):
                receipt = build_external_retrieval_receipt(
                    task, recommended_modes=["general_web_cross_check"]
                )
                anchor = next(item for item in receipt["exact_anchors"] if item["raw_text"] == raw)
                self.assertEqual(provider, anchor["provider_hint"])
                self.assertTrue(
                    any(item.get("direct_url") == direct_url for item in receipt["query_plan"])
                )
                self.assertFalse(
                    any("github.com" in str(item.get("direct_url", "")) for item in receipt["query_plan"])
                )

    def test_unknown_namespace_stays_unresolved_until_source_discovery(self) -> None:
        receipt = build_external_retrieval_receipt(
            "查找 acme/widget 官方来源",
            recommended_modes=["general_web_cross_check"],
        )
        anchor = next(item for item in receipt["exact_anchors"] if item["raw_text"] == "acme/widget")
        self.assertEqual("namespaced_identifier", anchor["type"])
        self.assertEqual("source_registry_unknown", anchor["provider_hint"])
        self.assertEqual(
            {"namespace_discovery"},
            {item["source_route_id"] for item in receipt["source_capability_candidates"]},
        )
        self.assertTrue(receipt["semantic_review_required_hint"])
        self.assertFalse(any(item.get("direct_url") for item in receipt["query_plan"] if item.get("source_route_id")))

    def test_standard_identifier_uses_issuing_body_discovery(self) -> None:
        receipt = build_external_retrieval_receipt(
            "核对 ISO/IEC 27001:2022 当前状态",
            recommended_modes=["official_authority_source_search"],
        )
        anchor = next(item for item in receipt["exact_anchors"] if item["type"] == "standard_id")
        self.assertEqual("ISO/IEC 27001:2022", anchor["raw_text"])
        self.assertIn(
            "standards_registry",
            {item["source_route_id"] for item in receipt["source_capability_candidates"]},
        )

    def test_every_recommended_mode_becomes_an_executable_query_candidate(self) -> None:
        modes = ["official_authority_source_search", "general_web_cross_check"]
        receipt = build_external_retrieval_receipt(
            "搜索 OpenAI 最新官方价格", recommended_modes=modes
        )
        original_modes = [
            item["mode"]
            for item in receipt["query_plan"]
            if item["query_type"] == "original_query"
        ]
        self.assertEqual(modes, original_modes)

    def test_multi_target_completion_is_not_global(self) -> None:
        task = '查找 "Alpha Beta"；"Gamma Delta"'
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        query_for = {
            item["exact_anchor"]: item["query_id"]
            for item in planned["query_plan"]
            if item["query_type"] == "literal_anchor"
        }
        target_for = {
            item["raw_text"]: item["target_id"] for item in planned["exact_anchors"]
        }
        receipt = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts=[
                {
                    "query_id": query_for["Alpha Beta"],
                    "exact_anchor_hits": ["Alpha Beta"],
                    "source_read": True,
                    "canonical_source_refs": ["https://example.com/alpha"],
                    "source_route_id": "official_entity",
                    "source_role": "official_authority",
                    "primary_source_verified": True,
                    "supports_target_ids": [target_for["Alpha Beta"]],
                    "conflict_status": "not_observed",
                    "authority_binding_status": "verified_for_target",
                    "authority_binding_basis": "opened source is the entity's official authority page",
                },
                {
                    "query_id": query_for["Gamma Delta"],
                    "exact_anchor_hits": ["Gamma Delta"],
                    "source_read": False,
                    "result_count": 1,
                },
            ],
        )
        self.assertEqual("source_read_required", receipt["coverage_status"])
        statuses = {item["raw_text"]: item["status"] for item in receipt["target_coverage"]}
        self.assertEqual("verified_at_primary_source", statuses["Alpha Beta"])
        self.assertEqual("source_read_required", statuses["Gamma Delta"])

    def test_negative_evidence_is_bound_to_each_target(self) -> None:
        task = "https://example.com/one https://example.com/two"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        first_query = next(
            item["query_id"]
            for item in planned["query_plan"]
            if item.get("exact_anchor") == "https://example.com/one"
        )
        first_target_id = next(
            item["target_id"]
            for item in planned["exact_anchors"]
            if item["raw_text"] == "https://example.com/one"
        )
        receipt = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": first_query,
                "verified_absent": True,
                "absence_basis": "known_url_verifiable_not_found",
                "source_read": True,
                "canonical_source_refs": ["https://example.com/one"],
                "source_role": "canonical_target",
                "primary_source_verified": True,
                "supports_target_ids": [first_target_id],
                "conflict_status": "not_observed",
            },
        )
        self.assertNotEqual("verified_absent", receipt["coverage_status"])
        statuses = {item["raw_text"]: item["status"] for item in receipt["target_coverage"]}
        self.assertEqual("verified_absent", statuses["https://example.com/one"])
        self.assertEqual("unresolved", statuses["https://example.com/two"])

    def test_unrelated_source_read_cannot_complete_unparsed_task(self) -> None:
        receipt = build_external_retrieval_receipt(
            "???",
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-001",
                "mode": "general_web_cross_check",
                "source_read": True,
                "canonical_source_refs": ["https://example.com/unrelated"],
            },
        )
        self.assertEqual("semantic_review_required", receipt["coverage_status"])

    def test_same_mode_on_two_targets_is_exhausted_by_query_not_mode(self) -> None:
        task = "https://example.com/one https://example.com/two"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        first = next(item for item in planned["query_plan"] if item.get("exact_anchor") == "https://example.com/one")
        second = next(item for item in planned["query_plan"] if item.get("exact_anchor") == "https://example.com/two")
        receipt = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": first["query_id"],
                "mode": first["mode"],
                "exact_anchor_hits": ["https://example.com/one"],
                "source_read": True,
                "canonical_source_refs": ["https://example.com/one"],
                "source_role": "canonical_target",
                "primary_source_verified": True,
                "supports_target_ids": [first["target_id"]],
                "conflict_status": "not_observed",
            },
        )
        self.assertEqual(second["query_id"], receipt["fallback_state"]["next_query_id"])

    def test_complete_requires_matching_primary_source_for_the_target(self) -> None:
        target = "https://example.com/resource"
        receipt = build_external_retrieval_receipt(
            target,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-001",
                "exact_anchor_hits": [target],
                "source_read": True,
                "canonical_source_refs": [target],
                "source_role": "canonical_target",
                "primary_source_verified": True,
                "supports_target_ids": ["anchor-001"],
                "conflict_status": "not_observed",
            },
        )
        self.assertEqual("complete", receipt["coverage_status"])
        self.assertEqual(
            "verified_at_primary_source", receipt["target_coverage"][0]["status"]
        )

    def test_overall_absence_requires_every_target_to_have_qualified_negative(self) -> None:
        targets = ["https://example.com/one", "https://example.com/two"]
        receipt = build_external_retrieval_receipt(
            " ".join(targets),
            recommended_modes=["general_web_cross_check"],
            attempts=[
                {
                    "query_id": f"q-{index:03d}",
                    "verified_absent": True,
                    "absence_basis": "known_url_verifiable_not_found",
                    "source_read": True,
                    "canonical_source_refs": [target],
                    "source_role": "canonical_target",
                    "primary_source_verified": True,
                    "supports_target_ids": [f"anchor-{index:03d}"],
                    "conflict_status": "not_observed",
                }
                for index, target in enumerate(targets, start=1)
            ],
        )
        self.assertEqual("verified_absent", receipt["coverage_status"])
        self.assertTrue(
            all(item["status"] == "verified_absent" for item in receipt["target_coverage"])
        )

    def test_provider_failure_advances_to_next_source_family_without_absence_claim(self) -> None:
        task = "核对 DOI 10.1145/290941.291025"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        crossref = next(
            item
            for item in planned["query_plan"]
            if item.get("source_route_id") == "crossref_metadata"
            and item["query_type"] == "source_native_fallback"
        )
        earlier = [
            item
            for item in planned["query_plan"]
            if int(item["query_id"].split("-")[1])
            < int(crossref["query_id"].split("-")[1])
        ]
        receipt = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts=[
                {
                    "query_id": item["query_id"],
                    "mode": item["mode"],
                    "provider_status": "provider_unavailable",
                    "result_count": 0,
                    "source_read": False,
                }
                for item in earlier
            ],
        )
        self.assertEqual("fallback_required", receipt["coverage_status"])
        self.assertEqual(crossref["query_id"], receipt["fallback_state"]["next_query_id"])
        self.assertNotEqual("verified_absent", receipt["coverage_status"])

    def test_unplanned_query_id_cannot_satisfy_a_target(self) -> None:
        target = "https://example.com/resource"
        receipt = build_external_retrieval_receipt(
            target,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-forged",
                "exact_anchor_hits": [target],
                "source_read": True,
                "canonical_source_refs": [target],
                "source_role": "canonical_target",
                "primary_source_verified": True,
                "supports_target_ids": ["anchor-001"],
                "conflict_status": "not_observed",
            },
        )
        self.assertNotEqual("complete", receipt["coverage_status"])
        self.assertEqual("unresolved", receipt["target_coverage"][0]["status"])

    def test_unrelated_canonical_ref_cannot_verify_url_target(self) -> None:
        target = "https://example.com/resource"
        receipt = build_external_retrieval_receipt(
            target,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-001",
                "exact_anchor_hits": [target],
                "source_read": True,
                "canonical_source_refs": ["https://unrelated.example/"],
                "source_role": "canonical_target",
                "primary_source_verified": True,
                "supports_target_ids": ["anchor-001"],
                "conflict_status": "not_observed",
            },
        )
        self.assertEqual("source_read_required", receipt["coverage_status"])
        self.assertEqual("source_read_required", receipt["target_coverage"][0]["status"])

    def test_qualified_absence_requires_read_source_ref_and_role(self) -> None:
        target = "https://example.com/missing"
        receipt = build_external_retrieval_receipt(
            target,
            recommended_modes=["general_web_cross_check"],
            attempts={
                "query_id": "q-001",
                "verified_absent": True,
                "absence_basis": "known_url_verifiable_not_found",
                "source_read": False,
                "canonical_source_refs": [],
            },
        )
        self.assertNotEqual("verified_absent", receipt["coverage_status"])
        self.assertEqual("unresolved", receipt["target_coverage"][0]["status"])

    def test_mixed_source_namespaces_bind_to_local_clause_context(self) -> None:
        receipt = build_external_retrieval_receipt(
            "比较 GitHub qimen039-code/claim-boundary-harness 与 Hugging Face meta-llama/Llama-3.1-8B-Instruct",
            recommended_modes=["general_web_cross_check"],
        )
        providers = {
            item["raw_text"]: item["provider_hint"] for item in receipt["exact_anchors"]
        }
        self.assertEqual("github", providers["qimen039-code/claim-boundary-harness"])
        self.assertEqual("huggingface", providers["meta-llama/Llama-3.1-8B-Instruct"])

        receipt = build_external_retrieval_receipt(
            "比较 GitHub openai/openai-python 与 npm @openai/codex",
            recommended_modes=["general_web_cross_check"],
        )
        providers = {
            item["raw_text"]: item["provider_hint"] for item in receipt["exact_anchors"]
        }
        self.assertEqual("github", providers["openai/openai-python"])
        self.assertEqual("npm", providers["@openai/codex"])

    def test_document_local_terms_are_facets_not_independent_entities(self) -> None:
        receipt = build_external_retrieval_receipt(
            "核对 RFC 9110 对 CONNECT 方法的规定",
            recommended_modes=["general_web_cross_check"],
        )
        self.assertNotIn("CONNECT", [item["raw_text"] for item in receipt["exact_anchors"]])
        self.assertTrue(any("CONNECT" in facet for facet in receipt["semantic_facets"]))

        receipt = build_external_retrieval_receipt(
            "核对 CVE-2024-3094 的 NVD 评分",
            recommended_modes=["general_web_cross_check"],
        )
        self.assertNotIn("NVD", [item["raw_text"] for item in receipt["exact_anchors"]])
        self.assertTrue(any("NVD" in facet for facet in receipt["semantic_facets"]))

    def test_current_claim_requires_checked_at_evidence_for_target_verification(self) -> None:
        target = "https://example.com/status"
        task = f"核对 {target} 当前状态"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["official_authority_source_search"]
        )
        query = next(item for item in planned["query_plan"] if item.get("exact_anchor") == target)
        common_attempt = {
            "query_id": query["query_id"],
            "exact_anchor_hits": [target],
            "source_read": True,
            "canonical_source_refs": [target],
            "source_role": "canonical_target",
            "primary_source_verified": True,
            "supports_target_ids": [query["target_id"]],
            "conflict_status": "not_observed",
        }
        missing_time = build_external_retrieval_receipt(
            task,
            recommended_modes=["official_authority_source_search"],
            attempts=common_attempt,
        )
        self.assertEqual("source_read_required", missing_time["target_coverage"][0]["status"])

        now = datetime.now(timezone.utc)
        for incomplete_freshness in (
            {"checked_at": now.isoformat()},
            {"checked_at": now.isoformat(), "freshness_window_seconds": 3600},
        ):
            incomplete = build_external_retrieval_receipt(
                task,
                recommended_modes=["official_authority_source_search"],
                attempts={**common_attempt, **incomplete_freshness},
            )
            self.assertEqual(
                "source_read_required", incomplete["target_coverage"][0]["status"]
            )

        with_time = build_external_retrieval_receipt(
            task,
            recommended_modes=["official_authority_source_search"],
            attempts={
                **common_attempt,
                "checked_at": now.isoformat(),
                "freshness_window_seconds": 3600,
                "freshness_basis": "live canonical target observation",
            },
        )
        self.assertEqual("verified_at_primary_source", with_time["target_coverage"][0]["status"])

        for invalid_checked_at, window in (
            ("not-a-date", 3600),
            ("2000-01-01T00:00:00Z", 3600),
            ((now + timedelta(days=1)).isoformat(), 172800),
            (now.replace(tzinfo=None).isoformat(), 3600),
            (now.isoformat(), 10**12),
        ):
            invalid_time = build_external_retrieval_receipt(
                task,
                recommended_modes=["official_authority_source_search"],
                attempts={
                    **common_attempt,
                    "checked_at": invalid_checked_at,
                    "freshness_window_seconds": window,
                    "freshness_basis": "live canonical target observation",
                },
            )
            self.assertEqual(
                "source_read_required",
                invalid_time["target_coverage"][0]["status"],
            )

    def test_semantic_facet_rejects_unregistered_route_and_accepts_planned_source(self) -> None:
        task = "核对 RFC 9110 对 CONNECT 方法的规定"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        facet = next(item for item in planned["facet_coverage"] if "CONNECT" in item["facet"])
        query = next(
            item
            for item in planned["query_plan"]
            if item.get("target_id") == facet["target_id"]
        )
        common = {
            "query_id": query["query_id"],
            "covered_facets": [facet["facet"]],
            "source_read": True,
            "source_role": "canonical_registry",
            "primary_source_verified": True,
            "supports_target_ids": [facet["target_id"]],
            "conflict_status": "not_observed",
            "authority_binding_status": "verified_for_target",
            "authority_binding_basis": "declared semantic source ownership",
        }
        forged = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **common,
                "source_route_id": "totally_fake_route",
                "canonical_source_refs": ["https://unrelated.example/rfc"],
            },
        )
        forged_facet = next(
            item for item in forged["facet_coverage"] if item["target_id"] == facet["target_id"]
        )
        self.assertEqual("source_read_required", forged_facet["status"])
        self.assertEqual(
            "unqualified_or_unbound",
            forged["source_ledger_or_citations"][0]["evidence_binding_status"],
        )

        canonical = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **common,
                "source_route_id": "rfc_editor",
                "canonical_source_refs": ["https://www.rfc-editor.org/rfc/rfc9110.html"],
            },
        )
        canonical_facet = next(
            item for item in canonical["facet_coverage"] if item["target_id"] == facet["target_id"]
        )
        self.assertEqual("verified_at_primary_source", canonical_facet["status"])

    def test_multi_entity_facets_bind_to_their_local_anchor_source_family(self) -> None:
        task = (
            "比较 GitHub openai/openai-python 的许可证 与 Hugging Face "
            "meta-llama/Llama-3.1-8B-Instruct 的安全限制"
        )
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        target_for = {
            item["raw_text"]: item["target_id"] for item in planned["exact_anchors"]
        }
        license_facet = next(
            item for item in planned["facet_coverage"] if "许可证" in item["facet"]
        )
        safety_facet = next(
            item for item in planned["facet_coverage"] if "安全限制" in item["facet"]
        )
        self.assertEqual(
            [target_for["openai/openai-python"]],
            license_facet["linked_target_ids"],
        )
        self.assertEqual(
            [target_for["meta-llama/Llama-3.1-8B-Instruct"]],
            safety_facet["linked_target_ids"],
        )
        safety_query = next(
            item
            for item in planned["query_plan"]
            if item.get("target_id") == safety_facet["target_id"]
        )
        self.assertEqual(
            ["huggingface"], safety_query["allowed_source_route_ids"]
        )

        common = {
            "query_id": safety_query["query_id"],
            "covered_facets": [safety_facet["facet"]],
            "source_read": True,
            "source_role": "canonical_target",
            "primary_source_verified": True,
            "supports_target_ids": [safety_facet["target_id"]],
            "conflict_status": "not_observed",
            "authority_binding_status": "verified_for_target",
            "authority_binding_basis": "opened source directly addresses this facet",
        }
        wrong_entity = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **common,
                "source_route_id": "github_repository",
                "canonical_source_refs": ["https://github.com/openai/openai-python"],
            },
        )
        wrong_safety = next(
            item
            for item in wrong_entity["facet_coverage"]
            if item["target_id"] == safety_facet["target_id"]
        )
        self.assertEqual("source_read_required", wrong_safety["status"])

        correct_entity = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **common,
                "source_route_id": "huggingface",
                "canonical_source_refs": [
                    "https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct"
                ],
            },
        )
        correct_safety = next(
            item
            for item in correct_entity["facet_coverage"]
            if item["target_id"] == safety_facet["target_id"]
        )
        self.assertEqual("verified_at_primary_source", correct_safety["status"])

    def test_source_native_query_enforces_its_expected_canonical_url(self) -> None:
        task = "RFC 9110"
        planned = build_external_retrieval_receipt(
            task, recommended_modes=["general_web_cross_check"]
        )
        query = next(
            item
            for item in planned["query_plan"]
            if item.get("source_route_id") == "rfc_editor"
            and item["query_type"] == "source_native_fallback"
        )
        base_attempt = {
            "query_id": query["query_id"],
            "exact_anchor_hits": ["RFC 9110"],
            "source_read": True,
            "source_route_id": "rfc_editor",
            "source_role": "canonical_registry",
            "primary_source_verified": True,
            "supports_target_ids": [query["target_id"]],
            "conflict_status": "not_observed",
        }
        unrelated = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **base_attempt,
                "canonical_source_refs": ["https://unrelated.example/rfc"],
            },
        )
        self.assertEqual("source_read_required", unrelated["target_coverage"][0]["status"])

        canonical = build_external_retrieval_receipt(
            task,
            recommended_modes=["general_web_cross_check"],
            attempts={
                **base_attempt,
                "canonical_source_refs": ["https://www.rfc-editor.org/rfc/rfc9110.html"],
            },
        )
        self.assertEqual("complete", canonical["coverage_status"])


if __name__ == "__main__":
    unittest.main()
