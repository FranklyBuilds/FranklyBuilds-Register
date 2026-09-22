#!/usr/bin/env python3
"""Archive and statically index a Windows installer.

The command is intentionally boring: it copies the installer, locates an
embedded ZIP without launching the executable, extracts safe ZIP members, and
writes JSON/Markdown evidence next to the archived files.  A later run can be
compared with an earlier archive to show added, removed, and changed files,
Python symbols, modules, and URL hosts.  By default output stays under
``artifacts/reverse``; ``--archive-root`` is available for an explicit test
workspace.

Only the Python standard library is used so this can run from the repository
virtual environment before the application dependencies are installed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import math
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE_ROOT = REPO_ROOT / "artifacts" / "reverse"
SNAPSHOT_NAME = "STATIC-ANALYSIS.json"
REPORT_NAME = "STATIC-ANALYSIS.md"
LEGACY_REPORT_NAME = "PROTOCOL-PAYMENT-ANALYSIS.md"
DIFF_NAME = "DIFF.md"
DIFF_JSON_NAME = "DIFF.json"
SCHEMA_VERSION = 2
ANALYZER_VERSION = 3

DEFAULT_MAX_INSTALLER_BYTES = 1 * 1024 * 1024 * 1024
DEFAULT_MAX_MEMBER_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_COMPRESSION_RATIO = 1_000
MAX_CONTAINER_CANDIDATES = 4_096

_LOCAL_HEADER = b"PK\x03\x04"
_EMPTY_HEADER = b"PK\x05\x06"
_SPANNED_HEADER = b"PK\x07\x08"
_EOCD = b"PK\x05\x06"
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SOURCE_EXTENSIONS = {
    ".py",
    ".pyw",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".cs",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".php",
    ".rb",
    ".swift",
    ".m",
    ".mm",
}


class InstallerAnalysisError(RuntimeError):
    """Raised for malformed input or an unsafe archive operation."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _safe_name(value: str) -> str:
    """Return a deterministic directory name suitable for Windows and POSIX."""

    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    value = value or "installer"
    if value.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        return f"archive-{value}"
    return value


def _normalise_member_name(name: str) -> str | None:
    """Normalize a ZIP member and reject absolute/path-traversal names."""

    raw = name.replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("/"):
        return None
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    # Avoid Windows device names and alternate-data-stream syntax while
    # materializing an untrusted archive on a Windows workstation.
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(
        any(ord(char) < 32 or ord(char) == 127 for char in part)
        or part != part.rstrip(" .")
        or re.search(r"[:*?\"<>|]", part)
        or part.split(".", 1)[0].upper() in reserved
        for part in parts
    ):
        return None
    return "/".join(parts)


def _member_collision_key(relative: str) -> str:
    """Return the Windows case-insensitive identity of a normalized path."""

    return "/".join(part.casefold().rstrip(" .") for part in relative.split("/"))


def _zip_end_candidates(data: bytes, start: int) -> Iterable[int]:
    """Yield plausible ZIP ends after ``start`` (including comments)."""

    cursor = start
    yielded = 0
    while True:
        position = data.find(_EOCD, cursor)
        if position < 0:
            return
        if position + 22 <= len(data):
            comment_size = int.from_bytes(data[position + 20 : position + 22], "little")
            end = position + 22 + comment_size
            if end <= len(data):
                yield end
                yielded += 1
                if yielded >= MAX_CONTAINER_CANDIDATES:
                    return
        cursor = position + 4


def _zip_end(data: bytes, start: int, zip_file: zipfile.ZipFile) -> int:
    """Find the end of the selected ZIP, including its optional comment."""

    search_from = start + max(0, int(getattr(zip_file, "start_dir", 0)))
    candidates = list(_zip_end_candidates(data, search_from))
    return candidates[0] if candidates else len(data)


def _open_embedded_zip(data: bytes) -> tuple[int, int, zipfile.ZipFile]:
    """Return ``(start, end, ZipFile)`` for the richest valid embedded ZIP."""

    candidates: list[tuple[int, int, int, int, zipfile.ZipFile]] = []
    signatures = (_LOCAL_HEADER, _EMPTY_HEADER, _SPANNED_HEADER)
    offsets: set[int] = set()
    for signature in signatures:
        cursor = 0
        while True:
            cursor = data.find(signature, cursor)
            if cursor < 0:
                break
            offsets.add(cursor)
            cursor += 1

    # A normal ZIP starts with a local-file header.  Empty ZIPs only have EOCD
    # and are still accepted when the input itself is a .zip file.
    candidate_offsets = sorted(offsets)
    if len(candidate_offsets) > MAX_CONTAINER_CANDIDATES:
        candidate_offsets = candidate_offsets[:MAX_CONTAINER_CANDIDATES]
    for offset in candidate_offsets:
        # A .NET single-file bundle has executable bytes after the embedded
        # ZIP.  Limit each attempt to a plausible EOCD boundary before asking
        # ``zipfile`` to parse it; otherwise its 64 KiB tail search misses the
        # central directory entirely.
        for end in _zip_end_candidates(data, offset):
            try:
                archive = zipfile.ZipFile(io.BytesIO(data[offset:end]))
                infos = archive.infolist()
                if infos or data[offset : offset + 4] == _EMPTY_HEADER:
                    names = [str(info.filename).replace("\\", "/").casefold() for info in infos]
                    payload_score = sum(
                        any(marker in name for marker in ("services/", "sms_tool/", "requirements.txt", "readme.md"))
                        for name in names
                    )
                    candidates.append((payload_score, len(infos), offset, end, archive))
                else:
                    archive.close()
            except (OSError, ValueError, zipfile.BadZipFile):
                continue

    if not candidates:
        for end in _zip_end_candidates(data, 0):
            try:
                archive = zipfile.ZipFile(io.BytesIO(data[:end]))
                return 0, end, archive
            except (OSError, ValueError, zipfile.BadZipFile):
                continue
        raise InstallerAnalysisError("未找到可读取的内嵌 ZIP")

    # Prefer the candidate with the most entries; for ties choose the earliest
    # offset, which is the usual single-file bundle layout.
    # Known source markers beat everything else.  When no marker exists (for
    # example a generic ZIP containing another ZIP), prefer the earliest
    # container instead of silently selecting a nested dependency by entry
    # count.
    candidates.sort(key=lambda item: (-item[0], item[2], -item[3], -item[1]))
    _, _, start, end, archive = candidates[0]
    for _, _, _, _, other in candidates[1:]:
        other.close()
    return start, end, archive


