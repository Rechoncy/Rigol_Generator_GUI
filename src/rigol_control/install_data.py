"""Byte-preserving user-data snapshots used before and after installer upgrades.

This module uses only the standard library. Its command-line entry points run
before Qt is imported and never open a device or restore hardware connections.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from uuid import uuid4


FORMAT = "lab-control-center-user-data"
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))


class InstallDataError(RuntimeError):
    pass


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _linked(info: os.stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _safe_components(path: Path) -> None:
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise InstallDataError(f"数据路径包含链接或重解析点：{candidate}")


def _directory(path: Path, *, missing: bool = False) -> bool:
    _safe_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise InstallDataError(f"目录不存在：{path}") from None
    if not stat.S_ISDIR(info.st_mode):
        raise InstallDataError(f"路径不是普通目录：{path}")
    return True


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _disjoint(data: Path, backup: Path) -> None:
    if _within(data, backup) or _within(backup, data):
        raise InstallDataError("用户数据目录与备份目录必须分开，不能相同或相互包含")
    if data == Path(data.anchor) or backup == Path(backup.anchor):
        raise InstallDataError("不能将磁盘根目录作为用户数据或备份目录")


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise InstallDataError("备份包含无效相对路径")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (path.is_absolute() or str(path) != value
            or any(part in ("", ".", "..") or part.endswith((".", " "))
                   or any(ord(char) < 32 for char in part)
                   or part.split(".", 1)[0].upper() in _RESERVED for part in parts)):
        raise InstallDataError(f"备份包含不安全路径：{value!r}")
    return value


def _fingerprint(info: os.stat_result) -> tuple:
    # Windows path stat and fstat can expose different deprecated ctime
    # semantics (birth time versus change time). File identity, mtime, size
    # and mode remain consistent across the two APIs.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns if os.name != "nt" else None)


def _scan(root: Path, *, exclude_updates: bool) -> tuple[bool, dict[str, os.stat_result], dict[str, os.stat_result]]:
    if not _directory(root, missing=True):
        return False, {}, {}
    files, directories, names = {}, {}, set()

    def visit(folder: Path, prefix: str = "") -> None:
        _directory(folder)
        with os.scandir(folder) as items:
            entries = sorted(items, key=lambda item: item.name.casefold())
        for item in entries:
            relative = _relative(prefix + item.name)
            info = item.stat(follow_symlinks=False)
            if _linked(info):
                raise InstallDataError(f"数据中包含链接或重解析点：{relative}")
            # Windows DirEntry.stat may omit file identity fields. A real
            # lstat is needed for the subsequent path/handle identity checks.
            info = (folder / item.name).lstat()
            if _linked(info):
                raise InstallDataError(f"数据中包含链接或重解析点：{relative}")
            if (exclude_updates and not prefix and item.name.casefold() == "updates"
                    and stat.S_ISDIR(info.st_mode)):
                continue
            key = relative.casefold()
            if key in names:
                raise InstallDataError(f"数据路径大小写冲突：{relative}")
            names.add(key)
            if stat.S_ISDIR(info.st_mode):
                directories[relative] = info
                visit(folder / item.name, relative + "/")
            elif stat.S_ISREG(info.st_mode):
                files[relative] = info
            else:
                raise InstallDataError(f"数据中包含非普通文件：{relative}")

    visit(root)
    return True, files, directories


def _inventory_equal(first, second) -> bool:
    if first[0] != second[0]:
        return False
    return all({key: _fingerprint(value) for key, value in left.items()}
               == {key: _fingerprint(value) for key, value in right.items()}
               for left, right in zip(first[1:], second[1:]))


def _regular(path: Path) -> os.stat_result:
    _safe_components(path)
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode):
        raise InstallDataError(f"路径不是普通文件：{path}")
    return info


def _read_stable(path: Path, *, destination: Path | None = None,
                 expected: os.stat_result | None = None,
                 maximum: int | None = None) -> tuple[str, int, bytes | None, os.stat_result]:
    original = _regular(path)
    if expected is not None and _fingerprint(original) != _fingerprint(expected):
        raise InstallDataError(f"备份期间文件已变化：{path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    output = None
    digest, size, content = hashlib.sha256(), 0, bytearray() if maximum is not None else None
    try:
        with os.fdopen(os.open(path, flags), "rb") as source:
            if _fingerprint(os.fstat(source.fileno())) != _fingerprint(original):
                raise InstallDataError(f"读取时文件已变化：{path}")
            if destination is not None:
                output = destination.open("xb")
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if maximum is not None and size > maximum:
                    raise InstallDataError("备份清单过大")
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
                if output is not None and output.write(chunk) != len(chunk):
                    raise InstallDataError("数据备份未能完整写入")
            if (_fingerprint(os.fstat(source.fileno())) != _fingerprint(original)
                    or _fingerprint(_regular(path)) != _fingerprint(original)
                    or size != original.st_size):
                raise InstallDataError(f"读取期间文件已变化：{path}")
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
        return digest.hexdigest(), size, bytes(content) if content is not None else None, original
    finally:
        if output is not None:
            output.close()


def _encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n").encode("utf-8")


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        if stream.write(content) != len(content):
            raise InstallDataError("备份清单未完整写入")
        stream.flush()
        os.fsync(stream.fileno())


def _cleanup_stage(stage: Path, root: Path) -> None:
    # Only remove the uniquely created staging directory, never either input.
    if stage.parent != root or not stage.name.startswith(".pending-"):
        return
    try:
        _directory(stage)
        shutil.rmtree(stage)
    except (OSError, InstallDataError):
        pass


def snapshot_user_data(data_dir: Path | str, backup_root: Path | str) -> dict:
    """Publish one complete snapshot; fail without modifying the user's data."""
    data, root = _absolute(data_dir), _absolute(backup_root)
    stage = None
    try:
        _disjoint(data, root)
        _safe_components(root)
        inventory = _scan(data, exclude_updates=True)
        root.mkdir(parents=True, exist_ok=True)
        _directory(root)
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
        content_root = stage / "files"
        content_root.mkdir()
        for relative in sorted(inventory[2], key=lambda value: (value.count("/"), value)):
            (content_root / relative).mkdir()
        entries = []
        for relative, info in sorted(inventory[1].items()):
            destination = content_root / relative
            digest, size, _, _ = _read_stable(data / relative, destination=destination, expected=info)
            os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
            entries.append({"path": relative, "size": size, "sha256": digest, "mtime_ns": info.st_mtime_ns})
        if not _inventory_equal(inventory, _scan(data, exclude_updates=True)):
            raise InstallDataError("备份期间用户数据目录已变化，请关闭其他写入程序后重试")
        manifest = {
            "format": FORMAT, "schema_version": SCHEMA_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_data_dir": str(data), "source_existed": inventory[0],
            "excluded_top_level": ["updates"],
            "directories": sorted(inventory[2]), "files": entries,
        }
        raw = _encoded(manifest)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise InstallDataError("备份清单过大")
        _write_new(stage / "manifest.json", raw)
        verified, manifest_digest, _ = _verify_backup(stage)
        if verified != manifest:
            raise InstallDataError("备份清单回读不一致")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final = root / f"backup-{stamp}-{uuid4().hex[:12]}"
        if final.exists():
            raise InstallDataError("备份目标已存在，未覆盖原备份")
        os.rename(stage, final)
        stage = None
        return _report("backup", data, final, manifest, manifest_digest,
                       source_existed=inventory[0])
    except InstallDataError:
        raise
    except (OSError, ValueError, OverflowError) as exc:
        raise InstallDataError(f"用户数据备份失败：{exc}") from exc
    finally:
        if stage is not None:
            _cleanup_stage(stage, root)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InstallDataError("备份清单包含重复字段")
        value[key] = item
    return value


