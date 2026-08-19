import os
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from review_processor.repository import ReviewRepository
from review_processor.service import ReviewAutomationService


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")


class ServiceAiConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))

        self.repository = ReviewRepository(db_path=self.db_path)
        self.service = ReviewAutomationService(repository=self.repository)
        self.material_subgroup_id = self.service._build_subgroup_id("positive", "Материал")
        self.repository.add_default_template_subgroup(group_id="positive", subgroup="Материал")
    def test_check_yandex_connection_success(self) -> None:
        with mock.patch("review_processor.service.urlopen", return_value=_FakeResponse({"result": {"alternatives": []}})):
            result = self.service.check_yandex_connection(
                api_key="abMYSECRETKEYzz",
                folder_id="folder-abc",
            )

        self.assertTrue(result["ok"])
        self.assertIn("message", result)
        self.assertIn("model_uri", result)

    def test_check_yandex_connection_auth_error(self) -> None:
        err = HTTPError(
            url="https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        err.read = lambda: b'{"message":"unauthorized"}'  # type: ignore[assignment]
        with mock.patch("review_processor.service.urlopen", side_effect=err):
            with self.assertRaisesRegex(Exception, "401"):
                self.service.check_yandex_connection(
                    api_key="abMYSECRETKEYzz",
                    folder_id="folder-abc",
                )

    def test_classify_test_review_with_yandex_success(self) -> None:
        payload = {
            "result": {
                "alternatives": [
                    {
                        "message": {
                            "text": f'{{"group_id":"positive","subgroup_id":"{self.material_subgroup_id}"}}',
                        }
                    }
                ]
            }
        }
        with mock.patch("review_processor.service.urlopen", return_value=_FakeResponse(payload)):
            result = self.service.classify_test_review_with_yandex(
                user_id=1,
                review_text="Отличное качество ткани, спасибо!",
                review_rating=5,
                settings={
                    "yandex_api_key": "abMYSECRETKEYzz",
                    "yandex_folder_id": "folder-abc",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["group_id"], "positive")
        self.assertEqual(result["subgroup_id"], self.material_subgroup_id)
        self.assertEqual(result["subgroup"], "Материал")
        self.assertEqual(result["raw_response"], f'{{"group_id":"positive","subgroup_id":"{self.material_subgroup_id}"}}')

    def test_classify_test_review_with_yandex_requires_text(self) -> None:
        with self.assertRaisesRegex(Exception, "Введите текст тестового отзыва"):
            self.service.classify_test_review_with_yandex(
                user_id=1,
                review_text="  ",
                settings={
                    "yandex_api_key": "abMYSECRETKEYzz",
                    "yandex_folder_id": "folder-abc",
                },
            )


if __name__ == "__main__":
    unittest.main()
