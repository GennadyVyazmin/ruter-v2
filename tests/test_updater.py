from pathlib import Path
import subprocess
import tempfile
import unittest

from ruter_v2.updater import FILES, UNITS, deploy


class UpdaterTests(unittest.TestCase):
    def test_code_only_deploy_and_rollback_without_service_restart(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, opt, var, units = [root / name for name in ('source', 'opt', 'var', 'units')]
                for path in (source, opt, var, units):
                    path.mkdir()
                for src, dst in FILES.items():
                    (source / src).parent.mkdir(parents=True, exist_ok=True)
                    (source / src).write_text('new')
                    (opt / dst).parent.mkdir(parents=True, exist_ok=True)
                    (opt / dst).write_text('old')
                (source / 'systemd').mkdir()
                for name in UNITS:
                    (source / 'systemd' / name).write_text('new')
                    (units / name).write_text('old')
                config = opt / 'config.json'
                config.write_text('unchanged subscription/ports')
                calls = []
                def run(*args):
                    calls.append(args)
                    if fail and len(calls) == 1:
                        raise subprocess.CalledProcessError(1, args)
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):
                        deploy(source, opt, var, units, run)
                else:
                    backup = deploy(source, opt, var, units, run)
                    self.assertTrue((backup / 'files.json').is_file())
                self.assertTrue(all(call == ('systemctl', 'daemon-reload') for call in calls))
                self.assertEqual(config.read_text(), 'unchanged subscription/ports')
                for dst in FILES.values():
                    self.assertEqual((opt / dst).read_text(), 'old' if fail else 'new')