def _verify_backup(backup: Path) -> tuple[dict, str, dict[str, os.stat_result]]:
    _directory(backup)
    if {path.name for path in backup.iterdir()} != {"manifest.json", "files"}:
        raise InstallDataError("备份目录内容不完整或存在额外文件")
    digest, _, raw, _ = _read_stable(backup / "manifest.json", maximum=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InstallDataError("无法读取用户数据备份清单") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != SCHEMA_VERSION
            or type(manifest.get("source_existed")) is not bool
            or not isinstance(manifest.get("source_data_dir"), str)
            or manifest.get("excluded_top_level") != ["updates"]
            or not isinstance(manifest.get("files"), list)
            or not isinstance(manifest.get("directories"), list)):
        raise InstallDataError("用户数据备份格式不受支持")
    directories, files, names = set(), {}, set()
    for entry in manifest["directories"]:
        relative = _relative(entry)
        if relative.casefold() in names:
            raise InstallDataError("备份清单包含重复或大小写冲突路径")
        names.add(relative.casefold())
        directories.add(relative)
    for item in manifest["files"]:
        if (not isinstance(item, dict) or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                or type(item.get("mtime_ns")) is not int
                or not -(2 ** 63) <= item["mtime_ns"] < 2 ** 63):
            raise InstallDataError("备份文件校验记录无效")
        relative = _relative(item.get("path"))
        if relative.casefold() in names:
            raise InstallDataError("备份清单包含重复或大小写冲突路径")
        names.add(relative.casefold())
        files[relative] = item
    for relative in (*directories, *files):
        for parent in PurePosixPath(relative).parents:
            if str(parent) != "." and str(parent) not in directories:
                raise InstallDataError("备份清单缺少父目录")
    content = backup / "files"
    inventory = _scan(content, exclude_updates=False)
    if set(inventory[1]) != set(files) or set(inventory[2]) != directories:
        raise InstallDataError("备份文件列表与清单不一致")
    for relative, item in files.items():
        actual_digest, size, _, _ = _read_stable(content / relative, expected=inventory[1][relative])
        if size != item["size"] or actual_digest != item["sha256"]:
            raise InstallDataError(f"备份文件大小或 SHA-256 校验失败：{relative}")
    if not _inventory_equal(inventory, _scan(content, exclude_updates=False)):
        raise InstallDataError("校验期间备份目录已变化")
    return manifest, digest, inventory[1]


