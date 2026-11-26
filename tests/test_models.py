import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kb.models import ModelClient, ModelError


TEST_ROOT = Path(__file__).resolve().parent


def settings(**changes):
    values = {
        "embedding_backend": "http",
        "embedding_model_path": "",
        "embedding_base_url": "http://127.0.0.1:9000/v1",
        "embedding_api_key": "",
        "embedding_model": "bge-m3",
        "embedding_dimension": 3,
        "vision_base_url": "",
        "vision_api_key": "",
        "vision_model": "",
        "chat_base_url": "",
        "chat_api_key": "",
        "chat_model": "",
        "chat_max_tokens": 2000,
        "model_timeout": 30,
    }
    values.update(changes)
    return types.SimpleNamespace(**values)


class ModelClientTests(unittest.TestCase):
    def vision_client(self):
        return ModelClient(settings(vision_base_url="http://127.0.0.1:9000/v1", vision_model="vision-test"))

    def test_http_embedding_works_without_visual_configuration_and_reorders_indices(self):
        client = ModelClient(settings())
        response = {
            "data": [
                {"index": 1, "embedding": [0, 2, 0]},
                {"index": 0, "embedding": [1, 0, 0]},
            ]
        }
        with patch.object(client, "_post", return_value=response):
            self.assertEqual(client.embed(["审批", "退回"]), [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0]])

    def test_http_embedding_rejects_duplicate_indices(self):
        client = ModelClient(settings())
        response = {"data": [{"index": 0, "embedding": [1, 0, 0]}] * 2}
        with patch.object(client, "_post", return_value=response):
            with self.assertRaisesRegex(ModelError, "索引"):
                client.embed(["审批", "退回"])

    def test_local_model_is_loaded_offline_and_reused_across_clients(self):
        fake_module = types.ModuleType("sentence_transformers")
        model = MagicMock()
        model.encode.return_value.tolist.return_value = [[1, 0, 0]]
        fake_module.SentenceTransformer = MagicMock(return_value=model)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            config = settings(embedding_backend="local", embedding_model_path=directory)
            with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
                with patch.object(ModelClient, "_local_models", {}):
                    first = ModelClient(config).embed(["主管审批"])
                    second = ModelClient(config).embed(["申请人修改"])
            self.assertEqual(first, [[1.0, 0.0, 0.0]])
            self.assertEqual(second, first)
            fake_module.SentenceTransformer.assert_called_once_with(
                str(Path(directory).resolve()), device="cpu",
                local_files_only=True, trust_remote_code=False,
            )
            self.assertEqual(model.encode.call_count, 2)
            self.assertEqual(model.encode.call_args.kwargs["batch_size"], 16)
            self.assertTrue(model.encode.call_args.kwargs["normalize_embeddings"])

    def test_missing_local_directory_fails_before_loading_or_http(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            config = settings(embedding_backend="local", embedding_model_path=str(Path(directory) / "missing"))
            client = ModelClient(config)
            with patch.object(client, "_post") as post:
                with self.assertRaisesRegex(ModelError, "目录不存在"):
                    client.embed(["审批"])
                post.assert_not_called()

    def test_local_encoding_failure_does_not_expose_exception_details(self):
        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = MagicMock(side_effect=RuntimeError("sensitive model details"))
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            client = ModelClient(settings(embedding_backend="local", embedding_model_path=directory))
            with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
                with patch.object(ModelClient, "_local_models", {}):
                    with self.assertRaises(ModelError) as caught:
                        client.embed(["审批"])
            self.assertIn("RuntimeError", str(caught.exception))
            self.assertNotIn("sensitive", str(caught.exception))

    def test_vectors_reject_wrong_dimension_nonfinite_and_zero(self):
        client = ModelClient(settings())
        for vector in ([1, 0], [float("nan"), 0, 0], [0, 0, 0], [True, 0, 0]):
            with self.subTest(vector=vector):
                with self.assertRaises(ModelError):
                    client._validate_vector(vector)

    def test_visual_configuration_is_required_when_analyzing_image(self):
        client = ModelClient(settings())
        with self.assertRaisesRegex(ModelError, "视觉模型地址"):
            client.describe_image(Path("unused.png"), "流程图")

    def test_21_images_are_sent_as_10_10_1_without_losing_images_or_context(self):
        client = self.vision_client()
        images = [(Path(f"figure-{i}.png"), f"context-{i}") for i in range(21)]
        sent_urls = []
        batch_sizes = []
        sent_texts = []

        def reply(base_url, api_key, endpoint, payload):
            self.assertEqual(endpoint, "chat/completions")
            content = payload["messages"][1]["content"]
            urls = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
            sent_texts.extend(part["text"] for part in content if part["type"] == "text")
            sent_urls.extend(urls)
            batch_sizes.append(len(urls))
            if len(urls) == 1:
                description = urls[0].split(",", 1)[1]
            else:
                items = [{"id": f"image_{i}", "description": url.split(",", 1)[1]}
                         for i, url in enumerate(urls, 1)]
                description = json.dumps({"images": list(reversed(items))})
            return {"choices": [{"message": {"content": description}}]}

        with patch.object(client, "_image_data_url", side_effect=lambda path: f"data:image/png;base64,{path.name}"):
            with patch.object(client, "_post", side_effect=reply):
                descriptions = client.describe_images(images)
        self.assertEqual(batch_sizes, [10, 10, 1])
        self.assertEqual(descriptions, [path.name for path, context in images])
        self.assertEqual(sent_urls, [f"data:image/png;base64,{path.name}" for path, context in images])
        self.assertEqual(len(sent_texts), 21)
        for sent_text, (path, context) in zip(sent_texts, images):
            self.assertIn(context, sent_text)

    def test_single_image_uses_the_existing_single_image_protocol(self):
        client = self.vision_client()
        image = (Path("figure.png"), "审批流程")
        with patch.object(client, "describe_image", return_value="原有单图结果") as describe:
            with patch.object(client, "_post") as post:
                self.assertEqual(client.describe_images([image]), ["原有单图结果"])
            describe.assert_called_once_with(*image)
            post.assert_not_called()

    def test_fenced_batch_json_is_mapped_back_to_input_order(self):
        client = self.vision_client()
        response = {"choices": [{"message": {"content": '```json\n{"images":['
                    '{"id":"image_2","description":" 第二张 "},'
                    '{"id":"image_1","description":"第一张"}]}\n```'}}]}
        with patch.object(client, "_image_data_url", return_value="data:image/png;base64,eA=="):
            with patch.object(client, "_post", return_value=response):
                self.assertEqual(client.describe_images([(Path("a.png"), ""), (Path("b.png"), "")]),
                                 ["第一张", "第二张"])

    def test_batch_rejects_missing_duplicate_unknown_or_empty_descriptions(self):
        client = self.vision_client()
        invalid = [
            {"images": [{"id": "image_1", "description": "one"}]},
            {"images": [{"id": "image_1", "description": "one"}, {"id": "image_1", "description": "two"}]},
            {"images": [{"id": "image_1", "description": "one"}, {"id": "image_3", "description": "two"}]},
            {"images": [{"id": "image_1", "description": "one"}, {"description": "two"}]},
            {"images": [{"id": "image_1", "description": "one"}, {"id": "image_2", "description": " "}]},
            {"images": [{"id": "image_1", "description": "one"}, {"id": "image_2", "description": 2}]},
            {"images": [{"id": "image_1", "description": "one"}, {"id": "image_2", "description": "x" * 24_001}]},
            [{"id": "image_1", "description": "one"}, {"id": "image_2", "description": "two"}],
        ]
        with patch.object(client, "_image_data_url", return_value="data:image/png;base64,eA=="):
            for value in invalid:
                with self.subTest(value_type=type(value).__name__):
                    response = {"choices": [{"message": {"content": json.dumps(value)}}]}
                    with patch.object(client, "_post", return_value=response):
                        with self.assertRaises(ModelError):
                            client.describe_images([(Path("a.png"), ""), (Path("b.png"), "")])

    def test_batch_invalid_json_fails_with_a_safe_error(self):
        client = self.vision_client()
        response = {"choices": [{"message": {"content": "sensitive invalid response"}}]}
        with patch.object(client, "_image_data_url", return_value="data:image/png;base64,eA=="):
            with patch.object(client, "_post", return_value=response):
                with self.assertRaisesRegex(ModelError, "有效JSON") as caught:
                    client.describe_images([(Path("a.png"), ""), (Path("b.png"), "")])
        self.assertNotIn("sensitive", str(caught.exception))

    def test_empty_image_list_does_not_require_visual_configuration(self):
        client = ModelClient(settings())
        with patch.object(client, "_post") as post:
            self.assertEqual(client.describe_images([]), [])
            post.assert_not_called()


    def test_chat_completion_returns_text_and_usage(self):
        client = ModelClient(settings(chat_base_url="http://127.0.0.1:9000/v1",
                                      chat_api_key="secret", chat_model="chat-test",
                                      chat_max_tokens=512))
        response = {"choices": [{"message": {"content": "  ## 结论\n资料[1]  "}}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 30, "total_tokens": 120}}
        with patch.object(client, "_post", return_value=response) as post:
            text, usage = client.generate("系统提示", "用户问题")
        self.assertEqual(text, "## 结论\n资料[1]")
        self.assertEqual(usage, {"prompt_tokens": 90, "completion_tokens": 30, "total_tokens": 120})
        base_url, api_key, endpoint, payload = post.call_args.args
        self.assertEqual((base_url, endpoint), ("http://127.0.0.1:9000/v1", "chat/completions"))
        self.assertEqual(api_key, "secret")
        self.assertEqual(payload["model"], "chat-test")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["messages"][0]["content"], "系统提示")
        self.assertEqual(payload["messages"][1]["content"], "用户问题")
        self.assertTrue(client.chat_configured)

    def test_chat_completion_accepts_content_parts(self):
        client = ModelClient(settings(chat_base_url="http://127.0.0.1:9000/v1", chat_model="chat-test"))
        response = {"choices": [{"message": {"content": [
            {"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]}}]}
        with patch.object(client, "_post", return_value=response):
            text, usage = client.generate("s", "u")
        self.assertEqual(text, "第一段第二段")
        self.assertEqual(usage, {})

    def test_chat_completion_rejects_malformed_empty_or_oversized_content(self):
        client = ModelClient(settings(chat_base_url="http://127.0.0.1:9000/v1", chat_model="chat-test"))
        values = ({}, {"choices": []}, {"choices": [{"message": {"content": "  "}}]},
                  {"choices": [{"message": {"content": "x" * (client.MAX_REPORT_CHARS + 1)}}]},
                  {"choices": [{"message": {"content": 2}}]})
        for response in values:
            with self.subTest(response=str(response)[:40]):
                with patch.object(client, "_post", return_value=response):
                    with self.assertRaises(ModelError):
                        client.generate("s", "u")

    def test_generate_requires_a_configured_endpoint_and_model(self):
        with self.assertRaisesRegex(ModelError, "生成模型地址"):
            ModelClient(settings()).generate("s", "u")
        with self.assertRaisesRegex(ModelError, "生成模型名称"):
            ModelClient(settings(chat_base_url="http://127.0.0.1:9000/v1")).generate("s", "u")


if __name__ == "__main__":
    unittest.main()
