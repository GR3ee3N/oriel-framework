import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib

from .repository import RepositoryTests


def fake_gh(args):
    store = Path(os.environ["TEST_GITHUB"])
    state = store / "state.json"
    data = json.loads(state.read_text())
    action, tag = args[1:3]
    directory = store / tag
    if action == "view":
        if tag not in data["releases"]:
            sys.exit(1)
        print(str(data["releases"][tag]).lower())
    elif action == "delete":
        shutil.rmtree(directory)
        del data["releases"][tag]
        if "--cleanup-tag" in args:
            subprocess.run(["git", "push", "-q", "origin", f":refs/tags/{tag}"], check=True, timeout=10)
    elif action == "create":
        # Model the draft left by gh after an interrupted upload.
        directory.mkdir()
        data["releases"][tag] = "--draft" in args
        state.write_text(json.dumps(data))
        for file in (Path(arg) for arg in args[3:] if not arg.startswith("-") and Path(arg).is_file()):
            if data.get("fail_upload") == file.name:
                sys.exit(1)
            shutil.copyfile(file, directory / file.name)
        data["created"] = data.get("created", 0) + 1
    else:
        raise AssertionError(args)
    state.write_text(json.dumps(data))


class ReleaseTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.build_tools()
        self.executable("gh", '''#!/usr/bin/env python3
import os,sys
sys.path.insert(0,os.environ['TEST_ROOT'])
from tests.test_release import fake_gh
fake_gh(sys.argv[1:])
''')
        checker = self.root / "build/check.sh"
        checker.write_text('''#!/bin/sh
set -eu
printf 'check\\n' >> "$ORIEL_CACHE/stages"
[ "${TEST_FAIL_CHECK:-}" != 1 ]
''')
        checker.chmod(0o755)
        self.github = self.temporary / "github"
        self.github.mkdir()
        self.state = self.github / "state.json"
        self.state.write_text(json.dumps({"releases": {}}))
        self.env["TEST_GITHUB"] = str(self.github)
        self.tag = f"v{self.v}"
        self.init_git()

    def release(self, success=True, **env):
        return self.command("tools/release.sh", "Release notes", success=success, **env)

    def data(self):
        return json.loads(self.state.read_text())

    def update(self, **changes):
        data = self.data()
        data.update(changes)
        self.state.write_text(json.dumps(data))

    def publish(self):
        releases = self.data()["releases"]
        releases[self.tag] = False
        self.update(releases=releases)

    def test_release_drafts_artifacts_from_committed_source(self):
        self.release()
        self.assertEqual(self.data()["releases"], {self.tag: True})
        head = self.command("git", "rev-parse", "HEAD")
        self.assertEqual((self.root / ".cache/release/COMMIT").read_text().strip(), head)
        self.assertIn(head, self.command("git", "ls-remote", "origin", f"refs/tags/{self.tag}^{{}}"))
        self.assertEqual(sorted(p.name for p in (self.github / self.tag).iterdir()),
                         sorted(p.name for p in (self.root / ".cache/release/files").iterdir()))
        for file in (self.root / ".cache/release/files").iterdir():
            self.assertEqual((self.github / self.tag / file.name).read_bytes(), file.read_bytes())
        with tarfile.open(self.github / self.tag / f"oriel-framework-{self.v}-sources.tar.xz") as archive:
            prefix = f"oriel-framework-{self.v}-sources"
            self.assertEqual(archive.extractfile(f"{prefix}/COMMIT").read().decode().strip(), head)
            self.assertEqual(archive.extractfile(f"{prefix}/oriel-framework/crates/shell/src/main.rs").read(),
                             (self.root / "crates/shell/src/main.rs").read_bytes())
            config = tomllib.loads(archive.extractfile(f"{prefix}/oriel-framework/.cargo/config.toml").read().decode())
            self.assertEqual(config["source"]["crates-io"]["replace-with"], "vendored-sources")
            self.assertEqual(config["source"]["vendored-sources"]["directory"], "vendor")
            self.assertEqual(archive.extractfile(f"{prefix}/oriel-framework/vendor/example-1.0/LICENSE").read(), b"Cargo license")
            self.assertEqual(archive.extractfile(f"{prefix}/oriel-framework/.cache/display/protocol/LICENSE").read(), b"Protocol license")
            self.assertIn(f"{prefix}/oriel-framework/.cache/display/oriel-display-{self.d}-protocol.tar.xz", archive.getnames())

    def test_dirty_checkout_and_dev_runtime_are_refused_before_build(self):
        (self.root / "CHANGES").write_text("uncommitted")
        self.assertIn("not clean", self.release(success=False))
        (self.root / "CHANGES").unlink()
        self.assertIn("unset WEBKIT_DEV", self.release(success=False, WEBKIT_DEV="dev"))
        self.assertIn("unset DISPLAY_DEV", self.release(success=False, DISPLAY_DEV="dev"))
        self.assertEqual(self.data()["releases"], {})
        self.assertFalse((self.root / ".cache/stages").exists())

    def test_failed_check_never_builds_or_uploads(self):
        self.release(success=False, TEST_FAIL_CHECK="1")
        self.assertEqual((self.root / ".cache/stages").read_text().splitlines(), ["check"])
        self.assertEqual(self.data()["releases"], {})
        self.assertEqual(self.command("git", "tag"), "")

    def test_interrupted_upload_is_replaced_on_retry(self):
        self.update(fail_upload=f"oriel-web-{self.v}.tgz")
        self.release(success=False)
        self.assertEqual(self.data()["releases"], {self.tag: True})
        self.update(fail_upload=None)
        self.release()
        self.assertEqual(self.data()["releases"], {self.tag: True})
        self.assertEqual(sorted(p.name for p in (self.github / self.tag).iterdir()),
                         sorted(p.name for p in (self.root / ".cache/release/files").iterdir()))

    def test_newer_commit_replaces_draft_but_failed_checks_preserve_it(self):
        self.release()
        old_head = self.command("git", "rev-parse", "HEAD")
        (self.root / "CHANGES").write_text("fix")
        self.commit("Fix")
        self.release(success=False, TEST_FAIL_CHECK="1")
        self.assertIn(old_head, self.command("git", "ls-remote", "origin", f"refs/tags/{self.tag}^{{}}"))
        self.assertEqual(self.data()["created"], 1)
        self.release()
        head = self.command("git", "rev-parse", "HEAD")
        self.assertIn(head, self.command("git", "ls-remote", "origin", f"refs/tags/{self.tag}^{{}}"))
        self.assertEqual(self.data()["created"], 2)

    def test_draft_published_during_build_is_not_deleted(self):
        self.release()
        old_head = self.command("git", "rev-parse", "HEAD")
        (self.root / "CHANGES").write_text("new code")
        self.commit("Change")
        self.assertIn("no longer a draft", self.release(success=False, TEST_PUBLISH=self.tag))
        self.assertEqual(self.data()["releases"], {self.tag: False})
        self.assertEqual(self.data()["created"], 1)
        self.assertIn(old_head, self.command("git", "ls-remote", "origin", f"refs/tags/{self.tag}^{{}}"))

    def test_published_version_cannot_be_reused_from_another_commit(self):
        self.release()
        self.publish()
        self.assertIn("already released", self.release())
        self.assertEqual(self.data()["created"], 1)
        (self.root / "CHANGES").write_text("new code")
        self.commit("Change")
        self.assertIn("bump VERSION", self.release(success=False))
        versions = self.root / "build/versions.env"
        versions.write_text(versions.read_text().replace(f"\nVERSION={self.v}", "\nVERSION=1.99.0"))
        self.commit("Bump")
        self.release()
        self.assertEqual(self.data()["releases"], {self.tag: False, "v1.99.0": True})

    def test_uncommitted_change_during_build_prevents_upload(self):
        self.assertIn("source changed", self.release(success=False, TEST_CHANGE="dirty"))
        self.assertEqual(self.data()["releases"], {})
        self.assertEqual(self.command("git", "tag"), "")

    def test_commit_during_build_does_not_relabel_artifacts(self):
        head = self.command("git", "rev-parse", "HEAD")
        self.assertIn("source changed", self.release(success=False, TEST_CHANGE="commit"))
        self.assertEqual((self.root / ".cache/release/COMMIT").read_text().strip(), head)
        self.assertNotEqual(self.command("git", "rev-parse", "HEAD"), head)
        self.assertEqual(self.data()["releases"], {})
