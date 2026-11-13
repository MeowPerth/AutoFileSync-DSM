#!/usr/bin/env python3
"""目录浏览/新建文件夹辅助程序（由守护进程通过 spm-exec 以 root 调用）。"""
import json
import os
import re
import sys

VOLUME_RE = re.compile(r"^/volume\d+(?:/|$)")


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False))


def fail(msg):
    emit(ok=False, error=str(msg))
    sys.exit(1)


def norm(path):
    path = (path or "").strip()
    return os.path.normpath(path) if path else ""


def list_dirs(path):
    path = norm(path)
    if path in ("", "/"):
        dirs = []
        for i in range(1, 65):
            p = "/volume%d" % i
            if os.path.isdir(p):
                dirs.append({"name": p, "path": p})
        emit(ok=True, path="/", parent="", dirs=dirs)
        return
    if not VOLUME_RE.match(path):
        fail("只能浏览 /volumeN 下的目录")
    if not os.path.isdir(path):
        fail("目录不存在：%s" % path)
    entries = []
    try:
        with os.scandir(path) as it:
            for ent in it:
                if ent.name.startswith(("@", "#", ".")):
                    continue
                if ent.is_dir(follow_symlinks=False):
                    entries.append({"name": ent.name,
                                    "path": os.path.join(path, ent.name)})
    except OSError as exc:
        fail("无法读取目录：%s" % exc)
    entries.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(path) if path != "/" else ""
    emit(ok=True, path=path, parent=parent, dirs=entries)


def make_dir(parent, name):
    parent = norm(parent)
    name = (name or "").strip()
    if not VOLUME_RE.match(parent) or not os.path.isdir(parent):
        fail("父目录不存在或不在 /volumeN 下")
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        fail("文件夹名称不合法")
    if len(name) > 128:
        fail("文件夹名称过长")
    if name.startswith(("@", "#", ".")):
        fail("名称不能以 @ # . 开头")
    target = os.path.normpath(os.path.join(parent, name))
    if os.path.dirname(target) != parent:
        fail("文件夹名称不合法")
    if os.path.isdir(target):
        emit(ok=True, path=target, existed=True)
        return
    if os.path.lexists(target):
        fail("同名文件已存在")
    try:
        os.mkdir(target)
    except OSError as exc:
        fail("新建文件夹失败：%s" % exc)
    emit(ok=True, path=target, existed=False)


def main():
    if len(sys.argv) < 2:
        fail("missing command")
    cmd = sys.argv[1]
    try:
        if cmd == "list" and len(sys.argv) == 3:
            list_dirs(sys.argv[2])
        elif cmd == "mkdir" and len(sys.argv) == 4:
            make_dir(sys.argv[2], sys.argv[3])
        else:
            fail("bad arguments")
    except Exception as exc:  # noqa: BLE001
        fail(exc)


if __name__ == "__main__":
    main()
