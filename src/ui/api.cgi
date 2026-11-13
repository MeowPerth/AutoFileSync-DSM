#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFileSync CGI→daemon 适配器（部署为 /webman/3rdparty/autofilesync/api.cgi）

职责：采集 DSM 会话 + token，按 action 转发到守护进程的 unix socket。
  GET  status|config|about|logs|runs|browse
  POST job_save|job_delete|job_toggle|settings|run|cancel|adopt|validate|notify_test|browse_mkdir
POST 约定：请求体为 JSON {"action": "...", "payload": {...}}（兼容表单 action/payload）。
输出：恒为 HTTP 200 + JSON（成败在 body 的 ok/error 字段表达）。
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse

AUTH_CGI = os.environ.get("AFS_AUTH_CGI", "/usr/syno/synoman/webman/modules/authenticate.cgi")
SOCK = os.environ.get("AFS_SOCK", "/var/packages/autofilesync/var/http/autofilesync.sock")
TIMEOUT = 120

GET_ACTIONS = {"status", "config", "about", "logs", "runs", "browse"}
POST_ACTIONS = {"job_save", "job_delete", "job_toggle", "job_mode", "settings",
                "run", "cancel", "adopt", "validate", "notify_test", "browse_mkdir"}


def emit(obj, code=b"200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Status: " + code + b"\r\n")
    sys.stdout.buffer.write(b"Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    sys.stdout.buffer.write(body)


def fail(msg):
    emit({"ok": False, "error": msg})


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        http.client.HTTPConnection.__init__(self, "localhost")
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        s.connect(self._path)
        self.sock = s


def dsm_user():
    """官方机制：CGI 环境里执行 authenticate.cgi；有输出=已登录用户名。"""
    env = dict(os.environ)
    xff = os.environ.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        real = xff.split(",")[0].strip()
        if real:
            env["REMOTE_ADDR"] = real
    try:
        p = subprocess.run([AUTH_CGI], capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None, False
    return p.stdout.decode("utf-8", "replace").strip() or None, True


def read_body():
    try:
        n = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
    except ValueError:
        n = 0
    return sys.stdin.buffer.read(n) if n > 0 else b""


def main():
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    qs = os.environ.get("QUERY_STRING", "")
    user, cgi_ok = dsm_user()

    token = os.environ.get("HTTP_X_AFS_TOKEN", "").strip()
    if not token:
        m = re.search(r"afs_token=([0-9a-fA-F]{16,})", os.environ.get("HTTP_COOKIE", ""))
        if m:
            token = m.group(1)

    headers = {
        "X-AFS-User": user or "",
        "X-AFS-Token": token,
        "X-AFS-CGI-Auth-Available": "1" if cgi_ok else "0",
        "Connection": "close",
    }

    try:
        if method == "POST":
            raw = read_body()
            payload = {}
            action = ""
            if raw:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    if not isinstance(obj, dict):
                        fail("请求体必须是 JSON 对象")
                        return
                    action = obj.get("action", "") or ""
                    payload = obj.get("payload", {})
                except ValueError:
                    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                    action = (form.get("action") or [""])[0]
                    try:
                        payload = json.loads((form.get("payload") or ["{}"])[0])
                    except ValueError:
                        fail("payload 不是合法 JSON")
                        return
            else:
                form = urllib.parse.parse_qs(qs)
                action = (form.get("action") or [""])[0]
            if action not in POST_ACTIONS:
                fail("unknown action: %s" % (action or "(empty)"))
                return
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            conn = UnixConn(SOCK)
            conn.request("POST", "/api/" + action, body=body, headers=headers)
        else:
            form = urllib.parse.parse_qs(qs, keep_blank_values=True)
            action = (form.get("action") or [""])[0]
            if action not in GET_ACTIONS:
                fail("unknown action: %s" % (action or "(empty)"))
                return
            keep = {k: v for k, v in form.items() if k != "action"}
            path = "/api/" + action
            if keep:
                path += "?" + urllib.parse.urlencode(keep, doseq=True)
            conn = UnixConn(SOCK)
            conn.request("GET", path, headers=headers)

        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    except (OSError, http.client.HTTPException) as e:
        fail("daemon unreachable: %s" % e)
        return

    sys.stdout.buffer.write(b"Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
