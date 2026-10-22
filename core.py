"""NTFS 只读恢复核心。

设计原则：
1. 对源盘只做读取，任何函数都不会写入源设备。
2. 不依赖文件系统驱动，直接按扇区读原始设备，因此 RAW 卷也能处理。
3. 不依赖第三方库，NTFS 结构全部自己解析，方便打包成 exe。
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import threading
import time

SECTOR = 512
BLOCK = 1 << 20

ATTR_STANDARD_INFO = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF


# --------------------------------------------------------------------------
# 系统信息
# --------------------------------------------------------------------------

def _powershell(script: str, timeout: int = 120) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return out.stdout or ""
    except Exception:
        return ""


def _json_list(text: str):
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    return data


def list_disks():
    """枚举物理磁盘。"""
    script = (
        "Get-Disk | Select-Object Number,FriendlyName,SerialNumber,"
        "@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}},PartitionStyle,"
        "OperationalStatus,HealthStatus,BusType | ConvertTo-Json -Compress"
    )
    rows = _json_list(_powershell(script))
    out = []
    for r in rows:
        out.append({
            "number": r.get("Number"),
            "name": (r.get("FriendlyName") or "").strip(),
            "serial": (r.get("SerialNumber") or "").strip(),
            "size_gb": r.get("SizeGB"),
            "style": r.get("PartitionStyle"),
            "status": r.get("OperationalStatus"),
            "health": r.get("HealthStatus"),
            "bus": r.get("BusType"),
        })
    return out


def list_partitions(disk_number: int):
    """枚举某块磁盘的分区，Offset 为字节偏移。"""
    script = (
        "Get-Partition -DiskNumber %d -ErrorAction SilentlyContinue | "
        "Select-Object PartitionNumber,DriveLetter,Offset,Size,Type | "
        "ConvertTo-Json -Compress" % disk_number
    )
    rows = _json_list(_powershell(script))
    out = []
    for r in rows:
        letter = r.get("DriveLetter")
        out.append({
            "number": r.get("PartitionNumber"),
            "letter": letter if letter else "",
            "offset": r.get("Offset") or 0,
            "size": r.get("Size") or 0,
            "type": (r.get("Type") or "").strip(),
        })
    out.sort(key=lambda x: x["offset"])
    return out


# --------------------------------------------------------------------------
# 原始设备读取
# --------------------------------------------------------------------------

class RawDevice:
    """按块读取原始设备，带超时保护。

    Windows 对坏扇区的重试可能长达十几分钟，这里每个块用独立线程读，
    超时就弃掉该块并换新句柄继续，避免整个扫描卡死。
    """

    def __init__(self, path: str, timeout: float = 15.0):
        self.path = path
        self.timeout = timeout
        self.fh = open(path, "rb", buffering=0)
        self.timeouts = 0
        self.errors = 0
        self._cache = {}

    def _reopen(self):
        """换一个新句柄。旧句柄可能还挂着未完成的读，直接放弃，避免 close 阻塞。"""
        try:
            self.fh = open(self.path, "rb", buffering=0)
            return True
        except Exception:
            return False

    def _load(self, block_index: int) -> bytes:
        if block_index in self._cache:
            return self._cache[block_index]
        offset = block_index * BLOCK

        for _attempt in (1, 2):
            box = {}

            def work():
                try:
                    self.fh.seek(offset)
                    box["data"] = self.fh.read(BLOCK)
                except Exception as ex:  # noqa: BLE001
                    box["err"] = ex

            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(self.timeout)

            if t.is_alive():
                # 坏道让系统一直重试，放弃这块，换句柄继续
                self.timeouts += 1
                self._reopen()
                continue
            if "err" in box:
                self.errors += 1
                self._reopen()
                continue

            data = box.get("data", b"")
            if data:
                self._cache[block_index] = data
                if len(self._cache) > 64:
                    self._cache.pop(next(iter(self._cache)))
            return data
        return b""

    def read_at(self, offset: int, size: int) -> bytes:
        out = bytearray()
        pos = offset
        remaining = size
        while remaining > 0:
            bi = pos // BLOCK
            off = pos % BLOCK
            data = self._load(bi)
            if not data:
                raise OSError("读取失败 @%d" % pos)
            take = min(remaining, len(data) - off)
            if take <= 0:
                out += b"\x00" * remaining
                break
            out += data[off:off + take]
            pos += take
            remaining -= take
        return bytes(out)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# NTFS 结构解析
# --------------------------------------------------------------------------

def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def parse_boot(b: bytes):
    """解析 NTFS 引导扇区，非 NTFS 返回 None。"""
    if len(b) < 512 or b[3:11] != b"NTFS    ":
        return None
    bps = u16(b, 11)
    spc = b[13]
    if spc > 0x80:
        spc = 1 << (256 - spc)
    if bps == 0 or spc == 0:
        return None
    cluster = bps * spc
    rc = struct.unpack_from("<b", b, 64)[0]
    rec_size = (1 << (-rc)) if rc < 0 else rc * cluster
    if rec_size < 512 or rec_size > 65536:
        return None
    return {
        "bytes_per_sector": bps,
        "sectors_per_cluster": spc,
        "cluster_size": cluster,
        "mft_lcn": u64(b, 48),
        "mftmirr_lcn": u64(b, 56),
        "record_size": rec_size,
        "total_sectors": u64(b, 40),
        "serial": u64(b, 72),
    }


def parse_runlist(buf: bytes, attr_start: int, runlist_rel: int, attr_len: int):
    """解析非驻留属性的 runlist，返回 [(lcn, clusters), ...]。"""
    pos = attr_start + runlist_rel
    end = min(attr_start + attr_len, len(buf))
    runs = []
    lcn = 0
    sparse = False
    while pos < end:
        hdr = buf[pos]
        pos += 1
        if hdr == 0:
            break
        lsz = hdr & 0x0F
        osz = (hdr >> 4) & 0x0F
        if lsz == 0 or pos + lsz + osz > end:
            break
        length = int.from_bytes(buf[pos:pos + lsz], "little")
        pos += lsz
        if osz == 0:
            sparse = True
            runs.append((None, length))
            continue
        delta = int.from_bytes(buf[pos:pos + osz], "little", signed=True)
        pos += osz
        lcn = (lcn if not sparse else 0) + delta
        sparse = False
        runs.append((lcn, length))
    return runs


def parse_record(buf: bytes, rec_size: int, full: bool = False):
    """解析一条 MFT 记录。

    返回 dict：in_use / is_dir / name / parent / size
    full=True 时额外返回 data_frags（$DATA 各片段）和 attr_list（属性列表位置）。
    记录无效时返回 None。
    """
    if len(buf) < rec_size or buf[0:4] != b"FILE":
        return None
    flags = u16(buf, 0x16)
    if not (flags & 0x01):
        return None
    is_dir = bool(flags & 0x02)
    first_attr = u16(buf, 0x14)
    used = u32(buf, 0x18)
    if used == 0 or used > rec_size:
        used = rec_size
    limit = min(used, rec_size)

    names = []
    size = 0
    resident = None
    data_frags = []
    attr_list = None

    off = first_attr
    while off + 8 <= limit:
        atype = u32(buf, off)
        if atype == ATTR_END:
            break
        alen = u32(buf, off + 4)
        if alen < 16 or off + alen > rec_size:
            break
        non_resident = buf[off + 8]
        name_len = buf[off + 9]

        if atype == ATTR_FILE_NAME and non_resident == 0 and not names:
            clen = u32(buf, off + 0x10)
            coff = u16(buf, off + 0x14)
            base = off + coff
            if clen >= 0x42 and base + 0x42 <= rec_size:
                parent = u64(buf, base) & 0xFFFFFFFFFFFF
                nlen = buf[base + 0x40]
                ns = buf[base + 0x41]
                raw = buf[base + 0x42:base + 0x42 + nlen * 2]
                try:
                    text = raw.decode("utf-16-le", "replace")
                except Exception:
                    text = ""
                if text:
                    names.append((ns, text, parent))

        elif atype == ATTR_DATA and name_len == 0:
            if non_resident == 0:
                clen = u32(buf, off + 0x10)
                coff = u16(buf, off + 0x14)
                size = clen
                if full:
                    resident = buf[off + coff:off + coff + clen]
            else:
                real = u64(buf, off + 0x30)
                size = real
                if full:
                    start_vcn = u64(buf, off + 0x10)
                    alloc = u64(buf, off + 0x28)
                    rl_rel = u16(buf, off + 0x20)
                    data_frags.append({
                        "start_vcn": start_vcn,
                        "runs": parse_runlist(buf, off, rl_rel, alen),
                        "real": real,
                        "alloc": alloc,
                    })

        elif atype == 0x20 and non_resident == 1 and full:
            rl_rel = u16(buf, off + 0x20)
            attr_list = {
                "runs": parse_runlist(buf, off, rl_rel, alen),
                "size": u64(buf, off + 0x30),
            }

        off += alen

    # 扩展记录（$ATTRIBUTE_LIST 指向的溢出记录）没有 $FILE_NAME，
    # 但它承载着别的记录放不下的 $DATA 片段，必须照样返回。
    if names:
        names.sort(key=lambda x: 0 if x[0] in (1, 3) else 1)
        ns, name, parent = names[0]
    else:
        name, parent = None, None
    out = {
        "in_use": True,
        "is_dir": is_dir,
        "name": name,
        "parent": parent,
        "size": size,
    }
    if full:
        out["data_frags"] = data_frags
        out["attr_list"] = attr_list
        out["resident"] = resident
    return out


def parse_attr_list_entries(content: bytes):
    """解析 $ATTRIBUTE_LIST 内容。"""
    out = []
    pos = 0
    while pos + 26 <= len(content):
        atype = u32(content, pos)
        length = u16(content, pos + 4)
        if length < 26 or pos + length > len(content):
            break
        name_len = content[pos + 6]
        start_vcn = u64(content, pos + 8)
        base_ref = u64(content, pos + 16) & 0xFFFFFFFFFFFF
        out.append({"type": atype, "start_vcn": start_vcn, "base_ref": base_ref})
        pos += length
    return out


def valid_runs(runs, max_clusters):
    """过滤掉明显不合理的数据段（越界、负偏移、零长度）。"""
    out = []
    for lcn, count in runs:
        if not count or count <= 0 or lcn is None:
            continue
        if lcn < 0 or lcn + count > max_clusters:
            continue
        out.append((lcn, count))
    return out


def merge_frags(frags, cluster, total_bytes=None):
    """把多个 $DATA 片段按 start_vcn 合并成一条 runlist，并按大小裁剪。"""
    frags = sorted(frags, key=lambda f: f["start_vcn"])
    runs = []
    for f in frags:
        runs.extend(f["runs"])
    if total_bytes:
        need = (total_bytes + cluster - 1) // cluster
        trimmed = []
        got = 0
        for lcn, count in runs:
            if got >= need:
                break
            take = min(count, need - got)
            if take > 0:
                trimmed.append((lcn, take))
            got += take
        runs = trimmed
    return runs


class DevStream:
    """把 RawDevice 包装成文件对象，供 dissect 之类的库使用。"""

    def __init__(self, dev: "RawDevice", base: int, size: int):
        self.dev = dev
        self.base = base
        self.size = size
        self.pos = 0

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, max(0, self.size - self.pos))
        if n <= 0:
            return b""
        data = self.dev.read_at(self.base + self.pos, n)
        self.pos += len(data)
        return data

    def close(self):
        pass


class Volume:
    """一个 NTFS 分区。所有读取都相对分区起始偏移。"""

    def __init__(self, dev: RawDevice, offset: int):
        self.dev = dev
        self.offset = offset
        self.boot = parse_boot(dev.read_at(offset, 512))
        if not self.boot:
            raise ValueError("该分区不是 NTFS")
        self.cluster = self.boot["cluster_size"]
        self.rec_size = self.boot["record_size"]
        self.mft_runs = []
        self.source = "self"
        self._load_mft()

    def _mft_runs_via_dissect(self):
        """$MFT 自身的 runlist 可能被切碎到多条记录里，解析链容易出错。

        这种情况下退回到成熟的 dissect 库来解析（只用来取 runlist，
        其余全部用本模块自己的解析器）。
        """
        try:
            from dissect.ntfs import NTFS
            from dissect.ntfs.exceptions import FileNotFoundError as NtfsNotFound
            from dissect.ntfs.mft import Mft
        except Exception:
            return None

        stream = DevStream(self.dev, self.offset,
                           self.boot["total_sectors"] * self.boot["bytes_per_sector"])
        orig = Mft.get

        def tolerant(mft_self, ref, root=None):
            try:
                return orig(mft_self, ref, root)
            except Exception as ex:  # noqa: BLE001
                raise NtfsNotFound(str(ex))

        Mft.get = tolerant
        try:
            ntfs = NTFS(stream)
            runs = [(int(o), int(n)) for o, n in ntfs.mft.fh.runlist]
            return runs or None
        except Exception:
            return None
        finally:
            Mft.get = orig

    def _load_mft(self):
        lcn = self.boot["mft_lcn"]
        rec = self.dev.read_at(self.offset + lcn * self.cluster, self.rec_size)
        info = parse_record(rec, self.rec_size, full=True)
        if not info or not info.get("data_frags"):
            raise ValueError("无法解析 $MFT 的数据段")

        frags = list(info["data_frags"])
        total_bytes = max((f["real"] for f in frags), default=0)
        if not total_bytes:
            total_bytes = max((f["alloc"] for f in frags), default=0)
        need = total_bytes // self.cluster
        max_clusters = (self.boot["total_sectors"] * self.boot["bytes_per_sector"]) // self.cluster

        # 先试成熟库，能拿到完整 runlist 就直接用
        ext_runs = self._mft_runs_via_dissect()
        if ext_runs:
            valid = valid_runs(ext_runs, max_clusters)
            if sum(c for _, c in valid) >= need:
                self.mft_runs = merge_frags(
                    [{"start_vcn": 0, "runs": valid}], self.cluster, total_bytes)
                self.source = "dissect"
                self.total_bytes = total_bytes
                self.total_records = total_bytes // self.rec_size
                return

        def collect():
            merged = []
            for f in sorted(frags, key=lambda x: x["start_vcn"]):
                merged.extend(f["runs"])
            return valid_runs(merged, max_clusters)

        self.mft_runs = collect()

        # $MFT 自己也会碎片化到一条记录放不下，完整 runlist 分散在
        # $ATTRIBUTE_LIST 指向的其他记录里，需要追过去补齐。
        alist = info.get("attr_list")
        seen = {f["start_vcn"] for f in frags}
        tries = 0
        while alist and sum(c for _, c in self.mft_runs) < need and tries < 8:
            tries += 1
            try:
                content = self._read_runs(alist["runs"], alist["size"])
            except Exception:
                break
            added = False
            for e in parse_attr_list_entries(content):
                if e["type"] != ATTR_DATA or not e["base_ref"]:
                    continue
                if e["start_vcn"] in seen:
                    continue
                sub = self.read_record(e["base_ref"], full=True)
                if not sub:
                    continue
                for f in sub.get("data_frags", []):
                    if f["start_vcn"] == e["start_vcn"]:
                        frags.append(f)
                        seen.add(f["start_vcn"])
                        added = True
                        break
            self.mft_runs = collect()
            if not added:
                break

        self.mft_runs = merge_frags(
            [{"start_vcn": 0, "runs": self.mft_runs}], self.cluster, total_bytes)
        self.total_bytes = total_bytes
        self.total_records = total_bytes // self.rec_size

    def _read_runs(self, runs, size):
        """按 runlist 顺序读出内容（用于读取 $ATTRIBUTE_LIST 本体）。"""
        out = bytearray()
        left = size
        for lcn, count in runs:
            if left <= 0:
                break
            length = min(count * self.cluster, left)
            if lcn is None:
                out += b"\x00" * length
            else:
                out += self.dev.read_at(self.offset + lcn * self.cluster, length)
            left -= length
        return bytes(out)

    def read_record(self, index: int, full: bool = False):
        """按记录号读取并解析一条记录。"""
        want = index * self.rec_size
        walked = 0
        for lcn, count in self.mft_runs:
            run_bytes = count * self.cluster
            if want < walked + run_bytes:
                if lcn is None:
                    return None
                pos = self.offset + lcn * self.cluster + (want - walked)
                data = self.dev.read_at(pos, self.rec_size)
                return parse_record(data, self.rec_size, full=full)
            walked += run_bytes
        return None

    def scan(self, progress=None, should_stop=None):
        """顺序扫描整个 MFT，返回记录列表。

        每项为 (index, parent, name, is_dir, size)。
        """
        rows = []
        rec = self.rec_size
        walked = 0
        t0 = time.time()
        for lcn, count in self.mft_runs:
            run_bytes = count * self.cluster
            if lcn is None:
                walked += run_bytes
                continue
            run_start = self.offset + lcn * self.cluster
            done = 0
            while done < run_bytes:
                if should_stop and should_stop():
                    return rows
                step = min(BLOCK, run_bytes - done)
                step = (step // rec) * rec or rec
                try:
                    data = self.dev.read_at(run_start + done, step)
                except OSError:
                    done += step
                    continue
                n = len(data) // rec
                base_index = (walked + done) // rec
                for k in range(n):
                    meta = parse_record(data[k * rec:(k + 1) * rec], rec, full=False)
                    if meta is None or not meta["name"]:
                        continue
                    rows.append((base_index + k, meta["parent"], meta["name"],
                                 1 if meta["is_dir"] else 0, meta["size"]))
                done += n * rec
                if progress:
                    progress((walked + done) // rec, self.total_records, len(rows), time.time() - t0)
            walked += run_bytes
        if progress:
            progress(self.total_records, self.total_records, len(rows), time.time() - t0)
        return rows

    def read_file(self, index: int, dest_path: str) -> int:
        """把一个文件写到 dest_path，返回写入字节数。"""
        meta = self.read_record(index, full=True)
        if meta is None or meta["is_dir"]:
            raise ValueError("不是可读取的文件记录")
        written = 0
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        if meta.get("resident"):
            with open(dest_path, "wb") as f:
                f.write(meta["resident"])
            return len(meta["resident"])
        runs = merge_frags(meta.get("data_frags") or [], self.cluster, meta["size"])
        if not runs:
            raise ValueError("没有数据段")
        remaining = meta["size"]
        with open(dest_path, "wb") as f:
            for lcn, count in runs:
                if remaining <= 0:
                    break
                length = count * self.cluster
                if lcn is None:
                    zero = min(length, remaining)
                    f.write(b"\x00" * zero)
                    written += zero
                    remaining -= zero
                    continue
                pos = self.offset + lcn * self.cluster
                left = min(length, remaining)
                while left > 0:
                    chunk = min(BLOCK, left)
                    data = self.dev.read_at(pos, chunk)
                    f.write(data[:left] if len(data) > left else data)
                    got = len(data[:left]) if len(data) > left else len(data)
                    written += got
                    remaining -= got
                    left -= got
                    pos += got
                    if got == 0:
                        break
        return written


# --------------------------------------------------------------------------
# 分区探测
# --------------------------------------------------------------------------

def find_ntfs_partitions(disk_number: int):
    """优先用系统分区表，失败时回退到原始 MBR/GPT 解析。"""
    parts = list_partitions(disk_number)
    if parts:
        return [{"offset": p["offset"], "size": p["size"], "letter": p["letter"],
                 "label": (p["letter"] + ": " if p["letter"] else "") + (p["type"] or "")}
                for p in parts if p["size"]]
    return []


def open_device(disk_number: int, offset: int, letter: str = ""):
    """打开设备。

    优先用物理驱动器（卷没有盘符时也能用）。没有管理员权限时，
    退回到卷句柄（此时偏移归零，因为卷句柄本身就从分区起点开始）。
    """
    try:
        dev = RawDevice("\\\\.\\PhysicalDrive%d" % disk_number)
        return dev, offset, "physical"
    except PermissionError:
        if letter:
            dev = RawDevice("\\\\.\\%s:" % letter.upper())
            return dev, 0, "volume"
        raise


def diagnose(disk_number: int, offset: int, letter: str = ""):
    """只读诊断：引导扇区、MFT 前 16 条记录、根目录状态。"""
    dev, base, mode = open_device(disk_number, offset, letter)
    try:
        boot = parse_boot(dev.read_at(base, 512))
        if not boot:
            return {"ok": False, "reason": "引导扇区不是 NTFS，可能是其他文件系统或已损坏",
                    "mode": mode}

        vol = Volume(dev, base)
        base_mft = base + vol.boot["mft_lcn"] * vol.cluster
        bad_records = []
        checked = []
        for i in range(16):
            data = dev.read_at(base_mft + i * vol.rec_size, vol.rec_size)
            good = data[0:4] == b"FILE"
            if not good:
                bad_records.append(i)
            checked.append({"index": i, "ok": good})

        root_ok = 5 not in bad_records
        return {
            "ok": True,
            "mode": mode,
            "boot": {
                "cluster_size": boot["cluster_size"],
                "record_size": boot["record_size"],
                "total_gb": round(boot["total_sectors"] * boot["bytes_per_sector"] / 2 ** 30, 2),
                "mft_lcn": boot["mft_lcn"],
            },
            "mft_runs": len(vol.mft_runs),
            "mft_source": vol.source,
            "total_records": vol.total_records,
            "checked": checked,
            "bad_records": bad_records,
            "root_ok": root_ok,
            "verdict": (
                "文件系统结构完好，可直接浏览"
                if root_ok else
                "根目录记录（MFT 第 5 条）已损坏，Windows 会判定为 RAW。可用 MFT 全表扫描重建目录树"
            ),
            "timeouts": dev.timeouts,
            "errors": dev.errors,
        }
    finally:
        dev.close()
