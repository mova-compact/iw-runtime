"""Minimal allowlisting HTTP/HTTPS CONNECT proxy for sandbox sidecars."""

import argparse
import select
import socket
import socketserver
from urllib.parse import urlsplit

MAX_HEADER_BYTES = 64 * 1024


def normalize_host(value: str) -> str:
    return value.rstrip(".").lower()


def host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    return normalize_host(host) in {normalize_host(item) for item in allowed_hosts}


def parse_target(method: str, target: str) -> tuple[str, int, str]:
    if method == "CONNECT":
        host, separator, port = target.rpartition(":")
        if not separator or not host:
            raise ValueError("CONNECT target must be host:port")
        return host.strip("[]"), int(port), target
    parsed = urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("proxy request requires an absolute HTTP URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    origin_target = parsed.path or "/"
    if parsed.query:
        origin_target += "?" + parsed.query
    return parsed.hostname, port, origin_target


class ProxyHandler(socketserver.StreamRequestHandler):
    allowed_hosts: set[str] = set()
    # Do not prefetch a request body into a BufferedReader before relay starts.
    rbufsize = 0

    def handle(self) -> None:
        self.connection.settimeout(20)
        request_line = self.rfile.readline(MAX_HEADER_BYTES + 1)
        if not request_line or len(request_line) > MAX_HEADER_BYTES:
            return
        try:
            method, target, version = request_line.decode("latin-1").strip().split(" ", 2)
            headers = self._read_headers()
            host, port, origin_target = parse_target(method.upper(), target)
        except (ValueError, UnicodeError):
            self._respond(400, "Bad Request")
            return
        if not host_allowed(host, self.allowed_hosts):
            self._respond(403, "Forbidden")
            return
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError:
            self._respond(502, "Bad Gateway")
            return
        with upstream:
            if method.upper() == "CONNECT":
                self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                filtered = [
                    line for line in headers
                    if not line.lower().startswith((b"proxy-connection:", b"proxy-authorization:"))
                ]
                upstream.sendall(
                    f"{method} {origin_target} {version}\r\n".encode("latin-1")
                    + b"".join(filtered) + b"\r\n"
                )
            self._relay(upstream)

    def _read_headers(self) -> list[bytes]:
        headers = []
        total = 0
        while True:
            line = self.rfile.readline(MAX_HEADER_BYTES + 1)
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            if line in (b"\r\n", b"\n", b""):
                return headers
            headers.append(line)

    def _relay(self, upstream: socket.socket) -> None:
        sockets = [self.connection, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                return
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)

    def _respond(self, status: int, reason: str) -> None:
        self.connection.sendall(
            f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode()
        )


class ThreadingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    ProxyHandler.allowed_hosts = set(args.allow_host)
    with ThreadingProxy(("0.0.0.0", args.port), ProxyHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
