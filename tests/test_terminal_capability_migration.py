import importlib.util
from pathlib import Path
import sys
import types
import unittest

from flask import Flask
from sqlalchemy import event

from CTFd.models import db


SOURCE = Path(__file__).resolve().parent.parent / "src"
package = types.ModuleType("_rd_capability_migration")
package.__path__ = [str(SOURCE)]
sys.modules[package.__name__] = package
modules = {}
for name in ("settings", "models"):
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.{name}", SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    modules[name] = module
settings, models = modules["settings"], modules["models"]

OLD_CAPS = "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE"
PREVIOUS_CAPS = OLD_CAPS + ",SYS_CHROOT"
CURRENT_CAPS = PREVIOUS_CAPS + ",KILL"


class TerminalCapabilityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask("terminal-capability-migration")
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.session.remove)

    def seed(self, schema, capabilities, readiness, revision):
        db.session.remove()
        for table in (models.DesktopSettingsModel.__table__, models.DesktopPluginMetadataModel.__table__):
            table.drop(db.engine, checkfirst=True)
            table.create(db.engine)
        db.session.add(models.DesktopSettingsModel(key="_settings_revision", value=str(revision)))
        if capabilities is not None:
            db.session.add(models.DesktopSettingsModel(key="cap_add", value=capabilities))
        if readiness is not None:
            db.session.add(models.DesktopSettingsModel(key="vnc_ready_attempts", value=str(readiness)))
        if schema is not None:
            db.session.add(models.DesktopPluginMetadataModel(key="settings_schema_version", value=str(schema)))
        db.session.commit()

    def snapshot(self):
        return {
            "settings": {
                row.key: row.value
                for row in models.DesktopSettingsModel.query.order_by(models.DesktopSettingsModel.key).all()
            },
            "metadata": {
                row.key: row.value
                for row in models.DesktopPluginMetadataModel.query.order_by(models.DesktopPluginMetadataModel.key).all()
            },
        }

    def test_default_and_versioned_profiles_are_idempotent(self):
        cases = [
            ("fresh", None, None, None, 1, CURRENT_CAPS, 420, 1),
            ("v1 old default", 1, OLD_CAPS, 180, 7, CURRENT_CAPS, 420, 8),
            ("v1 previous default", 1, PREVIOUS_CAPS, 180, 7, CURRENT_CAPS, 420, 8),
            ("v1 custom", 1, "NET_RAW", 240, 7, "NET_RAW", 240, 7),
            ("v1 readiness only", 1, "", 180, 7, "", 420, 8),
            ("v2 previous default", 2, PREVIOUS_CAPS, 180, 7, CURRENT_CAPS, 180, 8),
            ("v2 custom old default", 2, OLD_CAPS, 180, 7, OLD_CAPS, 180, 7),
            ("v2 reordered", 2, "SYS_CHROOT," + OLD_CAPS, 180, 7, "SYS_CHROOT," + OLD_CAPS, 180, 7),
            ("v2 empty", 2, "", 180, 7, "", 180, 7),
            ("v2 custom", 2, "NET_RAW", 180, 7, "NET_RAW", 180, 7),
            ("v3 intentional previous default", 3, PREVIOUS_CAPS, 180, 7, PREVIOUS_CAPS, 180, 7),
            ("v3 current default", 3, CURRENT_CAPS, 180, 7, CURRENT_CAPS, 180, 7),
            ("revision wraps", 2, PREVIOUS_CAPS, 180, 2147483647, CURRENT_CAPS, 180, 1),
        ]
        for name, schema, capabilities, readiness, revision, expected_caps, expected_ready, expected_revision in cases:
            with self.subTest(name=name):
                self.seed(schema, capabilities, readiness, revision)
                models.initialize_settings()
                first = self.snapshot()
                self.assertEqual(first["settings"]["cap_add"], expected_caps)
                self.assertEqual(first["settings"]["vnc_ready_attempts"], str(expected_ready))
                self.assertEqual(first["settings"]["_settings_revision"], str(expected_revision))
                self.assertEqual(first["metadata"]["settings_schema_version"], "3")
                self.assertEqual(len(first["settings"]), len(settings.PUBLIC_SETTING_KEYS) + 1)
                models.initialize_settings()
                self.assertEqual(self.snapshot(), first)

    def test_invalid_schemas_roll_back_without_changing_the_profile(self):
        for schema in (0, 4, "bogus"):
            with self.subTest(schema=schema):
                self.seed(schema, PREVIOUS_CAPS, 180, 7)
                before = self.snapshot()
                with self.assertRaises(settings.SettingsValidationError):
                    models.initialize_settings()
                self.assertEqual(self.snapshot(), before)

    def test_commit_failure_rolls_back_the_capability_upgrade(self):
        self.seed(2, PREVIOUS_CAPS, 180, 7)
        before = self.snapshot()

        def fail_commit(session):
            raise RuntimeError("injected commit failure")

        session = db.session()
        event.listen(session, "before_commit", fail_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
                models.initialize_settings()
        finally:
            event.remove(session, "before_commit", fail_commit)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
