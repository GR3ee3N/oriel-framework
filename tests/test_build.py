import shutil

from .repository import RepositoryTests, load


class BuildTests(RepositoryTests):
    def test_check_discovers_failing_tests_and_stops_before_web_tests(self):
        tests = self.root / "tests"
        shutil.rmtree(tests)
        tests.mkdir()
        (tests / "__init__.py").touch()
        (tests / "test_failure.py").write_text('import unittest\nclass Regression(unittest.TestCase):\n    def test_failure(self): self.fail("regression reached")\n')
        self.executable("shellcheck", "#!/bin/sh\nexit 0\n")
        self.executable("npm", "#!/bin/sh\necho should-not-run\n")
        output = self.command("build/check.sh", success=False)
        self.assertIn("regression reached", output)
        self.assertNotIn("should-not-run", output)

    def test_shell_compatibility_checks_requirements_only(self):
        checker = load("check_shell", self.root / "build/check-shell.py")
        dev = self.temporary / "dev"
        dev.mkdir()
        (dev / "provided.txt").write_text("libWPEWebKit-2.0.so.1\nlibglib-2.0.so.0\n")
        (dev / "box.env").write_text("GLIBC=2.44\nGLIBCXX=3.4.36\nCXXABI=1.3.17\nGCC=16.0\nLIBRARIES=^(?:ld-linux.*|lib(?:c|gcc_s|EGL)\\.so(?:\\..*)?)$\n")
        def dynamic(*names):
            return "".join(f" (NEEDED) Shared library: [{name}]\n" for name in names)
        def needs(*versions):
            return "Version symbols section\n GLIBC_9.99\nVersion needs section '.gnu.version_r'\n" + "\n".join(versions)
        self.assertEqual(checker.problems(dynamic("libWPEWebKit-2.0.so.1", "libEGL.so.1", "libc.so.6"), needs("GLIBC_2.34", "GCC_3.0"), dev), [])
        self.assertEqual(checker.problems(dynamic("libsoup-3.0.so.0"), needs(), dev),
                         ["needs libsoup-3.0.so.0, which neither WebKit's add-on nor the box provides"])
        self.assertEqual(checker.problems(dynamic(), needs("GLIBC_2.45"), dev),
                         ["requires GLIBC_2.45; the box provides GLIBC_2.44"])
