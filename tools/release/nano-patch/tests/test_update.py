import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
while not (ROOT/'update.py').exists() and ROOT != ROOT.parent: ROOT=ROOT.parent
sys.path.insert(0,str(ROOT))
import update
import setup_audio

class UpdateTests(unittest.TestCase):
    def test_unconfirmed_or_active_service_is_rejected(self):
        for status in ('active', 'activating', 'deactivating', '', 'unknown'):
            with patch.object(update.subprocess,'run',return_value=Mock(stdout=status,returncode=3)):
                with self.assertRaises(RuntimeError):update.service_stopped()
        for status in ('inactive', 'failed'):
            with patch.object(update.subprocess,'run',return_value=Mock(stdout=status,returncode=3)), patch.object(Path,'glob',return_value=[]):
                update.service_stopped()

    def test_bundle_tamper_and_unlisted_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'manifest.json').write_text(json.dumps({'files':[]}))
            (root/'checksums.json').write_text(json.dumps({'manifest.json':update.digest(root/'manifest.json')}))
            update.verify_bundle(root)
            (root/'unexpected').write_text('extra')
            with self.assertRaises(RuntimeError):update.verify_bundle(root)
            (root/'unexpected').unlink()
            (root/'manifest.json').write_text('{"files":[],"tampered":true}')
            with self.assertRaises(RuntimeError):update.verify_bundle(root)

    def test_custom_body_address_rejected_before_network_or_target_access(self):
        with patch.object(sys,'argv',['setup_audio.py','--ip','10.0.0.2']), patch.object(setup_audio.update,'verify_bundle') as verify:
            with self.assertRaises(SystemExit) as caught:setup_audio.main()
            self.assertEqual(caught.exception.code,2)
            verify.assert_not_called()

    def test_hash_guard_rejects_local_drift_and_existing_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'a').write_text('old')
            m={'files':[{'path':'a','before':update.digest(root/'a')},{'path':'b','before':None}]}
            update.verify_base_files(root,m)
            (root/'a').write_text('custom')
            with self.assertRaises(RuntimeError):update.verify_base_files(root,m)
            (root/'a').write_text('old');(root/'b').write_text('existing')
            with self.assertRaises(RuntimeError):update.verify_base_files(root,m)

    def test_rejects_symlink_and_parent_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'link').symlink_to('/tmp')
            for name in ('../escape','/absolute','link/file'):
                with self.assertRaises((ValueError,RuntimeError)):update.destination(root,name)

    def test_env_merge_preserves_identity_network_and_other_settings(self):
        original='DOG_AGENT_NAME=Other-Dog\nDOG_AGENT_RUNTIME_IP=10.2.3.4\nCAMERA_ID=8\nexport DOG_WARNING_AUDIO_ID=old\nDOG_WARNING_DIR=/old\n# keep\n'
        values={'DOG_WARNING_AUDIO_ID':'new','DOG_WARNING_DIR':'/new'}
        merged=update.merged_env(original,values)
        self.assertIn('DOG_AGENT_RUNTIME_IP=10.2.3.4',merged)
        self.assertIn('DOG_AGENT_NAME=Other-Dog',merged)
        self.assertEqual(merged.count('DOG_WARNING_AUDIO_ID='),1)
        self.assertEqual(update.merged_env(merged,values),merged)

    def test_transaction_restores_bytes_mode_and_new_files_without_touching_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root';root.mkdir();backup=Path(d)/'backup'
            (root/'app').write_text('old');(root/'app').chmod(0o600)
            (root/'identity').write_text('unique-to-target')
            update.snapshot(root,backup,['app','new'])
            update.atomic_write(root/'app',b'new',0o755)
            (root/'new').write_text('added')
            update.restore_files(root,backup)
            self.assertEqual((root/'app').read_text(),'old')
            self.assertEqual((root/'app').stat().st_mode&0o777,0o600)
            self.assertFalse((root/'new').exists())
            self.assertEqual((root/'identity').read_text(),'unique-to-target')

    def test_damaged_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root';root.mkdir();backup=Path(d)/'backup'
            (root/'app').write_text('original')
            update.snapshot(root,backup,['app'])
            (backup/'files/app').write_text('damaged')
            with self.assertRaises(RuntimeError):update.restore_files(root,backup)

    def test_audio_list_and_duplicate_handling(self):
        entries=[{'CUSTOM_NAME':'desired','UNIQUE_ID':'one'},{'CUSTOM_NAME':'desired','UNIQUE_ID':'two'}]
        reply={'data':{'header':{'status':{'code':0}},'data':json.dumps({'audio_list':entries})}}
        output='SAVED_AUDIO_LIST '+json.dumps(reply)
        self.assertEqual(setup_audio.audio_entries(output,'SAVED_AUDIO_LIST '),entries)
        with self.assertRaises(RuntimeError):setup_audio.choose(entries,'desired')
        self.assertEqual(setup_audio.choose(entries,'desired','two'),'two')
        self.assertIsNone(setup_audio.choose(entries,'other'))
        with self.assertRaises(RuntimeError):setup_audio.choose([{'CUSTOM_NAME':'desired','UNIQUE_ID':'bad;cmd'}],'desired')

    def test_rejects_unsuccessful_audio_list(self):
        with self.assertRaises(RuntimeError):setup_audio.audio_entries('', 'SAVED_AUDIO_LIST ')
        reply={'data':{'header':{'status':{'code':1}},'data':'{}'}}
        with self.assertRaises(RuntimeError):setup_audio.audio_entries('SAVED_AUDIO_LIST '+json.dumps(reply),'SAVED_AUDIO_LIST ')

if __name__=='__main__':unittest.main()
