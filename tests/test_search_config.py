import unittest
from unittest.mock import patch

from kb.config import load_settings


class SearchConfigTests(unittest.TestCase):
    def load(self, **changes):
        environment = {
            "DATABASE_URL": "postgresql://example:synthetic@127.0.0.1/search_fixture",
            "EMBEDDING_BACKEND": "http",
            "EMBEDDING_BASE_URL": "http://127.0.0.1:9000/v1",
        }
        environment.update(changes)
        with patch.dict("os.environ", environment, clear=True), patch("kb.config.load_dotenv"):
            return load_settings()

    def test_defaults_are_available_without_environment_values(self):
        settings = self.load()
        self.assertEqual(settings.search_similarity_threshold, 0.40)
        self.assertEqual(settings.search_top_k, 5)

    def test_environment_overrides_defaults(self):
        settings = self.load(SEARCH_SIMILARITY_THRESHOLD="0.62", SEARCH_TOP_K="12")
        self.assertEqual(settings.search_similarity_threshold, 0.62)
        self.assertEqual(settings.search_top_k, 12)

    def test_supported_boundaries_are_inclusive(self):
        for threshold, top_k in (("-1", "1"), ("1", "20")):
            with self.subTest(threshold=threshold, top_k=top_k):
                settings = self.load(SEARCH_SIMILARITY_THRESHOLD=threshold, SEARCH_TOP_K=top_k)
                self.assertEqual(settings.search_similarity_threshold, float(threshold))
                self.assertEqual(settings.search_top_k, int(top_k))

    def test_nonfinite_out_of_range_and_invalid_thresholds_are_rejected(self):
        for value in ("nan", "NaN", "inf", "-inf", "Infinity", "1.01", "-1.01", "", "synthetic-invalid"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "SEARCH_SIMILARITY_THRESHOLD") as caught:
                    self.load(SEARCH_SIMILARITY_THRESHOLD=value)
                self.assertNotIn("synthetic-invalid", str(caught.exception))

    def test_invalid_top_k_values_are_rejected(self):
        for value in ("0", "21", "-2", "5.0", "", "synthetic-invalid"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "SEARCH_TOP_K") as caught:
                    self.load(SEARCH_TOP_K=value)
                self.assertNotIn("synthetic-invalid", str(caught.exception))

    def test_search_configuration_keeps_embedding_fingerprint(self):
        baseline = self.load()
        changed = self.load(SEARCH_SIMILARITY_THRESHOLD="0.75", SEARCH_TOP_K="15")
        self.assertEqual(baseline.embedding_fingerprint, changed.embedding_fingerprint)


if __name__ == "__main__":
    unittest.main()
