#!/usr/bin/env python3
"""删除超限回滚的文件系统辅助程序（由守护进程通过 spm-exec 以 root 调用）。"""
import json
import os
import shutil
import sys
import time

TRASH = ".autofilesync-trash"


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False))


def fail(msg):
    emit(ok=False, error=str(msg))
    sys.exit(1)


def safe(base, rel):
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    path = os.path.normpath(os.path.join(base, rel))
    try:
        if os.path.commonpath([base, path]) != base:
            return None
    except ValueError:
        return None
    return path


def snapshot(dest, store, listfile):
    os.makedirs(store, exist_ok=True)
    count = 0
    with open(listfile, encoding="utf-8") as fh:
        for line in fh:
            rel = line.rstrip("\n")
            if not rel:
                continue
            src = safe(dest, rel)
            if not src or not os.path.lexists(src):
                continue
            dst = safe(store, rel)
            if not dst:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
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
                        st = os.stat(src)
                        try:
                            os.chown(dst, st.st_uid, st.st_gid)
                        except OSError:
                            pass
                count += 1
    emit(ok=True, files=count)


def restore(dest, store):
    if not os.path.isdir(store):
        emit(ok=True, files=0)
        return
    count = 0
    for root, _dirs, files in os.walk(store):
        rel = os.path.relpath(root, store)
        target = dest if rel == "." else safe(dest, rel)
        if not target:
            continue
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
                    st = os.stat(src)
                    try:
                        os.chown(dst, st.st_uid, st.st_gid)
                    except OSError:
                        pass
            count += 1
    emit(ok=True, files=count)


def cleanup(store):
    store = store.rstrip("/")
    parent = os.path.dirname(store)
    if os.path.basename(parent) != TRASH:
        fail("unexpected store path: %s" % store)
    shutil.rmtree(store, ignore_errors=True)
    try:
        os.rmdir(parent)
    except OSError:
        pass
    emit(ok=True)


def prune(root, age):
    root = root.rstrip("/")
    if os.path.basename(root) != TRASH:
        fail("unexpected prune path: %s" % root)
    if not os.path.isdir(root):
        emit(ok=True, removed=0)
        return
    cutoff = time.time() - float(age)
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.stat(path).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    emit(ok=True, removed=removed)


def main():
    if len(sys.argv) < 2:
        fail("missing command")
    cmd = sys.argv[1]
    try:
        if cmd == "snapshot" and len(sys.argv) == 5:
            snapshot(sys.argv[2], sys.argv[3], sys.argv[4])
        elif cmd == "restore" and len(sys.argv) == 4:
            restore(sys.argv[2], sys.argv[3])
        elif cmd == "cleanup" and len(sys.argv) == 3:
            cleanup(sys.argv[2])
        elif cmd == "prune" and len(sys.argv) == 4:
            prune(sys.argv[2], sys.argv[3])
        else:
            fail("bad arguments")
    except Exception as exc:  # noqa: BLE001
        fail(exc)


if __name__ == "__main__":
    main()
