#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFileSync 守护进程（M2）

职责：
  · unix socket 上的 HTTP API（状态 / 配置 / 任务 CRUD / 运行 / 取消 / 历史 / 日志 / 目录浏览）
  · 内建调度器（手动 / 间隔 / 每天 / 每周）
  · rsync 执行器：经 SimplePermissionManager 的 spm-exec 取 root，未授权自动降级为包用户
  · 安全护栏：路径校验 / 卷挂载 / 源非空 / 目标标记 / 目标卷身份 / max-delete / 锁 / 超时
  · 运行历史与每次运行的独立日志

零第三方依赖：DSM 7 自带 python3 即可运行。
"""

import argparse
import datetime
import fcntl
import hmac
import json
import logging
import os
import pwd
import queue
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PKG_NAME = "autofilesync"
APP_ID = "MeowPerth.AutoFileSync"
PKG_VERSION = "1.0.0"
SCHEMA = 2

DEFAULT_VAR = "/var/packages/autofilesync/var"
SOCKET_NAME = "autofilesync.sock"
TOKEN_NAME = "token"
LOG_NAME = "autofilesync.log"
MARKER = ".autofilesync-target"
STATE_FILE = ".autofilesync-state"
TRASH_DIR = ".autofilesync-trash"

START_TS = time.time()
log = logging.getLogger("afs")

SPM_EXEC_CANDIDATES = ("/usr/local/bin/spm-exec", "/usr/syno/bin/spm-exec")

CAPS = (
    "ACLs", "xattrs", "hardlinks", "symlinks", "iconv",
    "64-bit files", "64-bit inums", "64-bit timestamps",
    "socketpairs", "inplace", "append", "batchfiles",
    "sendfile", "ASM optimizations", "SIMD optimizations",
)

INTERNAL_EXCLUDES = (MARKER, STATE_FILE, TRASH_DIR + "/")

SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/dev", "/proc",
    "/sys", "/root", "/boot", "/run", "/var", "/tmp",
)

DEFAULT_SETTINGS = {
    "concurrency": 1,
    "rsync_path": "",
    "run_missed_on_start": True,
    "log_retention_days": 30,
    "notify_on": "failure",
    "notify_users": ["@administrators"],
    "first_run_preview": True,
    "default_excludes": [
        "@eaDir/", "#recycle/", "#snapshot/", "@tmp/",
        ".DS_Store", "Thumbs.db", "*.sock", "*.pid",
    ],
}

JOB_DEFAULTS = {
    "enabled": False,
    "delete": True,
    "delete_excluded": False,
    "keep_newer": False,
    "dry_run": True,
    "exclude": [],
    "max_delete": 500,
    "rollback": True,
    "checksum": False,
    "bwlimit_kb": 0,
    "preserve_acls": True,
    "preserve_xattrs": True,
    "timeout_hours": 6,
    "adopted": False,
    "schedule": {"mode": "daily", "time": "03:30", "weekdays": [1, 2, 3, 4, 5, 6, 7]},
}

JOB_KEYS = (
    "id", "name", "source", "dest", "enabled", "delete", "delete_excluded",
    "keep_newer", "dry_run", "exclude", "max_delete", "rollback", "checksum", "bwlimit_kb",
    "preserve_acls", "preserve_xattrs", "timeout_hours", "adopted",
    "schedule", "created_at", "updated_at",
)

MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_BODY = 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message, errors=None, code=None):
        Exception.__init__(self, message)
        self.message = message
        self.errors = errors or []
        self.code = code


# ------------------------------------------------------------------ 通用 ----
def now_ts():
    return int(time.time())


def fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("读取 %s 失败：%s", path, e)
        return default


def deep_merge(base, override):
    """默认值 base + 用户值 override：用户值优先，缺失键用默认值补齐。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_hhmm(text, fallback="03:30"):
    m = re.match(r"^(\d{1,2}):(\d{2})$", (text or "").strip())
    if not m:
        m = re.match(r"^(\d{1,2}):(\d{2})$", fallback)
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        hh, mm = 3, 30
    return hh, mm


def schedule_text(job):
    sch = job.get("schedule") or {}
    mode = sch.get("mode", "manual")
    if mode == "interval":
        minutes = int(sch.get("minutes", 0) or 0)
        return "每 %d 分钟" % minutes if minutes > 0 else "间隔（未设置）"
    if mode in ("daily", "weekly"):
        days = sch.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]
        name = ["一", "二", "三", "四", "五", "六", "日"]
        hh, mm = parse_hhmm(sch.get("time"))
        hhmm = "%02d:%02d" % (hh, mm)
        if len(days) == 7:
            return "每天 " + hhmm
        try:
            picked = "、".join(name[int(d) - 1] for d in days if 1 <= int(d) <= 7)
        except (TypeError, ValueError):
            picked = "?"
        return "每周%s %s" % (picked, hhmm)
    return "手动"


