import importlib.util
from pathlib import Path
import unittest
import tempfile

spec=importlib.util.spec_from_file_location('dog_core_config', Path(__file__).parents[1]/'dog_core_config.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class CoreConfigTest(unittest.TestCase):
    def test_atomic_replace_keeps_existing_ownership_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'launcher.sh'
            path.write_text('before')
            path.chmod(0o750)
            before=path.stat()
            m.atomic_write(path,'after',before.st_mode & 0o777)
            after=path.stat()
            self.assertEqual('after',path.read_text())
            self.assertEqual((before.st_uid,before.st_gid,0o750),
                             (after.st_uid,after.st_gid,after.st_mode & 0o777))
    def test_invalid_hosts_rejected(self):
        for value in ['x; reboot','https://core:8443','core:8443','999.1.2.3','a..b','-bad','a/b','']:
            with self.subTest(value=value),self.assertRaises(ValueError): m.validate_host(value)
        self.assertEqual('core.example.org',m.validate_host('Core.Example.org'))
    def test_launcher_must_have_source_controlled_config_hook(self):
        text='#!/bin/bash\n# ORANGE_DEMO_CORE_CONFIG:\nsource /etc/orange-demo/core.env\ncmd=(--runtime-ip "$DOG_AGENT_RUNTIME_IP")\n'
        m.validate_launcher(text)
        with self.assertRaises(ValueError):
            m.validate_launcher('#!/bin/bash\nunknown logic\n')
if __name__=='__main__':unittest.main()
