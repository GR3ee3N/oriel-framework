import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="oriel-tools-test-")))
        self.root = self.temporary / "repo"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", ".cache", "node_modules", "__pycache__", "target", "vendor"))
        self.bin = self.temporary / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", TEST_ROOT=str(self.root))
        for name in ("WEBKIT_DEV", "ORIEL_CACHE"):
            self.env.pop(name, None)
        versions = dict(line.split("=", 1) for line in (self.root / "build/versions.env").read_text().splitlines()
                        if line and not line.startswith("#"))
        self.enterContext(patch.dict(os.environ, versions))
        self.v, self.w = versions["VERSION"], versions["WEBKIT_VERSION"]

    def executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def command(self, *args, success=True, cwd=None, **env):
        result = subprocess.run(args, cwd=cwd or self.root, env=dict(self.env, **env), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result.stdout.strip() + result.stderr.strip()

    def init_git(self):
        self.command("git", "init", "-q", "-b", "main")
        self.command("git", "config", "user.name", "Release test")
        self.command("git", "config", "user.email", "test@example.invalid")
        self.commit("Release inputs")
        origin = self.temporary / "origin.git"
        self.command("git", "init", "--bare", "-q", str(origin))
        self.command("git", "remote", "add", "origin", str(origin))

    def commit(self, message):
        self.command("git", "add", ".")
        self.command("git", "commit", "-qm", message)

    def package_inputs(self):
        package = load("oriel_package", self.root / "build/package.py")
        runtime = self.root / ".cache/runtime"
        addon = runtime / "script.module.oriel.framework"
        shutil.copytree(self.root / "addon", addon)
        (addon / "bin/oriel-shell").write_text("shell")
        xml = addon / "addon.xml"
        xml.write_text(xml.read_text().replace("@VERSION@", self.v).replace("@WEBKIT_VERSION@", self.w))
        licenses = self.root / ".cache/licenses"
        for name in ("rust/COPYRIGHT-library.html", "cargo/example-1.0/LICENSE"):
            path = licenses / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("license text\n")
        source = self.root / f".cache/sources/oriel-framework-{self.v}-sources"
        shutil.copytree(self.root, source / "oriel-framework", ignore=shutil.ignore_patterns(".git", ".cache"))
        return package, runtime, source, licenses

    def build_tools(self):
        self.executable("docker", '''#!/usr/bin/env python3
import os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
if args[0]=='build': sys.exit(0)
mounts=[args[i+1].split(':') for i,a in enumerate(args) if a=='-v']
root=Path(next(m[0] for m in mounts if m[1]=='/work'))
cache=Path(next(m[0] for m in mounts if m[1]=='/work/.cache'))
if root/'.cache' != cache:
    (root/'.cache').symlink_to(cache)
env=dict(os.environ)
for i,arg in enumerate(args):
    if arg=='-e':
        name,value=args[i+1].split('=',1); env[name]=value
for mount in mounts:
    if mount[1]=='/webkit-dev': env['WEBKIT_DEV']=mount[0]
command=args[args.index('oriel-framework-builder')+1:]
sys.exit(subprocess.run(command,cwd=root,env=env).returncode)
''')
        self.executable("rustup", "#!/bin/sh\nexit 0\n")
        rust = self.temporary / "rust"
        (rust / "share/doc/rust").mkdir(parents=True)
        (rust / "share/doc/rust/COPYRIGHT-library.html").write_text("Rust license")
        self.env["TEST_RUST"] = str(rust)
        self.executable("rustc", "#!/bin/sh\n[ -n \"$RUSTUP_HOME\" ] || exit 1\nprintf '%s\\n' \"$TEST_RUST\"\n")
        self.executable("cargo", '''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['TEST_ROOT'])
if sys.argv[1]=='build':
    with (root/'.cache/stages').open('a') as f: f.write('build\\n')
    binary=Path(os.environ['CARGO_TARGET_DIR'])/'aarch64-unknown-linux-gnu/release/oriel-shell'
    binary.parent.mkdir(parents=True,exist_ok=True)
    binary.write_text('shell')
    if os.environ.get('TEST_PUBLISH'):
        state=Path(os.environ['TEST_GITHUB'])/'state.json'
        data=json.loads(state.read_text()); data['releases'][os.environ['TEST_PUBLISH']]=False
        state.write_text(json.dumps(data))
    if os.environ.get('TEST_CHANGE'):
        (root/'CHANGES').write_text('changed during build')
        if os.environ['TEST_CHANGE']=='commit':
            subprocess.run(['git','add','.'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','During build'],cwd=root,check=True)
else:
    vendor=Path(sys.argv[-1]); crate=vendor/'example-1.0'
    crate.mkdir(parents=True); (crate/'LICENSE').write_text('Cargo license')
    print('[source.crates-io]\\nreplace-with = "vendored-sources"\\n[source.vendored-sources]\\ndirectory = "'+str(vendor)+'"')
''')
        self.executable("aarch64-linux-gnu-readelf", "#!/bin/sh\n[ \"$1\" != -h ] || echo 'Machine: AArch64'\nexit 0\n")
        dev = self.root / ".cache/dev"
        dev.mkdir(parents=True)
        (dev / "provided.txt").write_text("")
        (dev / "box.env").write_text("GLIBC=2.44\nGLIBCXX=3.4.36\nCXXABI=1.3.17\nGCC=16.0\nLIBRARIES=.*\n")
        archive = self.root / f".cache/webkit/oriel-webkit-{self.w}-dev.tar.xz"
        archive.parent.mkdir()
        with tarfile.open(archive, "w:xz") as tar:
            tar.add(dev, arcname="dev")
