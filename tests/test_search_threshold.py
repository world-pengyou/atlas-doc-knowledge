"""Check post-fusion cosine filtering independently of embedding/model services."""
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from kb.database import Database
from kb.service import KnowledgeService


def search_row(identifier, similarity, score, *, image=False):
    attempt = str(uuid4())
    return {
        "id": identifier,
        "document_id": uuid4(),
        "filename": f"manual-{identifier}.pdf",
        "text": f"切片 {identifier}",
        "kind": "image" if image else "text",
        "location": {"page": 2, "bbox": [20, 30, 150, 180],
                     "preview": attempt + "/page-2.png"},
        "asset_name": attempt + "/figure.png" if image else None,
        "similarity": similarity,
        "score": score,
        "channels": ["semantic", "lexical"],
    }


class SearchThresholdTests(unittest.TestCase):
    def setUp(self):
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

    def test_filters_cosine_after_fusion_before_taking_default_count(self):
        rows = [search_row(1, 0.2, 0.09), search_row(2, 0.4, 0.05),
                search_row(3, 0.9, 0.04), search_row(4, 0.8, 0.03)]
        self.database.search.return_value = rows

        result = self.service.search("主管驳回")

        self.assertEqual([row["text"] for row in result["results"]], ["切片 2", "切片 3"])
        self.assertEqual([row["score"] for row in result["results"]], [0.05, 0.04])
        self.assertEqual([row["similarity"] for row in result["results"]], [0.4, 0.9])
        self.assertEqual(result["similarity_threshold"], 0.4)
        self.assertEqual(result["limit"], 2)

    def test_explicit_limit_overrides_configured_default(self):
        self.database.search.return_value = [search_row(i, 0.8, 1 / (60 + i)) for i in range(1, 5)]

        result = self.service.search("审批", limit=3)

        self.assertEqual(result["limit"], 3)
        self.assertEqual(len(result["results"]), 3)

    def test_threshold_boundary_is_inclusive(self):
        self.database.search.return_value = [search_row(1, 0.4, 0.02),
                                             search_row(2, 0.399999, 0.019)]

        result = self.service.search("审批")

        self.assertEqual([row["text"] for row in result["results"]], ["切片 1"])

    def test_all_candidates_below_threshold_returns_no_filler(self):
        self.database.search.return_value = [search_row(1, 0.39, 0.8),
                                             search_row(2, -0.2, 0.7)]

        result = self.service.search("无相关内容")

        self.assertEqual(result["results"], [])
        self.service.models.embed.assert_called_once_with(["无相关内容"])

    def test_filtered_image_keeps_source_coordinates_and_urls(self):
        image = search_row(1, 0.75, 0.03, image=True)
        self.database.search.return_value = [image]

        result = self.service.search("图片流程")["results"][0]

        self.assertEqual(result["similarity"], 0.75)
        self.assertEqual(result["channels"], image["channels"])
        self.assertEqual(result["location"]["bbox"], [20, 30, 150, 180])
        self.assertEqual(result["location"]["page"], 2)
        self.assertTrue(result["source_url"].endswith(f"/{image['document_id']}/source#page=2"))
        self.assertTrue(result["asset_url"].endswith(image["asset_name"]))
        self.assertTrue(result["location"]["preview_url"].endswith(image["location"]["preview"]))

    def test_invalid_query_or_count_does_not_embed(self):
        for query, limit in ((" ", None), ("甲" * 501, None), ("审批", 0), ("审批", 21)):
            with self.subTest(query_length=len(query), limit=limit), self.assertRaises(ValueError):
                self.service.search(query, limit)
        self.service.models.embed.assert_not_called()
        self.database.search.assert_not_called()


class DatabaseSimilarityTests(unittest.TestCase):
    def test_both_channels_compute_cosine_and_preserve_rrf_order(self):
        semantic = [search_row(1, 0.9, 0), search_row(2, 0.4, 0)]
        lexical = [dict(semantic[1]), search_row(3, 0.95, 0)]
        for row, distance in zip(semantic + lexical, (0.1, 0.6, 0.6, 0.05)):
            row["distance"] = distance
            del row["similarity"]
        connection = Mock()
        cursors = [Mock(), Mock()]
        cursors[0].fetchall.return_value = semantic
        cursors[1].fetchall.return_value = lexical
        connection.execute.side_effect = cursors
        database = Database(SimpleNamespace())

        @contextmanager
        def connect():
            yield connection

        database.connect = connect
        rows = database.search([0.25, 0.75], "'审批'", 20)

        self.assertEqual([row["id"] for row in rows], [2, 1, 3])
        by_id = {row["id"]: row for row in rows}
        self.assertAlmostEqual(by_id[1]["similarity"], 0.9)
        self.assertAlmostEqual(by_id[2]["similarity"], 0.4)
        self.assertAlmostEqual(by_id[3]["similarity"], 0.95)
        self.assertEqual(by_id[2]["channels"], ["semantic", "lexical"])
        self.assertEqual(by_id[3]["channels"], ["lexical"])
        self.assertAlmostEqual(by_id[2]["score"], 1 / 62 + 1 / 61)
        self.assertIn("embedding <=>", connection.execute.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
