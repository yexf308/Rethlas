from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path


try:
    import requests  # noqa: F401
except ModuleNotFoundError:  # Keep these memory-only tests dependency-free.
    sys.modules["requests"] = types.SimpleNamespace(post=None)

from agents.generation.mcp import server


LARGE_SEARCH_BUDGET = 1_000_000


class MemoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._original_memory_root = server.MEMORY_ROOT
        server.MEMORY_ROOT = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        server.MEMORY_ROOT = self._original_memory_root
        self._temporary_directory.cleanup()

    def test_memory_append_returns_compact_receipt_by_default(self) -> None:
        receipt = server.memory_append(
            "sample/problem",
            "proof_steps",
            {"claim": "the full record should not be echoed"},
        )

        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["problem_id"], "sample/problem")
        self.assertEqual(receipt["channel"], "proof_steps")
        self.assertTrue(receipt["record_id"].startswith("mem_"))
        self.assertTrue(receipt["active"])
        self.assertEqual(receipt["supersedes"], [])
        self.assertNotIn("entry", receipt)
        self.assertEqual(
            receipt["path"],
            str(server._channel_path("sample/problem", "proof_steps")),
        )

        stored = list(
            server._iter_jsonl(
                server._channel_path("sample/problem", "proof_steps")
            )
        )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["record_id"], receipt["record_id"])
        self.assertEqual(
            stored[0]["record"], {"claim": "the full record should not be echoed"}
        )

        event = list(
            server._iter_jsonl(server._channel_path("sample/problem", "events"))
        )[0]
        self.assertEqual(event["appended_record_id"], receipt["record_id"])

    def test_memory_append_full_mode_preserves_legacy_response_payload(self) -> None:
        receipt = server.memory_append(
            "sample",
            "toy_examples",
            {"example": "expanded response"},
            return_mode="full",
        )

        self.assertIn("path", receipt)
        self.assertIn("entry", receipt)
        self.assertEqual(receipt["entry"]["record_id"], receipt["record_id"])
        self.assertEqual(receipt["entry"]["record"], {"example": "expanded response"})

    def test_active_and_supersedes_filter_stale_records(self) -> None:
        first = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle first"},
        )
        replacement = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle replacement"},
            supersedes=[first["record_id"]],
        )
        inactive = server.memory_append(
            "sample",
            "proof_steps",
            {"text": "sharedneedle inactive"},
            active=False,
        )

        current = server.memory_search(
            "sample",
            "sharedneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        current_results = current["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(current["corpus_count"], 1)
        self.assertEqual(
            [result["item"]["record_id"] for result in current_results],
            [replacement["record_id"]],
        )

        history = server.memory_search(
            "sample",
            "sharedneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
            include_inactive=True,
        )
        history_results = history["results_by_channel"]["proof_steps"]["results"]
        self.assertEqual(history["corpus_count"], 3)
        active_by_id = {
            result["item"]["record_id"]: result["item"]["active"]
            for result in history_results
        }
        self.assertEqual(
            active_by_id,
            {
                first["record_id"]: False,
                replacement["record_id"]: True,
                inactive["record_id"]: False,
            },
        )
        stale_item = next(
            result["item"]
            for result in history_results
            if result["item"]["record_id"] == first["record_id"]
        )
        self.assertEqual(stale_item["superseded_by"], [replacement["record_id"]])

    def test_legacy_records_get_stable_ids_and_can_be_superseded(self) -> None:
        server.memory_init("legacy")
        server._append_jsonl(
            server._channel_path("legacy", "counterexamples"),
            {
                "timestamp_utc": "2025-01-01T00:00:00+00:00",
                "channel": "counterexamples",
                "record": {"text": "legacyneedle counterexample"},
            },
        )

        first_search = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        legacy_item = first_search["results_by_channel"]["counterexamples"][
            "results"
        ][0]["item"]
        legacy_id = legacy_item["record_id"]
        self.assertTrue(legacy_id.startswith("legacy_"))
        self.assertTrue(legacy_item["active"])
        self.assertEqual(legacy_item["supersedes"], [])

        repeated_search = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(
            repeated_search["results_by_channel"]["counterexamples"]["results"][0][
                "item"
            ]["record_id"],
            legacy_id,
        )

        replacement = server.memory_append(
            "legacy",
            "counterexamples",
            {"text": "legacyneedle corrected counterexample"},
            supersedes=[legacy_id],
        )
        current = server.memory_search(
            "legacy",
            "legacyneedle",
            channels=["counterexamples"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(
            current["results_by_channel"]["counterexamples"]["results"][0]["item"][
                "record_id"
            ],
            replacement["record_id"],
        )
        self.assertEqual(current["corpus_count"], 1)

    def test_search_ranks_by_relevance_then_breaks_ties_by_recency(self) -> None:
        server.memory_init("relevance-ordering")
        channel_path = server._channel_path(
            "relevance-ordering", "immediate_conclusions"
        )
        server._append_jsonl(
            channel_path,
            {
                "record_id": "highly-relevant-old",
                "timestamp_utc": "2025-01-01T00:00:00+00:00",
                "channel": "immediate_conclusions",
                "active": True,
                "supersedes": [],
                "record": {"text": "alpha beta gamma delta"},
            },
        )
        server._append_jsonl(
            channel_path,
            {
                "record_id": "weakly-relevant-new",
                "timestamp_utc": "2026-01-01T00:00:00+00:00",
                "channel": "immediate_conclusions",
                "active": True,
                "supersedes": [],
                "record": {"text": "alpha"},
            },
        )

        response = server.memory_search(
            "relevance-ordering",
            "alpha beta gamma delta",
            channels=["immediate_conclusions"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        relevance_results = response["results_by_channel"]["immediate_conclusions"][
            "results"
        ]
        ids = [result["item"]["record_id"] for result in relevance_results]
        self.assertEqual(ids, ["highly-relevant-old", "weakly-relevant-new"])
        self.assertGreater(relevance_results[0]["score"], relevance_results[1]["score"])

        server.memory_init("tie-ordering")
        tie_path = server._channel_path("tie-ordering", "immediate_conclusions")
        for record_id, timestamp in (
            ("equal-old", "2025-01-01T00:00:00+00:00"),
            ("equal-new", "2026-01-01T00:00:00+00:00"),
        ):
            server._append_jsonl(
                tie_path,
                {
                    "record_id": record_id,
                    "timestamp_utc": timestamp,
                    "channel": "immediate_conclusions",
                    "active": True,
                    "supersedes": [],
                    "record": {"text": "tieprobe identical"},
                },
            )

        tied = server.memory_search(
            "tie-ordering",
            "tieprobe",
            channels=["immediate_conclusions"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        tied_results = tied["results_by_channel"]["immediate_conclusions"]["results"]
        tied_ids = [result["item"]["record_id"] for result in tied_results]
        self.assertEqual(tied_ids, ["equal-new", "equal-old"])
        self.assertAlmostEqual(tied_results[0]["score"], tied_results[1]["score"])

    def test_search_budget_returns_only_whole_records_and_reports_omissions(self) -> None:
        for index in range(3):
            server.memory_append(
                "budget",
                "proof_steps",
                {"text": f"budgetneedle record {index}", "payload": "x" * 50},
            )
        server.memory_append(
            "budget",
            "proof_steps",
            {"text": "unrelated corpus entry"},
        )

        unbounded = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=LARGE_SEARCH_BUDGET,
        )
        unbounded_channel = unbounded["results_by_channel"]["proof_steps"]
        self.assertEqual(unbounded["corpus_count"], 4)
        self.assertEqual(unbounded["matched_count"], 3)
        self.assertTrue(unbounded["complete"])
        first_result = unbounded_channel["results"][0]
        first_chars = server._compact_json_chars(first_result)

        bounded = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=first_chars,
        )
        bounded_channel = bounded["results_by_channel"]["proof_steps"]
        self.assertEqual(bounded["count"], 1)
        self.assertEqual(bounded["returned_chars"], first_chars)
        self.assertFalse(bounded["complete"])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["omitted_count"], 2)
        self.assertEqual(len(bounded["omitted_ids"]), 2)
        self.assertEqual(bounded_channel["results"][0], first_result)
        self.assertEqual(bounded_channel["returned_chars"], first_chars)
        self.assertFalse(bounded_channel["complete"])

        no_room = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            max_chars=1,
        )
        self.assertEqual(no_room["count"], 0)
        self.assertEqual(no_room["returned_chars"], 0)
        self.assertEqual(len(no_room["omitted_ids"]), 3)

        limited = server.memory_search(
            "budget",
            "budgetneedle",
            channels=["proof_steps"],
            limit_per_channel=1,
            max_chars=LARGE_SEARCH_BUDGET,
        )
        self.assertEqual(limited["count"], 1)
        self.assertTrue(limited["truncated"])
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["omitted_count"], 2)
        self.assertEqual(len(limited["omitted_ids"]), 2)

    def test_memory_options_are_validated_before_writes(self) -> None:
        with self.assertRaisesRegex(ValueError, "return_mode"):
            server.memory_append(
                "invalid",
                "proof_steps",
                {"text": "not written"},
                return_mode="everything",
            )
        self.assertFalse((server.MEMORY_ROOT / "invalid").exists())

        with self.assertRaisesRegex(ValueError, "supersedes"):
            server.memory_append(
                "invalid",
                "proof_steps",
                {"text": "not written"},
                supersedes=[""],
            )
        with self.assertRaisesRegex(ValueError, "max_chars"):
            server.memory_search("invalid", "query", max_chars=-1)

    def test_omitted_id_metadata_is_bounded(self) -> None:
        for index in range(6):
            server.memory_append(
                "many-omissions",
                "proof_steps",
                {"text": f"needle omitted record {index}"},
            )
        original_limit = server.MAX_OMITTED_IDS
        server.MAX_OMITTED_IDS = 2
        try:
            response = server.memory_search(
                "many-omissions",
                "needle",
                channels=["proof_steps"],
                max_chars=1,
            )
        finally:
            server.MAX_OMITTED_IDS = original_limit

        channel = response["results_by_channel"]["proof_steps"]
        self.assertEqual(response["omitted_count"], 6)
        self.assertEqual(len(response["omitted_ids"]), 2)
        self.assertFalse(response["omitted_ids_complete"])
        self.assertEqual(len(channel["omitted_ids"]), 2)
        self.assertFalse(channel["omitted_ids_complete"])

    def test_omitted_id_cap_boundaries_are_explicit(self) -> None:
        original_limit = server.MAX_OMITTED_IDS
        server.MAX_OMITTED_IDS = 2
        try:
            for omitted_count in (1, 2, 3):
                problem_id = f"omission-boundary-{omitted_count}"
                for index in range(omitted_count):
                    server.memory_append(
                        problem_id,
                        "proof_steps",
                        {"text": f"boundaryneedle record {index}"},
                    )
                response = server.memory_search(
                    problem_id,
                    "boundaryneedle",
                    channels=["proof_steps"],
                    max_chars=1,
                )
                channel = response["results_by_channel"]["proof_steps"]
                expected_listed = min(omitted_count, 2)
                self.assertEqual(response["omitted_count"], omitted_count)
                self.assertEqual(len(response["omitted_ids"]), expected_listed)
                self.assertEqual(
                    response["omitted_ids_complete"], omitted_count <= 2
                )
                self.assertEqual(channel["omitted_count"], omitted_count)
                self.assertEqual(len(channel["omitted_ids"]), expected_listed)
                self.assertEqual(
                    channel["omitted_ids_complete"], omitted_count <= 2
                )
        finally:
            server.MAX_OMITTED_IDS = original_limit


if __name__ == "__main__":
    unittest.main()
