"""a 127.0.0.1 http server and a fake clock for client and pipeline tests. importing this refuses dns for any other host"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_real_getaddrinfo = socket.getaddrinfo


def _loopback_only(host, *args, **kwargs):
    if host not in ('127.0.0.1', 'localhost'):
        raise OSError(f"tests only talk to 127.0.0.1, refused {host}")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _loopback_only


class FakeClock:
    """time(), monotonic() and sleep() for BaTClient(clock=...); sleeping just moves time forward"""

    def __init__(self, wall: float = 1_790_000_000.0):
        self.wall = wall
        self.mono = 1_000.0
        self.sleeps = []

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.wall += seconds
        self.mono += seconds


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # clients hanging up mid-response are part of several tests
        pass


class LocalServer:
    """routes map a path (query ignored) to (status, headers, body), or to a function of the handler that returns
    one; a function returning None has written the response itself"""

    def __init__(self):
        self.routes = {}
        self.default = (404, {}, b'not found')
        self.hits = []
        self.connections = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                server.connections += 1

            def do_GET(self):
                server.hits.append(self.path)
                route = server.routes.get(self.path.split('?')[0], server.default)
                response = route(self) if callable(route) else route
                if response is None:
                    return
                status, headers, body = response
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if 'Content-Length' not in headers:
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = QuietServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def paths(self):
        return [hit.split('?')[0] for hit in self.hits]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port_url() -> str:
    """a loopback url nothing listens on, for connection-refused tests"""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"
