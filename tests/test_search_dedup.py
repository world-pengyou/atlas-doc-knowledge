"""Exercise retrieval deduplication through the public service, without models or SQL."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from kb.database import Database
from kb.service import KnowledgeService


class SearchDedupTests(unittest.TestCase):
    def setUp(self):
        self.document_id = uuid4()
        self.settings = SimpleNamespace(
            public_base_url="http://127.0.0.1:8100",
            search_similarity_threshold=0.4,
            search_top_k=2,
        )
        self.database = Mock(spec=Database)
        self.service = KnowledgeService(self.settings, self.database)
        self.service.models = Mock()
        self.service.models.embed.return_value = [[0.25, 0.75]]
        tokenizer = patch("kb.service.query_tokens", return_value="'审批'")
        tokenizer.start()
        self.addCleanup(tokenizer.stop)

    def row(self, identifier, text, *, ordinal=None, document_id=None,
            score=0.03, similarity=0.8, kind="text", asset=None, location=None):
        return {
            "id": identifier,
            "ordinal": identifier if ordinal is None else ordinal,
            "document_id": document_id or self.document_id,
            "filename": "审批流程.pdf",
            "text": text,
            "kind": kind,
            "location": location or {
                "page": 2, "bbox": [20, 30, 150, 180],
                "preview": "attempt/page-2.png",
                "char_start": 0, "char_end": len(text),
            },
            "asset_name": asset,
            "similarity": similarity,
            "score": score,
            "channels": ["semantic", "lexical"],
        }

    def pair(self, *, overlap="主管驳回后由申请人修改", markdown=False):
        left = "提交申请。" + overlap
        right = overlap + "，修改后再次提交审核。"
        start = len(left) - len(overlap)
        first = self.row(10, left, score=0.03, similarity=0.91)
        second = self.row(11, right, score=0.04, similarity=0.77)
        second["location"].update(char_start=start, char_end=start + len(right))
        if markdown:
            first["filename"] = second["filename"] = "审批流程.md"
            common = {"heading": "报销 / 审批", "language": "text"}
            first["location"] = {**common, "line_start": 10, "line_end": 12,
                                 "char_start": 0, "char_end": len(left)}
            second["location"] = {**common, "line_start": 12, "line_end": 14,
                                  "char_start": start, "char_end": start + len(right)}
        return first, second, left + right[len(overlap):]

    def search_rows(self, rows, limit=None):
        self.database.search.return_value = rows
        return self.service.search("主管驳回后由谁修改", limit)

    def test_exact_duplicates_do_not_consume_top_k(self):
        rows = [self.row(1, "审批要求", score=0.09),
                self.row(2, "审批要求", score=0.08),
                self.row(3, "付款要求", score=0.07),
                self.row(4, "归档要求", score=0.06)]

        result = self.search_rows(rows)

        self.assertEqual([hit["text"] for hit in result["results"]], ["审批要求", "付款要求"])
        self.assertEqual([hit["score"] for hit in result["results"]], [0.09, 0.07])
        self.assertEqual(result["results"][0]["merged_chunk_count"], 2)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(result["limit"], 2)
        self.assertEqual(result["candidate_limit"], 20)

    def test_exact_normalization_retains_every_document_and_image_source(self):
        first = self.row(1, " \r\n主管驳回后\r\n申请人修改。 \r\n", score=0.09)
        second = self.row(2, "主管驳回后\n申请人修改。", document_id=uuid4(),
                          kind="image", asset="attempt/流程图.png", score=0.08,
                          similarity=0.95)
        second["location"].update(page=5, bbox=[40, 50, 250, 280], preview="attempt/page-5.png")

        hit = self.search_rows([first, second])["results"][0]

        self.assertEqual(hit["merged_chunk_count"], 2)
        self.assertEqual(hit["score"], first["score"])
        self.assertEqual(hit["similarity"], first["similarity"])
        self.assertEqual(hit["document_id"], str(first["document_id"]))
        self.assertEqual(len(hit["sources"]), 2)
        sources = {str(source["chunk_id"]): source for source in hit["sources"]}
        for row in (first, second):
            source = sources[str(row["id"])]
            self.assertNotIn("text", source)
            self.assertEqual(source["document_id"], str(row["document_id"]))
            self.assertEqual(source["ordinal"], row["ordinal"])
            self.assertEqual(source["filename"], row["filename"])
            self.assertEqual(source["kind"], row["kind"])
            self.assertEqual(source["location"]["bbox"], row["location"]["bbox"])
            self.assertEqual(source["location"]["page"], row["location"]["page"])
            self.assertEqual(source["score"], row["score"])
            self.assertEqual(source["similarity"], row["similarity"])
            self.assertEqual(source["channels"], row["channels"])
            self.assertTrue(source["source_url"].endswith(f"/source#page={row['location']['page']}"))
            self.assertTrue(source["location"]["preview_url"].endswith(row["location"]["preview"]))
        self.assertIn("/assets/attempt/", sources["2"]["asset_url"])
        self.assertIn("%E6%B5%81%E7%A8%8B%E5%9B%BE.png", sources["2"]["asset_url"])

    def test_only_line_endings_and_outer_whitespace_are_normalized(self):
        texts = ["金额不得超过10万元。", "金额不得超过100万元。",
                 "金额得超过10万元。", "Amount A", "amount A", "Amount  A",
                 "金额不得超过10万元！"]
        rows = [self.row(i, text, ordinal=i * 2) for i, text in enumerate(texts, 1)]

        result = self.search_rows(rows, limit=10)

        self.assertEqual([hit["text"] for hit in result["results"]], texts)
        self.assertEqual(result["deduplicated_count"], 0)
        self.assertTrue(all(hit["merged_chunk_count"] == 1 for hit in result["results"]))

    def test_overlapping_pdf_neighbors_merge_in_source_order_keep_best_rank(self):
        first, second, expected = self.pair()
        original = deepcopy([second, first])

        result = self.search_rows([second, first])

        self.assertEqual(len(result["results"]), 1)
        hit = result["results"][0]
        self.assertEqual(hit["text"], expected)
        self.assertEqual(hit["merged_chunk_count"], 2)
        self.assertEqual(hit["score"], second["score"])
        self.assertEqual(hit["similarity"], second["similarity"])
        self.assertEqual(hit["location"]["char_start"], second["location"]["char_start"])
        self.assertEqual({str(source["chunk_id"]) for source in hit["sources"]}, {"10", "11"})
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual([second, first], original, "search must not mutate database candidate rows")

    def test_markdown_overlap_and_touching_line_ranges_keep_each_line_citation(self):
        for next_start in (12, 13):
            with self.subTest(line_start=next_start):
                first, second, expected = self.pair(markdown=True)
                second["location"]["line_start"] = next_start

                hit = self.search_rows([second, first])["results"][0]

                self.assertEqual(hit["text"], expected)
                self.assertEqual(hit["merged_chunk_count"], 2)
                self.assertEqual({source["location"]["line_start"] for source in hit["sources"]},
                                 {10, next_start})
                self.assertTrue(all(source["location"]["heading"] == "报销 / 审批"
                                    for source in hit["sources"]))

    def test_duplicate_group_and_three_neighbor_chain_merge_before_top_k(self):
        first, second, joined = self.pair()
        overlap = "修改后再次提交审核。"
        third_text = overlap + "审批通过后归档。"
        third = self.row(12, third_text, score=0.05)
        start = second["location"]["char_end"] - len(overlap)
        third["location"].update(char_start=start, char_end=start + len(third_text))
        duplicate = self.row(30, first["text"], document_id=uuid4(), score=0.02)
        distinct = self.row(40, "财务核对后付款。", score=0.01)

        result = self.search_rows([third, second, first, duplicate, distinct])

        self.assertEqual(len(result["results"]), 2)
        hit = result["results"][0]
        self.assertEqual(hit["text"], joined + third_text[len(overlap):])
        self.assertEqual(hit["merged_chunk_count"], 4)
        self.assertEqual(len(hit["sources"]), 4)
        self.assertEqual(hit["score"], third["score"])
        self.assertEqual(result["results"][1]["text"], distinct["text"])
        self.assertEqual(result["deduplicated_count"], 3)

    def test_neighbors_need_all_source_and_offset_evidence(self):
        changes = {
            "other_document": lambda a, b: b.update(document_id=uuid4()),
            "ordinal_gap": lambda a, b: b.update(ordinal=12),
            "other_page": lambda a, b: b["location"].update(page=3),
            "other_bbox": lambda a, b: b["location"].update(bbox=[20, 190, 150, 300]),
            "missing_bbox": lambda a, b: b["location"].pop("bbox"),
            "missing_ordinal": lambda a, b: b.pop("ordinal"),
            "missing_offsets": lambda a, b: b["location"].pop("char_start"),
            "equal_start": lambda a, b: b["location"].update(char_start=0),
            "non_increasing_end": lambda a, b: b["location"].update(char_end=a["location"]["char_end"]),
            "no_offset_overlap": lambda a, b: b["location"].update(char_start=a["location"]["char_end"]),
            "different_kind": lambda a, b: b.update(kind="image", asset_name="attempt/figure.png"),
        }
        for label, change in changes.items():
            with self.subTest(evidence=label):
                first, second, _ = self.pair()
                change(first, second)

                result = self.search_rows([second, first])

                self.assertEqual(len(result["results"]), 2)
                self.assertEqual(result["deduplicated_count"], 0)

    def test_adjacent_different_content_is_not_discarded(self):
        first, second, _ = self.pair()
        second["text"] = "主管批准后由财务核对付款信息。"

        result = self.search_rows([second, first])

        self.assertEqual([hit["text"] for hit in result["results"]], [second["text"], first["text"]])
        self.assertEqual(result["deduplicated_count"], 0)

    def test_markdown_heading_language_and_line_gap_prevent_merging(self):
        for field, value in (("heading", "报销 / 付款"), ("language", "python"), ("line_start", 14)):
            with self.subTest(field=field):
                first, second, _ = self.pair(markdown=True)
                second["location"][field] = value

                result = self.search_rows([second, first])

                self.assertEqual(len(result["results"]), 2)

    def test_image_neighbors_require_same_nonempty_original_asset(self):
        for assets, expected_count in ((("attempt/figure.png", "attempt/figure.png"), 1),
                                       (("attempt/figure.png", "attempt/other.png"), 2),
                                       ((None, None), 2)):
            with self.subTest(assets=assets):
                first, second, expected = self.pair()
                for row, asset in zip((first, second), assets):
                    row.update(kind="image", asset_name=asset)

                result = self.search_rows([second, first])

                self.assertEqual(len(result["results"]), expected_count)
                if expected_count == 1:
                    self.assertEqual(result["results"][0]["text"], expected)
                    self.assertTrue(all(source["asset_url"].endswith("attempt/figure.png")
                                        for source in result["results"][0]["sources"]))

    def test_minimum_exact_overlap_is_eight_characters(self):
        for overlap, expected_count in (("甲乙丙丁戊己庚辛", 1), ("甲乙丙丁戊己庚", 2)):
            with self.subTest(overlap_length=len(overlap)):
                first, second, expected = self.pair(overlap=overlap)

                result = self.search_rows([second, first])

                self.assertEqual(len(result["results"]), expected_count)
                if expected_count == 1:
                    self.assertEqual(result["results"][0]["text"], expected)

    def test_short_phrase_cannot_override_inconsistent_overlap_coordinates(self):
        phrase = "甲乙丙丁戊己庚辛"
        first = self.row(1, "开头正文" + "前" * 72 + phrase)
        right_text = phrase + "后" * 90
        second = self.row(2, right_text)
        second["location"].update(char_start=4, char_end=4 + len(right_text))

        result = self.search_rows([first, second])

        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["deduplicated_count"], 0)

    def test_overlap_allows_only_offset_difference_explained_by_stripped_edges(self):
        first, second, expected = self.pair(overlap="甲乙丙丁戊己庚辛")
        # Two stripped characters on each slice explain 12 raw vs 8 stored.
        first["location"]["char_end"] += 2
        second["location"]["char_start"] -= 2

        hit, = self.search_rows([second, first])["results"]

        self.assertEqual(hit["text"], expected)
        self.assertEqual(hit["merged_chunk_count"], 2)

    def test_merged_text_length_limit_is_inclusive(self):
        overlap = "甲乙丙丁戊己庚辛壬癸"
        left = "前" * 790 + overlap
        for length, expected_count in ((1400, 1), (1401, 2)):
            with self.subTest(merged_length=length):
                right = overlap + "后" * (length - len(left))
                first = self.row(1, left)
                second = self.row(2, right)
                second["location"].update(char_start=790, char_end=length)

                result = self.search_rows([first, second])

                self.assertEqual(len(result["results"]), expected_count)
                if expected_count == 1:
                    self.assertEqual(result["results"][0]["text"], "前" * 790 + overlap + "后" * 600)

    def test_similarity_filter_precedes_deduplication(self):
        below = self.row(1, "相同正文", score=0.09, similarity=0.39)
        boundary = self.row(2, "相同正文", score=0.08, similarity=0.4)
        distinct = self.row(3, "不同正文", score=0.07)

        result = self.search_rows([below, boundary, distinct])

        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["score"], 0.08)
        self.assertEqual(result["results"][0]["similarity"], 0.4)
        self.assertEqual(result["results"][0]["merged_chunk_count"], 1)
        self.assertEqual(result["deduplicated_count"], 0)

    def test_expands_recall_after_duplicates_then_takes_top_k_without_reembedding(self):
        duplicates = [self.row(i, "审批要求", ordinal=i * 2, score=0.1 - i / 1000)
                      for i in range(1, 21)]
        distinct = self.row(21, "付款要求", score=0.01)
        self.database.search.side_effect = [duplicates, duplicates + [distinct]]

        result = self.service.search("主管驳回后由谁修改")

        self.assertEqual([hit["text"] for hit in result["results"]], ["审批要求", "付款要求"])
        self.assertEqual(result["results"][0]["merged_chunk_count"], 20)
        self.assertEqual(result["deduplicated_count"], 19)
        self.assertEqual(result["candidate_limit"], 60)
        self.assertEqual([call.args[2] for call in self.database.search.call_args_list], [20, 60])
        self.service.models.embed.assert_called_once_with(["主管驳回后由谁修改"])

    def test_recall_expansion_is_bounded_at_two_hundred(self):
        def candidates(vector, tokens, count):
            return [self.row(i, "重复审批要求", ordinal=i * 2) for i in range(1, count + 1)]

        self.database.search.side_effect = candidates

        result = self.service.search("主管驳回后由谁修改")

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["merged_chunk_count"], 200)
        self.assertEqual(result["deduplicated_count"], 199)
        self.assertEqual(result["candidate_limit"], 200)
        self.assertEqual([call.args[2] for call in self.database.search.call_args_list], [20, 60, 180, 200])
        self.service.models.embed.assert_called_once()

    def test_full_candidate_page_with_enough_unique_groups_needs_no_expansion(self):
        rows = [self.row(i, f"审批步骤{i}", ordinal=i * 2) for i in range(1, 21)]

        result = self.search_rows(rows, limit=1)

        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["candidate_limit"], 20)
        self.assertEqual(result["deduplicated_count"], 0)
        self.database.search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
