from __future__ import annotations

import unittest
from pathlib import Path

from webui.app import create_app


class WebuiRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()

    @classmethod
    def _response(cls, path: str):
        route = next(route for route in cls.app.routes if getattr(route, "path", "") == path)
        return route.endpoint()

    def test_root_serves_original_operations_ui(self):
        response = self._response("/")

        self.assertEqual(Path(response.path).name, "index.html")
        body = Path(response.path).read_text(encoding="utf-8")
        self.assertIn('/assets/app.js', body)
        self.assertIn('/assets/view-switch.js', body)

    def test_dashboard_has_its_own_path(self):
        response = self._response("/dash")

        self.assertEqual(Path(response.path).name, "dash.html")

    def test_classic_alias_still_serves_original_ui(self):
        response = self._response("/classic")
        body = bytes(response.body).decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app"', body)
        self.assertIn('/assets/view-switch.js', body)


if __name__ == "__main__":
    unittest.main()
