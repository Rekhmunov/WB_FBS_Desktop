import unittest

from review_processor.web import (
    ROLE_ASSIGNABLE_BY_ADMIN,
    ROLE_FEEDBACK_MANAGER,
    ROLE_USER,
    TENANT_ROLE_OWNER,
)


class RoleModelConstraintTests(unittest.TestCase):
    def test_super_admin_assignable_roles_exclude_extra_admins(self) -> None:
        self.assertNotIn(TENANT_ROLE_OWNER, ROLE_ASSIGNABLE_BY_ADMIN)
        self.assertIn(ROLE_USER, ROLE_ASSIGNABLE_BY_ADMIN)
        self.assertIn(ROLE_FEEDBACK_MANAGER, ROLE_ASSIGNABLE_BY_ADMIN)


if __name__ == "__main__":
    unittest.main()
