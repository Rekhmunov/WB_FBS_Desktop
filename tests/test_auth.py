import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from review_processor.auth import create_session_token, hash_password, verify_password
from review_processor.repository import ReviewRepository
from review_processor.security import _load_fernet_key


class AuthTests(unittest.TestCase):
    def test_hash_and_verify_password(self) -> None:
        password_hash = hash_password("super-secret-123")
        self.assertTrue(verify_password("super-secret-123", password_hash))
        self.assertFalse(verify_password("wrong-pass", password_hash))

    def test_session_creation_and_cleanup(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))

        repository = ReviewRepository(db_path=db_path)
        user = repository.create_user(email="u@example.com", password_hash="x", role="admin")
        token = create_session_token()
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        repository.create_session(token=token, user_id=int(user["id"]), expires_at=expires_at)
        self.assertIsNotNone(repository.get_session_user(token))

        repository.cleanup_expired_sessions((datetime.now(UTC) + timedelta(hours=2)).isoformat())
        self.assertIsNone(repository.get_session_user(token))

    def test_profile_update_with_password_hash(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))

        repository = ReviewRepository(db_path=db_path)
        user = repository.create_user(
            email="initial@example.com",
            password_hash=hash_password("old-password"),
            role="admin",
            full_name="Old Name",
        )

        new_hash = hash_password("new-password-123")
        updated = repository.update_user_profile(
            user_id=int(user["id"]),
            email="new@example.com",
            full_name="New Name",
            password_hash=new_hash,
        )
        self.assertTrue(updated)

        reloaded = repository.get_user_by_id(int(user["id"]))
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["email"], "new@example.com")
        self.assertEqual(reloaded["full_name"], "New Name")
        self.assertTrue(verify_password("new-password-123", str(reloaded["password_hash"])))

    def test_admin_password_update_helper(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))

        repository = ReviewRepository(db_path=db_path)
        user = repository.create_user(
            email="manager@example.com",
            password_hash=hash_password("old-password"),
            role="user",
        )
        updated = repository.update_user_password(
            user_id=int(user["id"]),
            password_hash=hash_password("new-password-987"),
        )
        self.assertTrue(updated)

        reloaded = repository.get_user_by_id(int(user["id"]))
        self.assertIsNotNone(reloaded)
        self.assertTrue(verify_password("new-password-987", str(reloaded["password_hash"])))

    def test_security_requires_encryption_key_in_production(self) -> None:
        previous_env = os.environ.get("APP_ENV")
        previous_key = os.environ.get("APP_ENCRYPTION_KEY")
        previous_passphrase = os.environ.get("APP_ENCRYPTION_PASSPHRASE")
        try:
            os.environ["APP_ENV"] = "production"
            if "APP_ENCRYPTION_KEY" in os.environ:
                del os.environ["APP_ENCRYPTION_KEY"]
            if "APP_ENCRYPTION_PASSPHRASE" in os.environ:
                del os.environ["APP_ENCRYPTION_PASSPHRASE"]
            with self.assertRaises(RuntimeError):
                _load_fernet_key()
        finally:
            if previous_env is None:
                os.environ.pop("APP_ENV", None)
            else:
                os.environ["APP_ENV"] = previous_env
            if previous_key is None:
                os.environ.pop("APP_ENCRYPTION_KEY", None)
            else:
                os.environ["APP_ENCRYPTION_KEY"] = previous_key
            if previous_passphrase is None:
                os.environ.pop("APP_ENCRYPTION_PASSPHRASE", None)
            else:
                os.environ["APP_ENCRYPTION_PASSPHRASE"] = previous_passphrase


if __name__ == "__main__":
    unittest.main()