def next_run_for(schedule, now=None):
    """返回下一次运行的 epoch；手动返回 None。"""
    now = now or time.time()
    sch = schedule or {}
    mode = sch.get("mode", "manual")
    if mode == "interval":
        try:
            minutes = int(sch.get("minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0
        return int(now + minutes * 60) if minutes > 0 else None
    if mode in ("daily", "weekly"):
        hh, mm = parse_hhmm(sch.get("time"))
        raw_days = sch.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]
        try:
            days = set(int(d) for d in raw_days if 1 <= int(d) <= 7)
        except (TypeError, ValueError):
            days = set(range(1, 8))
        if not days:
            days = set(range(1, 8))
        base = datetime.datetime.fromtimestamp(now)
        for offset in range(0, 9):
            day = base + datetime.timedelta(days=offset)
            if day.isoweekday() not in days:
                continue
            cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand.timestamp() > now + 30:
                return int(cand.timestamp())
    return None


# ------------------------------------------------------------------ 校验 ----
def norm_path(path):
    path = (path or "").strip()
    if not path:
        return ""
    return os.path.normpath(path)


def path_in_system(path):
    for prefix in SYSTEM_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    return None


def validate_job(job):
    errors = []
    warnings = []
    test_mode = os.environ.get("AFS_TEST_ALLOW") == "1"

    name = (job.get("name") or "").strip()
    if not name:
        errors.append("任务名称不能为空")
    elif len(name) > 64:
        errors.append("任务名称过长（最多 64 字符）")

    src = norm_path(job.get("source"))
    dst = norm_path(job.get("dest"))
    for label, p in (("源目录", src), ("目标目录", dst)):
        if not p:
            errors.append("%s不能为空" % label)
            continue
        if not p.startswith("/"):
            errors.append("%s必须是绝对路径" % label)
            continue
        if ".." in p.split("/"):
            errors.append("%s不能包含 .." % label)
        if p == "/":
            errors.append("%s不能是根目录" % label)
            continue
        if not test_mode:
            hit = path_in_system(p)
            if hit:
                errors.append("%s不能位于系统目录 %s 下" % (label, hit))

    if dst and re.match(r"^/volume\d+$", dst):
        errors.append("目标目录不能是卷根目录（如 /volume2），请指定子目录")
    for seg in (dst or "").split("/"):
        if seg.startswith(("@", "#")):
            errors.append("目标目录不能包含 @ / # 系统目录")
            break

    if src and dst:
        if src == dst:
            errors.append("源目录与目标目录不能相同")
        elif dst.startswith(src.rstrip("/") + "/"):
            errors.append("目标目录不能位于源目录内部")
        elif src.startswith(dst.rstrip("/") + "/"):
            warnings.append("源目录位于目标目录内部，可能造成递归同步")

    try:
        if int(job.get("max_delete", 0) or 0) < 0:
            errors.append("删除上限不能为负数")
    except (TypeError, ValueError):
        errors.append("删除上限必须是整数")

    try:
        if int(job.get("bwlimit_kb", 0) or 0) < 0:
            errors.append("带宽限制不能为负数")
    except (TypeError, ValueError):
        errors.append("带宽限制必须是整数")

    try:
        timeout = float(job.get("timeout_hours", 6) or 6)
        if timeout <= 0 or timeout > 168:
            errors.append("超时需在 0–168 小时之间")
    except (TypeError, ValueError):
        errors.append("超时必须是数字")

    sch = job.get("schedule") or {}
    mode = sch.get("mode", "manual")
    if mode not in ("manual", "interval", "daily", "weekly"):
        errors.append("调度模式无效")
    elif mode == "interval":
        try:
            minutes = int(sch.get("minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0
        if not (1 <= minutes <= 10080):
            errors.append("间隔需在 1–10080 分钟之间")
    elif mode in ("daily", "weekly"):
        text = (sch.get("time") or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", text):
            errors.append("执行时间格式应为 HH:MM")
        else:
            hh, mm = parse_hhmm(text)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                errors.append("执行时间无效")
        days = sch.get("weekdays") or []
        if days:
            try:
                if not all(1 <= int(d) <= 7 for d in days):
                    errors.append("星期取值应为 1–7")
            except (TypeError, ValueError):
                errors.append("星期取值应为 1–7")

    exclude = job.get("exclude") or []
    if not isinstance(exclude, list):
        errors.append("排除规则必须是数组")
    elif len(exclude) > 200:
        errors.append("排除规则过多（最多 200 条）")
    else:
        for item in exclude:
            if not isinstance(item, str):
                errors.append("排除规则必须是字符串")
                break

    return errors, warnings


def normalize_job(payload):
    job = {}
    for key in JOB_KEYS:
        if key in payload:
            job[key] = payload[key]
    job["name"] = (job.get("name") or "").strip()
    job["source"] = norm_path(job.get("source"))
    job["dest"] = norm_path(job.get("dest"))
    for key in ("enabled", "delete", "delete_excluded", "keep_newer", "dry_run",
                "rollback", "checksum", "preserve_acls", "preserve_xattrs", "adopted"):
        job[key] = bool(job.get(key, JOB_DEFAULTS[key]))
    for key in ("max_delete", "bwlimit_kb"):
        try:
            job[key] = int(job.get(key, JOB_DEFAULTS[key]) or 0)
        except (TypeError, ValueError):
            job[key] = JOB_DEFAULTS[key]
    try:
        job["timeout_hours"] = float(job.get("timeout_hours", 6) or 6)
    except (TypeError, ValueError):
        job["timeout_hours"] = 6
    excludes = job.get("exclude") or []
    if not isinstance(excludes, list):
        excludes = []
    job["exclude"] = [str(x).strip() for x in excludes if str(x).strip()]
    sch = job.get("schedule") or {"mode": "daily"}
    if not isinstance(sch, dict):
        sch = {"mode": "daily"}
    mode = sch.get("mode", "daily")
    if mode not in ("interval", "daily", "weekly"):
        mode = "daily"
    clean = {"mode": mode}
    if mode == "interval":
        try:
            clean["minutes"] = int(sch.get("minutes", 60) or 60)
        except (TypeError, ValueError):
            clean["minutes"] = 60
    if mode in ("daily", "weekly"):
        hh, mm = parse_hhmm(sch.get("time"))
        clean["time"] = "%02d:%02d" % (hh, mm)
        days = sch.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]
        try:
            clean["weekdays"] = sorted(set(int(d) for d in days if 1 <= int(d) <= 7))
        except (TypeError, ValueError):
            clean["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
        if not clean["weekdays"]:
            clean["weekdays"] = [1, 2, 3, 4, 5, 6, 7]
    job["schedule"] = clean
    return job


# ------------------------------------------------------------------ 配置 ----
class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {}
        self._mtime = None
        self.reload(force=True)

    def _bootstrap(self):
        data = {"schema": SCHEMA, "settings": dict(DEFAULT_SETTINGS), "jobs": []}
        atomic_write_json(self.path, data)
        log.info("未找到配置，已生成空配置：%s", self.path)
        return data

    def reload(self, force=False):
        mtime = None
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            pass
        with self.lock:
            if not force and mtime is not None and mtime == self._mtime:
                return False
            if mtime is None:
                self.data = self._bootstrap()
            else:
                raw = load_json(self.path, {}) or {}
                data = {
                    "schema": int(raw.get("schema", SCHEMA)),
                    "settings": deep_merge(DEFAULT_SETTINGS, raw.get("settings") or {}),
                    "jobs": [],
                }
                for job in raw.get("jobs") or []:
                    item = dict(JOB_DEFAULTS)
                    item.update(job)
                    item.setdefault("id", "j-%s" % uuid.uuid4().hex[:8])
                    sch = item.get("schedule") or {}
                    if not isinstance(sch, dict) or sch.get("mode") not in ("interval", "daily", "weekly"):
                        # 旧配置的「手动」迁移为每天 03:30；同时停用，避免升级后突然自动跑
                        item["schedule"] = {"mode": "daily", "time": "03:30",
                                            "weekdays": [1, 2, 3, 4, 5, 6, 7]}
                        item["enabled"] = False
                    data["jobs"].append(item)
                self.data = data
            self._mtime = mtime
            return True

    def get(self):
        with self.lock:
            return self.data

    def jobs(self):
        return self.get().get("jobs") or []

    def find(self, job_id):
        for job in self.jobs():
            if job.get("id") == job_id:
                return job
        return None

    def save(self, data):
        with self.lock:
            atomic_write_json(self.path, data)
            self.data = data
            try:
                self._mtime = os.stat(self.path).st_mtime_ns
            except OSError:
                self._mtime = None


# ------------------------------------------------------------------ 状态 ----
class State:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = load_json(path, {}) or {}
        self.data.setdefault("jobs", {})
        self.data.setdefault("history", [])

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def update(self, fn):
        with self.lock:
            fn(self.data)
            try:
                atomic_write_json(self.path, self.data)
            except OSError as e:
                log.warning("写入 state 失败：%s", e)
            return self.data

    def save(self):
        with self.lock:
            try:
                atomic_write_json(self.path, self.data)
            except OSError as e:
                log.warning("写入 state 失败：%s", e)


# ------------------------------------------------------------------ rsync ----
def find_rsync(preferred=""):
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates += ["/usr/bin/rsync", "/bin/rsync", "/usr/local/bin/rsync"]
    which = shutil.which("rsync")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def probe_rsync(path):
    info = {"path": path or "", "ok": False, "version": "", "protocol": "",
            "capabilities": {}, "error": ""}
    if not path:
        info["error"] = "未找到 rsync 可执行文件"
        return info
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = "执行失败：%s" % e
        return info
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = text.splitlines()
    if lines:
        m = re.search(r"version\s+([0-9][0-9A-Za-z.+-]*)", lines[0])
        if m:
            info["version"] = m.group(1)
        m = re.search(r"protocol version\s+(\d+)", lines[0])
        if m:
            info["protocol"] = m.group(1)
    info["capabilities"] = {cap: (cap.lower() in text.lower()) for cap in CAPS}
    info["ok"] = bool(info["version"])
    if not info["ok"]:
        info["error"] = "无法解析版本号"
    return info


def find_spm_exec():
    for path in SPM_EXEC_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def probe_spm(path=None):
    info = {"path": "", "available": False, "authorized": False,
            "mode": "package", "detail": ""}
    spm = path or find_spm_exec()
    info["path"] = spm or ""
    if not spm:
        info["detail"] = "未安装 SimplePermissionManager（找不到 spm-exec）"
        return info
    info["available"] = True
    try:
        proc = subprocess.run([spm, "id"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as e:
        info["detail"] = "spm-exec 执行失败：%s" % e
        return info
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0 and re.search(r"uid=0\b", out):
        info["authorized"] = True
        info["mode"] = "root"
        info["detail"] = "经 SimplePermissionManager 授权 root"
    else:
        info["detail"] = out.splitlines()[-1] if out else "未授权（permission denied）"
    return info


# ------------------------------------------------------------------ 特权 ----
class Priv:
    def __init__(self, spm, root):
        self.spm = spm
        self.root = bool(root)

    def wrap(self, argv):
        if self.root and self.spm:
            return [self.spm] + list(argv)
        return list(argv)

    def run(self, argv, **kwargs):
        kwargs.setdefault("capture_output", True)
        return subprocess.run(self.wrap(argv), **kwargs)

    def write_text(self, path, text):
        if self.root:
            try:
                p = self.run(["sh", "-c", 'cat > "$1"', "sh", path],
                             input=text.encode("utf-8"), timeout=15)
                return p.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return True
        except OSError:
            return False

    def makedirs(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            return True
        except OSError:
            if self.root:
                try:
                    return self.run(["mkdir", "-p", path]).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    return False
            return False

    def rmtree(self, path):
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            if self.root:
                try:
                    return self.run(["rm", "-rf", path]).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    return False
            return False


# ------------------------------------------------------------------ 工具 ----
def on_mounted_volume(path):
    if os.environ.get("AFS_TEST_ALLOW") == "1":
        return True
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            mounts = []
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.append(parts[1].replace("\\040", " "))
    except OSError:
        return True
    target = os.path.realpath(path)
    best = ""
    for mp in mounts:
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            if len(mp) > len(best):
                best = mp
    return bool(best) and bool(re.match(r"^/volume\d+$", best))


def dir_empty(path):
    """True/False；无法读取时返回 None。"""
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return None


def dir_empty_priv(priv, path):
    """同 dir_empty，但权限不足时尝试用 root（spm-exec）读。"""
    result = dir_empty(path)
    if result is not None:
        return result
    if priv is not None and priv.root:
        try:
            p = priv.run(["find", path, "-mindepth", "1", "-maxdepth", "1",
                          "-print", "-quit"])
            if p.returncode == 0:
                return not (p.stdout or b"").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def tail_text(path, limit=200000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - limit)
            f.seek(start)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def tail_lines(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block, data = 8192, b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
    except OSError as e:
        return {"ok": False, "error": str(e), "lines": [], "path": path}
    lines = data.decode("utf-8", "replace").splitlines()
    return {"ok": True, "path": path, "lines": lines[-n:]}


def parse_stats(path):
    text = tail_text(path)
    out = {}
    patterns = {
        "files": r"Number of files",
        "transferred": r"Number of regular files transferred",
        "deleted": r"Number of deleted files",
        "bytes_total": r"Total file size",
        "bytes_transferred": r"Total transferred file size",
        "bytes_sent": r"Total bytes sent",
    }
    for key, pat in patterns.items():
        m = re.search(pat + r"[:\s]+([\d,\.]+)", text, re.M)
        if m:
            raw = m.group(1).replace(",", "").split(".")[0]
            try:
                out[key] = int(raw)
            except ValueError:
                pass
    m = re.search(r"speedup is ([\d\.,]+)", text)
    if m:
        try:
            out["speedup"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return out


# ------------------------------------------------------------------ 执行 ----
class Runner:
    def __init__(self, ctx):
        self.ctx = ctx
        self.var = ctx["var"]
        self.q = queue.Queue()
        self.lock = threading.RLock()
        self.active = {}
        self.queued = []
        self.stopping = threading.Event()
        self.dispatcher = None
        self.threads = set()
        self.cancel_events = {}

    def start(self):
        self.dispatcher = threading.Thread(target=self._dispatch, name="afs-runner", daemon=True)
        self.dispatcher.start()

    def stop(self):
        self.stopping.set()
        for ev in list(self.cancel_events.values()):
            ev.set()
        self.q.put(None)
        if self.dispatcher:
            self.dispatcher.join(timeout=5)
        deadline = time.time() + 10
        with self.lock:
            threads = list(self.threads)
        for t in threads:
            remain = deadline - time.time()
            if remain <= 0:
                break
            t.join(timeout=remain)

    def _concurrency(self):
        try:
            settings = self.ctx["config"].get().get("settings") or {}
            return max(1, min(3, int(settings.get("concurrency", 1) or 1)))
        except (TypeError, ValueError):
            return 1

    def enqueue(self, job_id, dry_run, reason="manual"):
        with self.lock:
            if job_id in self.active or job_id in self.queued:
                raise ApiError("该任务正在运行或排队中")
            self.queued.append(job_id)
        self.q.put({"id": job_id, "dry_run": bool(dry_run), "reason": reason})
        return True

    def cancel(self, job_id):
        with self.lock:
            if job_id in self.cancel_events:
                self.cancel_events[job_id].set()
                return True
            if job_id in self.queued:
                self.queued.remove(job_id)
                return True
        raise ApiError("该任务没有在运行")

    def is_active(self, job_id):
        with self.lock:
            return job_id in self.active or job_id in self.queued

    def status(self):
        with self.lock:
            now = time.time()
            running = []
            for jid, info in self.active.items():
                item = dict(info)
                item["id"] = jid
                item["elapsed"] = int(now - info.get("started", now))
                running.append(item)
            return {"running": running[0] if running else None,
                    "running_jobs": running,
                    "queued": list(self.queued)}

    def _dispatch(self):
        while not self.stopping.is_set():
            limit = self._concurrency()
            with self.lock:
                slots = limit - len(self.active)
            if slots <= 0:
                self.stopping.wait(0.3)
                continue
            try:
                item = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if item is None:
                break
            with self.lock:
                if item["id"] not in self.queued:
                    continue
                self.queued.remove(item["id"])
                job = self.ctx["config"].find(item["id"])
                self.active[item["id"]] = {
                    "id": item["id"],
                    "name": (job or {}).get("name") or item["id"],
                    "dry_run": bool(item.get("dry_run")),
                    "started": now_ts(),
                    "files": 0,
                    "last_file": "",
                    "reason": item.get("reason", "manual"),
                    "log": "",
                }
                self.cancel_events[item["id"]] = threading.Event()
            t = threading.Thread(target=self._run_guarded, args=(item,), daemon=True)
            with self.lock:
                self.threads.add(t)
            t.start()

    def _run_guarded(self, item):
        try:
            self._run(item)
        except Exception:
            log.exception("任务执行异常：%s", item)
        finally:
            with self.lock:
                self.active.pop(item["id"], None)
                self.cancel_events.pop(item["id"], None)
                self.threads.discard(threading.current_thread())

    def _set_active(self, job_id, info):
        with self.lock:
            if info is None:
                self.active.pop(job_id, None)
                self.cancel_events.pop(job_id, None)
            else:
                self.active[job_id] = info

    def _run(self, item):
        job_id = item["id"]
        self.ctx["priv"] = self.ctx["get_priv"]()
        job = self.ctx["config"].find(job_id)
        if not job:
            log.warning("任务不存在：%s", job_id)
            return
        dry_run = bool(item.get("dry_run"))
        reason = item.get("reason", "manual")
        started = now_ts()
        logdir = os.path.join(self.var, "logs", job_id)
        os.makedirs(logdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
        sid = "%s-%s" % (stamp, uuid.uuid4().hex[:4])
        log_path = os.path.join(logdir, sid + ".log")
        rel_log = os.path.join("logs", job_id, sid + ".log")
        info = {"id": job_id, "name": job.get("name") or job_id,
                "dry_run": dry_run, "started": started, "files": 0,
                "last_file": "", "reason": reason, "log": rel_log}
        self._set_active(job_id, info)
        cancel_ev = self.cancel_events.get(job_id) or threading.Event()
        with self.lock:
            self.cancel_events[job_id] = cancel_ev

        self.ctx["state"].update(lambda d: d.setdefault("jobs", {}).setdefault(
            job_id, {}).update({"running": True, "started": started}))
        self._log_line(log_path, "==== AutoFileSync run %s ====" % fmt_ts(started))
        self._log_line(log_path, "job=%s (%s) mode=%s dry_run=%s reason=%s" % (
            job_id, job.get("name"), "root" if self.ctx["priv"].root else "package",
            dry_run, reason))

        try:
            pre = self._preflight(job)
            if pre:
                for msg in pre:
                    self._log_line(log_path, "[BLOCKED] " + msg)
                self._finish(job, dry_run, started, log_path, rel_log,
                             status="blocked", exit_code=None, errors=pre, reason=reason)
                return

            rollback = None
            try:
                max_delete = int(job.get("max_delete", 0) or 0)
            except (TypeError, ValueError):
                max_delete = 0
            if (not dry_run and job.get("delete") and max_delete > 0
                    and job.get("rollback", True)):
                rollback = self._prepare_rollback(job, log_path)

            argv = self._build_argv(job, dry_run)
            cmd = self.ctx["priv"].wrap(argv)
            self._log_line(log_path, "cmd: " + " ".join(self._quote(c) for c in cmd))
            env = dict(os.environ)
            env.update({"LC_ALL": "C", "LANG": "C", "PYTHONUNBUFFERED": "1"})
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env, cwd="/")
            reader = threading.Thread(target=self._reader, args=(proc, log_path, info),
                                      daemon=True)
            reader.start()

            deadline = time.time() + float(job.get("timeout_hours", 6) or 6) * 3600
            timed_out = False
            canceled = False
            while proc.poll() is None:
                if cancel_ev.is_set():
                    canceled = True
                    self._kill(proc)
                    break
                if time.time() > deadline:
                    timed_out = True
                    self._kill(proc)
                    break
                time.sleep(0.4)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._kill(proc, force=True)
                proc.wait()
            reader.join(timeout=5)

            code = proc.returncode
            if canceled:
                status = "canceled"
            elif timed_out:
                status = "timeout"
            elif code == 0:
                status = "ok"
            elif code == 24:
                status = "ok_partial"
            elif code == 25:
                status = "aborted"
            else:
                status = "failed"
            rolled_back = 0
            store_kept = ""
            if rollback:
                if code == 25:
                    try:
                        rolled_back = self._restore_rollback(job, rollback["store"], log_path)
                        status = "aborted_rolled_back"
                        self._cleanup_store(rollback["store"])
                    except ApiError as exc:
                        self._log_line(log_path, "[ROLLBACK] " + exc.message)
                        store_kept = rollback["store"]
                elif code == 0:
                    self._cleanup_store(rollback["store"])
                else:
                    store_kept = rollback["store"]
                    self._log_line(log_path, "[ROLLBACK] 快照保留（未回滚）：%s" % store_kept)
            self._finish(job, dry_run, started, log_path, rel_log,
                         status=status, exit_code=code, reason=reason,
                         rolled_back=rolled_back, rollback_store=store_kept)
        except ApiError as e:
            self._log_line(log_path, "[ERROR] " + e.message)
            self._finish(job, dry_run, started, log_path, rel_log,
                         status="blocked", exit_code=None, errors=[e.message], reason=reason)
        except (OSError, subprocess.SubprocessError) as e:
            self._log_line(log_path, "[ERROR] " + str(e))
            self._finish(job, dry_run, started, log_path, rel_log,
                         status="failed", exit_code=None, errors=[str(e)], reason=reason)
        finally:
            self._set_active(job_id, None)

    def _kill(self, proc, force=False):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def _quote(self, value):
        if re.match(r"^[A-Za-z0-9_./:=%+@-]+$", value or ""):
            return value
        return "'" + str(value).replace("'", "'\\''") + "'"

    def _log_line(self, path, line):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    def _reader(self, proc, log_path, info):
        written = 0
        try:
            f = open(log_path, "ab")
        except OSError:
            f = None
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if f is not None and written < MAX_LOG_BYTES:
                    try:
                        f.write(line)
                        written += len(line)
                    except OSError:
                        pass
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                if text[0] in "<>ch*" or text.startswith("*deleting"):
                    info["files"] = info.get("files", 0) + 1
                    info["last_file"] = text[:240]
        finally:
            if f is not None:
                f.close()

    def _preflight(self, job):
        priv = self.ctx["priv"]
        errors = []
        src = norm_path(job.get("source"))
        dst = norm_path(job.get("dest"))
        errs, _ = validate_job(job)
        errors.extend(errs)

        if not os.path.isdir(src):
            errors.append("源目录不存在或不是目录：%s" % src)
        if not on_mounted_volume(src):
            errors.append("源目录不在已挂载的卷上：%s" % src)
        if not on_mounted_volume(dst):
            errors.append("目标目录不在已挂载的卷上：%s" % dst)

        empty = dir_empty_priv(priv, src) if os.path.isdir(src) else None
        if empty is None:
            errors.append("无法读取源目录（权限不足）：%s" % src)
        elif empty and job.get("delete"):
            errors.append("源目录为空，已拒绝执行（防止 --delete 清空备份）")

        if not os.path.isdir(dst):
            if not priv.makedirs(dst):
                errors.append("无法创建目标目录：%s" % dst)

        if os.path.isdir(dst):
            marker = os.path.join(dst, MARKER)
            if not os.path.exists(marker):
                dest_empty = dir_empty_priv(priv, dst)
                if dest_empty is False and not job.get("adopted"):
                    errors.append("目标目录非空且未标记，请先确认接管：%s" % dst)
                elif dest_empty is None:
                    errors.append("无法读取目标目录（权限不足）：%s" % dst)
                else:
                    if not priv.write_text(marker, json.dumps({
                        "job_id": job.get("id"), "source": src,
                        "created_at": now_ts(),
                    }, ensure_ascii=False, indent=2) + "\n"):
                        errors.append("无法写入目标标记（权限不足）：%s" % marker)
            recorded = self.ctx["state"].snapshot().get("jobs", {}).get(
                job.get("id"), {}).get("dest_dev")
            try:
                dev = os.stat(dst).st_dev
            except OSError:
                dev = None
            if recorded is not None and dev is not None and int(recorded) != int(dev):
                errors.append("目标卷身份变化（可能未挂载或已更换磁盘），已拒绝执行")
            elif recorded is None and dev is not None:
                self.ctx["state"].update(lambda d: d.setdefault("jobs", {}).setdefault(
                    job.get("id"), {}).update({"dest_dev": int(dev)}))
            try:
                sv = os.statvfs(dst)
                free = sv.f_bavail * sv.f_frsize
                if free < MIN_FREE_BYTES:
                    errors.append("目标卷剩余空间不足 1 GiB（%.1f GiB）" % (free / 1073741824.0))
            except OSError:
                pass

        return errors

    def _build_argv(self, job, dry_run):
        cfg = self.ctx["config"].get()
        settings = cfg.get("settings") or {}
        rsync = (job.get("rsync_path") or settings.get("rsync_path") or find_rsync())
        if not rsync:
            raise ApiError("未找到 rsync 可执行文件")
        info = probe_rsync(rsync)
        caps = info.get("capabilities") or {}
        priv = self.ctx["priv"]
        args = [rsync, "-a", "-H", "-S"]
        if caps.get("ACLs") and job.get("preserve_acls", True):
            args.append("-A")
        if caps.get("xattrs") and job.get("preserve_xattrs", True):
            args.append("-X")
        if not priv.root:
            args += ["--no-owner", "--no-group"]
        if job.get("keep_newer"):
            args.append("--keep-newer-files")
        if job.get("checksum"):
            args.append("--checksum")
        try:
            bw = int(job.get("bwlimit_kb", 0) or 0)
        except (TypeError, ValueError):
            bw = 0
        if bw > 0:
            args.append("--bwlimit=%d" % bw)
        if job.get("delete"):
            args.append("--delete-delay")
        if job.get("delete_excluded"):
            args.append("--delete-excluded")
        try:
            max_delete = int(job.get("max_delete", 0) or 0)
        except (TypeError, ValueError):
            max_delete = 0
        if max_delete > 0:
            args.append("--max-delete=%d" % max_delete)
        seen = set()
        for pat in list(settings.get("default_excludes") or []) + \
                list(job.get("exclude") or []) + list(INTERNAL_EXCLUDES):
            pat = (pat or "").strip()
            if pat and pat not in seen:
                seen.add(pat)
                args.append("--exclude=" + pat)
        args += ["--out-format=%i %n%L", "--stats"]
        if dry_run:
            args.append("--dry-run")
        args += [job["source"].rstrip("/") + "/", job["dest"].rstrip("/") + "/"]
        return args

    # ---------------- 删除超限回滚 ----------------
    def _store_root(self, job):
        return os.path.join(job["dest"], TRASH_DIR)

    def _new_store(self, job):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return os.path.join(self._store_root(job),
                            "%s-%s-%s" % (job["id"], stamp, uuid.uuid4().hex[:4]))

    def _prune_stores(self, job):
        root = self._store_root(job)
        if not os.path.isdir(root):
            return
        cutoff = time.time() - 86400
        try:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                try:
                    if os.path.isdir(path) and os.stat(path).st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass

    def _scan_deletions(self, job, log_path):
        argv = [a for a in self._build_argv(job, dry_run=True)
                if not a.startswith("--max-delete")]
        argv = [("--out-format=%i %n" if a.startswith("--out-format=") else a) for a in argv]
        cmd = self.ctx["priv"].wrap(argv)
        env = dict(os.environ)
        env.update({"LC_ALL": "C", "LANG": "C"})
        try:
            timeout = float(job.get("timeout_hours", 6) or 6) * 3600
        except (TypeError, ValueError):
            timeout = 6 * 3600
        timeout = min(max(60.0, timeout), 3600.0)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout,
                                  env=env, cwd="/")
        except subprocess.TimeoutExpired:
            raise ApiError("回滚预检超时，已取消本次运行")
        out = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode not in (0, 24):
            raise ApiError("回滚预检失败（rsync exit %s）" % proc.returncode)
        paths, seen = [], set()
        for line in out.splitlines():
            if not line.startswith("*deleting"):
                continue
            rel = line[len("*deleting"):].strip().rstrip("/")
            if not rel or rel == "." or rel in seen:
                continue
            if rel == TRASH_DIR or rel.startswith(TRASH_DIR + "/"):
                continue
            seen.add(rel)
            paths.append(rel)
        return paths

    def _snapshot_deletions(self, job, store, paths, log_path):
        dest = job["dest"]
        count = 0
        for rel in paths:
            src = os.path.normpath(os.path.join(dest, rel))
            try:
                if os.path.commonpath([dest, src]) != dest:
                    continue
            except ValueError:
                continue
            if not os.path.lexists(src):
                continue
            dst = os.path.join(store, rel)
            parent = os.path.dirname(dst)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.islink(src):
                    if not os.path.lexists(dst):
                        os.symlink(os.readlink(src), dst)
                elif os.path.isdir(src):
                    os.makedirs(dst, exist_ok=True)
                    try:
                        shutil.copystat(src, dst)
                    except OSError:
                        pass
                else:
                    if not os.path.lexists(dst):
                        try:
                            os.link(src, dst)
                        except OSError:
                            shutil.copy2(src, dst)
                    count += 1
            except OSError as exc:
                raise ApiError("回滚快照失败：%s（%s）" % (rel, exc))
        return count

    def _prepare_rollback(self, job, log_path):
        self._prune_stores(job)
        paths = self._scan_deletions(job, log_path)
        if not paths:
            return None
        store = self._new_store(job)
        os.makedirs(store, exist_ok=True)
        try:
            files = self._snapshot_deletions(job, store, paths, log_path)
        except Exception:
            shutil.rmtree(store, ignore_errors=True)
            raise
        self._log_line(log_path, "[ROLLBACK] 预检删除 %d 项，已快照 %d 个文件 → %s"
                       % (len(paths), files, store))
        return {"store": store, "planned": len(paths), "files": files}

    def _restore_rollback(self, job, store, log_path):
        dest = job["dest"]
        if not os.path.isdir(store):
            return 0
        count = 0
        for root, dirs, files in os.walk(store):
            rel = os.path.relpath(root, store)
            target = dest if rel == "." else os.path.join(dest, rel)
            try:
                if not os.path.isdir(target):
                    os.makedirs(target, exist_ok=True)
                    try:
                        shutil.copystat(root, target)
                    except OSError:
                        pass
                for name in files:
                    src = os.path.join(root, name)
                    dst = os.path.join(target, name)
                    if os.path.lexists(dst):
                        continue
                    if os.path.islink(src):
                        os.symlink(os.readlink(src), dst)
                    else:
                        try:
                            os.link(src, dst)
                        except OSError:
                            shutil.copy2(src, dst)
                    count += 1
            except OSError as exc:
                raise ApiError("回滚恢复失败：%s（%s）" % (rel, exc))
        self._log_line(log_path, "[ROLLBACK] 已恢复 %d 个文件" % count)
        return count

    def _cleanup_store(self, store):
        if not store:
            return
        shutil.rmtree(store, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(store))
        except OSError:
            pass

    def _finish(self, job, dry_run, started, log_path, rel_log,
                status, exit_code, errors=None, reason="", rolled_back=0, rollback_store=""):
        ended = now_ts()
        duration = max(0, ended - started)
        stats = parse_stats(log_path)
        job_id = job.get("id")
        summary = {
            "id": job_id,
            "name": job.get("name") or job_id,
            "start": started,
            "end": ended,
            "duration": duration,
            "status": status,
            "exit_code": exit_code,
            "dry_run": bool(dry_run),
            "reason": reason,
            "files": stats.get("files"),
            "transferred": stats.get("transferred"),
            "deleted": stats.get("deleted"),
            "bytes_total": stats.get("bytes_total"),
            "bytes_transferred": stats.get("bytes_transferred"),
            "bytes_sent": stats.get("bytes_sent"),
            "speedup": stats.get("speedup"),
            "log": rel_log,
            "errors": errors or [],
            "rolled_back": int(rolled_back or 0),
            "rollback_store": rollback_store or "",
        }

        def upd(d):
            d.setdefault("jobs", {}).setdefault(job_id, {}).update({
                "running": False,
                "last_status": status,
                "last_run": summary,
            })
            hist = d.setdefault("history", [])
            hist.append(summary)
            if len(hist) > 200:
                del hist[:-200]
        self.ctx["state"].update(upd)

        for msg in (errors or []):
            self._log_line(log_path, "[RESULT] " + msg)
        self._log_line(log_path, "[RESULT] %s exit=%s duration=%ss files=%s deleted=%s rolled_back=%s" % (
            status, exit_code, duration, summary.get("transferred"),
            summary.get("deleted"), summary.get("rolled_back")))

        self._write_dest_state(job, summary)
        self._prune(job, summary)
        self._notify(job, summary)

    def _write_dest_state(self, job, summary):
        dest = job.get("dest")
        if not dest or not os.path.isdir(dest):
            return
        payload = {
            "job": job.get("name"),
            "source": job.get("source"),
            "last_run": summary,
            "written_at": now_ts(),
            "rsync": (self.ctx["rsync_info"]() or {}).get("version"),
            "mode": "root" if self.ctx["priv"].root else "package",
        }
        self.ctx["priv"].write_text(
            os.path.join(dest, STATE_FILE),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _prune(self, job, summary):
        settings = self.ctx["config"].get().get("settings") or {}
        try:
            days = int(settings.get("log_retention_days", 30) or 30)
            if days > 0:
                cutoff = time.time() - days * 86400
                root = os.path.join(self.var, "logs")
                for jid in os.listdir(root):
                    jdir = os.path.join(root, jid)
                    if not os.path.isdir(jdir):
                        continue
                    for name in os.listdir(jdir):
                        path = os.path.join(jdir, name)
                        try:
                            if os.stat(path).st_mtime < cutoff:
                                os.remove(path)
                        except OSError:
                            pass
        except (OSError, ValueError):
            pass

    def _notify(self, job, summary):
        if summary.get("dry_run") or job.get("dry_run"):
            return  # 预览（dry-run）不发通知
        settings = self.ctx["config"].get().get("settings") or {}
        mode = settings.get("notify_on", "failure")
        failed = summary["status"] not in ("ok", "ok_partial")
        if mode == "never" or (mode == "failure" and not failed):
            return
        try:
            if summary["status"] == "ok":
                text = "%s 同步完成（%s 个文件）" % (job.get("name"), summary.get("files") or 0)
            elif summary["status"] == "ok_partial":
                text = "%s 同步完成（部分文件跳过）" % job.get("name")
            else:
                text = "%s 同步%s（code %s）" % (
                    job.get("name"),
                    {"blocked": "被拦截", "canceled": "已取消", "timeout": "超时",
                     "aborted": "删除超限中止",
                     "aborted_rolled_back": "删除超限，已回滚 %s 项" % summary.get("rolled_back", 0)}.get(summary["status"], "失败"),
                    summary.get("exit_code"))
            if summary["status"] == "aborted_rolled_back":
                level, msg_key = "warn", "rolled_back"
            else:
                level = "info" if summary["status"] in ("ok", "ok_partial") else "error"
                msg_key = "success" if summary["status"] in ("ok", "ok_partial") else "failed"
            name = job.get("name") or PKG_NAME
            for user in (settings.get("notify_users") or ["@administrators"]):
                subprocess.run(["/usr/syno/bin/synodsmnotify", "-l", level, user,
                                PKG_NAME + ":notify:title", PKG_NAME + ":notify:" + msg_key, name],
                               capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


# ------------------------------------------------------------------ 调度 ----
class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="afs-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        time.sleep(5)
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("调度 tick 失败")
            self.stop_event.wait(20)

    def tick(self):
        cfg = self.ctx["config"]
        cfg.reload()
        state = self.ctx["state"]
        runner = self.ctx["runner"]
        settings = cfg.get().get("settings") or {}
        allow_missed = bool(settings.get("run_missed_on_start", True))
        now = time.time()
        for job in cfg.jobs():
            if not job.get("enabled"):
                continue
            jid = job.get("id")
            if runner.is_active(jid):
                continue
            rt = state.snapshot().get("jobs", {}).get(jid, {})
            nxt = rt.get("next_run")
            if nxt is None:
                nxt = next_run_for(job.get("schedule") or {}, now)
                if nxt is not None:
                    state.update(lambda d, j=jid, n=nxt:
                                 d.setdefault("jobs", {}).setdefault(j, {}).update({"next_run": n}))
                continue
            if now >= float(nxt):
                sch = job.get("schedule") or {}
                mode = sch.get("mode")
                late = now - float(nxt)
                missed = False
                if mode == "interval":
                    try:
                        minutes = int(sch.get("minutes", 0) or 0)
                    except (TypeError, ValueError):
                        minutes = 0
                    missed = minutes > 0 and late > max(300, minutes * 60)
                elif mode in ("daily", "weekly"):
                    missed = late > 3600
                if missed and not allow_missed:
                    log.info("跳过错过的调度：%s（晚 %d 秒）", jid, int(late))
                else:
                    try:
                        runner.enqueue(jid, bool(job.get("dry_run")), "schedule")
                        log.info("调度触发：%s", jid)
                    except ApiError:
                        pass
                nxt2 = next_run_for(sch, now)
                state.update(lambda d, j=jid, n=nxt2:
                             d.setdefault("jobs", {}).setdefault(j, {}).update({"next_run": n}))


# ------------------------------------------------------------------ 认证 ----
class Auth:
    def __init__(self, var):
        self.token_path = os.path.join(var, TOKEN_NAME)
        self.token = self._read()

    def _read(self):
        try:
            with open(self.token_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def check(self, handler):
        if not self.token:
            return True
        supplied = handler.headers.get("X-AFS-Token", "")
        if not supplied:
            cookie = handler.headers.get("Cookie", "")
            m = re.search(r"(?:^|;\s*)afs_token=([0-9a-fA-F]{16,})", cookie)
            if m:
                supplied = m.group(1)
        if not supplied:
            return False
        return hmac.compare_digest(supplied, self.token)


# ------------------------------------------------------------------ HTTP ----
class Handler(BaseHTTPRequestHandler):
    server_version = "AutoFileSync/%s" % PKG_VERSION
    timeout = 60

    def log_message(self, fmt, *args):
        return

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, reason="unauthorized"):
        self._json(401, {"ok": False, "error": reason})

    def do_GET(self):
        app = self.server.app
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        if not app["auth"].check(self):
            self._deny()
            return
        app["reload"]()
        try:
            if path == "/api/status":
                self._json(200, app["status"]())
            elif path == "/api/config":
                self._json(200, {"ok": True, "path": app["config"].path,
                                 "config": app["config"].get()})
            elif path == "/api/about":
                self._json(200, app["about"]())
            elif path == "/api/logs":
                self._json(200, app["logs"](query))
            elif path == "/api/runs":
                self._json(200, app["runs"](query))
            elif path == "/api/browse":
                self._json(200, app["browse"](query))
            else:
                self._json(404, {"ok": False, "error": "unknown endpoint: %s" % path})
        except ApiError as e:
            self._json(200, {"ok": False, "error": e.message,
                             "errors": e.errors, "code": e.code})
        except Exception as e:
            log.exception("处理 %s 失败", path)
            self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        app = self.server.app
        parsed = urllib.parse.urlparse(self.path)
        action = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not app["auth"].check(self):
            self._deny()
            return
        app["reload"]()
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "请求体过大"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._json(400, {"ok": False, "error": "JSON 解析失败"})
            return
        try:
            handler = app["post"].get(action)
            if not handler:
                raise ApiError("unknown action: %s" % action)
            self._json(200, handler(payload))
        except ApiError as e:
            self._json(200, {"ok": False, "error": e.message,
                             "errors": e.errors, "code": e.code})
        except Exception as e:
            log.exception("POST %s 失败", action)
            self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def do_PUT(self):
        self._json(405, {"ok": False, "error": "use POST"})


class UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    address_family = socket.AF_UNIX

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except OSError:
            pass
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


# ------------------------------------------------------------------ 应用 ----
def build_app(var, cfg_path):
    config = Config(cfg_path)
    state = State(os.path.join(var, "state.json"))
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(START_TS))
    try:
        username = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        username = str(os.geteuid())

    rsync_holder = {"value": None, "at": 0.0}
    priv_holder = {"value": None, "at": 0.0}

    def privilege_info():
        now = time.time()
        if priv_holder["value"] is None or now - priv_holder["at"] > 60:
            priv_holder["value"] = probe_spm(find_spm_exec())
            priv_holder["at"] = now
        return priv_holder["value"]

    def reload_config():
        if config.reload():
            rsync_holder["at"] = 0.0
            priv_holder["at"] = 0.0

    def rsync_info():
        now = time.time()
        if rsync_holder["value"] is None or now - rsync_holder["at"] > 60:
            preferred = (config.get().get("settings") or {}).get("rsync_path", "")
            rsync_holder["value"] = probe_rsync(find_rsync(preferred))
            rsync_holder["at"] = now
        return rsync_holder["value"]

    def get_priv():
        info = privilege_info()
        return Priv(find_spm_exec() if info["authorized"] else None, info["authorized"])

    ctx = {
        "var": var,
        "config": config,
        "state": state,
        "rsync_info": rsync_info,
        "privilege_info": privilege_info,
        "get_priv": get_priv,
    }
    ctx["priv"] = get_priv()
    runner = Runner(ctx)
    ctx["runner"] = runner
    scheduler = Scheduler(ctx)
    ctx["scheduler"] = scheduler

    def status():
        cfg = config.get()
        rt = state.snapshot().get("jobs", {})
        rs = runner.status()
        running_ids = set(r.get("id") for r in (rs.get("running_jobs") or []))
        jobs = []
        for job in cfg.get("jobs", []):
            jid = job.get("id")
            item = rt.get(jid) or {}
            jobs.append({
                "id": jid,
                "name": job.get("name") or jid,
                "enabled": bool(job.get("enabled")),
                "dry_run": bool(job.get("dry_run")),
                "delete": bool(job.get("delete")),
                "source": job.get("source", ""),
                "dest": job.get("dest", ""),
                "schedule": job.get("schedule") or {"mode": "manual"},
                "schedule_text": schedule_text(job),
                "running": (jid in running_ids) or bool(item.get("running")),
                "queued": jid in rs["queued"],
                "last_status": item.get("last_status"),
                "last_run": item.get("last_run"),
                "needs_adopt": adopt_required(job),
                "next_run": item.get("next_run"),
                "next_run_text": fmt_ts(item.get("next_run")) if item.get("next_run") else "",
            })
        return {
            "ok": True,
            "pkg": {"name": PKG_NAME, "version": PKG_VERSION, "schema": SCHEMA},
            "daemon": {
                "pid": os.getpid(), "started_at": started,
                "uptime_sec": int(time.time() - START_TS), "user": username,
                "var": var, "socket": os.path.join(var, "http", SOCKET_NAME),
            },
            "rsync": rsync_info(),
            "privilege": privilege_info(),
            "runner": rs,
            "jobs": jobs,
        }

    def about():
        return {
            "ok": True, "name": PKG_NAME, "version": PKG_VERSION, "schema": SCHEMA,
            "python": sys.version.split()[0],
            "paths": {
                "var": var, "config": config.path,
                "state": os.path.join(var, "state.json"),
                "logs": os.path.join(var, "logs"),
                "service_log": os.path.join(var, LOG_NAME),
                "socket": os.path.join(var, "http", SOCKET_NAME),
            },
            "endpoints": ["status", "config", "logs", "runs", "browse", "about",
                          "job_save", "job_delete", "job_toggle", "settings",
                          "run", "cancel", "adopt", "validate", "notify_test"],
        }

    def logs(query):
        kind = (query.get("kind") or ["pkg"])[0]
        try:
            n = max(1, min(int((query.get("n") or ["200"])[0]), 2000))
        except ValueError:
            n = 200
        if kind == "run":
            jid = (query.get("job") or [""])[0]
            fname = (query.get("file") or [""])[0]
            if not re.match(r"^[A-Za-z0-9._-]+$", jid or "") or \
               not re.match(r"^[A-Za-z0-9._-]+\.log$", fname or ""):
                raise ApiError("非法的日志参数")
            path = os.path.join(var, "logs", jid, fname)
            res = tail_lines(path, n)
            if not res.get("ok"):
                return {"ok": True, "path": path, "lines": [], "note": res.get("error", "")}
            return res
        path = os.path.join(var, LOG_NAME)
        res = tail_lines(path, n)
        if not res.get("ok"):
            return {"ok": True, "path": path, "lines": [], "note": res.get("error", "")}
        return res

    def runs(query):
        jid = (query.get("job") or [""])[0]
        try:
            limit = max(1, min(int((query.get("limit") or ["50"])[0]), 200))
        except ValueError:
            limit = 50
        hist = list(state.snapshot().get("history") or [])
        if jid:
            hist = [h for h in hist if h.get("id") == jid]
        hist = list(reversed(hist))[:limit]
        return {"ok": True, "runs": hist}

    def browse(query):
        path = (query.get("path") or ["/"])[0]
        if path in ("", "/"):
            dirs = []
            for i in range(1, 65):
                p = "/volume%d" % i
                if os.path.isdir(p):
                    dirs.append({"name": p, "path": p})
            return {"ok": True, "path": "/", "parent": "", "dirs": dirs}
        path = os.path.normpath(path)
        if not re.match(r"^/volume\d+", path):
            raise ApiError("只能浏览 /volumeN 下的目录")
        if not os.path.isdir(path):
            raise ApiError("目录不存在：%s" % path)
        entries = []
        try:
            with os.scandir(path) as it:
                for ent in it:
                    if not ent.is_dir(follow_symlinks=False):
                        continue
                    if ent.name.startswith(("@", "#", ".")):
                        continue
                    entries.append({"name": ent.name,
                                    "path": os.path.join(path, ent.name)})
        except OSError as e:
            raise ApiError("无法读取目录：%s" % e)
        entries.sort(key=lambda x: x["name"].lower())
        parent = os.path.dirname(path) if path != "/" else ""
        return {"ok": True, "path": path, "parent": parent, "dirs": entries}

    def adopt_required(job):
        dst = job.get("dest")
        if not dst or not os.path.isdir(dst):
            return False
        if os.path.exists(os.path.join(dst, MARKER)):
            return False
        return dir_empty_priv(get_priv(), dst) is False and not job.get("adopted")

    def save_job(payload):
        job = normalize_job(payload or {})
        errs, _ = validate_job(job)
        if errs:
            raise ApiError("校验失败", errors=errs)
        if adopt_required(job):
            raise ApiError("目标目录非空且未标记，请先确认接管", errors=[
                "目标目录非空且没有 .autofilesync-target 标记：%s" % job.get("dest")
            ], code="need_adopt")
        cfg = config.get()
        jobs = list(cfg.get("jobs") or [])
        existing = None
        for i, old in enumerate(jobs):
            if old.get("id") and old.get("id") == job.get("id"):
                existing = (i, old)
                break
        now = now_ts()
        if existing:
            job["id"] = existing[1].get("id")
            job["created_at"] = existing[1].get("created_at", now)
            jobs[existing[0]] = job
        else:
            job["id"] = job.get("id") or ("j-%s" % uuid.uuid4().hex[:8])
            job["created_at"] = now
            jobs.append(job)
        job["updated_at"] = now
        cfg["jobs"] = jobs
        config.save(cfg)
        state.update(lambda d: d.setdefault("jobs", {}).setdefault(
            job["id"], {}).pop("next_run", None))
        return {"ok": True, "job": job}

    def delete_job(payload):
        jid = (payload or {}).get("id")
        if not jid:
            raise ApiError("缺少任务 id")
        if runner.is_active(jid):
            raise ApiError("任务正在运行，请先取消")
        cfg = config.get()
        before = len(cfg.get("jobs") or [])
        cfg["jobs"] = [j for j in (cfg.get("jobs") or []) if j.get("id") != jid]
        if len(cfg["jobs"]) == before:
            raise ApiError("任务不存在：%s" % jid)
        config.save(cfg)
        state.update(lambda d: d.setdefault("jobs", {}).pop(jid, None))
        return {"ok": True}

    def toggle_job(payload):
        jid = (payload or {}).get("id")
        if not jid:
            raise ApiError("缺少任务 id")
        cfg = config.get()
        target = None
        for job in cfg.get("jobs") or []:
            if job.get("id") == jid:
                target = job
                break
        if not target:
            raise ApiError("任务不存在：%s" % jid)
        enabled = payload.get("enabled")
        target["enabled"] = bool(enabled) if enabled is not None else not target.get("enabled")
        target["updated_at"] = now_ts()
        config.save(cfg)
        state.update(lambda d: d.setdefault("jobs", {}).setdefault(
            jid, {}).pop("next_run", None))
        return {"ok": True, "job": target}

    def set_job_mode(payload):
        jid = (payload or {}).get("id")
        if not jid:
            raise ApiError("缺少任务 id")
        cfg = config.get()
        target = None
        for job in cfg.get("jobs") or []:
            if job.get("id") == jid:
                target = job
                break
        if not target:
            raise ApiError("任务不存在：%s" % jid)
        target["dry_run"] = bool((payload or {}).get("dry_run"))
        target["updated_at"] = now_ts()
        config.save(cfg)
        state.update(lambda d: d.setdefault("jobs", {}).setdefault(
            jid, {}).pop("next_run", None))
        return {"ok": True, "job": target}

    def save_settings(payload):
        payload = payload or {}
        cfg = config.get()
        settings = dict(cfg.get("settings") or {})
        for key in DEFAULT_SETTINGS:
            if key in payload:
                settings[key] = payload[key]
        try:
            settings["concurrency"] = max(1, min(3, int(settings.get("concurrency", 1) or 1)))
        except (TypeError, ValueError):
            settings["concurrency"] = 1
        for key in ("log_retention_days",):
            try:
                settings[key] = max(0, int(settings.get(key, 30) or 0))
            except (TypeError, ValueError):
                settings[key] = 30
        for key in ("run_missed_on_start", "first_run_preview"):
            settings[key] = bool(settings.get(key))
        if settings.get("notify_on") not in ("never", "failure", "always"):
            settings["notify_on"] = "failure"
        if not isinstance(settings.get("default_excludes"), list):
            settings["default_excludes"] = list(DEFAULT_SETTINGS["default_excludes"])
        if not isinstance(settings.get("notify_users"), list):
            settings["notify_users"] = ["@administrators"]
        settings["notify_users"] = [str(x).strip() for x in settings["notify_users"]
                                    if str(x).strip()][:10]
        cfg["settings"] = settings
        config.save(cfg)
        return {"ok": True, "settings": settings}

    def do_run(payload):
        jid = (payload or {}).get("id")
        if not jid:
            raise ApiError("缺少任务 id")
        job = config.find(jid)
        if not job:
            raise ApiError("任务不存在：%s" % jid)
        if adopt_required(job):
            raise ApiError("目标目录非空且未标记，请先确认接管", errors=[
                "目标目录非空且没有 .autofilesync-target 标记：%s" % job.get("dest")
            ], code="need_adopt")
        dry = payload.get("dry_run", job.get("dry_run"))
        runner.enqueue(jid, bool(dry), "manual")
        return {"ok": True}

    def do_cancel(payload):
        jid = (payload or {}).get("id")
        if not jid:
            raise ApiError("缺少任务 id")
        runner.cancel(jid)
        return {"ok": True}

    def adopt(payload):
        payload = payload or {}
        src = norm_path(payload.get("source"))
        dst = norm_path(payload.get("dest"))
        if not src or not dst:
            raise ApiError("缺少源或目标目录")
        if not os.path.isdir(dst):
            if not get_priv().makedirs(dst):
                raise ApiError("无法创建目标目录")
        if not on_mounted_volume(dst):
            raise ApiError("目标目录不在已挂载的卷上")
        marker = os.path.join(dst, MARKER)
        if os.path.exists(marker):
            return {"ok": True, "already": True}
        ok = get_priv().write_text(marker, json.dumps({
            "job_id": payload.get("id"), "source": src, "created_at": now_ts(),
        }, ensure_ascii=False, indent=2) + "\n")
        if not ok:
            raise ApiError("无法写入目标标记（需要 root 或目标目录写权限）")
        return {"ok": True, "already": False}

    def validate(payload):
        job = normalize_job(payload or {})
        errs, warns = validate_job(job)
        return {"ok": True, "errors": errs, "warnings": warns, "job": job}

    def notify_test(payload):
        settings = config.get().get("settings") or {}
        try:
            code = 0
            err = ""
            for user in (settings.get("notify_users") or ["@administrators"]):
                proc = subprocess.run(["/usr/syno/bin/synodsmnotify", "-l", "info", user,
                                       PKG_NAME + ":notify:title", PKG_NAME + ":notify:test",
                                       "测试任务"],
                                      capture_output=True, timeout=10)
                code = proc.returncode
                err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
            return {"ok": True, "exit": code, "detail": err}
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError("通知发送失败：%s" % e)

    return {
        "var": var,
        "config": config,
        "state": state,
        "runner": runner,
        "scheduler": scheduler,
        "auth": Auth(var),
        "reload": reload_config,
        "status": status,
        "about": about,
        "logs": logs,
        "runs": runs,
        "browse": browse,
        "post": {
            "job_save": save_job,
            "job_delete": delete_job,
            "job_toggle": toggle_job,
            "job_mode": set_job_mode,
            "settings": save_settings,
            "run": do_run,
            "cancel": do_cancel,
            "adopt": adopt,
            "validate": validate,
            "notify_test": notify_test,
        },
    }


def main():
    ap = argparse.ArgumentParser(description="AutoFileSync daemon")
    ap.add_argument("--var", default=os.environ.get("SYNOPKG_PKGVAR") or DEFAULT_VAR)
    ap.add_argument("--config", default="")
    ap.add_argument("--socket", default="")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    var = os.path.abspath(args.var)
    cfg_path = os.path.abspath(args.config) if args.config else os.path.join(var, "config.json")
    sock_path = os.path.abspath(args.socket) if args.socket else os.path.join(var, "http", SOCKET_NAME)
    for sub in ("", "logs", "locks", "http"):
        os.makedirs(os.path.join(var, sub) if sub else var, exist_ok=True)

    app = build_app(var, cfg_path)
    server = UnixHTTPServer(sock_path, Handler)
    server.app = app
    try:
        os.chmod(sock_path, 0o660)
        try:
            pw = pwd.getpwnam("sc-autofilesync")
            os.chown(sock_path, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass
    except OSError as e:
        log.warning("设置 socket 权限失败：%s", e)

    app["state"].update(lambda d: d.update({"daemon": {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(START_TS)),
        "version": PKG_VERSION, "socket": sock_path,
    }}))

    st = app["status"]()
    log.info("AutoFileSync %s 启动：var=%s socket=%s", PKG_VERSION, var, sock_path)
    log.info("rsync: %s %s", st["rsync"].get("path") or "-",
             st["rsync"].get("version") or "(未找到)")
    log.info("privilege: %s (%s)", st["privilege"].get("mode"),
             st["privilege"].get("detail") or "-")
    log.info("user=%s pid=%d", st["daemon"]["user"], os.getpid())

    app["runner"].start()
    app["scheduler"].start()

    stopping = threading.Event()

    def _term(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("收到信号 %s，正在停止…", signum)
        app["scheduler"].stop()
        app["runner"].stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        app["state"].save()
        log.info("AutoFileSync 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
