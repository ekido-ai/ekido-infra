"""Check Caddy's actual routing with local stub upstreams, without external requests."""
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'service': self.server.service, 'path': self.path}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(os.environ.get('CADDY_BIN'), 'set CADDY_BIN to the Caddy executable')
class CaddyRoutes(unittest.TestCase):
    def test_inference_paths_are_routed_without_capturing_other_models(self):
        services = {}
        for name in ('platform', 'inference', 'coordinator'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
            server.service = name
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            services[name] = server.server_port
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        text = (Path(__file__).resolve().parents[1] / 'Caddyfile').read_text(encoding='utf-8')
        text = text.replace('{$API_HOST:api.ekido.xyz}, {$SITE_HOST:ekido.xyz}', f'http://127.0.0.1:{port}')
        for original, name in ((8010, 'platform'), (8020, 'inference'), (8003, 'coordinator')):
            text = text.replace(f'127.0.0.1:{original}', f'127.0.0.1:{services[name]}')
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'Caddyfile'
            config.write_text('{\n admin off\n}\n' + text, encoding='utf-8')
            env = {**os.environ, 'XDG_DATA_HOME': temp, 'XDG_CONFIG_HOME': temp}
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            process = subprocess.Popen([os.environ['CADDY_BIN'], 'run', '--config', str(config)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=flags)
            try:
                origin = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(origin + '/sepolia/v1/me', timeout=1) as response:
                            json.load(response)
                        break
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None:
                            self.fail('Caddy exited before it was ready')
                        time.sleep(.1)
                else:
                    self.fail('Caddy did not start')
                cases = [('/sepolia/v1/models/mdl_123/predict', 'inference', '/v1/models/mdl_123/predict'),
                         ('/sepolia/v1/chat/completions', 'inference', '/v1/chat/completions'),
                         ('/sepolia/v1/models/mdl_123/savings-report', 'platform', '/v1/models/mdl_123/savings-report'),
                         ('/sepolia/v1/me', 'platform', '/v1/me'),
                         ('/sepolia/jobs', 'coordinator', '/jobs')]
                for path, service, expected in cases:
                    with self.subTest(path=path), urllib.request.urlopen(origin + path, timeout=3) as response:
                        self.assertEqual(json.load(response), {'service': service, 'path': expected})
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
