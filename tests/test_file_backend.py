"""File backend invariants, exclusively against temporary fixture projects."""
import codecs
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_backend import BackendError, FileSourceBackend


APP = "fixture-app"
BEFORE = "def main(args):\n    return 'before'\n"
AFTER = "def main(args):\n    return 'after'\n"


class FileBackendTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".dev").mkdir()
        self.metadata = self.project / ".dev" / "perform.flow.json"
        self.metadata.write_text(json.dumps({"kind": "code", "name": "perform", "parameters": []}), encoding="utf-8")
        self.manifest = self.project / "package.json"
        self.manifest.write_text(json.dumps({"uuid": APP, "flows": [{"filename": "perform", "kind": "Code"}]}), encoding="utf-8")
        self.source = self.project / "perform.py"
        self.source.write_bytes(BEFORE.encode("utf-8"))
        self.config = self.root / "code_backends.json"
        self.write_config({"version": 1, "projects": {APP: str(self.project)}})
        self.backend = FileSourceBackend(self.config)

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def prepare(self, before=BEFORE, desired=AFTER):
        return self.backend.prepare(APP, "perform", before, desired)

    def test_unconfigured_is_opt_out(self):
        self.assertIsNone(FileSourceBackend(self.root / "absent.json").project_for(APP))
        self.assertIsNone(self.backend.project_for("another-app"))

    def test_config_validates_version_and_absolute_path(self):
        for data in ({"version": True, "projects": {}}, {"version": 2, "projects": {}},
                     {"version": 1, "projects": []}, {"version": 1, "projects": {APP: "relative"}}):
            with self.subTest(data=data):
                self.write_config(data)
                with self.assertRaises(BackendError):
                    self.backend.project_for(APP)

    def test_prepare_does_not_change_source_and_close_removes_temp(self):
        prepared = self.prepare()
        temporary = prepared._temp
        self.assertTrue(temporary.exists())
        self.assertEqual(self.source.read_bytes(), BEFORE.encode())
        self.assertFalse(prepared.receipt["file_written"])
        prepared.close()
        prepared.close()
        self.assertFalse(temporary.exists())
        self.assertEqual(self.source.read_bytes(), BEFORE.encode())

    def test_commit_is_atomic_and_receipt_verifies(self):
        with self.prepare() as prepared:
            receipt = prepared.commit()
            self.assertTrue(receipt["file_written"])
            self.assertTrue(receipt["disk_verified"])
            self.assertTrue(receipt["commit_attempted"])
            self.assertEqual(receipt["backend"], "file_reload")
            self.assertTrue(prepared.verify())
            self.assertEqual(self.source.read_bytes(), AFTER.encode())
            with self.assertRaises(BackendError):
                prepared.commit()
        with self.assertRaises(BackendError):
            prepared.verify()

    def test_preserves_utf8_bom_and_crlf(self):
        self.source.write_bytes(codecs.BOM_UTF8 + BEFORE.replace("\n", "\r\n").encode())
        with self.prepare() as prepared:
            prepared.commit()
        self.assertEqual(self.source.read_bytes(), codecs.BOM_UTF8 + AFTER.replace("\n", "\r\n").encode())

    def test_preserves_legacy_cr_format(self):
        self.source.write_bytes(BEFORE.replace("\n", "\r").encode())
        with self.prepare() as prepared:
            prepared.commit()
        self.assertEqual(self.source.read_bytes(), AFTER.replace("\n", "\r").encode())

    def test_unicode_and_empty_source(self):
        with self.prepare(desired="# 中文\n") as prepared:
            prepared.commit()
        with self.prepare(before="# 中文\n", desired="") as prepared:
            prepared.commit()
        self.assertEqual(self.source.read_bytes(), b"")

    def test_rejects_path_traversal_and_device_names(self):
        for flow in ("../perform", "..\\perform", "C:perform", "perform/child", ".", "..", "CON", "nul.txt", "perform "):
            with self.subTest(flow=flow), self.assertRaises(BackendError):
                self.backend.prepare(APP, flow, BEFORE, AFTER)
        self.assertEqual(self.source.read_bytes(), BEFORE.encode())

    def test_rejects_visual_metadata(self):
        self.metadata.write_text('{"kind":"visual"}', encoding="utf-8")
        with self.assertRaisesRegex(BackendError, "CodeFlow"):
            self.prepare()

    def test_rejects_wrong_project_identity_or_flow_registration(self):
        for data in ({"uuid": "other", "flows": []},
                     {"uuid": APP, "flows": []},
                     {"uuid": APP, "flows": [{"filename": "perform", "kind": "Visual"}]},
                     {"uuid": APP, "flows": [{"filename": "perform", "kind": "Code"}] * 2}):
            with self.subTest(data=data):
                self.manifest.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(BackendError):
                    self.prepare()

    def test_rejects_stale_studio_source(self):
        with self.assertRaisesRegex(BackendError, "Disk source differs"):
            self.prepare(before=AFTER)

    def test_rejects_invalid_python_and_glv_import(self):
        for source in ("def broken(:\n", "print(glv['x'])\n", "from package import variables as glv\nprint(glv['x'])\n",
                       "def main(args):\n    from .package import variables as glv\n    return glv['x']\n"):
            with self.subTest(source=source), self.assertRaises(BackendError):
                self.prepare(desired=source)
        with self.prepare(desired="from .package import variables as glv\nprint(glv['x'])\n"):
            pass

    def test_commit_rechecks_original_bytes_even_with_same_normalized_hash(self):
        with self.prepare() as prepared:
            self.source.write_bytes(BEFORE.replace("\n", "\r\n").encode())
            with self.assertRaisesRegex(BackendError, "Disk source changed"):
                prepared.commit()
            self.assertFalse(prepared.receipt["commit_attempted"])

    def test_commit_rechecks_metadata_and_manifest(self):
        for path in (self.metadata, self.manifest):
            with self.subTest(path=path):
                original = path.read_bytes()
                with self.prepare() as prepared:
                    path.write_bytes(original + b" ")
                    with self.assertRaisesRegex(BackendError, "metadata changed"):
                        prepared.commit()
                path.write_bytes(original)
        self.assertEqual(self.source.read_bytes(), BEFORE.encode())

    def test_commit_rechecks_whitelist(self):
        with self.prepare() as prepared:
            self.write_config({"version": 1, "projects": {}})
            with self.assertRaisesRegex(BackendError, "configured project changed"):
                prepared.commit()

    def test_commit_rejects_changed_temp(self):
        with self.prepare() as prepared:
            prepared._temp.write_bytes(b"external change")
            with self.assertRaisesRegex(BackendError, "prepared source file changed"):
                prepared.commit()
        self.assertEqual(self.source.read_bytes(), BEFORE.encode())

    def test_failed_replace_does_not_rollback_or_hide_unknown_outcome(self):
        real_replace = os.replace
        with self.prepare() as prepared:
            def replace_then_raise(source, destination):
                real_replace(source, destination)
                raise OSError("Simulated uncertain completion")
            with patch("file_backend.os.replace", side_effect=replace_then_raise):
                with self.assertRaisesRegex(BackendError, "no rollback"):
                    prepared.commit()
            self.assertTrue(prepared.receipt["commit_attempted"])
            self.assertFalse(prepared.receipt["file_written"])
            self.assertEqual(self.source.read_bytes(), AFTER.encode())
            with self.assertRaises(BackendError):
                prepared.commit()

    def test_readback_failure_keeps_observed_content(self):
        with self.prepare() as prepared:
            prepared.commit()
            self.source.write_bytes(b"# concurrent edit\n")
            with self.assertRaisesRegex(BackendError, "No rollback"):
                prepared.verify()
            self.assertTrue(prepared.committed)
            self.assertFalse(prepared.receipt["disk_verified"])
            self.assertEqual(self.source.read_bytes(), b"# concurrent edit\n")

    def test_commit_readback_failure_reports_actual_commit(self):
        real_replace = os.replace
        with self.prepare() as prepared:
            def replace_then_change(source, destination):
                real_replace(source, destination)
                Path(destination).write_bytes(b"# concurrent write\n")
            with patch("file_backend.os.replace", side_effect=replace_then_change):
                with self.assertRaisesRegex(BackendError, "No rollback"):
                    prepared.commit()
            self.assertTrue(prepared.committed)
            self.assertTrue(prepared.receipt["file_written"])
            self.assertFalse(prepared.receipt["disk_verified"])

    def test_lock_blocks_second_prepare_and_releases_on_close(self):
        with self.prepare():
            with self.assertRaisesRegex(BackendError, "lock"):
                self.prepare()
        with self.prepare():
            pass

    def test_lock_is_cross_process(self):
        script = "from file_backend import FileSourceBackend, BackendError; import sys\ntry:\n FileSourceBackend(sys.argv[1]).prepare(sys.argv[2], 'perform', sys.argv[3], sys.argv[3])\nexcept BackendError:\n sys.exit(23)\nsys.exit(0)\n"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        with self.prepare():
            result = subprocess.run([sys.executable, "-c", script, str(self.config), APP, BEFORE], env=env,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))

    def test_failed_prepare_releases_lock(self):
        with self.assertRaises(BackendError):
            self.prepare(before=AFTER)
        with self.prepare():
            pass

    def test_rejects_hard_link_source(self):
        try:
            os.link(self.source, self.root / "other.py")
        except OSError:
            self.skipTest("Hard links unavailable")
        with self.assertRaisesRegex(BackendError, "hard link"):
            self.prepare()

    def test_rejects_symlink_source(self):
        original = self.source.read_bytes()
        self.source.unlink()
        destination = self.root / "outside.py"
        destination.write_bytes(original)
        try:
            self.source.symlink_to(destination)
        except OSError:
            self.source.write_bytes(original)
            self.skipTest("Symlink creation not permitted")
        with self.assertRaisesRegex(BackendError, "Symlinks"):
            self.prepare()
        self.assertEqual(destination.read_bytes(), original)

    def test_rejects_windows_reparse_attribute(self):
        if os.name != "nt":
            self.skipTest("Windows reparse attributes only")
        import file_backend
        real_lstat = Path.lstat
        class ReparseStat:
            st_mode = 0o100644
            st_file_attributes = 0x400
        def lstat(path, *args, **kwargs):
            return ReparseStat() if path == self.source else real_lstat(path, *args, **kwargs)
        with patch.object(file_backend.Path, "lstat", lstat):
            with self.assertRaisesRegex(BackendError, "reparse"):
                self.prepare()


if __name__ == "__main__":
    unittest.main()
