# NTFS Read-Only Recovery Tool

**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [Español](README.es.md) | [Deutsch](README.de.md)

A local web tool that pulls files out of a damaged NTFS volume — the kind Windows reports as **RAW**, refuses to open, and keeps offering to format.

It reads the raw disk, scans the entire MFT (Master File Table), and rebuilds the directory tree from the parent reference stored inside every record. That means it still works when the root directory itself has been destroyed, and it keeps the original file names and folder structure.

**Read-only: it never writes to the source disk.**

---

## Why it works when other tools don't

Conventional recovery tools walk *volume → root directory → subdirectory → file*. If MFT record #5 — the root directory — is damaged, that chain breaks at the very first link. Windows marks the volume as RAW, most tools see an empty disk, and the ones that keep going fall back to signature carving, which throws away every file name.

NTFS file records are **self-contained**. Each 1024-byte record carries the file name, the record number of its parent directory, and the clusters holding its data. So this tool ignores the root directory entirely:

1. read the boot sector to get the MFT start position and cluster size
2. resolve `$MFT`'s own data runs — the MFT is a file too, and it can be fragmented into hundreds of extents spread over several records
3. scan every MFT record sequentially, pulling *parent reference + file name* from each
4. aggregate in reverse to rebuild the whole tree

---

## Features

- **Survives a destroyed root directory** — the most common reason a volume turns RAW
- **Keeps file names and folders** — unlike signature carving
- **Any file type** — photos, videos, documents, archives; there is no extension filter
- **Does not hang on bad sectors** — each block is read in a worker thread with a timeout; unreadable areas are skipped and counted
- **Local web interface** — disk list, diagnosis, scan progress, folder tree, type filter, live extraction progress
- **Scan results are cached** — reopening the tool does not require a rescan
- **No framework, no build step** — standard-library HTTP server and vanilla JavaScript
- **Strictly read-only** — no code path opens the source device for writing

---

## Requirements

- Windows (raw device access is Windows-specific)
- Python 3.8 or newer
- `dissect.ntfs` — optional; used only to resolve `$MFT`'s run list when it is heavily fragmented

---

## Quick start

```bash
git clone https://github.com/createBean/DiskRepairTool.git
cd DiskRepairTool
pip install dissect.ntfs
```

Then double-click `启动.bat`. It requests administrator rights by itself, installs the dependency if it is missing, and opens the interface in your browser.

Or start it manually:

```bash
python server.py
```

The server listens on `http://127.0.0.1:8765/`.

---

## How to use

1. **Select the disk** — pick the faulty drive by model and capacity
2. **Select the partition and diagnose** — checks the boot sector and the first 16 MFT records, and tells you whether the root directory is damaged
3. **Scan** — reads the entire MFT and rebuilds the tree; an MFT with millions of records takes a few minutes
4. **Browse and select** — walk the folder tree and tick what you need; the panel also lists the folders holding the most images
5. **Extract** — choose a destination folder and start copying; progress and byte counts update live

---

## Limitations

**Works for** NTFS logical corruption: missing root directory, damaged partition table, corrupted filesystem structures, a volume turned RAW after a power loss or an unsafe removal.

**Does not work for**:

- **Physical damage** — clicking, grinding, dropped or water-damaged drives. Do not power them on; they need a clean room.
- **exFAT / FAT32** — different on-disk structures, not implemented
- **Encrypted volumes** — BitLocker / VeraCrypt must be unlocked first
- **Overwritten records** — if a file's record now points at recycled clusters, the content cannot be recovered through it; only signature carving can help, and that loses names

---

## Troubleshooting

**The volume has no drive letter.** When running elevated, the tool opens `\\.\PhysicalDriveN`, so no drive letter is required. Without elevation it falls back to a volume handle and then does need one.

**Antivirus flags the tool.** Expected — the behaviour profile (walking every disk, reading gigabytes) resembles ransomware. The source is short enough to audit, and it only opens devices for reading.

**A built exe refuses to start.** Windows Smart App Control and SmartScreen block unsigned, unreputable executables. Run from source instead, or sign the binary.

**The scan finds nothing.** Check the diagnosis output first. If the boot sector is not NTFS, or the MFT run list cannot be resolved, the volume is either physically damaged or uses a different filesystem.

---

## Safety

- The program only reads from the source disk; no code path opens it for writing
- Administrator rights are needed solely to open a raw device handle
- The only write is into the destination folder you choose
- Recovery is best-effort — no tool can guarantee every file

---

## License

MIT