def _pe_description(data: bytes) -> str:
    if len(data) < 64 or data[:2] != b"MZ":
        return "非 PE 文件（仍按 ZIP 容器解析）"
    pe_offset = int.from_bytes(data[0x3C:0x40], "little")
    if pe_offset + 26 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return "MZ 文件（PE 头未确认）"
    machine = int.from_bytes(data[pe_offset + 4 : pe_offset + 6], "little")
    optional_magic = int.from_bytes(data[pe_offset + 24 : pe_offset + 26], "little")
    arch = {0x8664: "x64", 0x14C: "x86", 0xAA64: "ARM64"}.get(machine, f"machine-0x{machine:X}")
    fmt = {0x20B: "PE32+", 0x10B: "PE32"}.get(optional_magic, "PE")
    return f"{fmt} {arch}"


def _safe_extract(
    archive: zipfile.ZipFile,
    destination: Path,
    *,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract files and return manifest records plus skipped-member warnings."""

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    infos = archive.infolist()
    if len(infos) > max_entries:
        raise InstallerAnalysisError(
            f"ZIP 条目数超过上限：{len(infos)} > {max_entries}"
        )
    declared_total = 0
    actual_total = 0
    seen: set[str] = set()
    for info in infos:
        relative = _normalise_member_name(info.filename)
        if relative is None:
            warnings.append(f"跳过不安全 ZIP 条目：{info.filename}")
            continue
        if info.is_dir() or info.filename.endswith(("/", "\\")):
            continue
        collision_key = _member_collision_key(relative)
        if collision_key in seen:
            warnings.append(f"跳过重复 ZIP 条目：{relative}")
            continue
        declared_size = int(info.file_size)
        compressed_size = int(info.compress_size)
        if declared_size > max_member_bytes:
            raise InstallerAnalysisError(
                f"ZIP 条目超过单文件大小上限：{relative} ({declared_size} bytes)"
            )
        if compressed_size and declared_size / compressed_size > max_compression_ratio:
            raise InstallerAnalysisError(
                f"ZIP 条目压缩比超过上限：{relative} ({declared_size}/{compressed_size})"
            )
        declared_total += declared_size
        if declared_total > max_total_bytes:
            raise InstallerAnalysisError(
                f"ZIP 解包总大小超过上限：{declared_total} > {max_total_bytes}"
            )
        target = destination.joinpath(*relative.split("/"))
        # Windows treats case, trailing dots and spaces equivalently.  Check
        # every parent before creating it so ``foo`` and ``foo/bar`` cannot
        # silently overwrite one another.
        parent = target.parent
        while parent != destination:
            if parent.is_file():
                warnings.append(f"跳过文件/目录冲突条目：{relative}")
                break
            parent = parent.parent
        else:
            if target.is_dir():
                warnings.append(f"跳过文件/目录冲突条目：{relative}")
                continue
            seen.add(collision_key)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("wb") as output:
                    digest = hashlib.sha256()
                    size = 0
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        actual_total += len(block)
                        if size > max_member_bytes:
                            raise InstallerAnalysisError(
                                f"ZIP 条目实际解压大小超过上限：{relative}"
                            )
                        if actual_total > max_total_bytes:
                            raise InstallerAnalysisError(
                                f"ZIP 实际解包总大小超过上限：{actual_total} > {max_total_bytes}"
                            )
                        output.write(block)
                        digest.update(block)
            except InstallerAnalysisError:
                raise
            except (OSError, RuntimeError, ValueError, NotImplementedError, zipfile.BadZipFile) as exc:
                raise InstallerAnalysisError(f"解包条目失败：{relative}") from exc
            records.append(
                {
                    "path": relative,
                    "size": size,
                    "compressed_size": compressed_size,
                    "crc32": f"{int(info.CRC) & 0xFFFFFFFF:08X}",
                    "sha256": digest.hexdigest().upper(),
                }
            )
            continue
    records.sort(key=lambda item: item["path"])
    return records, warnings


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _payload_member_path(payload: Path, relative: str) -> Path | None:
    """Resolve a manifest member while keeping it inside ``payload``."""

    normalized = _normalise_member_name(relative)
    if normalized is None:
        return None
    payload_root = payload.resolve()
    candidate = payload_root.joinpath(*normalized.split("/")).resolve()
    if candidate != payload_root and payload_root not in candidate.parents:
        return None
    return candidate


def _hosts_from_text(source: str) -> set[str]:
    hosts: set[str] = set()
    for match in _URL_RE.finditer(source):
        value = match.group(0).rstrip(".,;:)]}'\"}>\u3002\uff09")
        try:
            host = urlsplit(value).hostname
        except ValueError:
            host = None
        if host:
            # URL templates often contain ``{path}``, escaped dots, or a
            # non-ASCII sentence immediately after the literal.  Keep only a
            # concrete DNS/IP/localhost host so the diff stays actionable.
            host = host.casefold().replace("\\.", ".")
            host = re.split(r"[\[{(<\s]", host, maxsplit=1)[0].strip(".")
            if not host or "{" in host or "}" in host or any(ord(char) > 127 for char in host):
                continue
            if host != "localhost" and "." not in host and not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host):
                continue
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                continue
            hosts.add(host)
    return hosts


def _python_file_index(path: Path, relative: str) -> dict[str, Any]:
    source = _read_text(path)
    result: dict[str, Any] = {
        "path": relative,
        "size": path.stat().st_size if path.is_file() else 0,
        "sha256": _hash_file(path) if path.is_file() else "",
        "functions": [],
        "classes": [],
        "imports": [],
        "hosts": sorted(_hosts_from_text(source)),
        "parsed": False,
    }
    if not source:
        return result
    try:
        tree = ast.parse(source, filename=relative)
    except (SyntaxError, ValueError):
        return result
    result["parsed"] = True
    functions: set[str] = set()
    classes: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    result["functions"] = sorted(functions)
    result["classes"] = sorted(classes)
    result["imports"] = sorted(imports)
    return result


def _source_index(payload: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    functions: dict[str, set[str]] = {}
    classes: dict[str, set[str]] = {}
    imports: set[str] = set()
    hosts: set[str] = set()
    parsed_files = 0
    for entry in entries:
        relative = str(entry["path"])
        path = _payload_member_path(payload, relative)
        if path is None:
            continue
        suffix = path.suffix.casefold()
        if suffix not in _SOURCE_EXTENSIONS:
            continue
        source = _read_text(path)
        hosts.update(_hosts_from_text(source))
        if suffix not in {".py", ".pyw"}:
            files.append(
                {
                    "path": relative,
                    "size": int(entry["size"]),
                    "sha256": str(entry["sha256"]),
                    "functions": [],
                    "classes": [],
                    "imports": [],
                    "hosts": sorted(_hosts_from_text(source)),
                    "parsed": False,
                }
            )
            continue
        item = _python_file_index(path, relative)
        files.append(item)
        if item["parsed"]:
            parsed_files += 1
        for name in item["functions"]:
            functions.setdefault(name, set()).add(relative)
        for name in item["classes"]:
            classes.setdefault(name, set()).add(relative)
        imports.update(item["imports"])
    return {
        "files": sorted(files, key=lambda item: item["path"]),
        "functions": sorted(functions),
        "function_locations": {name: sorted(paths) for name, paths in sorted(functions.items())},
        "classes": sorted(classes),
        "class_locations": {name: sorted(paths) for name, paths in sorted(classes.items())},
        "imports": sorted(imports),
        "hosts": sorted(hosts),
        "source_file_count": len(files),
        "parsed_python_file_count": parsed_files,
    }


def _module_index(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group source files into stable, human-readable module directories."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        relative = str(entry["path"])
        path = PurePosixPath(relative)
        if path.suffix.casefold() not in _SOURCE_EXTENSIONS:
            continue
        parts = path.parts
        if len(parts) == 1:
            key = "[root]"
        elif parts[0] == "services" and len(parts) >= 2:
            # Group a direct file under its service directory; a nested
            # directory becomes the module key (for example
            # services/protocol-payment/momo).
            if len(parts) == 2 or (len(parts) == 3 and path.suffix):
                key = "/".join(parts[:2])
            else:
                key = "/".join(parts[:3])
        elif parts[0] == "sms_tool":
            key = "sms_tool" if len(parts) < 3 else "/".join(parts[:2])
        else:
            key = parts[0]
        groups.setdefault(key, []).append(entry)
    output: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        output.append(
            {
                "key": key,
                "file_count": len(items),
                "files": sorted(str(item["path"]) for item in items),
                "total_size": sum(int(item["size"]) for item in items),
                "sha256": _sha256_bytes(
                    "\n".join(f"{item['path']}:{item['sha256']}" for item in sorted(items, key=lambda value: value["path"])).encode()
                ),
            }
        )
    return output


def _payload_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    extensions: dict[str, int] = {}
    total_size = 0
    for entry in entries:
        suffix = Path(str(entry["path"])).suffix.casefold() or "[no extension]"
        extensions[suffix] = extensions.get(suffix, 0) + 1
        total_size += int(entry["size"])
    return {
        "entry_count": len(entries),
        "file_count": len(entries),
        "directory_count": 0,
        "total_size": total_size,
        "extensions": dict(sorted(extensions.items())),
    }


def _manifest_for_archive(archive_dir: Path) -> dict[str, Any]:
    path = archive_dir / "MANIFEST.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerAnalysisError(f"归档清单读取失败：{path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise InstallerAnalysisError(f"归档清单格式不受支持：{path}")
    return value


def _snapshot_from_archive(archive_dir: Path, *, prefer_manifest: bool = False) -> dict[str, Any]:
    """Load a generated snapshot, or derive one from an older manifest."""

    snapshot_path = archive_dir / SNAPSHOT_NAME
    if snapshot_path.is_file() and not prefer_manifest:
        try:
            value = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("entries"), list):
                return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    manifest = _manifest_for_archive(archive_dir)
    entries = [
        item
        for item in manifest["entries"]
        if isinstance(item, dict)
        and item.get("path")
        and _normalise_member_name(str(item["path"])) == str(item["path"]).replace("\\", "/")
    ]
    payload_value = manifest.get("payload_directory") or "payload"
    payload = Path(payload_value)
    if not payload.is_absolute():
        payload = archive_dir / payload
    else:
        archive_resolved = archive_dir.resolve()
        try:
            payload.resolve().relative_to(archive_resolved)
        except ValueError:
            # Older manifests stored an absolute path.  A moved archive must
            # use its local ``payload`` directory rather than reading a path
            # outside the archive tree.
            local_payload = archive_dir / "payload"
            if not local_payload.is_dir():
                raise InstallerAnalysisError(
                    f"归档 payload 路径越界：{payload}"
                )
            payload = local_payload
    source = _source_index(payload, entries)
    return {
        "schema": SCHEMA_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "archive_name": archive_dir.name,
        "installer": dict(manifest.get("installer") or {}),
        "payload": _payload_stats(entries),
        "entries": entries,
        "source_index": source,
        "modules": _module_index(entries),
        "safety": {
            "read_only": True,
            "installer_executed": False,
            "credentials_loaded": False,
            "network_requests": False,
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _markdown_report(snapshot: dict[str, Any], warnings: list[str]) -> str:
    installer = snapshot.get("installer") or {}
    payload = snapshot.get("payload") or {}
    source = snapshot.get("source_index") or {}
    lines = [
        "# Installer Static Analysis",
        "",
        f"- Archive: `{_md(snapshot.get('archive_name', ''))}`",
        f"- Installer SHA-256: `{_md(installer.get('sha256', ''))}`",
        f"- Installer size: `{installer.get('size', 0):,}` bytes",
        f"- Container: `{_md(installer.get('pe_format', ''))}`",
        f"- Embedded ZIP: `{installer.get('embedded_zip_start', '')}`–`{installer.get('embedded_zip_end', '')}`",
        f"- ZIP selection: `{_md(installer.get('zip_selection', ''))}`",
        f"- ZIP entries: `{installer.get('zip_entries', payload.get('entry_count', 0))}`; extracted files: `{payload.get('file_count', 0)}`",
        "- Analysis mode: read-only archive and AST inspection; the installer and payload entry points were not executed.",
        "",
        "## Payload",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Files | {payload.get('file_count', 0)} |",
        f"| Total source/payload bytes | {payload.get('total_size', 0):,} |",
        f"| Source files indexed | {source.get('source_file_count', 0)} |",
        f"| Python files parsed | {source.get('parsed_python_file_count', 0)} |",
        "",
        "## Modules",
        "",
        "| Module | Files | Bytes |",
        "| --- | ---: | ---: |",
    ]
    modules = snapshot.get("modules") or []
    if modules:
        for module in modules:
            lines.append(f"| `{_md(module['key'])}` | {module['file_count']} | {module['total_size']:,} |")
    else:
        lines.append("| _none detected_ | 0 | 0 |")
    lines.extend(
        [
            "",
            "## Symbols And Hosts",
            "",
            f"- Functions: `{len(source.get('functions') or [])}`",
            f"- Classes: `{len(source.get('classes') or [])}`",
            f"- Imports: `{len(source.get('imports') or [])}`",
            f"- External hosts: `{len(source.get('hosts') or [])}`",
        ]
    )
    if source.get("hosts"):
        lines.extend(["", "### Hosts", "", *[f"- `{_md(host)}`" for host in source["hosts"]]])
    if source.get("functions"):
        lines.extend(["", "### Python Functions", "", *[f"- `{_md(name)}`" for name in source["functions"]]])
    if source.get("classes"):
        lines.extend(["", "### Python Classes", "", *[f"- `{_md(name)}`" for name in source["classes"]]])
    if warnings:
        lines.extend(["", "## Warnings", "", *[f"- {_md(warning)}" for warning in warnings]])
    lines.extend(
        [
            "",
            "## Runtime Boundary",
            "",
            "The archive is evidence only. No executable, credentials, subprocess, or network request is used by this analysis.",
            "",
        ]
    )
    return "\n".join(lines)


def _set_map(items: Iterable[Any], key: str | None = None) -> set[str]:
    if key is None:
        return {str(item) for item in items}
    return {str(item.get(key)) for item in items if isinstance(item, dict) and item.get(key)}


def _md(value: Any) -> str:
    """Escape values interpolated into inline Markdown code spans/tables."""

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _location_set(source: dict[str, Any], field: str, fallback: str) -> set[str]:
    locations = source.get(field) or {}
    if isinstance(locations, dict):
        return {
            f"{name}@{path}"
            for name, paths in locations.items()
            for path in (paths if isinstance(paths, list) else [paths])
        }
    return {f"{name}@<unknown>" for name in source.get(fallback, [])}


def _installer_changes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields = (
        "sha256",
        "size",
        "md5",
        "pe_format",
        "embedded_zip_start",
        "embedded_zip_end",
        "embedded_zip_size",
        "embedded_zip_sha256",
        "zip_entries",
        "zip_selection",
    )
    old = previous.get("installer") or {}
    new = current.get("installer") or {}
    return {
        field: {"previous": old.get(field), "current": new.get(field)}
        for field in fields
        if old.get(field) != new.get(field)
    }


def _archive_name_key(name: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.split(r"(\d+)", name)
        if token
    )


def _diff_snapshot(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    old_files = {str(item["path"]): item for item in previous.get("entries", []) if isinstance(item, dict) and item.get("path")}
    new_files = {str(item["path"]): item for item in current.get("entries", []) if isinstance(item, dict) and item.get("path")}
    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    changed = sorted(
        path
        for path in set(old_files) & set(new_files)
        if str(old_files[path].get("sha256", "")) != str(new_files[path].get("sha256", ""))
        or int(old_files[path].get("size", 0)) != int(new_files[path].get("size", 0))
    )
    old_source = previous.get("source_index") or {}
    new_source = current.get("source_index") or {}
    old_modules = _set_map(previous.get("modules") or [], "key")
    new_modules = _set_map(current.get("modules") or [], "key")
    old_functions = _set_map(old_source.get("functions") or [])
    new_functions = _set_map(new_source.get("functions") or [])
    old_classes = _set_map(old_source.get("classes") or [])
    new_classes = _set_map(new_source.get("classes") or [])
    return {
        "previous_archive": previous.get("archive_name", ""),
        "current_archive": current.get("archive_name", ""),
        "installer": {"changed": _installer_changes(previous, current)},
        "files": {"added": added, "removed": removed, "changed": changed},
        "functions": {
            "added": sorted(new_functions - old_functions),
            "removed": sorted(old_functions - new_functions),
            "locations_added": sorted(
                _location_set(new_source, "function_locations", "functions")
                - _location_set(old_source, "function_locations", "functions")
            ),
            "locations_removed": sorted(
                _location_set(old_source, "function_locations", "functions")
                - _location_set(new_source, "function_locations", "functions")
            ),
        },
        "classes": {
            "added": sorted(new_classes - old_classes),
            "removed": sorted(old_classes - new_classes),
            "locations_added": sorted(
                _location_set(new_source, "class_locations", "classes")
                - _location_set(old_source, "class_locations", "classes")
            ),
            "locations_removed": sorted(
                _location_set(old_source, "class_locations", "classes")
                - _location_set(new_source, "class_locations", "classes")
            ),
        },
        "imports": {
            "added": sorted(_set_map(new_source.get("imports") or []) - _set_map(old_source.get("imports") or [])),
            "removed": sorted(_set_map(old_source.get("imports") or []) - _set_map(new_source.get("imports") or [])),
        },
        "hosts": {
            "added": sorted(_set_map(new_source.get("hosts") or []) - _set_map(old_source.get("hosts") or [])),
            "removed": sorted(_set_map(old_source.get("hosts") or []) - _set_map(new_source.get("hosts") or [])),
        },
        "modules": {"added": sorted(new_modules - old_modules), "removed": sorted(old_modules - new_modules)},
    }


def _diff_markdown(diff: dict[str, Any]) -> str:
    lines = [
        "# Installer Version Diff",
        "",
        f"- Previous: `{_md(diff['previous_archive'])}`",
        f"- Current: `{_md(diff['current_archive'])}`",
        "",
        "## Files",
        "",
        f"- Added: `{len(diff['files']['added'])}`",
        f"- Removed: `{len(diff['files']['removed'])}`",
        f"- Changed: `{len(diff['files']['changed'])}`",
    ]
    installer_changes = (diff.get("installer") or {}).get("changed", {})
    lines.extend(["", "## Installer Container", "", f"- Changed fields: `{len(installer_changes)}`"])
    if installer_changes:
        lines.extend(
            [
                "",
                "Changed",
                "",
                *[
                f"- `{_md(field)}`: `{_md(values.get('previous'))}` -> `{_md(values.get('current'))}`"
                for field, values in sorted(installer_changes.items())
                ],
            ]
        )
    for label, key in (("Added", "added"), ("Removed", "removed"), ("Changed", "changed")):
        values = diff["files"][key]
        if values:
            lines.extend(["", f"### {label} Files", "", *[f"- `{_md(value)}`" for value in values]])
    for title, key in (
        ("Modules", "modules"),
        ("Functions", "functions"),
        ("Classes", "classes"),
        ("Imports", "imports"),
        ("Hosts", "hosts"),
    ):
        section = diff[key]
        lines.extend(["", f"## {title}", "", f"- Added: `{len(section['added'])}`", f"- Removed: `{len(section['removed'])}`"])
        if section["added"]:
            lines.extend(["", "Added", "", *[f"- `{_md(value)}`" for value in section["added"]]])
        if section["removed"]:
            lines.extend(["", "Removed", "", *[f"- `{_md(value)}`" for value in section["removed"]]])
        for location_label in ("locations_added", "locations_removed"):
            if section.get(location_label):
                label = "Locations added" if location_label == "locations_added" else "Locations removed"
                lines.extend(["", label, "", *[f"- `{_md(value)}`" for value in section[location_label]]])
    if all(
        not values
        for group in (
            diff["files"],
            {"changed": installer_changes},
            diff["modules"],
            diff["functions"],
            diff["classes"],
            diff["imports"],
            diff["hosts"],
        )
        for values in group.values()
    ):
        lines.extend(["", "No differences detected."])
    lines.append("")
    return "\n".join(lines)


def _find_previous_archive(archive_root: Path, current: Path) -> Path | None:
    candidates = []
    for item in archive_root.iterdir() if archive_root.is_dir() else []:
        if not item.is_dir() or item.resolve() == current.resolve() or not (item / "MANIFEST.json").is_file():
            continue
        try:
            snapshot = _snapshot_from_archive(item)
        except (InstallerAnalysisError, OSError, ValueError, TypeError, KeyError):
            continue
        name = str(snapshot.get("archive_name") or item.name)
        try:
            mtime = item.stat().st_mtime_ns
        except OSError:
            mtime = 0
        candidates.append((_archive_name_key(name), mtime, item))
    return sorted(candidates, key=lambda pair: (pair[0], pair[1]))[-1][2] if candidates else None


def _write_baseline_diff(archive_dir: Path, snapshot: dict[str, Any]) -> None:
    """Write a deterministic first-version diff for an existing archive."""

    source = snapshot.get("source_index") or {}
    entries = snapshot.get("entries") or []
    baseline = {
        "previous_archive": None,
        "current_archive": snapshot.get("archive_name", archive_dir.name),
        "installer": {
            "changed": {
                field: {"previous": None, "current": value}
                for field, value in (snapshot.get("installer") or {}).items()
                if field in {"sha256", "size", "md5", "pe_format", "embedded_zip_start", "embedded_zip_end", "embedded_zip_size", "embedded_zip_sha256", "zip_entries", "zip_selection"}
            }
        },
        "files": {"added": sorted(item["path"] for item in entries), "removed": [], "changed": []},
        "functions": {
            "added": source.get("functions", []),
            "removed": [],
            "locations_added": sorted(_location_set(source, "function_locations", "functions")),
            "locations_removed": [],
        },
        "classes": {
            "added": source.get("classes", []),
            "removed": [],
            "locations_added": sorted(_location_set(source, "class_locations", "classes")),
            "locations_removed": [],
        },
        "imports": {"added": source.get("imports", []), "removed": []},
        "hosts": {"added": source.get("hosts", []), "removed": []},
        "modules": {"added": [item["key"] for item in snapshot.get("modules", [])], "removed": []},
    }
    _write_json(archive_dir / DIFF_JSON_NAME, baseline)
    (archive_dir / DIFF_NAME).write_text(
        "# Installer Version Diff\n\nNo previous archive was selected; this archive is the baseline for installer comparison only. The source repository remains the product behavior baseline.\n",
        encoding="utf-8",
    )


def _write_index(archive_root: Path) -> None:
    rows: list[tuple[str, str, int, str, Path]] = []
    for item in sorted(archive_root.iterdir() if archive_root.is_dir() else [], key=lambda path: path.name):
        if not item.is_dir() or not (item / "MANIFEST.json").is_file():
            continue
        try:
            snapshot = _snapshot_from_archive(item)
        except (InstallerAnalysisError, OSError, ValueError, TypeError, KeyError):
            continue
        installer = snapshot.get("installer") or {}
        rows.append((item.name, str(installer.get("sha256") or ""), len(snapshot.get("entries") or []), str(installer.get("size") or 0), item))
    rows.sort(key=lambda row: _archive_name_key(row[0]))
    lines = [
        "# Installer Archive Index",
        "",
        "Generated by `app/scripts/analyze_installer.py`. All entries are static archives; installers are never launched.",
        "",
        "| Archive | Installer SHA-256 | Files | Installer bytes |",
        "| --- | --- | ---: | ---: |",
    ]
    if rows:
        for name, digest, count, size, _ in rows:
            safe_name = _md(name)
            try:
                size_value = int(size)
            except (TypeError, ValueError):
                size_value = 0
            lines.append(f"| [`{safe_name}`](./{safe_name}/) | `{_md(digest)}` | {count} | {size_value:,} |")
    else:
        lines.append("| _none_ |  | 0 | 0 |")
    lines.append("")
    (archive_root / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    latest_path = archive_root / "LATEST-DIFF.md"
    if rows:
        latest = rows[-1][4]
        diff_path = latest / DIFF_NAME
        diff_body = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else "No diff has been generated yet.\n"
        latest_path.write_text(
            f"# Latest Installer Diff\n\nArchive: [`{_md(latest.name)}`](./{_md(latest.name)}/)\n\n{diff_body}",
            encoding="utf-8",
        )
    elif latest_path.exists():
        latest_path.unlink()


def _analyze_installer(
    installer_path: Path,
    *,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    version: str | None = None,
    previous: Path | None = None,
    force: bool = False,
    max_installer_bytes: int = DEFAULT_MAX_INSTALLER_BYTES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> Path:
    """Archive and analyze ``installer_path``. Return the archive directory."""

    if min(
        max_installer_bytes,
        max_member_bytes,
        max_total_bytes,
        max_entries,
    ) <= 0 or not math.isfinite(max_compression_ratio) or max_compression_ratio <= 0:
        raise InstallerAnalysisError("解包大小、条目数和压缩比上限必须为正数")
    installer_path = installer_path.expanduser().resolve()
    if not installer_path.is_file():
        raise InstallerAnalysisError(f"安装包不存在：{installer_path}")
    installer_size = installer_path.stat().st_size
    if installer_size > max_installer_bytes:
        raise InstallerAnalysisError(
            f"安装包超过大小上限：{installer_size} > {max_installer_bytes}"
        )
    # Read before any forced replacement.  This also supports analyzing an
    # installer directly from a previous archive without deleting the source
    # before it has been copied.
    data = installer_path.read_bytes()
    installer_digest = _sha256_bytes(data)
    archive_root = archive_root.expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_name = _safe_name(version or installer_path.stem)
    archive_dir = archive_root / archive_name
    if archive_dir.exists() and not force:
        old_manifest = archive_dir / "MANIFEST.json"
        if old_manifest.is_file():
            try:
                old = _manifest_for_archive(archive_dir)
                if str((old.get("installer") or {}).get("sha256", "")).casefold() == installer_digest.casefold():
                    archived_value = old.get("archived_installer") or ""
                    archived_path = Path(str(archived_value))
                    if not archived_path.is_absolute():
                        archived_path = archive_dir / archived_path
                    try:
                        archived_path.resolve().relative_to(archive_dir.resolve())
                        archived_inside_archive = True
                    except ValueError:
                        archived_inside_archive = False
                    if (
                        not archived_inside_archive
                        or not archived_path.is_file()
                        or not (archive_dir / "payload").is_dir()
                    ):
                        raise InstallerAnalysisError(
                            f"归档证据不完整（使用 --force 重建）：{archive_dir}"
                        )
                    # Idempotent rerun.  Older hand-built archives only have a
                    # manifest, so derive the new machine-readable snapshot
                    # beside it without touching the original evidence.
                    snapshot_path = archive_dir / SNAPSHOT_NAME
                    try:
                        current_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        current_snapshot = {}
                    if (
                        current_snapshot.get("analyzer_version") != ANALYZER_VERSION
                        or "zip_selection" not in (current_snapshot.get("installer") or {})
                    ):
                        snapshot = _snapshot_from_archive(archive_dir, prefer_manifest=True)
                        snapshot["generated_at"] = datetime.now(timezone.utc).isoformat()
                        snapshot["analyzer_version"] = ANALYZER_VERSION
                        snapshot.setdefault("installer", {}).setdefault(
                            "zip_selection",
                            "historical manifest; selection offset recorded in archive",
                        )
                        _write_json(snapshot_path, snapshot)
                        (archive_dir / REPORT_NAME).write_text(
                            _markdown_report(snapshot, snapshot.get("warnings", [])),
                            encoding="utf-8",
                        )
                        _write_baseline_diff(archive_dir, snapshot)
                    else:
                        try:
                            existing_diff = json.loads(
                                (archive_dir / DIFF_JSON_NAME).read_text(encoding="utf-8")
                            )
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            existing_diff = {}
                        if "imports" not in existing_diff:
                            snapshot = _snapshot_from_archive(archive_dir)
                            _write_baseline_diff(archive_dir, snapshot)
                    try:
                        refreshed_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                        (archive_dir / REPORT_NAME).write_text(
                            _markdown_report(refreshed_snapshot, refreshed_snapshot.get("warnings", [])),
                            encoding="utf-8",
                        )
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                        pass
                    _write_index(archive_root)
                    return archive_dir
            except InstallerAnalysisError:
                pass
        if not force:
            raise InstallerAnalysisError(f"归档目录已存在（使用 --force 覆盖）：{archive_dir}")
    if archive_dir.exists():
        resolved_root = archive_root.resolve()
        if resolved_root not in archive_dir.resolve().parents:
            raise InstallerAnalysisError("归档路径越界")
        shutil.rmtree(archive_dir)

    archive: zipfile.ZipFile | None = None
    try:
        start, end, archive = _open_embedded_zip(data)
        original = archive_dir / "original"
        payload = archive_dir / "payload"
        original.mkdir(parents=True, exist_ok=True)
        payload.mkdir(parents=True, exist_ok=True)
        archived_name = installer_path.name
        if archived_name.casefold() == "payload.zip":
            archived_name = "source-installer.zip"
        archived_installer = original / archived_name
        archived_installer.write_bytes(data)
        payload_zip = original / "payload.zip"
        payload_zip.write_bytes(data[start:end])
        zip_entry_count = len(archive.infolist())
        entries, warnings = _safe_extract(
            archive,
            payload,
            max_member_bytes=max_member_bytes,
            max_total_bytes=max_total_bytes,
            max_entries=max_entries,
            max_compression_ratio=max_compression_ratio,
        )
    except InstallerAnalysisError:
        if archive is not None:
            archive.close()
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise
    except (OSError, RuntimeError, ValueError, NotImplementedError, zipfile.BadZipFile) as exc:
        if archive is not None:
            archive.close()
        shutil.rmtree(archive_dir, ignore_errors=True)
        raise InstallerAnalysisError("安装包归档写入失败") from exc
    finally:
        if archive is not None:
            archive.close()
    installer = {
        "size": len(data),
        "sha256": _sha256_bytes(data),
        "md5": hashlib.md5(data).hexdigest().upper(),
        "pe_format": _pe_description(data),
        "embedded_zip_start": start,
        "embedded_zip_end": end,
        "embedded_zip_size": end - start,
        "embedded_zip_sha256": _sha256_bytes(data[start:end]),
        "zip_entries": zip_entry_count,
        "zip_selection": "source-marker score, then earliest valid container",
    }
    source_index = _source_index(payload, entries)
    snapshot: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_name": archive_name,
        "source_path": str(installer_path),
        "archived_installer": "original/" + archived_installer.name,
        "payload_zip": "original/payload.zip",
        "payload_directory": "payload",
        "installer": installer,
        "payload": _payload_stats(entries),
        "entries": entries,
        "source_index": source_index,
        "modules": _module_index(entries),
        "warnings": warnings,
        "safety": {
            "read_only": True,
            "installer_executed": False,
            "credentials_loaded": False,
            "network_requests": False,
        },
    }
    # Keep the historical manifest shape available to existing readers.
    has_protocol_payment = any(
        str(item["path"]).startswith("services/protocol-payment/") for item in entries
    )
    report_name = LEGACY_REPORT_NAME if has_protocol_payment else REPORT_NAME
    manifest = {
        "schema": SCHEMA_VERSION,
        "source_path": str(installer_path),
        "archived_installer": str(archived_installer.relative_to(archive_dir)).replace("\\", "/"),
        "installer": installer,
        "payload_zip": str(payload_zip.relative_to(archive_dir)).replace("\\", "/"),
        "payload_directory": "payload",
        "analysis_report": report_name,
        "static_analysis": SNAPSHOT_NAME,
        "entries": entries,
        "warnings": warnings,
    }
    _write_json(archive_dir / "MANIFEST.json", manifest)
    _write_json(archive_dir / SNAPSHOT_NAME, snapshot)
    (archive_dir / REPORT_NAME).write_text(_markdown_report(snapshot, warnings), encoding="utf-8")
    # Keep the historical filename for payment-oriented archives so a future
    # update can be opened beside the existing report without any renaming.
    if has_protocol_payment:
        (archive_dir / LEGACY_REPORT_NAME).write_text(
            _markdown_report(snapshot, warnings),
            encoding="utf-8",
        )

    if previous is not None:
        previous_dir = previous.expanduser().resolve()
        if not previous_dir.is_dir() or not (previous_dir / "MANIFEST.json").is_file():
            raise InstallerAnalysisError(f"上一版本归档不存在或缺少清单：{previous_dir}")
    else:
        previous_dir = _find_previous_archive(archive_root, archive_dir)
    if previous_dir and previous_dir.is_dir():
        old_snapshot = _snapshot_from_archive(previous_dir)
        diff = _diff_snapshot(old_snapshot, snapshot)
        _write_json(archive_dir / DIFF_JSON_NAME, diff)
        (archive_dir / DIFF_NAME).write_text(_diff_markdown(diff), encoding="utf-8")
    else:
        _write_baseline_diff(archive_dir, snapshot)
    _write_index(archive_root)
    return archive_dir


def analyze_installer(
    installer_path: Path,
    *,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    version: str | None = None,
    previous: Path | None = None,
    force: bool = False,
    max_installer_bytes: int = DEFAULT_MAX_INSTALLER_BYTES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> Path:
    """Public wrapper that removes a newly-created partial archive on error."""

    resolved_root = archive_root.expanduser().resolve()
    target = resolved_root / _safe_name(version or Path(installer_path).stem)
    existed_before = target.exists()
    try:
        return _analyze_installer(
            installer_path,
            archive_root=archive_root,
            version=version,
            previous=previous,
            force=force,
            max_installer_bytes=max_installer_bytes,
            max_member_bytes=max_member_bytes,
            max_total_bytes=max_total_bytes,
            max_entries=max_entries,
            max_compression_ratio=max_compression_ratio,
        )
    except InstallerAnalysisError:
        if (force or not existed_before) and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise
    except (OSError, RuntimeError, ValueError, NotImplementedError, zipfile.BadZipFile) as exc:
        if (force or not existed_before) and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise InstallerAnalysisError("安装包静态归档失败") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive and statically compare a Windows installer.")
    parser.add_argument("installer", type=Path, help="path to the .exe (or a ZIP container)")
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT, help="archive directory (default: artifacts/reverse)")
    parser.add_argument("--version", help="archive directory name; defaults to the installer file name")
    parser.add_argument("--previous", type=Path, help="previous archive directory for DIFF.md")
    parser.add_argument("--force", action="store_true", help="replace an existing archive with the same name")
    parser.add_argument("--max-installer-bytes", type=int, default=DEFAULT_MAX_INSTALLER_BYTES)
    parser.add_argument("--max-member-bytes", type=int, default=DEFAULT_MAX_MEMBER_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-compression-ratio", type=float, default=DEFAULT_MAX_COMPRESSION_RATIO)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        archive_dir = analyze_installer(
            args.installer,
            archive_root=args.archive_root,
            version=args.version,
            previous=args.previous,
            force=args.force,
            max_installer_bytes=args.max_installer_bytes,
            max_member_bytes=args.max_member_bytes,
            max_total_bytes=args.max_total_bytes,
            max_entries=args.max_entries,
            max_compression_ratio=args.max_compression_ratio,
        )
    except InstallerAnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    snapshot = json.loads((archive_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    diff = json.loads((archive_dir / DIFF_JSON_NAME).read_text(encoding="utf-8"))
    report_path = archive_dir / (
        LEGACY_REPORT_NAME if (archive_dir / LEGACY_REPORT_NAME).is_file() else REPORT_NAME
    )
    print(json.dumps({
        "archive": str(archive_dir),
        "manifest": str(archive_dir / "MANIFEST.json"),
        "report": str(report_path),
        "static_report": str(archive_dir / REPORT_NAME),
        "diff": str(archive_dir / DIFF_NAME),
        "installer_sha256": snapshot.get("installer", {}).get("sha256", ""),
        "files": snapshot.get("payload", {}).get("file_count", 0),
        "changes": {
            "installer_fields_changed": len((diff.get("installer") or {}).get("changed", {})),
            "files_added": len((diff.get("files") or {}).get("added", [])),
            "files_removed": len((diff.get("files") or {}).get("removed", [])),
            "files_changed": len((diff.get("files") or {}).get("changed", [])),
            "functions_added": len((diff.get("functions") or {}).get("added", [])),
            "function_locations_added": len((diff.get("functions") or {}).get("locations_added", [])),
            "modules_added": len((diff.get("modules") or {}).get("added", [])),
            "imports_added": len((diff.get("imports") or {}).get("added", [])),
            "hosts_added": len((diff.get("hosts") or {}).get("added", [])),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
