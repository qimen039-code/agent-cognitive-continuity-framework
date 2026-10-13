from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_action_consumer import (
    DEFAULT_COMMON_ERROR_META,
    _compact_runtime_receipt,
    build_action_consumption,
    select_common_errors,
    select_memory_records,
)
from semantic_memory import append_memory_record, build_memory_record


class ActionConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.meta = self.root / "_META_INDEX.md"
        self.meta.write_text(
            "\n".join(
                [
                    "# Common Error Corpus Meta Index",
                    "",
                    "| CE ID | Class | Status | Retrieval terms | Applies | Solution summary | Upgrade boundary |",
                    "| --- | --- | --- | --- | --- | --- | --- |",
                    "| `CE-FOREACH` | function_tool_call_error | ACTIVE | foreach pipeline, empty pipe element, collect rows | PowerShell loop | collect rows first | repeat |",
                    "| `CE-PREFLIGHT` | function_tool_call_error | ACTIVE | command interface, unsupported parameter, help, param block | CLI calls | inspect interface first | repeat |",
                    "| `CE-ENCODING` | function_tool_call_error | ACTIVE | Chinese encoding, mojibake, Unicode round trip | Windows text | preserve Unicode | repeat |",
                    "| `CE-UNRELATED` | function_tool_call_error | ACTIVE | unrelated sentinel | none | none | repeat |",
                ]
            ),
            encoding="utf-8",
        )
        (self.root / "CE-FOREACH.md").write_text(
            "# CE-FOREACH\n\n## Prevention\n\nCollect loop results into `$rows`, then pipe `$rows`; never pipe a bare foreach statement.\n",
            encoding="utf-8",
        )
        (self.root / "CE-PREFLIGHT.md").write_text(
            "# CE-PREFLIGHT\n\n## Prevention\n\nRead the actual parameter block or help before invoking a nontrivial command.\n",
            encoding="utf-8",
        )
        (self.root / "CE-ENCODING.md").write_text(
            "# CE-ENCODING\n\n## Prevention\n\nPreserve recoverable Unicode and verify the final Chinese text after every shell boundary.\n",
            encoding="utf-8",
        )
        # Deliberately do not create CE-UNRELATED.md. A bounded selector must not
        # try to open every payload in the corpus.

        self.contract = self.root / "host_capability_contract.json"
        self.contract.write_text(
            json.dumps(
                {
                    "schema": "cbh.host_capability_contract.v1",
                    "consumer": "harness_action_consumer",
                    "action_support": {
                        "retrieve_matching_memory": "consumer_supported",
                        "prepare_task_local_prediction_review": "consumer_supported",
                        "prepare_task_local_correction_bundle": "consumer_supported",
                        "apply_first_principles_profile": "context_binding_only",
                        "perform_skill_audit_profile": "context_binding_only",
                        "perform_external_research_route": "agent_required",
                        "write_selected_memory_lane": "host_pending",
                        "resolve_conversation_link": "consumer_supported",
                        "bind_verified_facts": "consumer_supported",
                        "await_scoped_user_confirmation": "agent_required",
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def public_history_sources(self) -> list[dict]:
        """Synthetic histories exercise navigation without an installed corpus."""
        sources = []
        rows = {
            "INC": ("Incident ID | Trigger aliases | Current status | Paired ERR | Paired SOL",
                    "INC-PUBLIC-CLIENT-2026-06-13 | `客户端故障`, `插件冲突` | historical | none | none",
                    "INC-PUBLIC-CLIENT-2026-06-13", "causal_chain", "客户端插件冲突。"),
            "ERR": ("Error ID | Outer retrieval surface | Paired solutions",
                    "ERR-PUBLIC-HISTORY | public historical failure | none",
                    "ERR-PUBLIC-HISTORY", "prevention_rule", "Historical context only."),
            "SOL": ("Solution ID | Outer retrieval surface | Paired errors",
                    "SOL-PUBLIC-HISTORY | legacy configuration | none",
                    "SOL-PUBLIC-HISTORY", "future_reuse_rule", "Historical context only."),
        }
        for family, (columns, row, record_id, field, rule) in rows.items():
            path = self.root / f"public-{family}.md"
            path.write_text(
                f"| {columns} |\n| " + " | ".join(["---"] * len(columns.split("|"))) + " |\n"
                f"| {row} |\n\n## {record_id}\n\n"
                "status:\n- `historical_superseded_do_not_auto_reuse`\n\n"
                f"{field}:\n- {rule}\n", encoding="utf-8",
            )
            sources.append({"family": family, "index_path": path, "payload_path": path,
                            "heading_level": 2, "rule_fields": [field]})
        return sources

    def test_fast_path_selects_two_direct_matches_without_scanning_unrelated_payloads(self) -> None:
        selected = select_common_errors(
            "Inspect the command interface and avoid the foreach pipeline failure",
            "",
            meta_path=self.meta,
            max_records=2,
        )
        self.assertEqual([item["record_id"] for item in selected], ["CE-FOREACH", "CE-PREFLIGHT"])
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(Path(item["path"]).exists() for item in selected))

    def test_generated_bare_foreach_pipeline_selects_exact_prevention_record(self) -> None:
        selected = select_common_errors(
            "Run a local inspection",
            "$rows = foreach($p in $paths) { Get-Item $p } | ConvertTo-Json",
            meta_path=self.meta,
            max_records=2,
        )
        self.assertEqual(selected[0]["record_id"], "CE-FOREACH")
        self.assertIn("sha256", selected[0])
        self.assertEqual(len(selected[0]["sha256"]), 64)

    def test_historical_foreach_candidate_binds_profile_memory_and_current_action(self) -> None:
        historical_candidate = (
            "$candidates=@('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',"
            "'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',"
            "'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',"
            "'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',"
            "'C:\\Users\\example\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe'); "
            "foreach($p in $candidates){[pscustomobject]@{Path=$p;Exists=(Test-Path -LiteralPath $p)}} "
            "| Format-Table -AutoSize"
        )
        route = {
            "tool_surface_need": "shell",
            "preferred_call_surface": "shell",
            "action_bindings": [
                {
                    "action": "prepare_task_local_correction_bundle",
                    "completion_evidence": "task_local_correction_bundle",
                }
            ],
        }
        first = build_action_consumption(
            route,
            prompt="检查五个浏览器路径",
            tool_input_text=historical_candidate,
            meta_path=DEFAULT_COMMON_ERROR_META,
            capability_contract_path=self.contract,
        )
        second = build_action_consumption(
            route,
            prompt="检查五个浏览器路径",
            tool_input_text=historical_candidate,
            meta_path=DEFAULT_COMMON_ERROR_META,
            capability_contract_path=self.contract,
        )
        bundle = first["task_local_correction_bundle"]
        self.assertEqual("cbh.task_local_correction_bundle.v1", bundle["schema"])
        self.assertEqual("rewrite_candidate", bundle["decision"])
        self.assertIn(
            "powershell_statement_loop_pipeline_pretool_migration",
            bundle["profile_ids"],
        )
        self.assertEqual([], bundle["source_record_ids"])
        self.assertEqual(bundle["candidate_key"], second["task_local_correction_bundle"]["candidate_key"])
        self.assertEqual("task_local_until_postcondition", bundle["lifecycle"])
        self.assertEqual("completed", first["actions"][0]["status"])

    def test_keyboard_mouse_candidate_requires_complete_focus_binding(self) -> None:
        route = {
            "tool_surface_need": "keyboard_mouse",
            "preferred_call_surface": "keyboard_mouse",
            "interaction_action_binding": {
                "interaction_surface": "foreground keyboard/mouse automation",
                "target_app": "WPS",
                "target_window": "",
                "target_control": "",
                "focus_evidence": "",
                "observation_ref": "",
                "observation_age": "unknown",
                "coordinate_space": "screen",
                "action": "SendKeys Ctrl+S",
                "expected_postcondition": "document saved",
                "fallback_boundary": "request user takeover when state is ambiguous",
                "risk": "local_write",
            },
            "action_bindings": [
                {
                    "action": "prepare_task_local_correction_bundle",
                    "completion_evidence": "task_local_correction_bundle",
                }
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="Computer Use 不可用时不要把 SendKeys 当成通用回退",
            tool_input_text="SendKeys Ctrl+S",
            meta_path=DEFAULT_COMMON_ERROR_META,
            capability_contract_path=self.contract,
        )
        bundle = receipt["task_local_correction_bundle"]
        self.assertEqual("semantic_review_required", bundle["decision"])
        self.assertEqual([], bundle["source_record_ids"])
        self.assertEqual(
            {"target_window", "target_control", "focus_evidence", "observation_ref"},
            set(bundle["action_binding"]["missing_fields"]),
        )
        self.assertEqual("incomplete", bundle["action_binding"]["status"])

    def test_keyboard_mouse_binding_requires_fresh_observation_before_validation(self) -> None:
        binding = {
            "interaction_surface": "foreground keyboard/mouse automation",
            "target_app": "WPS",
            "target_window": "WPS Writer - example.docx",
            "target_control": "document editor",
            "focus_evidence": "foreground hwnd and title match",
            "observation_ref": "OBS-001",
            "observation_age": "stale",
            "coordinate_space": "screen",
            "action": "SendKeys Ctrl+S",
            "expected_postcondition": "saved marker visible",
            "fallback_boundary": "request user takeover when state is ambiguous",
            "risk": "local_write",
        }
        route = {
            "tool_surface_need": "keyboard_mouse",
            "preferred_call_surface": "keyboard_mouse",
            "interaction_action_binding": binding,
            "action_bindings": [
                {
                    "action": "prepare_task_local_correction_bundle",
                    "completion_evidence": "task_local_correction_bundle",
                }
            ],
        }
        stale = build_action_consumption(
            route,
            prompt="WPS 中使用已验证焦点完成一次保存",
            tool_input_text="SendKeys Ctrl+S",
            meta_path=DEFAULT_COMMON_ERROR_META,
            capability_contract_path=self.contract,
        )["task_local_correction_bundle"]
        self.assertEqual("semantic_review_required", stale["decision"])
        self.assertEqual("stale_observation", stale["action_binding"]["status"])

        route["interaction_action_binding"] = {**binding, "observation_age": "5s"}
        fresh = build_action_consumption(
            route,
            prompt="WPS 中使用已验证焦点完成一次保存",
            tool_input_text="SendKeys Ctrl+S",
            meta_path=DEFAULT_COMMON_ERROR_META,
            capability_contract_path=self.contract,
        )["task_local_correction_bundle"]
        self.assertEqual("validation_required", fresh["decision"])
        self.assertEqual("complete", fresh["action_binding"]["status"])

    def test_direct_matches_expand_past_soft_target_and_prepare_task_local_predictions(self) -> None:
        route = {
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                {
                    "action": "prepare_task_local_prediction_review",
                    "completion_evidence": "task_local_prediction_review",
                },
            ],
            "first_principles_profile": "none",
            "skill_audit_profile": "none",
        }
        receipt = build_action_consumption(
            route,
            prompt=(
                "Avoid the foreach pipeline recurrence, inspect the command interface, "
                "and prevent Chinese encoding mojibake"
            ),
            tool_input_text="foreach($p in $paths) { Get-Item $p } | Format-Table",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["schema"], "cbh.action_consumption_receipt.v1")
        selected_ids = [item["record_id"] for item in receipt["selected_records"]]
        self.assertEqual(selected_ids[0], "CE-FOREACH")
        self.assertEqual(set(selected_ids), {"CE-FOREACH", "CE-PREFLIGHT", "CE-ENCODING"})
        self.assertTrue(
            all(
                item["score_method"] == "field_weighted_exact_and_lexical_v1"
                for item in receipt["selected_records"]
            )
        )
        self.assertEqual(receipt["retrieval"]["soft_target_records"], 2)
        self.assertEqual(receipt["retrieval"]["coverage_status"], "complete")
        self.assertTrue(receipt["retrieval"]["expanded_for_direct_coverage"])
        self.assertEqual(len(receipt["prediction_reviews"]), 3)
        self.assertEqual(receipt["prediction_reviews"][0]["record_id"], "CE-FOREACH")
        self.assertEqual(receipt["prediction_reviews"][0]["applicability_status"], "candidate")
        self.assertEqual(receipt["prediction_reviews"][0]["lifecycle"], "task_local_until_observation")
        self.assertIn("候选预测", receipt["additional_context"])
        self.assertNotIn("主动不变量", receipt["additional_context"])
        self.assertIn("CE-ENCODING", receipt["additional_context"])

    def test_duplicate_prevention_rules_prepare_one_prediction_without_dropping_provenance(self) -> None:
        with self.meta.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n| `CE-FOREACH-DUP` | function_tool_call_error | ACTIVE | foreach duplicate | PowerShell loop | collect rows first | repeat |\n"
            )
        (self.root / "CE-FOREACH-DUP.md").write_text(
            "# CE-FOREACH-DUP\n\n## Prevention\n\nCollect loop results into `$rows`, then pipe `$rows`; never pipe a bare foreach statement.\n",
            encoding="utf-8",
        )
        route = {
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                {
                    "action": "prepare_task_local_prediction_review",
                    "completion_evidence": "task_local_prediction_review",
                },
            ]
        }
        receipt = build_action_consumption(
            route,
            prompt="prevent the foreach pipeline and foreach duplicate recurrence",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertIn("CE-FOREACH-DUP", [item["record_id"] for item in receipt["selected_records"]])
        self.assertEqual(len(receipt["prediction_reviews"]), 1)
        self.assertEqual(
            set(receipt["prediction_reviews"][0]["source_record_ids"]),
            {"CE-FOREACH", "CE-FOREACH-DUP"},
        )

    def test_weak_match_requires_semantic_review_instead_of_claiming_complete_coverage(self) -> None:
        route = {
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
            ]
        }
        receipt = build_action_consumption(
            route,
            prompt="command",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["retrieval"]["coverage_status"], "semantic_review_required")
        self.assertGreaterEqual(len(receipt["retrieval"]["review_candidate_ids"]), 1)
        self.assertEqual(receipt["actions"][0]["status"], "ready_for_model_semantic_selection")
        self.assertEqual(receipt["semantic_review_owner"], "host_model_agent")
        self.assertEqual(receipt["status"], "complete")

    def test_direct_match_does_not_hide_weaker_distinct_candidates(self) -> None:
        route = {
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
            ]
        }
        receipt = build_action_consumption(
            route,
            prompt="foreach command",
            tool_input_text="foreach($p in $paths) { Get-Item $p } | Format-Table",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertIn("CE-FOREACH", [item["record_id"] for item in receipt["selected_records"]])
        self.assertIn("CE-PREFLIGHT", receipt["retrieval"]["review_candidate_ids"])
        self.assertEqual(receipt["retrieval"]["coverage_status"], "semantic_review_required")
        self.assertIn(
            "CE-PREFLIGHT",
            [item["record_id"] for item in receipt["semantic_review_candidates"]],
        )
        self.assertIn("CE-PREFLIGHT", receipt["additional_context"])
        self.assertEqual(receipt["actions"][0]["status"], "ready_for_model_semantic_selection")
        self.assertEqual(receipt["status"], "complete")

    def test_multi_family_direct_matches_are_all_retrieved(self) -> None:
        err_skill = self.root / "ERR-SKILL.md"
        sol_skill = self.root / "SOL-SKILL.md"
        anchor_skill = self.root / "ANCHOR-SKILL.md"
        err_payload = self.root / "error-points.md"
        sol_payload = self.root / "solution-points.md"
        anchor_payload = self.root / "semantic-anchors.md"
        err_skill.write_text(
            "| Error ID | Outer retrieval surface | Paired solution |\n"
            "| --- | --- | --- |\n"
            "| `ERR-ROUTE-MISS` | governance route miss, framework behavior change | `SOL-ROUTE-FIX` |\n",
            encoding="utf-8",
        )
        sol_skill.write_text(
            "| Solution ID | Outer retrieval surface | Paired errors |\n"
            "| --- | --- | --- |\n"
            "| `SOL-ROUTE-FIX` | governance trigger repair, route fix | `ERR-ROUTE-MISS` |\n",
            encoding="utf-8",
        )
        anchor_skill.write_text(
            "| Anchor ID | Exact phrases |\n"
            "| --- | --- |\n"
            "| `ANCHOR-COMPLETE-FIRST` | `能力完整优先`, `成本只是软预算` |\n",
            encoding="utf-8",
        )
        err_payload.write_text(
            "## ERR-ROUTE-MISS\n\nprevention_rule:\n- Governance changes must route through R3.\n",
            encoding="utf-8",
        )
        sol_payload.write_text(
            "## SOL-ROUTE-FIX\n\nfuture_reuse_rule:\n- Add narrow triggers and verify representative routes.\n",
            encoding="utf-8",
        )
        anchor_payload.write_text(
            "### ANCHOR-COMPLETE-FIRST\n\nanchored_meaning:\n- Preserve complete capability before optimizing retrieval cost.\n",
            encoding="utf-8",
        )
        sources = [
            {
                "family": "ERR",
                "index_path": err_skill,
                "payload_path": err_payload,
                "heading_level": 2,
                "rule_fields": ["prevention_rule"],
            },
            {
                "family": "SOL",
                "index_path": sol_skill,
                "payload_path": sol_payload,
                "heading_level": 2,
                "rule_fields": ["future_reuse_rule"],
            },
            {
                "family": "ANCHOR",
                "index_path": anchor_skill,
                "payload_path": anchor_payload,
                "heading_level": 3,
                "rule_fields": ["anchored_meaning"],
            },
        ]
        route = {
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                {
                    "action": "prepare_task_local_prediction_review",
                    "completion_evidence": "task_local_prediction_review",
                },
            ]
        }
        receipt = build_action_consumption(
            route,
            prompt="能力完整优先；修复 governance route miss，执行 governance trigger repair",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=sources,
        )
        self.assertEqual(
            {item["family"] for item in receipt["selected_records"]},
            {"ERR", "SOL", "ANCHOR"},
        )
        self.assertEqual(receipt["retrieval"]["coverage_status"], "complete")

    def test_route_facets_bridge_chinese_cbh_governance_request_to_err_sol_indexes(self) -> None:
        err_skill = self.root / "ERR-ROUTE-SKILL.md"
        sol_skill = self.root / "SOL-ROUTE-SKILL.md"
        err_payload = self.root / "route-errors.md"
        sol_payload = self.root / "route-solutions.md"
        err_skill.write_text(
            "| Error ID | Outer retrieval surface | Paired solution |\n"
            "| --- | --- | --- |\n"
            "| `ERR-CBH-ROUTE-MISS` | embedded harness intake router missed R3, governance layer update, routing trigger update, framework behavior change | `SOL-CBH-ROUTE-FIX` |\n",
            encoding="utf-8",
        )
        sol_skill.write_text(
            "| Solution ID | Outer retrieval surface | Paired errors |\n"
            "| --- | --- | --- |\n"
            "| `SOL-CBH-ROUTE-FIX` | embedded harness R3 trigger fix, governance layer update, routing rule, framework behavior update | `ERR-CBH-ROUTE-MISS` |\n",
            encoding="utf-8",
        )
        err_payload.write_text(
            "## ERR-CBH-ROUTE-MISS\n\nprevention_rule:\n- Governance changes must trigger paired retrieval.\n",
            encoding="utf-8",
        )
        sol_payload.write_text(
            "## SOL-CBH-ROUTE-FIX\n\nfuture_reuse_rule:\n- Bridge route facets into retrieval terms.\n",
            encoding="utf-8",
        )
        sources = [
            {"family": "ERR", "index_path": err_skill, "payload_path": err_payload, "heading_level": 2, "rule_fields": ["prevention_rule"]},
            {"family": "SOL", "index_path": sol_skill, "payload_path": sol_payload, "heading_level": 2, "rule_fields": ["future_reuse_rule"]},
        ]
        route = {
            "risk_level": "R3",
            "target_surface": "local_harness",
            "memory_need": "paired_err_sol",
            "feedback_loop_profile": "prevention_review",
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                {"action": "prepare_task_local_prediction_review", "completion_evidence": "task_local_prediction_review"},
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="完善 Codex 本地 CBH，重新检查隐藏控制层并修复主动触发断联",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=sources,
        )
        self.assertEqual({item["family"] for item in receipt["selected_records"]}, {"ERR", "SOL"})
        self.assertEqual(receipt["retrieval"]["coverage_status"], "complete")

    def test_paired_memory_expands_from_direct_error_to_index_declared_solution(self) -> None:
        err_skill = self.root / "ERR-PAIR-SKILL.md"
        sol_skill = self.root / "SOL-PAIR-SKILL.md"
        err_payload = self.root / "pair-errors.md"
        sol_payload = self.root / "pair-solutions.md"
        err_skill.write_text(
            "| Error ID | Outer retrieval surface | Paired solution |\n"
            "| --- | --- | --- |\n"
            "| `ERR-PAIR-DIRECT` | exact pair recurrence | `SOL-PAIR-CLOSURE` |\n",
            encoding="utf-8",
        )
        sol_skill.write_text(
            "| Solution ID | Outer retrieval surface | Paired errors |\n"
            "| --- | --- | --- |\n"
            "| `SOL-PAIR-CLOSURE` | unrelated wording that is not in the prompt | `ERR-PAIR-DIRECT` |\n",
            encoding="utf-8",
        )
        err_payload.write_text(
            "## ERR-PAIR-DIRECT\n\nprevention_rule:\n- Load the declared solution pair.\n",
            encoding="utf-8",
        )
        sol_payload.write_text(
            "## SOL-PAIR-CLOSURE\n\nfuture_reuse_rule:\n- Preserve the ERR/SOL closure.\n",
            encoding="utf-8",
        )
        route = {
            "memory_need": "paired_err_sol",
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="exact pair recurrence",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=[
                {"family": "ERR", "index_path": err_skill, "payload_path": err_payload, "heading_level": 2, "rule_fields": ["prevention_rule"]},
                {"family": "SOL", "index_path": sol_skill, "payload_path": sol_payload, "heading_level": 2, "rule_fields": ["future_reuse_rule"]},
            ],
        )
        selected_ids = [item["record_id"] for item in receipt["selected_records"]]
        self.assertEqual(selected_ids, ["ERR-PAIR-DIRECT", "SOL-PAIR-CLOSURE"])
        self.assertIn("paired_with:ERR-PAIR-DIRECT", receipt["selected_records"][1]["selection_reasons"])

    def test_default_catalogs_belong_to_the_adopting_package(self) -> None:
        import harness_action_consumer as consumer
        runtime_parent = Path(consumer.__file__).resolve().parent.parent
        for path in (consumer.DEFAULT_COMMON_ERROR_META, consumer.DEFAULT_MAJOR_INCIDENT_META,
                     consumer.DEFAULT_ERROR_PAYLOAD, consumer.DEFAULT_ANCHOR_PAYLOAD):
            self.assertTrue(path.resolve().is_relative_to(runtime_parent))
            self.assertNotIn("skill-tools", str(path))

    def test_vague_incident_clue_returns_brief_candidates_without_claiming_a_match(self) -> None:
        route = {
            "memory_need": "paired_err_sol",
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="我只记得6月有一次重大事件，后来客户端好像坏了，列出可能内容让我确认",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=self.public_history_sources(),
        )
        retrieval = receipt["retrieval"]
        self.assertEqual(retrieval["coverage_status"], "semantic_review_required")
        self.assertFalse(retrieval["major_incident_routed"])
        self.assertGreaterEqual(len(retrieval["disambiguation_candidates"]), 1)
        candidate = retrieval["disambiguation_candidates"][0]
        self.assertEqual(
            candidate["record_id"],
            "INC-PUBLIC-CLIENT-2026-06-13",
        )
        self.assertEqual(candidate["event_date"], "2026-06-13")
        self.assertIn("客户端", candidate["summary"])
        self.assertIn("请让用户确认", receipt["additional_context"])
        self.assertEqual(receipt["prediction_reviews"], [])

    def test_users_exact_sparse_clue_still_lists_candidates_instead_of_opening_incident_payload(self) -> None:
        receipt = build_action_consumption(
            {
                "memory_need": "paired_err_sol",
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                ],
            },
            prompt="6月的一次重大事件，导致客户端损坏问题",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=self.public_history_sources(),
        )
        self.assertFalse(receipt["retrieval"]["major_incident_routed"])
        self.assertEqual(receipt["selected_records"], [])
        self.assertEqual(receipt["retrieval"]["coverage_status"], "semantic_review_required")
        self.assertEqual(len(receipt["retrieval"]["disambiguation_candidates"]), 1)

    def test_written_chinese_and_english_month_fragments_find_the_same_navigation_candidate(self) -> None:
        for prompt in ("六月", "六月份那次问题", "June incident"):
            with self.subTest(prompt=prompt):
                receipt = build_action_consumption(
                    {
                        "memory_need": "index_only",
                        "action_bindings": [
                            {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                        ],
                    },
                    prompt=prompt,
                    meta_path=self.meta,
                    capability_contract_path=self.contract,
                    memory_sources=self.public_history_sources(),
                )
                self.assertEqual(receipt["selected_records"], [])
                self.assertEqual(
                    receipt["retrieval"]["disambiguation_candidates"][0]["event_date"],
                    "2026-06-13",
                )

    def test_month_only_incident_query_lists_all_same_month_candidates_without_arbitrary_selection(self) -> None:
        incidents = self.root / "incidents.md"
        incidents.write_text(
            "# Major Incident Index\n\n"
            "| Incident ID | Trigger aliases | Current status | Paired ERR | Paired SOL |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| `INC-CLIENT-DAMAGE-2026-06-13` | `客户端故障`, `插件冲突` | `historical` | none | none |\n"
            "| `INC-CONFIG-LOSS-2026-06-28` | `项目消失`, `配置损坏` | `historical` | none | none |\n"
            "| `INC-JULY-ONLY-2026-07-01` | `七月事件` | `historical` | none | none |\n\n"
            "## INC-CLIENT-DAMAGE-2026-06-13\n\n"
            "current_status:\n- `historical`\n\ncausal_chain:\n- 客户端插件冲突后无法正常工作。\n\n"
            "## INC-CONFIG-LOSS-2026-06-28\n\n"
            "current_status:\n- `historical`\n\ncausal_chain:\n- 配置损坏后项目入口消失。\n\n"
            "## INC-JULY-ONLY-2026-07-01\n\n"
            "current_status:\n- `historical`\n\ncausal_chain:\n- 七月的无关事件。\n",
            encoding="utf-8",
        )
        result = select_memory_records(
            "我只记得是6月，列出可能事件让我确认",
            "",
            memory_sources=[
                {
                    "family": "INC",
                    "index_path": incidents,
                    "payload_path": incidents,
                    "heading_level": 2,
                    "rule_fields": ["current_status", "causal_chain"],
                }
            ],
        )
        self.assertEqual(result["coverage_status"], "semantic_review_required")
        self.assertEqual(
            [item["record_id"] for item in result["disambiguation_candidates"]],
            ["INC-CLIENT-DAMAGE-2026-06-13", "INC-CONFIG-LOSS-2026-06-28"],
        )
        self.assertFalse(result["major_incident_routed"])

    def test_historical_solution_is_context_only_and_never_a_current_prediction(self) -> None:
        sol_index = self.root / "historical-solutions.md"
        sol_payload = self.root / "historical-solution-points.md"
        sol_index.write_text(
            "| Solution ID | Outer retrieval surface | Paired errors |\n"
            "| --- | --- | --- |\n"
            "| `SOL-HISTORICAL-PIN` | legacy source pin repair | none |\n",
            encoding="utf-8",
        )
        sol_payload.write_text(
            "## SOL-HISTORICAL-PIN\n\n"
            "status:\n- `historical_superseded_harmful_after_update_do_not_auto_reuse`\n\n"
            "future_reuse_rule:\n- Preserve only as historical evidence.\n",
            encoding="utf-8",
        )
        receipt = build_action_consumption(
            {
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                    {
                        "action": "prepare_task_local_prediction_review",
                        "completion_evidence": "task_local_prediction_review",
                    },
                ]
            },
            prompt="legacy source pin repair",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=[
                {
                    "family": "SOL",
                    "index_path": sol_index,
                    "payload_path": sol_payload,
                    "heading_level": 2,
                    "rule_fields": ["future_reuse_rule"],
                }
            ],
        )
        self.assertEqual(len(receipt["selected_records"]), 1)
        self.assertFalse(receipt["selected_records"][0]["eligible_for_current_reuse"])
        self.assertEqual(receipt["prediction_reviews"], [])
        self.assertIn("不得自动复用", receipt["additional_context"])

    def test_current_configuration_question_never_reactivates_historical_solution(self) -> None:
        receipt = build_action_consumption(
            {
                "memory_need": "paired_err_sol",
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                    {
                        "action": "prepare_task_local_prediction_review",
                        "completion_evidence": "task_local_prediction_review",
                    },
                ],
            },
            prompt="Is legacy configuration still applicable now?",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=self.public_history_sources(),
        )
        selected_ids = {item["record_id"] for item in receipt["selected_records"]}
        prediction_ids = {item["record_id"] for item in receipt["prediction_reviews"]}
        self.assertIn("SOL-PUBLIC-HISTORY", selected_ids)
        self.assertNotIn("SOL-PUBLIC-HISTORY", prediction_ids)

    def test_historical_readonly_incident_is_not_injected_into_ordinary_cbh_design_review(self) -> None:
        receipt = build_action_consumption(
            {
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                ],
                "memory_need": "self_reflection_matrix",
                "memory_lane": "self_reflection_matrix",
                "first_principles_profile": "constraint_gate",
                "skill_audit_profile": "none",
            },
            prompt="Review the foreach pipeline design",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=self.public_history_sources(),
        )
        selected_ids = {item["record_id"] for item in receipt["selected_records"]}
        review_ids = {item["record_id"] for item in receipt["semantic_review_candidates"]}
        self.assertEqual(set(), selected_ids)
        self.assertNotIn("ERR-PUBLIC-HISTORY", review_ids)
        self.assertNotIn("ERR-PUBLIC-HISTORY", receipt["additional_context"])

    def test_exact_historical_record_id_remains_retrievable_as_noncurrent_context(self) -> None:
        record_id = "ERR-PUBLIC-HISTORY"
        receipt = build_action_consumption(
            {
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                ],
                "memory_need": "paired_err_sol",
                "memory_lane": "self_reflection_matrix",
                "first_principles_profile": "none",
                "skill_audit_profile": "none",
            },
            prompt=f"历史回查 {record_id}",
            meta_path=self.meta,
            capability_contract_path=self.contract,
            memory_sources=self.public_history_sources(),
        )
        records = receipt["selected_records"] + receipt["semantic_review_candidates"]
        matched = [item for item in records if item["record_id"] == record_id]
        self.assertEqual(len(matched), 1)
        self.assertFalse(matched[0]["eligible_for_current_reuse"])
        self.assertIn(record_id, receipt["additional_context"])

    def test_route_declared_current_conversation_lane_is_read_dynamically(self) -> None:
        lane = self.root / "conversation-lane"
        lane.mkdir()
        lane_meta = lane / "_META_INDEX.md"
        lane_meta.write_text("# Current Conversation Lane\n", encoding="utf-8")
        decisions = lane / "decisions.jsonl"
        decisions.write_text(
            json.dumps(
                {
                    "record_id": "DEC-CURRENT-LANE-001",
                    "status": "active",
                    "retrieval_terms": ["exact current lane decision"],
                    "decision": "Use the exact current conversation lane.",
                    "source_tag": "conversation_ledger",
                    "belief_status": "verified_local_record",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (lane / "index.json").write_text(
            json.dumps(
                {"record_families": {"decisions": "decisions.jsonl"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        receipt = build_action_consumption(
            {
                "memory_source_hints": [
                    {
                        "lane": "current_conversation",
                        "root_path": str(lane),
                        "meta_path": str(lane_meta),
                        "isolation": "exact_active_lane",
                    }
                ],
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                ],
            },
            prompt="exact current lane decision",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["dynamic_memory_sources"]["status"], "accepted")
        selected = {item["record_id"]: item for item in receipt["selected_records"]}
        self.assertIn("DEC-CURRENT-LANE-001", selected)
        self.assertEqual(selected["DEC-CURRENT-LANE-001"]["family"], "LANE")
        self.assertEqual(selected["DEC-CURRENT-LANE-001"]["source_tag"], "conversation_ledger")

    def test_unindexed_dynamic_memory_root_is_rejected(self) -> None:
        empty_lane = self.root / "unindexed-lane"
        empty_lane.mkdir()
        receipt = build_action_consumption(
            {
                "memory_source_hints": [
                    {"lane": "current_conversation", "root_path": str(empty_lane)}
                ],
                "action_bindings": [
                    {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
                ],
            },
            prompt="missing lane record",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["dynamic_memory_sources"]["status"], "none")
        self.assertEqual(
            receipt["dynamic_memory_sources"]["rejected"][0]["reason"],
            "no_meta_or_indexed_family",
        )

    def test_dynamic_v3_source_rejects_invalid_store_manifest(self) -> None:
        store = self.root / "invalid-v3-store"
        record = build_memory_record(
            record_id="MEM-INVALID-MANIFEST-001",
            memory_class="conversation_event",
            lane_id="CONVERSATION-INVALID-MANIFEST",
            lane_kind="conversation",
            owner_skill="test",
            observed_at="2026-08-14T10:00:00Z",
            state="historical",
            current_status="completed_task_checkpoint",
            confidence={"label": "high", "basis": "test fixture"},
            query_types=["history_reason"],
            candidate_label="invalid manifest fixture",
            summary="invalid manifest fixture",
            retrieval_terms=["MEM-INVALID-MANIFEST-001"],
            facets={"surface": ["test"]},
            content={
                "event_summary": "invalid manifest fixture",
                "details": "fixture",
                "applicable_boundaries": ["test"],
                "non_applicable_boundaries": [],
                "evidence_boundary": "test fixture only",
            },
        )
        append_memory_record(store, record)
        (store / "store.json").write_text("{}\n", encoding="utf-8")
        receipt = build_action_consumption(
            {
                "memory_need": "index_only",
                "memory_source_hints": [
                    {"lane": "current_conversation", "root_path": str(store)}
                ],
                "action_bindings": [
                    {
                        "action": "retrieve_matching_memory",
                        "completion_evidence": "selected_record_id",
                    }
                ],
            },
            prompt="MEM-INVALID-MANIFEST-001",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["dynamic_memory_sources"]["status"], "none")
        self.assertEqual(receipt["selected_records"], [])
        self.assertIn(
            "invalid_semantic_memory_store_manifest",
            receipt["dynamic_memory_sources"]["rejected"][0]["reason"],
        )

    def test_referenced_conversation_reads_meta_link_and_ledger_navigation_bundle(self) -> None:
        lane = self.root / "current-lane"
        old_lane = self.root / "old-lane"
        ledger = self.root / "current-ledger"
        old_ledger = self.root / "old-ledger"
        for path in (lane, old_lane, ledger, old_ledger):
            path.mkdir()
        (lane / "_META_INDEX.md").write_text("# Current CBH lane\n", encoding="utf-8")
        (lane / "conversation_state.md").write_text("Current CBH state.\n", encoding="utf-8")
        (old_lane / "_META_INDEX.md").write_text("# Linked prior CBH lane\n", encoding="utf-8")
        (ledger / "_LEDGER_INDEX.md").write_text("# Current ledger index\n", encoding="utf-8")
        (old_ledger / "_LEDGER_INDEX.md").write_text("# Linked prior ledger index\n", encoding="utf-8")
        raw = self.root / "raw.jsonl"
        raw.write_text('{"type":"event"}\n', encoding="utf-8")
        (ledger / "sessions.jsonl").write_text(
            json.dumps(
                {
                    "raw_session_path": str(raw),
                    "raw_size_bytes": raw.stat().st_size,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (ledger / "capsules.jsonl").write_text("", encoding="utf-8")
        (ledger / "evidence_refs.jsonl").write_text("", encoding="utf-8")
        memory_id = "conversation:test-current"
        (lane / "index.json").write_text(
            json.dumps(
                {
                    "memory_id": memory_id,
                    "conversation_ledger": {"path": str(ledger)},
                    "record_families": {"memory_links": "memory_links.jsonl"},
                }
            ),
            encoding="utf-8",
        )
        (lane / "memory_links.jsonl").write_text(
            json.dumps(
                {
                    "link_id": "LINK-TEST-CBH-001",
                    "status": "ACTIVE",
                    "link_type": "continuation",
                    "from_memory_id": "conversation:test-old",
                    "from_path": str(old_lane),
                    "from_ledger_path": str(old_ledger),
                    "to_memory_id": memory_id,
                    "to_path": str(lane),
                    "to_ledger_path": str(ledger),
                    "write_policy": "new_memory_only",
                    "evidence_boundary": "meta_only",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        route = {
            "memory_need": "conversation_state",
            "memory_lane": "referenced_conversation",
            "memory_source_hints": [
                {
                    "lane": "referenced_conversation",
                    "memory_id": memory_id,
                    "root_path": str(lane),
                    "meta_path": str(lane / "_META_INDEX.md"),
                    "ledger_root_path": str(ledger),
                    "ledger_index_path": str(ledger / "_LEDGER_INDEX.md"),
                    "navigation_profile": "meta_state_links_ledger_one_hop",
                    "isolation": "unique_active_registry_match_link_only_read",
                }
            ],
            "action_bindings": [
                {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"},
                {"action": "resolve_conversation_link", "completion_evidence": "resolved_readonly_navigation_bundle"},
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="迁移我们的 CBH 框架",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        navigation = receipt["conversation_navigation"]
        self.assertEqual(navigation["status"], "resolved")
        bundle = navigation["bundles"][0]
        kinds = {item["kind"] for item in bundle["documents"]}
        self.assertTrue(
            {
                "lane_meta",
                "conversation_state",
                "memory_links",
                "ledger_index",
                "linked_lane_meta",
                "linked_ledger_index",
            }.issubset(kinds)
        )
        self.assertEqual(bundle["selected_links"][0]["link_id"], "LINK-TEST-CBH-001")
        self.assertEqual(bundle["ledger"]["snapshot"]["status"], "current_snapshot")
        link_action = next(
            item for item in receipt["actions"] if item["action_id"] == "resolve_conversation_link"
        )
        self.assertEqual(link_action["status"], "completed")
        context_pages = [receipt["additional_context"]]
        cursor = receipt["transport_receipt"]["next_cursor"]
        while cursor is not None:
            page_receipt = build_action_consumption(
                route,
                prompt="迁移我们的 CBH 框架",
                meta_path=self.meta,
                capability_contract_path=self.contract,
                transport_cursor=cursor,
            )
            context_pages.append(page_receipt["additional_context"])
            cursor = page_receipt["transport_receipt"]["next_cursor"]
        rebuilt_context = "".join(context_pages)
        self.assertIn("Linked prior ledger index", rebuilt_context)
        self.assertEqual(
            hashlib.sha256(rebuilt_context.encode("utf-8")).hexdigest(),
            receipt["transport_receipt"]["full_result_sha256"],
        )

    def test_missing_ledger_root_does_not_fall_back_to_process_cwd(self) -> None:
        lane = self.root / "lane-without-ledger"
        lane.mkdir()
        (lane / "_META_INDEX.md").write_text("# Lane without ledger\n", encoding="utf-8")
        (lane / "index.json").write_text(
            json.dumps({"memory_id": "conversation:no-ledger"}),
            encoding="utf-8",
        )
        rogue_ledger = self.root / "_LEDGER_INDEX.md"
        rogue_ledger.write_text("# Must not be read through cwd fallback\n", encoding="utf-8")
        prior_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            receipt = build_action_consumption(
                {
                    "memory_source_hints": [
                        {
                            "memory_id": "conversation:no-ledger",
                            "root_path": str(lane),
                            "meta_path": str(lane / "_META_INDEX.md"),
                            "navigation_profile": "meta_state_links_ledger_one_hop",
                        }
                    ],
                    "action_bindings": [
                        {
                            "action": "resolve_conversation_link",
                            "completion_evidence": "resolved_readonly_navigation_bundle",
                        }
                    ],
                },
                prompt="只读恢复当前对话导航",
                meta_path=self.meta,
                capability_contract_path=self.contract,
            )
        finally:
            os.chdir(prior_cwd)

        bundle = receipt["conversation_navigation"]["bundles"][0]
        kinds = {item["kind"] for item in bundle["documents"]}
        self.assertNotIn("ledger_index", kinds)
        self.assertEqual(bundle["ledger"]["root_path"], "")
        self.assertEqual(bundle["ledger"]["snapshot"]["reason"], "ledger_root_missing")
        self.assertNotIn("Must not be read through cwd fallback", receipt["additional_context"])

    def test_conversation_navigation_follows_atomic_ledger_generation_pointer(self) -> None:
        lane = self.root / "pointer-lane"
        ledger_root = self.root / "pointer-ledger"
        generation = ledger_root / "generations" / "generation-test"
        lane.mkdir()
        generation.mkdir(parents=True)
        (lane / "_META_INDEX.md").write_text("# Pointer lane\n", encoding="utf-8")
        (lane / "conversation_state.md").write_text("Pointer state.\n", encoding="utf-8")
        (lane / "memory_links.jsonl").write_text("", encoding="utf-8")
        memory_id = "conversation:pointer-test"
        (lane / "index.json").write_text(
            json.dumps(
                {
                    "memory_id": memory_id,
                    "conversation_ledger": {"path": str(ledger_root)},
                }
            ),
            encoding="utf-8",
        )
        (ledger_root / "CURRENT.json").write_text(
            json.dumps(
                {
                    "schema": "cbh.ledger_generation_pointer.v1",
                    "generation_id": "generation-test",
                    "relative_path": "generations/generation-test",
                }
            ),
            encoding="utf-8",
        )
        (generation / "_LEDGER_INDEX.md").write_text(
            "# Current generation ledger\n",
            encoding="utf-8",
        )
        raw = self.root / "pointer-raw.jsonl"
        raw.write_text('{"type":"event"}\n', encoding="utf-8")
        (generation / "sessions.jsonl").write_text(
            json.dumps(
                {
                    "raw_session_path": str(raw),
                    "raw_size_bytes": raw.stat().st_size,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (generation / "capsules.jsonl").write_text("", encoding="utf-8")
        (generation / "evidence_refs.jsonl").write_text("", encoding="utf-8")
        receipt = build_action_consumption(
            {
                "memory_source_hints": [
                    {
                        "memory_id": memory_id,
                        "root_path": str(lane),
                        "meta_path": str(lane / "_META_INDEX.md"),
                        "ledger_root_path": str(ledger_root),
                        "ledger_index_path": str(ledger_root / "_LEDGER_INDEX.md"),
                        "navigation_profile": "meta_state_links_ledger_one_hop",
                    }
                ],
                "action_bindings": [{"action": "resolve_conversation_link"}],
            },
            prompt="读取当前 generation ledger",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        bundle = receipt["conversation_navigation"]["bundles"][0]
        ledger_document = next(
            item for item in bundle["documents"] if item["kind"] == "ledger_index"
        )
        self.assertEqual(
            Path(ledger_document["path"]).resolve(),
            (generation / "_LEDGER_INDEX.md").resolve(),
        )
        self.assertIn("Current generation ledger", ledger_document["excerpt"])
        self.assertEqual(Path(bundle["ledger"]["root_path"]).resolve(), generation.resolve())
        self.assertEqual(bundle["ledger"]["snapshot"]["status"], "current_snapshot")

    def test_cli_stdout_is_strict_utf8_without_replacement_characters(self) -> None:
        consumer = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "harness_action_consumer.py"
        route = {
            "first_principles_profile": "micro_constraints",
            "action_bindings": [
                {
                    "action": "apply_first_principles_profile",
                    "completion_evidence": "constraints_alternatives_and_verifier",
                }
            ],
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(consumer),
                "--route-json",
                json.dumps(route, ensure_ascii=False),
                "--prompt",
                "验证中文输出与 Unicode 保真",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        decoded = completed.stdout.decode("utf-8", errors="strict")
        self.assertNotIn("\ufffd", decoded)
        self.assertIn("任务局部轻量复核", decoded)

    def test_cli_accepts_large_route_from_stdin_and_defaults_to_compact(self) -> None:
        consumer = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "harness_action_consumer.py"
        route = {
            "padding": "x" * 60_000,
            "compact_receipt": {
                "action_bindings": [],
                "memory_mode": "none",
                "memory_lane": "none",
            },
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(consumer),
                "--route-stdin",
                "--prompt",
                "只读检查一个超长 route 的传输链",
            ],
            input=json.dumps(route, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        decoded = completed.stdout.decode("utf-8", errors="strict")
        receipt = json.loads(decoded)
        self.assertEqual(receipt["schema"], "cbh.action_consumption_receipt.compact.v1")
        self.assertLess(len(completed.stdout), 8192)
        self.assertIn("additional_context", receipt)
        self.assertNotIn("context_segments", receipt)

    def test_cli_diagnostic_mode_preserves_full_receipt(self) -> None:
        consumer = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "harness_action_consumer.py"
        route = {"compact_receipt": {"action_bindings": [], "memory_mode": "none"}}
        completed = subprocess.run(
            [
                sys.executable,
                str(consumer),
                "--route-stdin",
                "--receipt-mode",
                "diagnostic",
            ],
            input=json.dumps(route).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        receipt = json.loads(completed.stdout.decode("utf-8", errors="strict"))
        self.assertEqual(receipt["schema"], "cbh.action_consumption_receipt.v1")
        self.assertIn("context_segments", receipt)

    def test_cli_accepts_utf8_bom_route_file(self) -> None:
        consumer = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "harness_action_consumer.py"
        route_path = self.root / "large-route.json"
        route_path.write_text(
            json.dumps(
                {
                    "padding": "中文" * 30_000,
                    "compact_receipt": {
                        "action_bindings": [],
                        "memory_mode": "none",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8-sig",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(consumer),
                "--route-file",
                str(route_path),
                "--receipt-mode",
                "compact",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(
            "cbh.action_consumption_receipt.compact.v1",
            json.loads(completed.stdout.decode("utf-8", errors="strict"))["schema"],
        )

    def test_compact_projection_keeps_provenance_without_duplicate_record_text(self) -> None:
        receipt = {
            "status": "complete",
            "selected_records": [
                {
                    "record_id": "CE-1",
                    "path": "CE-1.md",
                    "sha256": "a" * 64,
                    "score": 9,
                    "score_method": "field_weighted_exact_and_lexical_v1",
                    "rule": "full selected record text",
                    "prevention": "full selected record text",
                }
            ],
            "semantic_review_candidates": [
                {
                    "record_id": "SOL-1",
                    "path": "SOL-1.md",
                    "sha256": "b" * 64,
                    "score": 2,
                    "rule": "full semantic candidate text",
                }
            ],
            "additional_context": "full selected record text\nfull semantic candidate text",
        }

        compact = _compact_runtime_receipt(receipt)

        self.assertEqual(receipt["additional_context"], compact["additional_context"])
        self.assertEqual("a" * 64, compact["selected_records"][0]["sha256"])
        self.assertNotIn("rule", compact["selected_records"][0])
        self.assertNotIn("prevention", compact["selected_records"][0])
        self.assertNotIn("rule", compact["semantic_review_candidates"][0])

    def test_first_principles_and_skill_audit_are_bound_to_near_action_context(self) -> None:
        route = {
            "action_bindings": [
                {"action": "apply_first_principles_profile", "completion_evidence": "constraints_and_minimal_patch"},
                {"action": "perform_skill_audit_profile", "completion_evidence": "skill_audit_findings"},
            ],
            "first_principles_profile": "constraint_gate",
            "skill_audit_profile": "safety_and_redundancy_audit",
        }
        receipt = build_action_consumption(
            route,
            prompt="审计这个 skill 的安全、功能重叠、长期未用和可合并项",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        statuses = {item["action_id"]: item["status"] for item in receipt["actions"]}
        self.assertEqual(statuses["apply_first_principles_profile"], "bound_to_context")
        self.assertEqual(statuses["perform_skill_audit_profile"], "bound_to_context")
        self.assertEqual(receipt["reasoning_activation"]["profile"], "focused_review")
        self.assertIn("具体事实", receipt["additional_context"])
        self.assertIn("不可违反约束", receipt["additional_context"])
        self.assertIn("替代假设", receipt["additional_context"])
        self.assertIn("功能重叠", receipt["additional_context"])
        self.assertLessEqual(receipt["context_char_count"], receipt["context_soft_target_chars"])

    def test_existing_cbh_signals_compose_one_multi_lens_reasoning_activation(self) -> None:
        route = {
            "risk_level": "R3",
            "target_surface": "local_harness",
            "first_principles_profile": "constraint_gate",
            "feedback_loop_profile": "explicit_cycle",
            "required_gates": [
                "first_principles_gate",
                "global_task_context_gate",
                "feedback_loop_gate",
            ],
            "read_semantic_boundary": ["causal_scope", "change_integrity"],
            "action_bindings": [
                {
                    "action": "apply_first_principles_profile",
                    "completion_evidence": "constraints_and_minimal_patch",
                },
                {
                    "action": "verify_and_calibrate_feedback",
                    "completion_evidence": "verified_postcondition_or_calibration",
                },
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="连接 CBH 的全局观、因果闭环和预测闭环，但不要让第一性原理成为唯一视角",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        activation = receipt["reasoning_activation"]
        self.assertEqual(activation["status"], "active")
        self.assertEqual(activation["profile"], "causal_prediction_cycle")
        self.assertEqual(
            activation["lenses"],
            [
                "concrete_case_evidence",
                "first_principles_constraints",
                "global_system_context",
                "causal_relationships",
                "prediction_observation",
                "counterfactual_alternative",
                "verifier_selection",
            ],
        )
        self.assertIn("具体事实", receipt["additional_context"])
        self.assertIn("全局依赖", receipt["additional_context"])
        self.assertIn("替代假设", receipt["additional_context"])
        self.assertIn("可证伪", receipt["additional_context"])
        self.assertIn("能力完整", receipt["additional_context"])
        self.assertNotIn("只做最小补丁", receipt["additional_context"])

    def test_micro_constraints_use_a_compact_non_anchoring_review(self) -> None:
        route = {
            "risk_level": "R2",
            "first_principles_profile": "micro_constraints",
            "feedback_loop_profile": "none",
            "required_gates": [],
            "read_semantic_boundary": [],
            "action_bindings": [
                {
                    "action": "apply_first_principles_profile",
                    "completion_evidence": "constraints_and_minimal_patch",
                }
            ],
        }
        receipt = build_action_consumption(
            route,
            prompt="做一个普通但非平凡的小修改",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["reasoning_activation"]["profile"], "micro_review")
        self.assertEqual(
            receipt["reasoning_activation"]["lenses"],
            [
                "concrete_case_evidence",
                "first_principles_constraints",
                "counterfactual_alternative",
                "verifier_selection",
            ],
        )
        self.assertIn("一个反例", receipt["additional_context"])
        self.assertLess(len(receipt["additional_context"]), 220)

    def test_low_confidence_route_activates_bounded_reasoning_review(self) -> None:
        route = {
            "risk_level": "R0",
            "classification_confidence": "low",
            "first_principles_profile": "none",
            "feedback_loop_profile": "none",
            "required_gates": ["model_boundary_review_gate"],
            "read_semantic_boundary": [],
            "action_bindings": [],
        }
        receipt = build_action_consumption(
            route,
            prompt="A long, unfamiliar task whose execution boundary is not yet classified.",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        activation = receipt["reasoning_activation"]
        self.assertEqual(activation["status"], "active")
        self.assertEqual(activation["profile"], "focused_review")
        self.assertIn("classification_confidence:low", activation["activation_reasons"])
        self.assertIn("uncertainty_boundary", activation["lenses"])
        self.assertIn("具体事实", receipt["additional_context"])
        self.assertIn("替代假设", receipt["additional_context"])
        self.assertIn("机械验证", receipt["additional_context"])

    def test_reflexive_gap_gate_adds_one_shallow_progressive_lens(self) -> None:
        route = {
            "risk_level": "R3",
            "required_gates": ["reflexive_gap_gate", "reflexive_gap_gate"],
            "matched_risk_triggers": {
                "reflexive_gap_gate": [
                    "knowledge_action_mismatch",
                    "goal_fidelity_gap",
                    "knowledge_action_mismatch",
                ]
            },
            "first_principles_profile": "none",
            "feedback_loop_profile": "none",
            "read_semantic_boundary": [],
            "action_bindings": [],
        }
        receipt = build_action_consumption(
            route,
            prompt="The known rule and selected action conflict while proxy work grows.",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        activation = receipt["reasoning_activation"]
        self.assertEqual(activation["status"], "active")
        self.assertEqual(
            activation["lenses"].count("reflexive_gap_classification"), 1
        )
        self.assertIn(
            "required_gate:reflexive_gap_gate", activation["activation_reasons"]
        )
        review = receipt["reflexive_gap_review"]
        self.assertEqual(review["schema"], "cbh.reflexive_gap_probe.v1")
        self.assertEqual(review["status"], "candidate_review_required")
        self.assertEqual(
            review["trigger_sources"],
            ["knowledge_action_mismatch", "goal_fidelity_gap"],
        )
        self.assertEqual(
            review["required_receipt_fields"],
            [
                "gap_class",
                "counterevidence",
                "next_action_binding",
                "verifier",
                "retire_condition",
            ],
        )
        self.assertFalse(review["standing_constraint"])
        self.assertEqual(review["memory_write"], "not_authorized")
        self.assertIn("知识覆盖缺口", receipt["additional_context"])
        self.assertIn("知识激活缺口", receipt["additional_context"])
        self.assertIn("目标保真缺口", receipt["additional_context"])
        self.assertIn("环境或接口缺口", receipt["additional_context"])
        self.assertIn("$reflexive-gap-analysis", receipt["additional_context"])
        self.assertLess(len(receipt["additional_context"]), 650)
        self.assertEqual(receipt["actions"], [])
        self.assertEqual(receipt["selected_records"], [])
    def test_ordinary_route_has_no_reflexive_gap_probe(self) -> None:
        receipt = build_action_consumption(
            {
                "required_gates": [],
                "matched_risk_triggers": {},
                "first_principles_profile": "none",
                "feedback_loop_profile": "none",
                "read_semantic_boundary": [],
                "action_bindings": [],
            },
            prompt="Explain what a hash is.",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["reasoning_activation"]["status"], "not_requested")
        self.assertIsNone(receipt["reflexive_gap_review"])
        self.assertEqual(receipt["additional_context"], "")

    def test_reflexive_gap_probe_has_no_cross_call_state_in_1000_rounds(self) -> None:
        reflexive_route = {
            "required_gates": ["reflexive_gap_gate"],
            "matched_risk_triggers": {
                "reflexive_gap_gate": ["knowledge_action_mismatch"]
            },
            "first_principles_profile": "none",
            "feedback_loop_profile": "none",
            "read_semantic_boundary": [],
            "action_bindings": [],
        }
        ordinary_route = {
            "required_gates": [],
            "matched_risk_triggers": {},
            "first_principles_profile": "none",
            "feedback_loop_profile": "none",
            "read_semantic_boundary": [],
            "action_bindings": [],
        }
        for index in range(1000):
            active = index % 2 == 0
            receipt = build_action_consumption(
                reflexive_route if active else ordinary_route,
                prompt="synthetic pressure case",
                meta_path=self.meta,
                capability_contract_path=self.contract,
            )
            if active:
                self.assertIsNotNone(receipt["reflexive_gap_review"])
                self.assertEqual(
                    receipt["reasoning_activation"]["lenses"].count(
                        "reflexive_gap_classification"
                    ),
                    1,
                )
                self.assertNotIn("Rationalization Warnings", receipt["additional_context"])
            else:
                self.assertIsNone(receipt["reflexive_gap_review"])
                self.assertEqual(receipt["reasoning_activation"]["status"], "not_requested")
                self.assertEqual(receipt["additional_context"], "")

    def test_unexecutable_actions_remain_explicitly_incomplete(self) -> None:
        route = {
            "action_bindings": [
                {"action": "perform_external_research_route", "completion_evidence": "source_ledger_or_citations"},
                {"action": "write_selected_memory_lane", "completion_evidence": "written_record_or_artifact_path"},
            ],
            "first_principles_profile": "none",
            "skill_audit_profile": "none",
        }
        receipt = build_action_consumption(
            route,
            prompt="检查最新版本并记录长期记忆",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        statuses = {item["action_id"]: item["status"] for item in receipt["actions"]}
        self.assertEqual(statuses["perform_external_research_route"], "deferred_to_agent")
        self.assertEqual(statuses["write_selected_memory_lane"], "host_pending")
        self.assertEqual(
            "cbh.external_retrieval_receipt.v1",
            receipt["external_retrieval_receipt"]["schema"],
        )
        self.assertEqual(
            "planned", receipt["external_retrieval_receipt"]["coverage_status"]
        )
        self.assertEqual([], receipt["selected_records"])
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(
            set(receipt["unconsumed_action_ids"]),
            {"perform_external_research_route", "write_selected_memory_lane"},
        )

    def test_declared_consumer_support_without_handler_never_reports_completed(self) -> None:
        contract = json.loads(self.contract.read_text(encoding="utf-8"))
        contract["action_support"]["future_unimplemented_action"] = "consumer_supported"
        self.contract.write_text(
            json.dumps(contract, ensure_ascii=False), encoding="utf-8"
        )
        receipt = build_action_consumption(
            {
                "action_bindings": [
                    {
                        "action": "future_unimplemented_action",
                        "completion_evidence": "imaginary_receipt",
                    }
                ]
            },
            prompt="exercise an unimplemented consumer contract",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        action = receipt["actions"][0]
        self.assertEqual("consumer_handler_missing", action["status"])
        self.assertEqual("no_concrete_consumer_receipt", action["completion_evidence"])
        self.assertEqual(["future_unimplemented_action"], receipt["unconsumed_action_ids"])
        self.assertEqual("incomplete", receipt["status"])

    def test_scoped_user_confirmation_stays_pending_and_stateless(self) -> None:
        confirmation_request = {
            "schema": "cbh.scoped_user_confirmation_request.v1",
            "action": "permanent_delete",
            "target": [r"C:\cbh-fixture\obsolete\payload.bin"],
            "scope": "single_event_single_scope_once",
            "impact": "irreversible_disk_deletion",
            "recovery": "external_backup_only",
            "non_targets": ["all_other_paths"],
            "persistence": "none",
            "required_disclosures": [
                "action",
                "target",
                "scope",
                "impact",
                "recovery",
                "non_targets",
            ],
        }
        route = {
            "routing_status": "classified",
            "execution_disposition": "pending_user_confirmation",
            "risk_level": "R5",
            "action_bindings": [
                {
                    "action": "await_scoped_user_confirmation",
                    "completion_evidence": "fresh_user_confirmation_bound_to_current_event",
                    "confirmation_request": confirmation_request,
                    "token": "must-not-survive",
                    "ttl": 300,
                }
            ],
        }
        before = sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        )
        first = build_action_consumption(
            route,
            prompt="永久删除指定文件",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        second = build_action_consumption(
            route,
            prompt="永久删除指定文件",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        after = sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        )

        action = next(
            item
            for item in first["actions"]
            if item["action_id"] == "await_scoped_user_confirmation"
        )
        self.assertEqual(action["status"], "pending_user_confirmation")
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["execution_disposition"], "pending_user_confirmation")
        self.assertEqual(first["pending_user_confirmation"], confirmation_request)
        self.assertEqual(second["pending_user_confirmation"], confirmation_request)
        self.assertEqual(
            first["unconsumed_action_ids"], ["await_scoped_user_confirmation"]
        )
        self.assertNotIn("token", first["pending_user_confirmation"])
        self.assertNotIn("ttl", first["pending_user_confirmation"])
        self.assertNotIn("expires_at", first["pending_user_confirmation"])
        self.assertNotIn("state_path", first["pending_user_confirmation"])
        self.assertIn("等待当前事件的一次性明确确认", first["additional_context"])
        self.assertEqual(before, after)

    def test_non_r5_route_has_no_pending_confirmation(self) -> None:
        receipt = build_action_consumption(
            {
                "routing_status": "classified",
                "execution_disposition": "advisory_route_ready",
                "risk_level": "R1",
                "action_bindings": [],
            },
            prompt="只读检查",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["execution_disposition"], "advisory_route_ready")
        self.assertIsNone(receipt["pending_user_confirmation"])

    def test_verified_control_facts_are_bound_to_real_outputs_atomically(self) -> None:
        checked_at = "2026-08-13T10:00:00+08:00"
        route = {
            "action_bindings": [
                {
                    "action": "bind_verified_facts",
                    "completion_evidence": "canonical_fact_binding_receipt",
                }
            ],
            "canonical_fact_packet": {
                "schema": "cbh.canonical_fact_packet.v1",
                "facts": [
                    {
                        "fact_id": "release_version_for_tool",
                        "channel": "control_fact",
                        "value": "v1.2.4",
                        "value_type": "string",
                        "status": "verified",
                        "source_refs": ["github:releases/latest"],
                        "verification_method": "primary_source_exact_match",
                        "valid_scope": "claim-boundary-harness latest release",
                        "conflict_status": "none",
                        "volatility": "revision_sensitive",
                        "checked_at": checked_at,
                        "freshness_window_seconds": 86400,
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "producer": {
                            "producer_id": "typed_tool_result",
                            "receipt_ref": "github:releases/latest#tag_name",
                            "value_sha256": "d0d0d05e9b4ff2b6ea5caf4f98735fb9305995a8d1b1a6187399099ab9911f11",
                        },
                        "consumer": {
                            "consumer_id": "typed_tool_argument_binder",
                            "consumption_phase": "pretool",
                            "effect": "typed_value_set",
                            "target": "/arguments/tag",
                        },
                    },
                    {
                        "fact_id": "release_version_for_answer",
                        "channel": "control_fact",
                        "value": "v1.2.4",
                        "value_type": "string",
                        "status": "verified",
                        "source_refs": ["github:releases/latest"],
                        "verification_method": "primary_source_exact_match",
                        "valid_scope": "claim-boundary-harness latest release",
                        "conflict_status": "none",
                        "volatility": "revision_sensitive",
                        "checked_at": checked_at,
                        "freshness_window_seconds": 86400,
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "producer": {
                            "producer_id": "typed_tool_result",
                            "receipt_ref": "github:releases/latest#tag_name",
                            "value_sha256": "d0d0d05e9b4ff2b6ea5caf4f98735fb9305995a8d1b1a6187399099ab9911f11",
                        },
                        "consumer": {
                            "consumer_id": "answer_slot_binder",
                            "consumption_phase": "final_assembly",
                            "effect": "literal_slot_replace",
                            "target": "release_version",
                        },
                    },
                ],
            },
            "fact_binding_request": {
                "schema": "cbh.fact_binding_request.v1",
                "observed_at": checked_at,
                "tool_call": {
                    "tool_name": "github_release_create",
                    "arguments": {"tag": {"$fact": "release_version_for_tool"}},
                },
                "answer_template": "Published {{fact:release_version_for_answer}}.",
            },
        }

        receipt = build_action_consumption(
            route,
            prompt="把已经核对的版本号写入发布调用和最终答复",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        self.assertIn("canonical_fact_binding", receipt)
        binding = receipt["canonical_fact_binding"]
        self.assertEqual("complete", binding["status"])
        self.assertEqual("v1.2.4", binding["bound_tool_call"]["arguments"]["tag"])
        self.assertEqual("Published v1.2.4.", binding["rendered_answer"])
        self.assertEqual(
            ["release_version_for_tool", "release_version_for_answer"],
            binding["bound_control_fact_ids"],
        )
        self.assertEqual([], binding["unconsumed_fact_ids"])
        self.assertNotIn("v1.2.4", receipt["additional_context"])
        action = next(item for item in receipt["actions"] if item["action_id"] == "bind_verified_facts")
        self.assertEqual("completed", action["status"])
        self.assertEqual("complete", receipt["status"])

    def test_unknown_control_fact_cannot_change_candidate_output(self) -> None:
        original_tool_call = {
            "tool_name": "shell_command",
            "arguments": {"workdir": {"$fact": "workspace_path"}},
        }
        route = {
            "action_bindings": [
                {
                    "action": "bind_verified_facts",
                    "completion_evidence": "canonical_fact_binding_receipt",
                }
            ],
            "canonical_fact_packet": {
                "schema": "cbh.canonical_fact_packet.v1",
                "facts": [
                    {
                        "fact_id": "workspace_path",
                        "channel": "control_fact",
                        "value": r"C:\wrong-or-unverified",
                        "value_type": "string",
                        "status": "unknown",
                        "source_refs": [],
                        "verification_method": "none",
                        "valid_scope": "current task",
                        "volatility": "stable",
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "consumer": {
                            "consumer_id": "typed_tool_argument_binder",
                            "consumption_phase": "pretool",
                            "effect": "typed_value_set",
                            "target": "/arguments/workdir",
                        },
                    }
                ],
            },
            "fact_binding_request": {
                "schema": "cbh.fact_binding_request.v1",
                "tool_call": original_tool_call,
            },
        }

        receipt = build_action_consumption(
            route,
            prompt="使用已经核对的工作目录",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        self.assertIn("canonical_fact_binding", receipt)
        binding = receipt["canonical_fact_binding"]
        self.assertEqual("incomplete", binding["status"])
        self.assertEqual(original_tool_call, binding["bound_tool_call"])
        self.assertEqual(["workspace_path"], binding["unconsumed_fact_ids"])
        self.assertIn("fact_not_verified:workspace_path", binding["errors"])
        self.assertEqual("incomplete", receipt["status"])

    def test_one_bad_control_target_rolls_back_every_control_binding(self) -> None:
        def verified_fact(fact_id: str, value: str, target: str) -> dict[str, object]:
            return {
                "fact_id": fact_id,
                "channel": "control_fact",
                "value": value,
                "value_type": "string",
                "status": "verified",
                "source_refs": [f"fixture:{fact_id}"],
                "verification_method": "fixture_exact_match",
                "valid_scope": "current test",
                "conflict_status": "none",
                "volatility": "stable",
                "on_conflict": "do_not_bind",
                "on_unknown": "do_not_bind",
                "audit_destination": "receipt",
                "producer": {
                    "producer_id": "mechanical_verifier",
                    "receipt_ref": f"fixture:{fact_id}",
                    "value_sha256": hashlib.sha256(
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                },
                "consumer": {
                    "consumer_id": "typed_tool_argument_binder",
                    "consumption_phase": "pretool",
                    "effect": "typed_value_set",
                    "target": target,
                },
            }

        original_tool_call = {
            "tool_name": "publish",
            "arguments": {
                "tag": {"$fact": "release_tag"},
                "title": {"$fact": "release_title"},
            },
        }
        route = {
            "action_bindings": [
                {
                    "action": "bind_verified_facts",
                    "completion_evidence": "canonical_fact_binding_receipt",
                }
            ],
            "canonical_fact_packet": {
                "schema": "cbh.canonical_fact_packet.v1",
                "facts": [
                    verified_fact("release_tag", "v1.2.4", "/arguments/tag"),
                    verified_fact("release_title", "CBH v1.2.4", "/arguments/missing"),
                ],
            },
            "fact_binding_request": {
                "schema": "cbh.fact_binding_request.v1",
                "tool_call": original_tool_call,
            },
        }

        receipt = build_action_consumption(
            route,
            prompt="发布已验证版本",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        binding = receipt["canonical_fact_binding"]
        self.assertEqual(original_tool_call, binding["bound_tool_call"])
        self.assertEqual([], binding["bound_control_fact_ids"])
        self.assertEqual(
            ["release_tag", "release_title"], binding["unconsumed_fact_ids"]
        )
        self.assertIn(
            "tool_binding_target_mismatch:release_title", binding["errors"]
        )

    def test_model_context_and_audit_facts_stay_out_of_control_binding(self) -> None:
        route = {
            "action_bindings": [
                {
                    "action": "bind_verified_facts",
                    "completion_evidence": "canonical_fact_binding_receipt",
                }
            ],
            "canonical_fact_packet": {
                "schema": "cbh.canonical_fact_packet.v1",
                "facts": [
                    {
                        "fact_id": "reasoning_constraint",
                        "channel": "model_context",
                        "value": "The source disagrees with the cached summary.",
                        "value_type": "string",
                        "status": "verified",
                        "source_refs": ["evidence:primary"],
                        "verification_method": "source_comparison",
                        "valid_scope": "current reasoning step",
                        "conflict_status": "none",
                        "volatility": "stable",
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "producer": {
                            "producer_id": "deterministic_parser",
                            "receipt_ref": "evidence:primary#comparison",
                            "value_sha256": "87c96bd156be5805a8965d52e76297530629de87b2f81d4fb427509b06e72c34",
                        },
                        "consumer": {
                            "consumer_id": "host_model_agent",
                            "consumption_phase": "reasoning",
                            "effect": "minimal_context",
                            "target": "additional_context",
                        },
                    },
                    {
                        "fact_id": "source_digest",
                        "channel": "audit_only",
                        "value": "sha256:abc123",
                        "value_type": "string",
                        "status": "verified",
                        "source_refs": ["evidence:primary"],
                        "verification_method": "sha256",
                        "valid_scope": "receipt audit",
                        "conflict_status": "none",
                        "volatility": "stable",
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "producer": {
                            "producer_id": "mechanical_verifier",
                            "receipt_ref": "evidence:primary#sha256",
                            "value_sha256": "c34c680e5f9f529a83ef9628aa89923d5efb89c4ac52275aa22c5edc3345ab89",
                        },
                        "consumer": {
                            "consumer_id": "receipt_audit",
                            "consumption_phase": "postconsume",
                            "effect": "audit_only",
                            "target": "canonical_fact_binding.audit_records",
                        },
                    },
                ],
            },
            "fact_binding_request": {
                "schema": "cbh.fact_binding_request.v1",
                "answer_template": "No mechanical slots in this answer.",
            },
        }

        receipt = build_action_consumption(
            route,
            prompt="解释冲突",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        self.assertIn("canonical_fact_binding", receipt)
        binding = receipt["canonical_fact_binding"]
        self.assertEqual("complete", binding["runtime_field_audit"]["invariant_status"])
        self.assertEqual(["reasoning_constraint"], binding["model_context_fact_ids"])
        self.assertEqual(["source_digest"], binding["audit_only_fact_ids"])
        self.assertIn("The source disagrees with the cached summary.", receipt["additional_context"])
        self.assertNotIn("sha256:abc123", receipt["additional_context"])
        self.assertEqual("No mechanical slots in this answer.", binding["rendered_answer"])

    def test_verified_label_without_mechanical_producer_is_not_consumable(self) -> None:
        route = {
            "action_bindings": [
                {
                    "action": "bind_verified_facts",
                    "completion_evidence": "canonical_fact_binding_receipt",
                }
            ],
            "canonical_fact_packet": {
                "schema": "cbh.canonical_fact_packet.v1",
                "facts": [
                    {
                        "fact_id": "model_asserted_version",
                        "channel": "control_fact",
                        "value": "v9.9.9",
                        "value_type": "string",
                        "status": "verified",
                        "source_refs": ["model:self-assertion"],
                        "verification_method": "model_assertion",
                        "valid_scope": "current answer",
                        "conflict_status": "none",
                        "volatility": "stable",
                        "on_conflict": "do_not_bind",
                        "on_unknown": "do_not_bind",
                        "audit_destination": "receipt",
                        "consumer": {
                            "consumer_id": "answer_slot_binder",
                            "consumption_phase": "final_assembly",
                            "effect": "literal_slot_replace",
                            "target": "release_version",
                        },
                    }
                ],
            },
            "fact_binding_request": {
                "schema": "cbh.fact_binding_request.v1",
                "answer_template": "Version {{fact:model_asserted_version}}.",
            },
        }

        receipt = build_action_consumption(
            route,
            prompt="不要把模型自己的标签当成机械事实",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        binding = receipt["canonical_fact_binding"]
        self.assertEqual("incomplete", binding["status"])
        self.assertEqual(
            "Version {{fact:model_asserted_version}}.", binding["rendered_answer"]
        )
        self.assertIn("missing_producer:model_asserted_version", binding["errors"])

    def test_external_only_route_plans_exact_fallback_without_memory_injection(self) -> None:
        slug = "qimen039-code/claim-boundary-harness"
        receipt = build_action_consumption(
            {
                "action_bindings": [
                    {
                        "action": "perform_external_research_route",
                        "completion_evidence": "source_ledger_or_citations",
                    }
                ],
                "external_need": [
                    "general_web_cross_check",
                    "github_open_source_repository_search",
                ],
                "memory_need": "none",
                "correction_lifecycle_profile": "none",
            },
            prompt=f"search GitHub repository {slug}",
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        self.assertEqual([], receipt["selected_records"])
        external = receipt["external_retrieval_receipt"]
        self.assertEqual("exact_anchor_first", external["retrieval_profile"])
        self.assertEqual(
            [slug],
            [
                item["raw_text"]
                for item in external["exact_anchors"]
                if item["type"] == "owner_repo_slug"
            ],
        )
        self.assertTrue(
            any(item["query_text"] == f"repo:{slug}" for item in external["query_plan"])
        )
        self.assertEqual(
            ["perform_external_research_route"], receipt["unconsumed_action_ids"]
        )

    def test_ordinary_route_does_not_open_missing_memory_payloads(self) -> None:
        missing_meta = self.root / "missing" / "_META_INDEX.md"
        receipt = build_action_consumption(
            {"action_bindings": []},
            prompt="Explain what a hash is",
            meta_path=missing_meta,
            capability_contract_path=self.contract,
        )
        self.assertEqual(receipt["retrieval"]["coverage_status"], "not_requested")
        self.assertEqual(receipt["selected_records"], [])
        self.assertEqual(receipt["prediction_reviews"], [])
        self.assertEqual(receipt["additional_context"], "")

    def test_engineering_profile_and_task_continuity_are_consumed(self) -> None:
        route = {
            "engineering_execution_profiles": ["tracer_bullet_plan"],
            "task_continuity_decision": {
                "schema": "cbh.task_continuity_decision.v1",
                "decision": "arm",
                "reasons": ["tool_required"],
                "source_event_ids": [],
                "host_delivery": "ready",
            },
            "action_bindings": [
                {"action": "prepare_task_continuity_capsule"},
                {"action": "apply_engineering_execution_profile"},
            ],
            "memory_need": "none",
        }
        task_event = {
            "schema": "cbh.task_event.v1",
            "event_id": "consumer-engineering-1",
            "type": "task_observed",
            "objective": "按 tracer-bullet 完成迁移",
            "tool_required": True,
            "plan_steps": [
                {"id": "expand", "status": "completed", "slice_kind": "expand"},
                {"id": "migrate", "status": "pending", "slice_kind": "migrate"},
            ],
        }

        receipt = build_action_consumption(
            route,
            prompt="按 tracer-bullet 完成迁移",
            task_event=task_event,
            meta_path=self.meta,
            capability_contract_path=self.contract,
        )

        self.assertEqual("ARMED", receipt["task_capsule"]["lifecycle"])
        self.assertEqual(
            "MIGRATE",
            receipt["engineering_execution_receipt"]["results"]
            ["tracer_bullet_plan"]["migration_phase"],
        )
        self.assertLessEqual(
            len(receipt["additional_context"]),
            receipt["context_soft_target_chars"],
        )
        self.assertFalse(receipt["engineering_execution_receipt"]["authority_granted"])
        self.assertIn("CBH task-continuity control", receipt["additional_context"])
        self.assertTrue(receipt["continuity_control_in_first_page"])
        receipt_with_transition = dict(receipt)
        receipt_with_transition["task_transition"] = {
            "schema": "cbh.task_transition.v1",
            "changed": True,
            "lifecycle": "ARMED",
            "capsule": receipt["task_capsule"],
        }
        compact = _compact_runtime_receipt(receipt_with_transition)
        self.assertNotIn("capsule", compact["task_transition"])
        self.assertLess(len(compact["task_capsule"]), len(receipt["task_capsule"]))

    def test_large_context_keeps_task_continuity_control_on_first_page(self) -> None:
        route = {
            "task_continuity_decision": {
                "schema": "cbh.task_continuity_decision.v1",
                "decision": "arm",
                "reasons": ["tool_required"],
                "source_event_ids": [],
                "host_delivery": "ready",
            },
            "action_bindings": [{"action": "prepare_task_continuity_capsule"}],
            "memory_need": "none",
        }
        task_event = {
            "schema": "cbh.task_event.v1",
            "event_id": "consumer-priority-1",
            "type": "task_observed",
            "objective": "修改并验证文件",
            "tool_required": True,
        }
        with patch("harness_action_consumer._context_parts", return_value=["x" * 20_000]):
            receipt = build_action_consumption(
                route,
                prompt="修改并验证文件",
                task_event=task_event,
                meta_path=self.meta,
                capability_contract_path=self.contract,
            )

        self.assertEqual("continuation_required", receipt["transport_receipt"]["delivery"])
        self.assertIn("CBH task-continuity capsule", receipt["additional_context"])
        self.assertIn("CBH task-continuity control", receipt["additional_context"])
        self.assertTrue(receipt["continuity_control_in_first_page"])


if __name__ == "__main__":
    unittest.main()
