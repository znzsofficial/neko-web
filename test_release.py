import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('web_build_release', Path(__file__).parent/'scripts/build_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_final_package_allowlist_and_real_sdk_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'web.zip'
            first = release.build(path)
            self.assertEqual(first, release.build(path))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), set(release.FILES))
                for name in archive.namelist():
                    self.assertNotIn('__pycache__', name)
                    self.assertNotEqual(name, 'config.toml')
                    self.assertNotIn('keys.json', name)

    def test_unknown_archive_member_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bad.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('../plugin.py', 'raise RuntimeError("must not import")')
            with self.assertRaisesRegex(ValueError, 'Unexpected release members'):
                release.verify(path)
