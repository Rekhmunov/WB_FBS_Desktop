import unittest

from review_processor.web import build_app_html


class TeamButtonVisibilityTests(unittest.TestCase):
    def test_owner_user_role_can_see_team_nav_link(self) -> None:
        html = build_app_html(
            {
                "id": 42,
                "owner_user_id": 42,
                "email": "owner@example.com",
                "role": "user",
                "is_super_admin": False,
            }
        )
        self.assertIn("is_tenant_owner: true", html)
        self.assertIn('id="nav-team"', html)

    def test_super_admin_can_see_team_nav_link(self) -> None:
        html = build_app_html(
            {
                "id": 1,
                "owner_user_id": 1,
                "email": "admin@example.com",
                "role": "admin",
                "is_super_admin": True,
            }
        )
        self.assertIn("is_tenant_owner: true", html)
        self.assertIn('id="nav-team"', html)

    def test_non_owner_cannot_see_team_nav_link(self) -> None:
        html = build_app_html(
            {
                "id": 43,
                "owner_user_id": 42,
                "email": "manager@example.com",
                "role": "user",
                "is_super_admin": False,
            }
        )
        self.assertIn("is_tenant_owner: false", html)
        self.assertNotIn('id="nav-team"', html)

    def test_feedback_manager_cannot_see_team_nav_link(self) -> None:
        html = build_app_html(
            {
                "id": 44,
                "owner_user_id": 42,
                "email": "fbmanager@example.com",
                "role": "feedback_manager",
                "is_super_admin": False,
            }
        )
        self.assertIn("is_tenant_owner: false", html)
        self.assertNotIn('id="nav-team"', html)


if __name__ == "__main__":
    unittest.main()
