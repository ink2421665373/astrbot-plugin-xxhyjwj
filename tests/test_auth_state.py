import ast
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auth_state import read_saved_auth


class AuthStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "login.json"
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        method = next(node for node in plugin.body if getattr(node, "name", "") == "_reload_web_auth")
        env = dict(read_saved_auth=read_saved_auth)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        self.reload = env["_reload_web_auth"]

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_empty_and_invalid_have_distinct_states(self):
        self.assertEqual(read_saved_auth(self.path)[1], "missing")
        self.path.write_text("{}", encoding="utf-8")
        self.assertEqual(read_saved_auth(self.path)[1], "empty")
        self.path.write_text("not-json", encoding="utf-8")
        self.assertEqual(read_saved_auth(self.path)[1], "invalid")
        self.path.write_text('{"cookies": []}', encoding="utf-8")
        self.assertEqual(read_saved_auth(self.path)[1], "invalid")

    def test_bom_saved_login_is_loaded(self):
        auth = dict(cookies={"sample": "test-only"}, uid="test-user")
        self.path.write_text(json.dumps(auth), encoding="utf-8-sig")
        self.assertEqual(read_saved_auth(self.path), (auth, "saved"))

    def test_stale_web_instance_refreshes_client_after_chat_login(self):
        auth = dict(cookies={"sample": "test-only"})
        self.path.write_text(json.dumps(auth), encoding="utf-8")
        fake = SimpleNamespace(_login_path=self.path, _auth={}, _refresh_api=Mock())
        self.assertEqual(self.reload(fake), "saved")
        self.assertEqual(fake._auth, auth)
        fake._refresh_api.assert_called_once()
        self.reload(fake)
        fake._refresh_api.assert_called_once()

    def test_deleted_login_file_clears_stale_instance_after_logout(self):
        fake = SimpleNamespace(_login_path=self.path, _auth=dict(cookies={"sample": "test-only"}), _refresh_api=Mock())
        self.assertEqual(self.reload(fake), "missing")
        self.assertEqual(fake._auth, {})
        fake._refresh_api.assert_called_once()

    def test_read_failure_preserves_memory_and_reports_file_problem(self):
        path = Mock()
        path.read_text.side_effect = PermissionError("denied")
        auth = dict(cookies={"sample": "test-only"})
        fake = SimpleNamespace(_login_path=path, _auth=auth, _refresh_api=Mock())
        self.assertEqual(self.reload(fake), "unreadable")
        self.assertEqual(fake._auth, auth)
        fake._refresh_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
