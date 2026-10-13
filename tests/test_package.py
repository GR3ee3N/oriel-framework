import hashlib
import json
import tarfile
import xml.etree.ElementTree as ET
import zipfile

from .repository import RepositoryTests


class PackageTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.package, runtime, self.source, licenses = self.package_inputs()
        self.output = self.root / ".cache/release"
        self.package.package(runtime, self.source, licenses, self.output)
        self.files = self.output / "files"
        self.url = f"https://github.com/GR3ee3N/oriel-framework/releases/download/v{self.v}/{self.source.name}.tar.xz\n"

    def test_addon_contains_shell_licenses_and_runtime_requirement(self):
        with zipfile.ZipFile(self.files / f"script.module.oriel.framework-{self.v}.zip") as archive:
            self.assertEqual(archive.read("script.module.oriel.framework/SOURCE.txt").decode(), self.url)
            self.assertEqual(archive.read("script.module.oriel.framework/bin/oriel-shell"), b"shell")
            for name in ("rust/COPYRIGHT-library.html", "cargo/example-1.0/LICENSE"):
                self.assertEqual(archive.read(f"script.module.oriel.framework/LICENSES/{name}"), b"license text\n")
            self.assertIn(f"Oriel WebKit {self.w}", archive.read("script.module.oriel.framework/THIRD-PARTY-NOTICES.txt").decode())
            addon = ET.fromstring(archive.read("script.module.oriel.framework/addon.xml"))
            self.assertEqual(addon.find("requires/import[@addon='script.module.oriel.webkit']").get("version"), self.w)

    def test_web_package_has_version_sources_and_license(self):
        with tarfile.open(self.files / f"oriel-web-{self.v}.tgz") as archive:
            self.assertIn("package/LICENSE", archive.getnames())
            self.assertEqual(archive.extractfile("package/src/index.ts").read(), (self.root / "web/src/index.ts").read_bytes())
            self.assertEqual(json.loads(archive.extractfile("package/package.json").read())["version"], self.v)
            self.assertEqual(archive.extractfile("package/SOURCE.txt").read().decode(), self.url)

    def test_index_and_checksums_match_shipped_files(self):
        addon = ET.parse(self.files / "addons.xml").getroot().find("addon")
        self.assertEqual((addon.get("id"), addon.get("version")), ("script.module.oriel.framework", self.v))
        self.assertEqual(addon.find("requires/import[@addon='script.module.oriel.webkit']").get("version"), self.w)
        zip_name = f"script.module.oriel.framework-{self.v}.zip"
        self.assertEqual(addon.findtext("extension/path"), zip_name)
        for name in (zip_name, "addons.xml"):
            digest = hashlib.sha256((self.files / name).read_bytes()).hexdigest()
            self.assertEqual((self.files / (name + ".sha256")).read_text(), f"{digest}  {name}\n")
        self.assertEqual(sorted(p.name for p in self.files.iterdir()), sorted([
            zip_name, zip_name + ".sha256", "addons.xml", "addons.xml.sha256",
            f"oriel-web-{self.v}.tgz", f"oriel-framework-{self.v}-sources.tar.xz",
        ]))

    def test_source_archive_contains_framework_and_license_texts(self):
        with tarfile.open(self.files / f"{self.source.name}.tar.xz") as archive:
            self.assertEqual(archive.extractfile(f"{self.source.name}/licenses/cargo/example-1.0/LICENSE").read(), b"license text\n")
            self.assertEqual(archive.extractfile(f"{self.source.name}/oriel-framework/crates/shell/src/main.rs").read(),
                             (self.root / "crates/shell/src/main.rs").read_bytes())
