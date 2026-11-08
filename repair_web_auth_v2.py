"""DSH 0.2.2-rc.1 local Web authentication hotfix v2. No credentials are stored.

Prepare: python repair_web_auth_v2.py
Install: python repair_web_auth_v2.py --install
Restore: python repair_web_auth_v2.py --restore
Close DeepSeek Harness before install or restore. The original archive is retained.
"""
import argparse
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parent
DEFAULT_ASAR = Path(r"F:\dsh\dsh desktop\resources\app.asar")
EXPECTED_MAIN_SHA256 = "f42ed25b0b844a63e515a3aff93dee89b781dae7fcbbbbfc93e04d95ad399ec1"
OLD = '\t\t\t\twelcomeBackend = await connectDesktopWelcome(ready.url, (input, init) => net.fetch(input, init), async () => (await session.defaultSession.cookies.get({ url: ready.url })).map((cookie) => `${cookie.name}=${cookie.value}`).join("; "));'
NEW = '''\t\t\t\t// Local hotfix v2: keep native welcome RPC on the shell-owned Host session.
\t\t\t\tconst welcomeCookie = hostCookie;
\t\t\t\tconst welcomeOrigin = new URL(ready.url).origin;
\t\t\t\twelcomeBackend = await connectDesktopWelcome(`${welcomeOrigin}/`, (input, init) => {
\t\t\t\t\tif (new URL(input).origin !== welcomeOrigin) throw new Error("desktop welcome: cross-origin Host request blocked");
\t\t\t\t\tconst headers = new Headers(init?.headers);
\t\t\t\t\theaders.set("cookie", welcomeCookie);
\t\t\t\t\treturn fetch(input, { ...init, headers, redirect: "error" });
\t\t\t\t}, async () => welcomeCookie);'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def parse(raw):
    first, header_size, payload_size, json_size = struct.unpack("<4I", raw[:16])
    if first != 4 or header_size != payload_size + 4:
        raise ValueError("Unsupported ASAR header")
    header = json.loads(raw[16:16 + json_size])
    return header, 8 + header_size


def entry(header, path):
    item = header
    for part in path.split("/"):
        item = item["files"][part]
    return item


def extract(raw, header, base, path):
    item = entry(header, path)
    start = base + int(item["offset"])
    return raw[start:start + item["size"]]


def walk(header, prefix=""):
    for name, item in header.get("files", {}).items():
        path = prefix + name
        if "files" in item:
            yield from walk(item, path + "/")
        else:
            yield path, item


def require_closed():
    if os.name != "nt":
        return
    result = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq DeepSeek Harness.exe", "/FO", "CSV", "/NH"], capture_output=True, check=True)
    if b'"DeepSeek Harness.exe"' in result.stdout:
        raise RuntimeError("Close DeepSeek Harness before replacing its application archive")


def prepare(target):
    raw = target.read_bytes()
    header, base = parse(raw)
    package = json.loads(extract(raw, header, base, "package.json"))
    if package["version"] != "0.2.2-rc.1":
        raise ValueError("This hotfix is restricted to DSH 0.2.2-rc.1")
    original = extract(raw, header, base, "lib/main.js")
    if sha(original) != EXPECTED_MAIN_SHA256:
        raise ValueError("Installed main.js differs from the diagnosed build; refusing to overwrite it")
    source = original.decode("utf-8")
    newline = "\r\n" if b"\r\n" in original else "\n"
    old, new = OLD.replace("\n", newline), NEW.replace("\n", newline)
    if source.count(old) != 1:
        raise ValueError("Expected unique welcome authentication call not found")
    patched = source.replace(old, new, 1).encode("utf-8")
    modified_header = copy.deepcopy(header)
    item = entry(modified_header, "lib/main.js")
    item["offset"] = str(len(raw) - base)
    item["size"] = len(patched)
    block_size = item.get("integrity", {}).get("blockSize", 4194304)
    item["integrity"] = {"algorithm": "SHA256", "hash": sha(patched), "blockSize": block_size, "blocks": [sha(patched[i:i + block_size]) for i in range(0, len(patched), block_size)]}
    encoded = json.dumps(modified_header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = (-len(encoded)) % 4
    archive_header = struct.pack("<4I", 4, 8 + len(encoded) + padding, 4 + len(encoded) + padding, len(encoded)) + encoded + bytes(padding)
    result = archive_header + raw[base:] + patched
    checked_header, checked_base = parse(result)
    count = 0
    for path, old_item in walk(header):
        new_item = entry(checked_header, path)
        if path == "lib/main.js":
            assert extract(result, checked_header, checked_base, path) == patched
        else:
            assert new_item == old_item, f"Unexpected metadata change: {path}"
        if "offset" in old_item:
            expected = patched if path == "lib/main.js" else extract(raw, header, base, path)
            assert extract(result, checked_header, checked_base, path) == expected, f"Archive content mismatch: {path}"
        count += 1
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    staging = work / "app.web-auth_v2.asar"
    staging.write_bytes(result)
    (work / "main.original.js").write_bytes(original)
    (work / "main.patched.mjs").write_bytes(patched)
    diff = "".join(difflib.unified_diff(source.splitlines(keepends=True), patched.decode("utf-8").splitlines(keepends=True), fromfile="lib/main.js.before", tofile="lib/main.js.web-auth_v2"))
    (ROOT / "web_auth_v2.patch").write_bytes(diff.encode("utf-8"))
    report = {"patch_version": "v2", "application_version": package["version"], "target": str(target), "original_archive_bytes": len(raw), "patched_archive_bytes": len(result), "original_archive_sha256": sha(raw), "patched_archive_sha256": sha(result), "original_main_sha256": sha(original), "patched_main_sha256": sha(patched), "archive_entries_checked": count, "changed_entries": ["lib/main.js"], "backup": str(target.parent.parent.parent / "repair-backups" / "web-auth_20261107_v2" / "app.asar"), "installed": False}
    (ROOT / "repair_result_v2.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return staging, report


def install(target, staging, report):
    require_closed()
    if sha(target.read_bytes()) != report["original_archive_sha256"]:
        raise RuntimeError("Application changed after preparation; refusing to install")
    backup = Path(report["backup"])
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if sha(backup.read_bytes()) != report["original_archive_sha256"]:
            raise RuntimeError("A different original backup already exists; refusing to overwrite")
    else:
        shutil.copy2(target, backup)
    if sha(backup.read_bytes()) != report["original_archive_sha256"]:
        raise RuntimeError("Backup hash mismatch")
    try:
        os.replace(staging, target)
        report["replacement_method"] = "atomic-replace"
    except PermissionError:
        # Electron-based tools may retain a read handle without delete sharing.
        # Do not stop MCode or alter ACLs; the verified original remains backed up.
        require_closed()
        if sha(target.read_bytes()) != report["original_archive_sha256"]:
            raise RuntimeError("Current archive changed; refusing in-place fallback")
        try:
            shutil.copyfile(staging, target)
            if sha(target.read_bytes()) != report["patched_archive_sha256"]:
                raise RuntimeError("In-place installation hash mismatch")
        except Exception:
            shutil.copyfile(backup, target)
            if sha(target.read_bytes()) != report["original_archive_sha256"]:
                raise RuntimeError("Rollback failed; restore the original archive manually")
            raise
        report["replacement_method"] = "verified-in-place-copy"
    if sha(target.read_bytes()) != report["patched_archive_sha256"]:
        raise RuntimeError("Installed archive hash mismatch; restore the backup")
    report["installed"] = True
    (ROOT / "repair_result_v2.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def restore(target):
    require_closed()
    report = json.loads((ROOT / "repair_result_v2.json").read_text(encoding="utf-8"))
    backup = Path(report["backup"])
    if sha(backup.read_bytes()) != report["original_archive_sha256"]:
        raise RuntimeError("Original backup hash mismatch")
    journal = target.with_name("app.restore-in-progress_v2.json")
    transaction = {"target": str(target.resolve()), "backup": str(backup.resolve()), "original": report["original_archive_sha256"], "patched": report["patched_archive_sha256"]}
    current = sha(target.read_bytes())
    retry = journal.exists() and json.loads(journal.read_text(encoding="utf-8")) == transaction
    if current == report["original_archive_sha256"]:
        if retry:
            journal.unlink()
        print("Original archive is already restored; backup retained.")
        return
    if current != report["patched_archive_sha256"] and not retry:
        raise RuntimeError("Current archive is not this patch or its interrupted restore; refusing to overwrite it")
    staging = target.with_name("app.restore_v2.asar")
    shutil.copy2(backup, staging)
    # Persist the recovery identity before touching the target. An interrupted
    # non-atomic copy can then be retried without permitting arbitrary overwrite.
    with journal.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(transaction, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(staging, target)
    except PermissionError:
        require_closed()
        if sha(target.read_bytes()) != current:
            raise RuntimeError("Current archive changed during restore; refusing fallback")
        shutil.copyfile(staging, target)
        staging.unlink()
    if sha(target.read_bytes()) != report["original_archive_sha256"]:
        raise RuntimeError("Restored archive hash mismatch; retry --restore using retained backup")
    journal.unlink()
    print("Original application archive restored; backup retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asar", type=Path, default=DEFAULT_ASAR)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.restore:
        restore(args.asar)
        return
    staging, report = prepare(args.asar)
    if args.install:
        install(args.asar, staging, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
