import codecs
import json
from unittest.mock import Mock, patch

from .repository import RepositoryTests, load


class DeployTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.env["BOX"] = "test-target"
        self.env["TEST_STATE"] = str(self.temporary)
        self.executable("ssh", '''#!/usr/bin/env python3
import os,sys
from pathlib import Path
root=Path(os.environ['TEST_STATE'])
cmd=' '.join(sys.argv[1:])
if 'kodirpc.py --enable' in cmd:
    with (root/'enable-calls').open('a') as f: f.write(cmd+'\\n')
    if (root/'fail-enable').exists(): sys.exit(1)
''')
        self.executable("rsync", '''#!/usr/bin/env python3
import os,sys
from pathlib import Path
if sys.argv[-1].endswith('script.module.oriel.framework/'):
    root=Path(os.environ['TEST_STATE'])
    (root/'installed').touch()
    if (root/'fail-transfer').exists(): sys.exit(1)
''')
        binary = self.root / ".cache/runtime/script.module.oriel.framework/bin/oriel-shell"
        binary.parent.mkdir(parents=True)
        binary.write_text("shell")

    def test_retry_after_failed_activation_enables_again(self):
        failure = self.temporary / "fail-enable"
        failure.touch()
        self.command("tools/deploy.sh", success=False)
        self.assertTrue((self.temporary / "installed").exists())
        failure.unlink()
        self.assertIn("installed script.module.oriel.framework", self.command("tools/deploy.sh"))
        self.assertEqual(len((self.temporary / "enable-calls").read_text().splitlines()), 2)

    def test_failed_transfer_does_not_report_success_or_enable(self):
        (self.temporary / "fail-transfer").touch()
        output = self.command("tools/deploy.sh", success=False)
        self.assertNotIn("installed script.module.oriel.framework", output)
        self.assertFalse((self.temporary / "enable-calls").exists())

    def test_example_is_enabled_too(self):
        self.command("tools/deploy.sh", "--example")
        calls = (self.temporary / "enable-calls").read_text()
        self.assertIn("script.module.oriel.framework", calls)
        self.assertIn("script.oriel.hello", calls)

    def test_enable_waits_for_registration_and_verifies_state(self):
        rpc = load("kodi_rpc", self.root / "tools/box/kodirpc.py")
        client = Mock()
        client.call.side_effect = [RuntimeError("not registered"), {"addon": {"enabled": False}}, "OK", {"addon": {"enabled": True}}]
        with patch.object(rpc, "Kodi", return_value=client), patch.object(rpc.time, "sleep") as sleep:
            rpc.enable("script.test")
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(client.call.call_args.args, ("Addons.GetAddonDetails", {"addonid": "script.test", "properties": ["enabled"]}))

    def test_enabled_addon_needs_no_change(self):
        rpc = load("kodi_rpc", self.root / "tools/box/kodirpc.py")
        client = Mock(call=Mock(return_value={"addon": {"enabled": True}}))
        with patch.object(rpc, "Kodi", return_value=client):
            rpc.enable("script.test")
        client.call.assert_called_once_with("Addons.GetAddonDetails", {"addonid": "script.test", "properties": ["enabled"]})

    def test_enable_timeout_is_a_failure(self):
        rpc = load("kodi_rpc", self.root / "tools/box/kodirpc.py")
        with patch.object(rpc, "Kodi", return_value=Mock(call=Mock(side_effect=RuntimeError("not ready")))), \
             patch.object(rpc.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(RuntimeError, "did not enable"):
                rpc.enable("script.test")

    def test_tcp_character_split_preserves_unicode(self):
        rpc = load("kodi_rpc", self.root / "tools/box/kodirpc.py")
        client = rpc.Kodi.__new__(rpc.Kodi)
        client.buf = ""
        client.dec = json.JSONDecoder()
        client.utf8 = codecs.getincrementaldecoder("utf-8")()
        client.sock = Mock(recv=Mock(side_effect=[b'{"result":"caf\xc3', b'\xa9","id":1}']))
        self.assertEqual(client.read(), {"result": "café", "id": 1})
