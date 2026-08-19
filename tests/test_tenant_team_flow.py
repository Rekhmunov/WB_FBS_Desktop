import os
import tempfile
import unittest

from review_processor.auth import hash_password
from review_processor.repository import ReviewRepository
from review_processor.web import create_app


class TenantTeamFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))

        self.repository = ReviewRepository(db_path=self.db_path)
        self.owner = self.repository.create_user(
            email="owner@example.com",
            password_hash=hash_password("owner-password-123"),
            role="user",
            owner_user_id=None,
            is_super_admin=False,
        )
        self.owner_id = int(self.owner["id"])

    def test_create_manager_then_list_team_contains_it(self) -> None:
        manager = self.repository.create_tenant_user(
            owner_user_id=self.owner_id,
            email="manager@example.com",
            password_hash=hash_password("manager-password-123"),
            role="feedback_manager",
            full_name="Менеджер тест",
        )
        manager_id = int(manager["id"])
        self.repository.replace_manager_permissions(
            manager_user_id=manager_id,
            permissions=[],
        )
        team = self.repository.list_tenant_users(owner_user_id=self.owner_id)
        emails = [str(item.get("email") or "") for item in team]
        self.assertIn("manager@example.com", emails)


if __name__ == "__main__":
    unittest.main()
