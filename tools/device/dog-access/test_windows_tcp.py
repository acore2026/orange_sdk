import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent


class RelayTests(unittest.TestCase):
    def test_binary_stream_and_half_close(self):
        payload = bytes(range(256)) * 512
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(10)
            errors = []

            def echo():
                try:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(10)
                        chunks = []
                        while True:
                            data = conn.recv(65536)
                            if not data:
                                break
                            chunks.append(data)
                        conn.sendall(b''.join(chunks) + b'\x00DONE\xff')
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=echo, daemon=True)
            thread.start()
            result = subprocess.run([sys.executable, str(ROOT / 'windows_tcp.py'),
                                     '--host', '127.0.0.1', '--port', str(server.getsockname()[1])],
                                    input=payload, capture_output=True, timeout=15)
            thread.join(11)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload + b'\x00DONE\xff')

    def test_missing_destination_and_invalid_port(self):
        for args in [[], ['--host', '127.0.0.1', '--port', '0'],
                     ['--host', '127.0.0.1', '--port', '65536']]:
            result = subprocess.run([sys.executable, str(ROOT / 'windows_tcp.py'), *args], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_wrapper_passes_overridden_target_and_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='relay test ') as temp:
            folder = Path(temp)
            python = folder / 'windows python.exe'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            python.chmod(0o755)
            wslpath = folder / 'wslpath'
            wslpath.write_text('#!/bin/sh\nprintf "%s\\n" "$2"\n')
            wslpath.chmod(0o755)
            result = subprocess.run(['bash', str(ROOT / 'windows-tcp'), '--host', '192.0.2.31', '--port', '40033'],
                                    env=dict(os.environ, WINDOWS_PYTHON=str(python), PATH=temp+os.pathsep+os.environ['PATH']),
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['-u', str(ROOT / 'windows_tcp.py'), '--host', '192.0.2.31', '--port', '40033'])


if __name__ == '__main__':
    unittest.main()
