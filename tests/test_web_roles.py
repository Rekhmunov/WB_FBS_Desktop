import unittest

from review_processor.web import build_app_html


class WebRoleAccessTests(unittest.TestCase):
    def test_feedback_manager_hides_analytics_and_settings_tabs(self) -> None:
        html = build_app_html({"email": "manager@example.com", "role": "feedback_manager"})
        self.assertIn('id="nav-reviews"', html)
        self.assertIn('id="nav-conversations"', html)
        self.assertIn('id="nav-profile"', html)
        self.assertNotIn('id="nav-analytics"', html)
        self.assertNotIn('id="nav-settings"', html)
        self.assertIn("can_view_analytics: false", html)
        self.assertIn("can_view_settings: false", html)

    def test_admin_keeps_analytics_and_settings_tabs(self) -> None:
        html = build_app_html({"email": "admin@example.com", "role": "admin"})
        self.assertIn('id="nav-analytics"', html)
        self.assertIn('id="nav-settings"', html)
        self.assertIn("can_view_analytics: true", html)
        self.assertIn("can_view_settings: true", html)


class FrontendCsrfHeaderTests(unittest.TestCase):
    def test_app_js_mutating_requests_include_csrf_header(self) -> None:
        with open("/workspace/web_static/app.js", "r", encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('headers: jsonHeaders()', script)
        self.assertIn('headers: withCsrfHeaders()', script)
        self.assertIn('"X-CSRF-Token"', script)

    def test_admin_js_mutating_requests_include_csrf_header(self) -> None:
        with open("/workspace/web_static/admin.js", "r", encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('headers: csrfHeaders({ "Content-Type": "application/json" })', script)
        self.assertIn('"X-CSRF-Token"', script)


if __name__ == "__main__":
    unittest.main()
