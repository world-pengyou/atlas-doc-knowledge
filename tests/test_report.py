import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from kb.models import ModelError
from kb.service import KnowledgeService


def hit(document_id, filename, heading, location, text, similarity=0.82):
    return {"document_id": str(document_id), "filename": filename, "kind": "text",
            "location": location, "text": text, "score": 0.03,
            "similarity": similarity, "channels": ["semantic"],
            "heading": heading, "heading_level": 1, "unit_id": None,
            "source_url": "http://127.0.0.1:8000/api/documents/%s/source" % document_id,
            "asset_url": None}


class ReportTests(unittest.TestCase):
    def build(self, chat_model="", results=None):
        settings = SimpleNamespace(
            data_dir=Path("."), public_base_url="http://127.0.0.1:8000",
            chat_base_url="http://model.local/v1" if chat_model else "",
            chat_api_key="", chat_model=chat_model, chat_max_tokens=2000,
            search_similarity_threshold=0.4, search_top_k=5)
        service = KnowledgeService(settings, Mock())
        service.models = Mock()
        service.models.embed.return_value = [[0.0, 1.0]]
        service.search = Mock(return_value={"results": results or []})
        return service

    def two_hits(self):
        first, second = uuid4(), uuid4()
        return [
            hit(first, "流程.pdf", "第二章 审批", {"page": 2, "page_end": 3}, "审批由主管发起。" * 80),
            hit(second, "制度.md", "3 权限", {"format": "markdown", "line_start": 4, "line_end": 9},
                "管理员可撤回流程。" * 80),
        ]

    def test_unconfigured_model_returns_an_extractive_digest(self):
        service = self.build(results=self.two_hits())
        result = service.report("审批流程", "report", 5)
        self.assertEqual(result["mode"], "extractive")
        self.assertIsNone(result["model"])
        self.assertIn("未配置生成模型", result["note"])
        self.assertIn("## 资料整合", result["report"])
        self.assertIn("《流程.pdf》", result["report"])
        self.assertIn("**第二章 审批**", result["report"])
        self.assertIn("[1]", result["report"])
        self.assertIn("[2]", result["report"])
        self.assertEqual([source["citation"] for source in result["sources"]], [1, 2])
        self.assertEqual(result["sources"][0]["location_label"], "第 2–3 页")
        self.assertEqual(result["sources"][1]["location_label"], "第 4–9 行")
        self.assertTrue(result["sources"][0]["text"].endswith("…"))
        service.models.generate.assert_not_called()

    def test_configured_model_receives_numbered_context_and_sources(self):
        service = self.build(chat_model="qwen2.5-72b", results=self.two_hits())
        service.models.generate.return_value = ("## 结论\n审批由主管发起[1]。", {"total_tokens": 120})
        result = service.report("审批流程", "summary", 2)
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["model"], "qwen2.5-72b")
        self.assertEqual(result["usage"], {"total_tokens": 120})
        self.assertIsNone(result["note"])
        self.assertEqual(result["report"], "## 结论\n审批由主管发起[1]。")
        system, prompt = service.models.generate.call_args.args
        self.assertIn("只能依据", system)
        self.assertIn("要点摘要", prompt)
        self.assertIn("主题或问题：审批流程", prompt)
        self.assertIn("[1] 文档：流程.pdf｜章节：第二章 审批｜位置：第 2–3 页", prompt)
        self.assertIn("[2] 文档：制度.md｜章节：3 权限｜位置：第 4–9 行", prompt)

    def test_model_failure_falls_back_to_the_extractive_digest(self):
        service = self.build(chat_model="qwen2.5-72b", results=self.two_hits())
        service.models.generate.side_effect = ModelError("模型请求超时，请检查模型服务后重试。")
        result = service.report("审批流程", "qa", 2)
        self.assertEqual(result["mode"], "extractive")
        self.assertIn("生成模型调用失败", result["note"])
        self.assertIn("模型请求超时", result["note"])
        self.assertIn("## 资料整合", result["report"])

    def test_no_results_skip_the_model_entirely(self):
        service = self.build(chat_model="qwen2.5-72b", results=[])
        result = service.report("不存在的主题")
        self.assertEqual(result["mode"], "empty")
        self.assertEqual(result["sources"], [])
        self.assertIn("没有检索到", result["report"])
        service.models.generate.assert_not_called()

    def test_arguments_are_validated_before_searching(self):
        service = self.build(results=[])
        for query, style, top_k in ((" ", "report", 5), ("主题", "x", 5),
                                    ("主题", "report", 0), ("主题", "report", 13),
                                    ("长" * 501, "report", 5)):
            with self.subTest(query=query[:10], style=style, top_k=top_k):
                with self.assertRaises(ValueError):
                    service.report(query, style, top_k)
        service.search.assert_not_called()

    def test_context_is_bounded_and_later_sources_are_dropped(self):
        results = [hit(uuid4(), "文档%d.docx" % number, "章节", {"format": "docx", "paragraph": number},
                       "内容" * 800) for number in range(1, 13)]
        service = self.build(chat_model="qwen", results=results)
        service.models.generate.return_value = ("ok", {})
        service.report("主题", "report", 12)
        prompt = service.models.generate.call_args.args[1]
        self.assertLess(len(prompt), 13_000)
        self.assertIn("[1] 文档：文档1.docx", prompt)
        self.assertNotIn("[12] 文档：文档12.docx", prompt)

    def test_location_labels_cover_ranges_and_single_positions(self):
        label = KnowledgeService._location_label
        self.assertEqual(label({"format": "docx", "paragraph": 7, "paragraph_end": 12}), "段落 7–12")
        self.assertEqual(label({"format": "docx", "paragraph": 7}), "段落 7")
        self.assertEqual(label({"format": "docx", "table": 1, "row": 2, "row_end": 4,
                                "cell": 1, "cell_end": 3}), "表格 1 第 2–4 行 单元格 1–3")
        self.assertEqual(label({"format": "docx", "table": 1, "row": 2, "cell": 1}),
                         "表格 1 第 2 行 单元格 1")
        self.assertEqual(label({"page": 2}), "第 2 页")
        self.assertEqual(label({"format": "markdown", "line_start": 3}), "第 3 行")
        self.assertEqual(label({}), "")


if __name__ == "__main__":
    unittest.main()
