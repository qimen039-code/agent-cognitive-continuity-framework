from __future__ import annotations

import copy
import hashlib
import json
import tomllib
import unittest
from pathlib import Path

from compile_policy_from_toml import (
    TRACKED_PATHS,
    _normal_behavior_correction_contract,
    _normal_correction_lifecycle_contract,
    _normal_reflexive_gap_contract,
    _normal_search_and_learning_decision_matrix,
)


class PolicyCompilerTests(unittest.TestCase):
    def test_compiled_sections_match_tracked_paths(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        authoring = tomllib.loads(
            (root / "embedded_harness_policy.authoring.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [".".join(path) for path in TRACKED_PATHS],
            authoring["compiled_sections"],
        )

    def test_policy_sources_have_no_duplicate_list_entries(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        documents = {
            "embedded_harness_policy.authoring.toml": tomllib.loads(
                (root / "embedded_harness_policy.authoring.toml").read_text(
                    encoding="utf-8"
                )
            ),
            "embedded_harness_policy.json": json.loads(
                (root / "embedded_harness_policy.json").read_text(encoding="utf-8")
            ),
        }
        duplicates: list[str] = []

        def visit(node: object, path: str) -> None:
            if isinstance(node, list):
                seen: dict[str, int] = {}
                for index, item in enumerate(node):
                    key = json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if key in seen:
                        duplicates.append(f"{path}[{index}] duplicates [{seen[key]}]")
                    else:
                        seen[key] = index
                    visit(item, f"{path}[{index}]")
            elif isinstance(node, dict):
                for key, value in node.items():
                    visit(value, f"{path}.{key}")

        for name, document in documents.items():
            visit(document, name)
        self.assertEqual([], duplicates)

    def test_behavior_correction_contract_is_hash_bound_and_nonblocking(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        authoring = tomllib.loads(
            (root / "embedded_harness_policy.authoring.toml").read_text(
                encoding="utf-8"
            )
        )

        contract = _normal_behavior_correction_contract(authoring)

        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(
            hashlib.sha256(
                (root / "behavior_correction_profiles.json").read_bytes()
            ).hexdigest(),
            contract["profile_registry_sha256"],
        )
        self.assertFalse(contract["automatic_freeze"])
        self.assertFalse(contract["automatic_long_term_memory_write"])
        self.assertFalse(contract["automatic_policy_mutation"])
        migration = contract["migration_hook"]
        self.assertFalse(migration["host_blocking"])
        self.assertFalse(migration["stateful"])
        self.assertEqual("PreToolUse", migration["hook_event"])
        self.assertEqual("^Bash$", migration["tool_name_matcher"])
        self.assertEqual("allow_updated_input_only", migration["output_contract"])
        self.assertEqual(
            hashlib.sha256((root / migration["entrypoint"]).read_bytes()).hexdigest(),
            migration["entrypoint_sha256"],
        )
        for obsolete in (
            "first_match_enforcement",
            "repeat_match_enforcement",
            "verifier_unavailable_enforcement",
            "capacity_overflow_mode",
            "exact_target_unresolved_enforcement",
        ):
            self.assertNotIn(obsolete, contract)

    def test_correction_lifecycle_contract_prioritizes_effectiveness_before_cost(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        authoring = tomllib.loads(
            (root / "embedded_harness_policy.authoring.toml").read_text(
                encoding="utf-8"
            )
        )
        contract = _normal_correction_lifecycle_contract(authoring)
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(
            [
                "real_effectiveness_and_required_components",
                "minimum_sufficient_implementation",
                "execution_time_and_token_efficiency",
                "surface_simplicity",
            ],
            contract["objective_order"],
        )
        self.assertEqual(
            [
                "surface_identification",
                "memory_routing",
                "candidate_prediction",
                "current_action_binding",
                "rewrite_or_validate_or_review",
                "execution",
                "postcondition_verification",
                "calibration",
                "task_local_retirement",
            ],
            contract["stages"],
        )
        self.assertEqual(
            {"auto_rewrite", "preflight_validate", "predictive_review", "no_match"},
            set(contract["decision_modes"]),
        )
        self.assertEqual(
            "current_or_age_le_30_seconds",
            contract["interaction_observation_freshness"],
        )

    def test_external_retrieval_contract_preserves_anchors_and_negative_boundary(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        authoring = tomllib.loads(
            (root / "embedded_harness_policy.authoring.toml").read_text(
                encoding="utf-8"
            )
        )
        matrix = _normal_search_and_learning_decision_matrix(authoring)
        self.assertIsNotNone(matrix)
        assert matrix is not None
        contract = matrix["external_retrieval_contract"]
        self.assertEqual("cbh.external_retrieval_contract.v1", contract["schema"])
        self.assertEqual(
            "preserve_user_intent_and_exact_anchors",
            contract["objective_order"][0],
        )
        self.assertIn("literal_anchor", contract["query_stage_order"])
        self.assertIn("source_native_fallback", contract["query_stage_order"])
        self.assertIn("verified_absent", contract["coverage_status_values"])
        self.assertIn("source_capability_rule", contract)
        self.assertIn("unknown_source_rule", contract)
        self.assertIn("currentness_evidence_rule", contract)
        self.assertEqual(300, contract["max_future_skew_seconds"])
        self.assertEqual(86400, contract["max_freshness_window_seconds"])
        self.assertEqual(
            ["official_entity"], contract["generic_facet_source_route_ids"]
        )
        self.assertIn(
            "verified_for_target", contract["authority_binding_status_values"]
        )
        routes = contract["source_native_routes"]
        for route_id in (
            "official_entity",
            "doi_resolver",
            "crossref_metadata",
            "datacite_metadata",
            "rfc_editor",
            "rfc_status",
            "standards_registry",
            "arxiv",
            "cve_record",
            "nvd_cve",
            "pypi",
            "npm",
            "huggingface",
            "github_repository",
            "namespace_discovery",
        ):
            self.assertIn(route_id, routes)
            self.assertTrue(routes[route_id]["anchor_types"])
            self.assertTrue(routes[route_id]["provider_hints"])
            self.assertTrue(routes[route_id]["query_template"])
            self.assertTrue(routes[route_id]["authority_boundary"])
        self.assertIn("never proof of absence", contract["negative_claim_rule"])
        self.assertIn("freshness_window_seconds", contract["source_ledger_fields"])
        self.assertIn("authority_binding_status", contract["source_ledger_fields"])
        self.assertNotIn("max_queries", contract)
        self.assertNotIn("max_sources", contract)

    def test_behavior_correction_contract_rejects_retired_event_control_fields(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        authoring = tomllib.loads(
            (root / "embedded_harness_policy.authoring.toml").read_text(
                encoding="utf-8"
            )
        )
        mutated = copy.deepcopy(authoring)
        mutated["runtime_enforcement"]["behavior_correction_contract"][
            "first_match_enforcement"
        ] = "retired-sentinel"
        with self.assertRaisesRegex(ValueError, "retired fields"):
            _normal_behavior_correction_contract(mutated)

    def test_runtime_policy_has_no_generic_permit_or_hard_block_layer(self) -> None:
        root = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
        policy = json.loads(
            (root / "embedded_harness_policy.json").read_text(encoding="utf-8")
        )
        runtime = policy["runtime_enforcement"]
        self.assertFalse(runtime["mandatory"])
        self.assertEqual([], runtime["entry_scripts"])
        self.assertEqual([], runtime["hard_block_conditions"])
        self.assertEqual([], runtime["hard_tool_patterns"])
        self.assertNotIn("human_confirmation_permit", runtime)
        advisor = runtime["dangerous_delete_advisory_contract"]
        self.assertTrue(advisor["enabled"])
        self.assertEqual([], advisor["registered_hook_events"])
        self.assertFalse(advisor["blocking"])
        self.assertFalse(advisor["stateful"])
        self.assertEqual("direct_on_demand_only", advisor["invocation_mode"])
        self.assertEqual(
            hashlib.sha256((root / advisor["entrypoint"]).read_bytes()).hexdigest(),
            advisor["entrypoint_sha256"],
        )

    def test_reflexive_gap_group_rejects_mis_scoped_parent_field(self) -> None:
        authoring_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "embedded-harness"
            / "embedded_harness_policy.authoring.toml"
        )
        authoring = tomllib.loads(authoring_path.read_text(encoding="utf-8"))
        mutated = copy.deepcopy(authoring)
        mutated["router_decision_contract"]["reflexive_gap_contract"][
            "knowledge_coverage_groups"
        ]["common_error_prevention_triggers"] = ["mis-scoped sentinel"]

        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            _normal_reflexive_gap_contract(mutated)


if __name__ == "__main__":
    unittest.main()
