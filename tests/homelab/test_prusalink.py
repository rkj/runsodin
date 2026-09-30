"""Exercise actual connection routes against authentication-required HTTP server."""
import os, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, os.environ.get('ODIN_TEST_BACKEND', '/app/backend'))
os.environ['JWT_SECRET_KEY'] = 'offline-test-only-not-a-service-credential'
scratch = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + scratch.name + '/test.db'
from modules.printers.route_utils import TestConnectionRequest
from modules.printers.routes_crud import test_printer_connection
from modules.system.routes_setup import SetupTestPrinterRequest, setup_test_printer

class Handler(BaseHTTPRequestHandler):
    mode = 'key'
    def log_message(self, *args): pass
    def do_GET(self):
        if self.mode == 'key':
            ok = self.headers.get('X-Api-Key') == 'test-only-key'
        else:
            ok = self.headers.get('Authorization', '').startswith('Digest ')
        if not ok:
            self.send_response(401)
            if self.mode == 'digest':
                self.send_header('WWW-Authenticate', 'Digest realm="test", nonce="fixed-test-nonce", algorithm=MD5, qop="auth"')
            self.end_headers()
            return
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(b'{"api":"2.0.0"}')

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close()
    def run_routes(self, key):
        host = '127.0.0.1:' + str(self.server.server_port)
        # Production SSRF guard stays intact; permit only this local test fixture.
        with patch('modules.printers.routes_crud._check_ssrf_blocklist'), patch('modules.printers.route_utils._check_ssrf_blocklist'), patch('modules.system.routes_setup._validate_setup_phase'):
            yield test_printer_connection(TestConnectionRequest(api_type='prusalink',api_host=host,api_key=key), current_user={})
            yield setup_test_printer(SetupTestPrinterRequest(api_type='prusalink',api_host=host,api_key=key), http_request=None, db=None)
    def test_key_both_routes(self):
        Handler.mode='key'
        for r in self.run_routes('test-only-key'): self.assertTrue(r['success'],r)
    def test_wrong_key_both_routes(self):
        Handler.mode='key'
        for r in self.run_routes('wrong'): self.assertFalse(r['success']); self.assertIn('401',r['error'])
    def test_digest_challenge_both_routes(self):
        Handler.mode='digest'
        for r in self.run_routes('maker|test-only-password'): self.assertTrue(r['success'],r)
if __name__=='__main__': unittest.main(verbosity=2)
