import os
import unittest

from review_processor.config import load_app_config


class AppConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "APP_ENV": os.environ.get("APP_ENV"),
            "APP_DB_URL": os.environ.get("APP_DB_URL"),
            "APP_SELF_REGISTRATION_ENABLED": os.environ.get("APP_SELF_REGISTRATION_ENABLED"),
            "FEEDPILOT_TEST_MODE": os.environ.get("FEEDPILOT_TEST_MODE"),
        }
        os.environ["FEEDPILOT_TEST_MODE"] = "1"

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults(self) -> None:
        os.environ.pop("APP_ENV", None)
        os.environ.pop("APP_DB_URL", None)
        os.environ.pop("APP_SELF_REGISTRATION_ENABLED", None)
        cfg = load_app_config()
        self.assertEqual(cfg.app_env, "development")
        self.assertIsNone(cfg.db_url)
        self.assertFalse(cfg.self_registration_enabled)
        self.assertFalse(cfg.is_production)

    def test_missing_db_url_raises_outside_test_mode(self) -> None:
        os.environ.pop("FEEDPILOT_TEST_MODE", None)
        os.environ.pop("APP_DB_URL", None)
        with self.assertRaises(RuntimeError) as ctx:
            load_app_config()
        self.assertIn("APP_DB_URL", str(ctx.exception))

    def test_custom_values(self) -> None:
        os.environ["APP_ENV"] = "production"
        os.environ["APP_DB_URL"] = "postgresql://feedpilot:secret@localhost:5432/feedpilot"
        os.environ["APP_SELF_REGISTRATION_ENABLED"] = "true"
        cfg = load_app_config()
        self.assertEqual(cfg.app_env, "production")
        self.assertEqual(cfg.db_url, "postgresql://feedpilot:secret@localhost:5432/feedpilot")
        self.assertTrue(cfg.self_registration_enabled)
        self.assertTrue(cfg.is_production)


if __name__ == "__main__":
    unittest.main()
