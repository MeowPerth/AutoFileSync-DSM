#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地开发服务器：静态托管 src/ui，并把 /api.cgi 转发到守护进程的 unix socket。

用途：在没有 DSM 的环境里调 UI（不模拟 DSM 登录，token 直接注入页面）。
    python3 tools/dev_server.py --var /tmp/afs-test/var --port 8765
然后浏览器打开 http://127.0.0.1:8765/index.html
"""
import argparse
import http.client
import json
import os
import posixpath
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.normpath(os.path.join(HERE, "..", "src", "ui"))

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".cgi": "text/plain; charset=utf-8",
}


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(self._path)
        self.sock = s


class Handler(BaseHTTPRequestHandler):
    var = ""
    ui = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("[dev] %s\n" % (fmt % args))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        try:
            with open(os.path.join(self.var, "token"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api.cgi":
            self._proxy(parsed.query)
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith(".."):
            self._send(403, b"forbidden", "text/plain")
            return
        full = os.path.join(self.ui, rel)
        if not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return
        with open(full, "rb") as f:
            body = f.read()
        if rel.endswith(".html"):
            body = body.replace(b"__AFS_TOKEN__", self._token().encode())
        ctype = MIME.get(os.path.splitext(full)[1], "application/octet-stream")
        self._send(200, body, ctype)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api.cgi":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
            return
        action = obj.get("action", "")
        payload = obj.get("payload", {})
        token = self._token()
        headers = {"X-AFS-Token": token, "Content-Type": "application/json",
                   "Connection": "close"}
        try:
            conn = UnixConn(os.path.join(self.var, "http", "autofilesync.sock"))
            conn.request("POST", "/api/" + action,
                         body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            data = ('{"ok": false, "error": "daemon unreachable: %s"}' % e).encode()
        self._send(200, data, "application/json; charset=utf-8")

    def _proxy(self, query):
        token = self._token()
        form = urllib.parse.parse_qs(query, keep_blank_values=True)
        path = "/api/" + (form.get("action") or [""])[0]
        keep = {k: v for k, v in form.items() if k != "action"}
        if keep:
            path += "?" + urllib.parse.urlencode(keep, doseq=True)
        headers = {"X-AFS-Token": token, "Connection": "close"}
        try:
            conn = UnixConn(os.path.join(self.var, "http", "autofilesync.sock"))
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            data = ('{"ok": false, "error": "daemon unreachable: %s"}' % e).encode()
        self._send(200, data, "application/json; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--var", default=os.environ.get("AFS_VAR", "/tmp/afs-test/var"))
    ap.add_argument("--ui", default=UI_DIR)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    Handler.var = os.path.abspath(args.var)
    Handler.ui = os.path.abspath(args.ui)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("dev server: http://127.0.0.1:%d/index.html  (var=%s)" % (args.port, Handler.var))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