def _report(operation: str, data: Path, backup: Path, manifest: dict, digest: str, **extra) -> dict:
    return {"format": "lab-control-center-install-data-report", "schema_version": 1,
            "success": True, "operation": operation, "data_dir": str(data),
            "backup_path": str(backup), "manifest_sha256": digest,
            "source_data_dir": manifest["source_data_dir"],
            "file_count": len(manifest["files"]),
            "directory_count": len(manifest["directories"]),
            "byte_count": sum(item["size"] for item in manifest["files"]), **extra}


def _publish_file(temporary: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, destination)  # Windows refuses an existing target.
    else:
        os.link(temporary, destination)
        temporary.unlink()


def restore_user_data(data_dir: Path | str, backup_dir: Path | str) -> dict:
    """Verify retained files and restore only missing files without overwriting."""
    data, backup = _absolute(data_dir), _absolute(backup_dir)
    restored = 0
    try:
        _disjoint(data, backup)
        _directory(data, missing=True)
        manifest, digest, source_stats = _verify_backup(backup)
        missing = []
        for relative in manifest["directories"]:
            _directory(data / relative, missing=True)
        for item in manifest["files"]:
            target = data / item["path"]
            _safe_components(target)
            if target.exists():
                actual_digest, size, _, _ = _read_stable(target)
                if size != item["size"] or actual_digest != item["sha256"]:
                    raise InstallDataError(f"现有用户文件与备份不同，已保留原文件：{item['path']}")
            else:
                missing.append(item)
        data.mkdir(parents=True, exist_ok=True)
        _directory(data)
        for relative in sorted(manifest["directories"], key=lambda value: (value.count("/"), value)):
            target = data / relative
            target.mkdir(exist_ok=True)
            _directory(target)
        for item in missing:
            relative = item["path"]
            target = data / relative
            _directory(target.parent)
            temporary = target.parent / f".restore-{uuid4().hex}.tmp"
            try:
                actual_digest, size, _, _ = _read_stable(
                    backup / "files" / relative, destination=temporary, expected=source_stats[relative])
                if size != item["size"] or actual_digest != item["sha256"]:
                    raise InstallDataError(f"恢复期间备份内容已变化：{relative}")
                os.utime(temporary, ns=(item["mtime_ns"], item["mtime_ns"]))
                _safe_components(target)
                _publish_file(temporary, target)
                restored += 1
            finally:
                if temporary.exists():
                    temporary.unlink()
        # Confirm the full result, not only the subset that needed copying.
        for item in manifest["files"]:
            actual_digest, size, _, _ = _read_stable(data / item["path"])
            if size != item["size"] or actual_digest != item["sha256"]:
                raise InstallDataError(f"恢复后用户文件校验失败：{item['path']}")
        return _report("restore", data, backup, manifest, digest,
                       restored_file_count=restored, verified_file_count=len(manifest["files"]))
    except InstallDataError:
        raise
    except (OSError, ValueError, OverflowError) as exc:
        raise InstallDataError(f"用户数据恢复失败：{exc}") from exc


def _write_report(path: Path, result: dict) -> None:
    _safe_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}-{uuid4().hex}.tmp"
    try:
        _write_new(temporary, _encoded(result))
        _safe_components(path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_install_data_cli(argv: list[str]) -> int:
    """Headless installer contract; no Qt, GUI or hardware imports occur here."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--installer-data-backup", type=Path)
    mode.add_argument("--installer-data-restore", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.installer_data_backup is not None:
            if args.backup_root is None or args.backup is not None:
                parser.error("backup requires --backup-root and does not accept --backup")
            data, backup, operation = _absolute(args.installer_data_backup), _absolute(args.backup_root), "backup"
        else:
            if args.backup is None or args.backup_root is not None:
                parser.error("restore requires --backup and does not accept --backup-root")
            data, backup, operation = _absolute(args.installer_data_restore), _absolute(args.backup), "restore"
    except SystemExit as exc:
        return int(exc.code)
    report = _absolute(args.report)
    try:
        _safe_components(report)
        if _within(report, data) or (operation == "restore" and _within(report, backup)):
            raise InstallDataError("安装数据报告必须保存在用户数据和备份内容目录之外")
        if operation == "backup":
            result = snapshot_user_data(data, backup)
        else:
            result = restore_user_data(data, backup)
        _write_report(report, result)
        return 0
    except (InstallDataError, OSError, ValueError) as exc:
        result = {"format": "lab-control-center-install-data-report", "schema_version": 1,
                  "success": False, "operation": operation, "data_dir": str(data), "error": str(exc)}
        if not _within(report, data) and not (operation == "restore" and _within(report, backup)):
            try:
                _write_report(report, result)
            except (InstallDataError, OSError, ValueError):
                pass
        if sys.stderr is not None:
            print(f"安装用户数据处理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_install_data_cli(sys.argv[1:]))
