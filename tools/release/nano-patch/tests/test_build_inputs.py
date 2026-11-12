"""Packaging must not silently include site files or stale validation evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_patch
import finalize_patch


class BuildInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'orange_dog.py').write_text('print("committed")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT)

    def snapshot(self):
        return build_patch.snapshot_repository(self.repo, self.root / 'snapshot')

    def test_untracked_site_files_excluded(self):
        (self.repo / 'site-secret.json').write_text('local fixture')
        files, provenance = self.snapshot()
        self.assertEqual(set(files), {'orange_dog.py'})
        self.assertEqual(provenance['head'], self.git('rev-parse', 'HEAD').decode().strip())
        self.assertEqual(files['orange_dog.py'].read_bytes(), (self.repo / 'orange_dog.py').read_bytes())

    def test_unstaged_and_staged_drift_rejected(self):
        (self.repo / 'orange_dog.py').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'Tracked changes'):
            self.snapshot()
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'Tracked changes'):
            self.snapshot()

    def test_committed_symlink_rejected(self):
        (self.repo / 'site').symlink_to('/etc/hosts')
        self.git('add', '.')
        self.git('commit', '-qm', 'symlink fixture')
        with self.assertRaisesRegex(ValueError, 'regular tracked files'):
            self.snapshot()

    def test_overlay_requires_reason_and_records_hash(self):
        committed = self.repo / 'orange_dog.py'
        live = self.root / 'live.py'
        live.write_text('explicit field overlay')
        with self.assertRaisesRegex(ValueError, 'overlay-reason'):
            build_patch.select_live_dog(committed, live, None)
        _, record = build_patch.select_live_dog(committed, live, 'exported from test device')
        self.assertFalse(record['matches_repository_head'])
        self.assertEqual(record['sha256'], build_patch.sha(live))
        _, record = build_patch.select_live_dog(committed, None, None)
        self.assertTrue(record['matches_repository_head'])

    def test_sdk_wheel_must_match_committed_modules(self):
        src = self.repo / 'python/src/agent_sdk'
        src.mkdir(parents=True)
        (src / '__init__.py').write_text('VERSION = "fixture"\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'SDK fixture')
        wheel = self.root / 'sdk.whl'
        for changed in (False, True):
            with zipfile.ZipFile(wheel, 'w') as z:
                z.writestr('agent_sdk/__init__.py', b'changed' if changed else (src / '__init__.py').read_bytes())
                z.writestr('sdk.dist-info/METADATA', 'Name: agent-connect-sdk\nVersion: 0.17.11+orange.20261110\n')
            if changed:
                with self.assertRaisesRegex(ValueError, 'modules do not match'):
                    build_patch.verify_sdk_wheel(self.repo, wheel)
            else:
                build_patch.verify_sdk_wheel(self.repo, wheel)

    def evidence_fixture(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        (bundle / 'update.py').write_text('tested installer')
        harness = self.root / 'harness'
        harness.mkdir()
        for name in ('validate-overlay.sh', 'validate-in-chroot.py'):
            (harness / name).write_text('fixture')
        snapshot = self.root / 'inputs.json'
        snapshot.write_text(json.dumps(finalize_patch.validation_inputs(bundle, harness)))
        marker = 'VALIDATION_INPUTS_SHA256 ' + finalize_patch.sha(snapshot)
        return bundle, harness, snapshot, marker

    def test_validation_rejects_bundle_changed_after_tests(self):
        bundle, harness, snapshot, marker = self.evidence_fixture()
        finalize_patch.check_validation_inputs(bundle, harness, snapshot, marker)
        (bundle / 'update.py').write_text('untested replacement')
        with self.assertRaisesRegex(ValueError, 'changed since'):
            finalize_patch.check_validation_inputs(bundle, harness, snapshot, marker)

    def test_validation_rejects_unbound_log(self):
        bundle, harness, snapshot, _ = self.evidence_fixture()
        with self.assertRaisesRegex(ValueError, 'not bound'):
            finalize_patch.check_validation_inputs(bundle, harness, snapshot, 'old successful log')

    def test_validation_rejects_harness_changed_after_tests(self):
        bundle, harness, snapshot, marker = self.evidence_fixture()
        (harness / 'validate-in-chroot.py').write_text('different test')
        with self.assertRaisesRegex(ValueError, 'changed since'):
            finalize_patch.check_validation_inputs(bundle, harness, snapshot, marker)

    def test_finalize_cli_seals_fixture_with_bound_evidence(self):
        # Synthetic log exercises archive wiring; it is not hardware evidence.
        bundle, harness, snapshot, _ = self.evidence_fixture()
        snapshot.unlink()
        (bundle / 'validation').mkdir()
        (bundle / 'sources').mkdir()
        (bundle / 'manifest.json').write_text('{}')
        script = Path(finalize_patch.__file__)
        args = [sys.executable, str(script), '--bundle', str(bundle),
                '--harness-dir', str(harness), '--validation-inputs', str(snapshot)]
        marker = subprocess.check_output(args + ['--snapshot-validation'], text=True)
        log = self.root / 'validation.log'
        log.write_text(marker + ''.join('Ran %s tests in 0.1s\n\nOK\n' % n for n in (49, 51, 10, 3, 3))
                       + 'AUTOMATIC_FAILURE_ROLLBACK_PASS\nARM64_OFFLINE_PATCH_VALIDATION_PASSED\n')
        subprocess.run(args + ['--validation-log', str(log)], check=True, capture_output=True)
        results = json.loads((bundle / 'validation/results.json').read_text())
        self.assertEqual(results['input_snapshot_sha256'], finalize_patch.sha(snapshot))
        self.assertEqual(results['tested_files']['update.py'], finalize_patch.sha(bundle / 'update.py'))
        checks = json.loads((bundle / 'checksums.json').read_text())
        self.assertTrue(all(finalize_patch.sha(bundle / name) == digest for name, digest in checks.items()))
        with tarfile.open(bundle.with_suffix('.tar.gz')) as archive:
            self.assertIn('bundle/validation/inputs.json', archive.getnames())


if __name__ == '__main__':
    unittest.main()
