import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

APP_PATH = Path(os.environ.get("CONFIG_STORAGE_APP", Path(__file__).resolve().parents[1] / "app.py"))


def load_storage_functions(base_dir: Path):
    engine = types.ModuleType("automation_engine")
    engine.__dict__.update(
        BASE_DIR=str(base_dir),
        CHROME_PATH="/dummy/chrome",
        SCREENSHOTS_DIR=str(base_dir),
        GoogleUltraAutomator=object,
        LocalHttpProxyAuthBridge=object,
        get_totp=mock.Mock(),
        get_profile_dir=mock.Mock(),
        resolve_account_proxy=mock.Mock(),
        parse_proxy_string=mock.Mock(),
    )
    spec = importlib.util.spec_from_file_location("storage_app", APP_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load storage app from {APP_PATH}")
    storage_app = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(
            sys.modules, {"automation_engine": engine, "storage_app": storage_app}
        ),
        mock.patch.object(os, "makedirs"),
    ):
        spec.loader.exec_module(storage_app)
    return vars(storage_app)


class ConfigStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.storage = load_storage_functions(self.path.parent)
        self.storage["CONFIG_PATH"] = str(self.path)
        self.previous = {"accounts": [{"id": "existing", "email": "test@example.invalid"}], "unknown_field": "préservé"}
        self.path.write_text(json.dumps(self.previous, ensure_ascii=False, indent=2), encoding="utf-8")
        self.previous_bytes = self.path.read_bytes()

    def assert_untouched(self):
        self.assertEqual(self.path.read_bytes(), self.previous_bytes)
        self.assertEqual(list(self.path.parent.glob(".config-*.tmp")), [])

    def test_round_trip_and_json_format_unchanged(self):
        config = {"accounts": [{"id": "नया", "status": "pending"}], "auto_cancel": False, "card": {}, "unknown_field": [1, "préservé"]}
        self.assertIsNone(self.storage["save_config"](config))
        self.assertEqual(self.storage["load_config"](), config)
        with self.path.open("r", encoding="utf-8") as stream:
            self.assertEqual(stream.read(), json.dumps(config, ensure_ascii=False, indent=2))
        self.assertEqual(list(self.path.parent.glob(".config-*.tmp")), [])

    def test_existing_expiry_is_normalised_without_losing_account_data(self):
        config = {"accounts": [{"id": "a", "expiry_date": "2028.10.08", "password": "preserved"}],
                  "unknown_field": "preserved"}
        self.storage["save_config"](config)
        loaded = self.storage["load_config"]()
        self.assertEqual(loaded["accounts"][0]["expiry_date"], "2028-10-08")
        self.assertEqual(loaded["accounts"][0]["password"], "preserved")
        self.assertEqual(loaded["unknown_field"], "preserved")
        self.storage["save_config"](loaded)
        self.assertEqual(self.storage["load_config"](), loaded)

    def test_serialization_failure_preserves_original(self):
        with self.assertRaises(TypeError):
            self.storage["save_config"]({"accounts": [], "invalid": object()})
        self.assert_untouched()

    def test_replace_failure_preserves_original(self):
        with (
            mock.patch.object(os, "replace", side_effect=PermissionError("replacement blocked")),
            self.assertRaisesRegex(PermissionError, "replacement blocked"),
        ):
            self.storage["save_config"]({"accounts": []})
        self.assert_untouched()

    def test_flush_failure_preserves_original(self):
        with (
            mock.patch.object(os, "fsync", side_effect=OSError("disk flush failed")),
            self.assertRaisesRegex(OSError, "disk flush failed"),
        ):
            self.storage["save_config"]({"accounts": []})
        self.assert_untouched()

    def test_temp_creation_failure_preserves_original(self):
        with (
            mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=PermissionError("creation blocked")),
            self.assertRaisesRegex(PermissionError, "creation blocked"),
        ):
            self.storage["save_config"]({"accounts": []})
        self.assert_untouched()

    def test_replacement_receives_a_complete_closed_file_in_same_directory(self):
        config = {"accounts": [{"id": "next"}], "unknown_field": "préservé"}
        replace = os.replace

        def checked_replace(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), config)
            self.assertEqual(self.path.read_bytes(), self.previous_bytes)
            replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=checked_replace) as replacement:
            self.storage["save_config"](config)
        replacement.assert_called_once()
        self.assertEqual(self.storage["load_config"](), config)

    def test_missing_config_keeps_defaults_and_can_be_created(self):
        self.path.unlink()
        self.assertEqual(self.storage["load_config"](), {
            "target_region": "US", "target_plan": "pro_2year_zero", "auto_cancel": True,
            "card": {}, "accounts": [],
        })
        self.storage["save_config"](self.previous)
        self.assertEqual(self.storage["load_config"](), self.previous)

    def test_invalid_existing_json_still_raises(self):
        self.path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.storage["load_config"]()

    def test_repeated_saves_leave_no_temporary_files(self):
        for number in range(25):
            self.storage["save_config"]({"accounts": [], "revision": number})
            self.assertEqual(self.storage["load_config"]()["revision"], number)
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ["config.json"])


if __name__ == "__main__":
    unittest.main()
