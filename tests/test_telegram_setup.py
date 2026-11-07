import importlib.util
import os
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).parents[1] / "api" / "telegram.py"
    spec = importlib.util.spec_from_file_location("api_telegram", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    def __init__(self):
        self.hook = {"url": "", "pending_update_count": 3}
        self.calls = []

    def get_webhook_info(self):
        return dict(self.hook)

    def set_webhook(self, url, secret=""):
        self.calls.append(("set_webhook", url, secret))
        self.hook = {"url": url, "pending_update_count": 0}

    def set_commands(self):
        self.calls.append(("set_commands",))

    def set_menu_button(self, url):
        self.calls.append(("set_menu_button", url))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("WEBAPP_URL", "TELEGRAM_WEBHOOK_SECRET")}
        os.environ.pop("WEBAPP_URL", None)
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = "s3cret"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_check_only_reports(self):
        api = FakeApi()
        report = _load().setup_webhook(False, api)
        self.assertEqual(api.calls, [])
        self.assertIn("getUpdates", report["before"]["url"])
        self.assertNotIn("after", report)

    def test_setup_points_the_bot_at_vercel(self):
        api = FakeApi()
        report = _load().setup_webhook(True, api)
        hook = "https://start-bogdan22807s-projects.vercel.app/api/telegram"
        self.assertEqual(api.calls[0], ("set_webhook", hook, "s3cret"))
        self.assertIn(("set_menu_button", "https://start-bogdan22807s-projects.vercel.app"), api.calls)
        self.assertEqual(report["after"]["url"], hook)


if __name__ == "__main__":
    unittest.main()
