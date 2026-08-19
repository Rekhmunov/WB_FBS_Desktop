import os
import unittest

from review_processor.config import AppConfig
from review_processor.web import create_app


class PostgresRuntimeSmokeTests(unittest.TestCase):
    def test_create_app_accepts_postgres_configuration_without_connecting_when_unused(self) -> None:
        dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
        if not dsn:
            self.skipTest("TEST_POSTGRES_DSN is not configured")
        try:
            import psycopg  # type: ignore  # noqa: F401
        except Exception:
            self.skipTest("psycopg is not installed in this environment")
        cfg = AppConfig(
            app_env="production",
            db_url=dsn,
            self_registration_enabled=False,
            sync_chats_enabled=False,
        )
        # The app factory should accept PostgreSQL URL configuration shape
        # and connect when explicit integration DSN is provided.
        self.assertIsNotNone(create_app(config=cfg))


if __name__ == "__main__":
    unittest.main()
