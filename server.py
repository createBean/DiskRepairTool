"""NTFS 只读恢复工具 - 本地服务。

启动后自动打开浏览器。所有对源盘的访问都是只读的。
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import core

# 打包成 exe 后，web 资源在临时解压目录里，缓存要放在 exe 旁边
if getattr(sys, "frozen", False):
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    BASE_DIR = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = HERE

WEB = os.path.join(HERE, "web")
PORT = int(os.environ.get("RECOVERY_PORT", "8765"))

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".dng", ".cr2", ".nef", ".arw", ".psd",
    ".mp4", ".mov", ".m4v", ".avi",
}

LOCK = threading.Lock()
STATE = {
    "scan": {"state": "idle", "scanned": 0, "total": 0, "kept": 0, "elapsed": 0, "error": ""},
    "extract": {"state": "idle", "done": 0, "total": 0, "bytes": 0, "failed": 0, "elapsed": 0,
                "error": "", "current": ""},
    "volume": None,
    "rows": [],
    "by_parent": {},
    "disk": None,
    "offset": None,
}


def _reset_scan():
    STATE["rows"] = []
    STATE["by_parent"] = {}
    STATE["volume"] = None
    STATE["scan"] = {"state": "idle", "scanned": 0, "total": 0, "kept": 0, "elapsed": 0, "error": ""}


def _apply_rows(rows):
    by_parent = {}
    for idx, parent, name, is_dir, size in rows:
        by_parent.setdefault(parent, []).append((idx, name, is_dir, size))
    STATE["rows"] = rows
    STATE["by_parent"] = by_parent


def _load_cache(disk, offset):
    path = _cache_path(disk, offset)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cache(disk, offset, rows):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(disk, offset), "wb") as f:
            pickle.dump(rows, f, protocol=4)
    except Exception:
        pass


CACHE_DIR = os.path.join(BASE_DIR, ".cache")


def _cache_path(disk, offset):
    return os.path.join(CACHE_DIR, "mft_v2_%d_%d.pkl" % (disk, offset))



DEMO_TREE = [
    (5, None, "根目录", True),
]


def _load_demo():
    """载入一组合成数据，让没有故障盘的人也能预览界面。"""
    rows = []

    def d(idx, parent, name):
        rows.append((idx, parent, name, 1, 0))

    def f(idx, parent, name, size):
        rows.append((idx, parent, name, 0, size))

    d(100, 5, "照片")
    d(200, 5, "视频")
    d(300, 5, "文档")

    d(101, 100, "2026-05-01 海边")
    for i in range(18):
        f(1000 + i, 101, "IMG_%04d.JPG" % (4201 + i), 3_100_000 + i * 48_213)

    d(102, 100, "2026-06-12 生日")
    for i in range(12):
        f(1100 + i, 102, "DSC_%04d.JPG" % (880 + i), 9_400_000 + i * 71_339)

    d(103, 100, "2026-07-03 郊游")
    for i in range(31):
        f(1200 + i, 103, "IMG_%04d.HEIC" % (5310 + i), 2_600_000 + i * 39_417)

    for i in range(9):
        f(2000 + i, 200, "DJI_20260501180917_%04d_D.MP4" % (760 + i), 120_000_000 + i * 9_734_118)
    for i in range(6):
        f(2100 + i, 200, "VID_20260612_%06d.mp4" % (110230 + i), 45_000_000 + i * 3_204_551)

    f(3000, 300, "行程安排.pdf", 1_284_000)
    f(3001, 300, "费用明细表.xlsx", 88_289_193)
    f(3002, 300, "合同扫描件.docx", 2_047_360)
    f(3003, 300, "照片备份.zip", 65_011_712)

    _apply_rows(rows)
    STATE["demo"] = True
    STATE["scan"] = {"state": "done", "scanned": len(rows), "total": len(rows),
                     "kept": len(rows), "elapsed": 0, "error": "", "demo": True}
    return rows


def start_scan(disk, offset, letter="", force=False):
    if not force:
        cached = _load_cache(disk, offset)
        if cached is not None:
            with LOCK:
                _reset_scan()
                STATE["disk"] = disk
                STATE["offset"] = offset
                STATE["letter"] = letter
                _apply_rows(cached)
                STATE["scan"]["state"] = "done"
                STATE["scan"]["total"] = len(cached)
                STATE["scan"]["scanned"] = len(cached)
                STATE["scan"]["kept"] = len(cached)
                STATE["scan"]["cached"] = True
            return
    with LOCK:
        _reset_scan()
        STATE["disk"] = disk
        STATE["offset"] = offset
        STATE["letter"] = letter
        STATE["scan"]["state"] = "running"
    threading.Thread(target=_scan_worker, args=(disk, offset, letter), daemon=True).start()


def _scan_worker(disk, offset, letter=""):
    try:
        dev, base, mode = core.open_device(disk, offset, letter)
        vol = core.Volume(dev, base)
        STATE["volume"] = vol
        STATE["scan"]["total"] = vol.total_records
        STATE["scan"]["mode"] = mode

        def progress(scanned, total, kept, elapsed):
            STATE["scan"].update({"scanned": scanned, "total": total,
                                  "kept": kept, "elapsed": round(elapsed, 1)})

        rows = vol.scan(progress=progress)
        _apply_rows(rows)
        _save_cache(disk, offset, rows)
        STATE["scan"]["state"] = "done"
        STATE["scan"]["timeouts"] = dev.timeouts
        STATE["scan"]["errors"] = dev.errors
        dev.close()
    except Exception as ex:  # noqa: BLE001
        STATE["scan"]["state"] = "error"
        STATE["scan"]["error"] = "%s: %s" % (type(ex).__name__, ex)
        traceback.print_exc()


def start_extract(records, dest):
    with LOCK:
        STATE["extract"] = {"state": "running", "done": 0, "total": len(records), "bytes": 0,
                            "failed": 0, "elapsed": 0, "error": "", "current": ""}
    threading.Thread(target=_extract_worker, args=(records, dest), daemon=True).start()


def _ensure_volume():
    if STATE["volume"] is not None:
        return STATE["volume"]
    dev, base, _mode = core.open_device(STATE["disk"], STATE["offset"], STATE.get("letter", ""))
    vol = core.Volume(dev, base)
    STATE["volume"] = vol
    return vol


def _extract_worker(records, dest):
    if STATE.get("demo"):
        STATE["extract"]["state"] = "error"
        STATE["extract"]["error"] = "演示数据不能提取，请接入真实磁盘"
        return
    try:
        vol = _ensure_volume()
    except Exception:
        vol = None
    if vol is None:
        STATE["extract"]["state"] = "error"
        STATE["extract"]["error"] = "还没有完成扫描"
        return
    t0 = time.time()
    done = failed = 0
    total_bytes = 0
    os.makedirs(dest, exist_ok=True)

    name_of = {}
    parent_of = {}
    for idx, parent, name, _is_dir, _size in STATE["rows"]:
        name_of[idx] = name
        parent_of[idx] = parent

    def safe(name):
        for ch in '/:*?"<>|':
            name = name.replace(ch, "_")
        return name or "unnamed"

    def rel_path(idx):
        parts = []
        cur = idx
        for _ in range(32):
            nm = name_of.get(cur)
            if nm is None:
                break
            parts.append(safe(nm))
            nxt = parent_of.get(cur)
            if nxt is None or nxt == cur:
                break
            cur = nxt
        parts.reverse()
        return os.path.join(*parts) if parts else safe(name_of.get(idx, "unknown"))

    for n, idx in enumerate(records):
        STATE["extract"]["current"] = name_of.get(idx, "?")
        try:
            path = os.path.join(dest, rel_path(idx))
            written = vol.read_file(idx, path)
            done += 1
            total_bytes += written
        except Exception:
            failed += 1
        STATE["extract"].update({"done": n + 1, "bytes": total_bytes, "failed": failed,
                                 "elapsed": round(time.time() - t0, 1)})
    STATE["extract"]["state"] = "done"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/style.css":
            return self._static("style.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")

        if path == "/api/disks":
            try:
                return self._send(200, {"ok": True, "disks": core.list_disks()})
            except Exception as ex:
                return self._send(500, {"ok": False, "error": str(ex)})

        if path == "/api/partitions":
            disk = int(qs.get("disk", ["0"])[0])
            try:
                return self._send(200, {"ok": True, "partitions": core.find_ntfs_partitions(disk)})
            except Exception as ex:
                return self._send(500, {"ok": False, "error": str(ex)})

        if path == "/api/scan/status":
            return self._send(200, {"ok": True, "status": STATE["scan"]})

        if path == "/api/extract/status":
            return self._send(200, {"ok": True, "status": STATE["extract"]})

        if path == "/api/tree":
            parent = int(qs.get("parent", ["5"])[0])
            kids = STATE["by_parent"].get(parent, [])
            kids = sorted(kids, key=lambda k: (not k[2], k[1].lower()))
            out = [{"index": i, "name": n, "dir": bool(d), "size": s}
                   for i, n, d, s in kids[:2000]]
            return self._send(200, {"ok": True, "children": out, "total": len(kids)})

        if path == "/api/folders":
            return self._send(200, self._top_folders())

        if path == "/api/demo":
            _load_demo()
            return self._send(200, {"ok": True, "rows": len(STATE["rows"])})

        if path == "/api/home":
            return self._send(200, {
                "ok": True,
                "dest": os.path.join(os.path.expanduser("~"), "Desktop", "恢复文件"),
            })

        if path == "/api/collect":
            parent = int(qs.get("parent", ["5"])[0])
            limit = int(qs.get("limit", ["20000"])[0])
            out = []
            stack = [parent]
            while stack and len(out) < limit:
                cur = stack.pop()
                for idx, _name, is_dir, _sz in STATE["by_parent"].get(cur, []):
                    if is_dir:
                        stack.append(idx)
                    else:
                        out.append(idx)
                        if len(out) >= limit:
                            break
            return self._send(200, {"ok": True, "records": out, "truncated": len(out) >= limit})

        return self._send(404, {"ok": False, "error": "not found"})

    def _static(self, name, ctype):
        full = os.path.join(WEB, name)
        if not os.path.exists(full):
            return self._send(404, "missing " + name, "text/plain; charset=utf-8")
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, ctype)

    def _top_folders(self):
        by_parent = STATE["by_parent"]
        rows = STATE["rows"]
        parent_of = {idx: p for idx, p, _n, _d, _s in rows}
        name_of = {idx: n for idx, _p, n, _d, _s in rows}

        def direct_images(idx):
            n = 0
            for _ci, cname, cdir, _cs in by_parent.get(idx, []):
                if not cdir and os.path.splitext(cname)[1].lower() in IMAGE_EXT:
                    n += 1
            return n

        def path_of(idx):
            parts = []
            cur = idx
            for _ in range(24):
                nm = name_of.get(cur)
                if not nm:
                    break
                parts.append(nm)
                nxt = parent_of.get(cur)
                if not nxt or nxt == cur:
                    break
                cur = nxt
            return "\\".join(reversed(parts))

        scored = []
        for idx, _parent, _name, is_dir, _sz in rows:
            if not is_dir:
                continue
            n = direct_images(idx)
            if n >= 10:
                scored.append({"index": idx, "name": name_of.get(idx, ""), "images": n,
                               "path": path_of(idx)})
        scored.sort(key=lambda x: -x["images"])
        return {"ok": True, "folders": scored[:30]}

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._json_body()

        if path == "/api/diagnose":
            disk = int(body.get("disk", 0))
            offset = int(body.get("offset", 0))
            letter = body.get("letter", "") or ""
            try:
                return self._send(200, {"ok": True, "result": core.diagnose(disk, offset, letter)})
            except Exception as ex:
                return self._send(500, {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)})

        if path == "/api/scan":
            start_scan(int(body.get("disk", 0)), int(body.get("offset", 0)),
                       body.get("letter", "") or "")
            return self._send(200, {"ok": True})

        if path == "/api/extract":
            records = [int(x) for x in body.get("records", [])]
            dest = body.get("dest") or os.path.join(os.path.expanduser("~"), "Desktop", "恢复文件")
            if not records:
                return self._send(400, {"ok": False, "error": "没有选中文件"})
            start_extract(records, dest)
            return self._send(200, {"ok": True, "dest": dest})

        return self._send(404, {"ok": False, "error": "not found"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d/" % PORT
    print("=" * 56)
    print(" NTFS 只读恢复工具")
    print(" 地址: %s" % url)
    print(" 关闭这个窗口即停止服务")
    print("=" * 56)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
