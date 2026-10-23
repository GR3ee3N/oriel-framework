import io
import os
import tarfile
from unittest.mock import patch

from .kodi import kodi
from .repository import ROOT, RepositoryTests, load


class WebKitTests(RepositoryTests):
    def test_unpack_preserves_links_and_executables_and_rejects_escape(self):
        oriel, _, _ = self.enterContext(kodi())
        installed = self.temporary / "installed"
        installed.mkdir()
        usr = self.temporary / "usr"
        (usr / "lib").mkdir(parents=True)
        (usr / "lib/libexample.so.1").write_bytes(b"example")
        (usr / "lib/libexample.so").symlink_to("libexample.so.1")
        (usr / "lib/libgbm.so.1").symlink_to("libMali.so")
        (usr / "libexec").mkdir()
        (usr / "libexec/WPEWebProcess").write_text("process")
        (usr / "libexec/WPEWebProcess").chmod(0o755)
        with tarfile.open(installed / "usr.tar.gz", "w:gz") as archive:
            archive.add(usr, arcname="usr")
        # Restore the real unpacker to test extraction independently of the Kodi fixture.
        webkit = load("oriel.webkit", ROOT / "addon/lib/oriel/webkit.py")
        with patch.object(oriel.config, "webkit_dir", return_value=str(installed)):
            self.assertEqual(webkit.ensure(), str(installed / "usr"))
            self.assertTrue((installed / "usr/lib/libexample.so").is_symlink())
            self.assertTrue((installed / "usr/lib/libgbm.so.1").is_symlink())
            self.assertTrue(os.access(installed / "usr/libexec/WPEWebProcess", os.X_OK))
            self.assertEqual(webkit.ensure(), str(installed / "usr"))
        malicious = self.temporary / "malicious"
        malicious.mkdir()
        with tarfile.open(malicious / "usr.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo("../escape")
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"data"))
        with patch.object(oriel.config, "webkit_dir", return_value=str(malicious)), self.assertRaises(tarfile.FilterError):
            webkit.ensure()
        self.assertFalse((self.temporary / "escape").exists())
        self.assertEqual(list(malicious.glob(".unpack-*")), [])
