from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "analyze_installer.py"
SPEC = importlib.util.spec_from_file_location("installer_archive_script", SCRIPT_PATH)
assert SPEC and SPEC.loader
installer_archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer_archive)


def _make_installer(
    path: Path,
    files: dict[str, str],
    *,
    prefix: bytes = b"bundle-prefix",
    suffix: bytes = b"bundle-tail",
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("unsafe/../outside.txt", "ignored")
    path.write_bytes(prefix + payload.getvalue() + suffix)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_archive_and_compare_versions(tmp_path: Path) -> None:
    first = tmp_path / "Register-v1.exe"
    _make_installer(
        first,
        {
            "services/protocol-payment/momo/main.py": (
                "def first():\n    return 'one'\n"
                "URL = 'https://one.example.test/pay'\n"
            ),
            "README.md": "one",
        },
    )
    archive_root = tmp_path / "archives"
    first_dir = installer_archive.analyze_installer(first, archive_root=archive_root)

    first_manifest = _json(first_dir / "MANIFEST.json")
    first_snapshot = _json(first_dir / "STATIC-ANALYSIS.json")
    assert {item["path"] for item in first_manifest["entries"]} == {
        "README.md",
        "services/protocol-payment/momo/main.py",
    }
    assert first_snapshot["source_index"]["functions"] == ["first"]
    assert first_snapshot["source_index"]["hosts"] == ["one.example.test"]
    assert (first_dir / "payload/services/protocol-payment/momo/main.py").is_file()
    assert "baseline" in (first_dir / "DIFF.md").read_text(encoding="utf-8")

    second = tmp_path / "Register-v2.exe"
    _make_installer(
        second,
        {
            "services/protocol-payment/momo/main.py": (
                "def second():\n    return 'two'\n"
                "URL = 'https://two.example.test/pay'\n"
            ),
            "services/protocol-payment/pix/pix.py": "def pix():\n    return True\n",
        },
    )
    second_dir = installer_archive.analyze_installer(
        second,
        archive_root=archive_root,
        previous=first_dir,
    )
    diff = _json(second_dir / "DIFF.json")
    assert diff["files"]["added"] == ["services/protocol-payment/pix/pix.py"]
    assert diff["files"]["removed"] == ["README.md"]
    assert diff["files"]["changed"] == ["services/protocol-payment/momo/main.py"]
    assert diff["functions"]["added"] == ["pix", "second"]
    assert diff["functions"]["removed"] == ["first"]
    assert diff["hosts"]["added"] == ["two.example.test"]
    assert diff["hosts"]["removed"] == ["one.example.test"]
    assert diff["modules"]["added"] == ["services/protocol-payment/pix"]
    assert (archive_root / "INDEX.md").is_file()


def test_same_installer_is_idempotent(tmp_path: Path) -> None:
    installer = tmp_path / "same.exe"
    _make_installer(installer, {"main.py": "def run():\n    pass\n"})
    archive_root = tmp_path / "archives"
    first = installer_archive.analyze_installer(installer, archive_root=archive_root)
    second = installer_archive.analyze_installer(installer, archive_root=archive_root)
    assert first == second
    assert (second / "MANIFEST.json").is_file()


def test_empty_embedded_zip_is_still_archived(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w"):
        pass
    installer = tmp_path / "empty.exe"
    installer.write_bytes(b"prefix" + payload.getvalue() + b"a" * 70000)
    archive = installer_archive.analyze_installer(installer, archive_root=tmp_path / "archives")
    manifest = _json(archive / "MANIFEST.json")
    assert manifest["installer"]["zip_entries"] == 0
    assert manifest["entries"] == []
    assert (archive / "original/payload.zip").read_bytes() == payload.getvalue()


def test_windows_name_collisions_are_skipped_and_limits_are_enforced(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("A.txt", "first")
        archive.writestr("a.txt", "second")
        archive.writestr("foo", "file")
        archive.writestr("foo/bar.txt", "nested")
    installer = tmp_path / "collision.exe"
    installer.write_bytes(payload.getvalue())
    archive_dir = installer_archive.analyze_installer(
        installer,
        archive_root=tmp_path / "archives",
        max_member_bytes=100,
    )
    manifest = _json(archive_dir / "MANIFEST.json")
    assert [item["path"] for item in manifest["entries"]] == ["A.txt", "foo"]
    assert manifest["warnings"]

    limited = tmp_path / "limited.exe"
    limited.write_bytes(payload.getvalue())
    try:
        installer_archive.analyze_installer(
            limited,
            archive_root=tmp_path / "limited-archives",
            max_member_bytes=2,
        )
    except installer_archive.InstallerAnalysisError as exc:
        assert "大小上限" in str(exc)
        assert not (tmp_path / "limited-archives" / "limited").exists()
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected the member-size limit to reject the archive")


def test_previous_archive_selection_uses_numeric_version_parts(tmp_path: Path) -> None:
    root = tmp_path / "archives"
    for version in ("Register-v2", "Register-v10"):
        installer = tmp_path / f"{version}.exe"
        _make_installer(installer, {"main.py": f"def {version.replace('-', '_')}():\n    pass\n"})
        installer_archive.analyze_installer(installer, archive_root=root)
    current = tmp_path / "Register-v11.exe"
    _make_installer(current, {"main.py": "def current():\n    pass\n"})
    latest = installer_archive.analyze_installer(current, archive_root=root)
    diff = _json(latest / "DIFF.json")
    assert diff["previous_archive"] == "Register-v10"
    assert "Register-v11" in (root / "LATEST-DIFF.md").read_text(encoding="utf-8")


def test_installer_container_changes_and_invalid_previous_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "archives"
    first = tmp_path / "same-payload-v1.exe"
    second = tmp_path / "same-payload-v2.exe"
    files = {"main.py": "def run():\n    pass\n"}
    _make_installer(first, files, prefix=b"prefix-one")
    _make_installer(second, files, prefix=b"prefix-two-longer")
    first_dir = installer_archive.analyze_installer(first, archive_root=root)
    second_dir = installer_archive.analyze_installer(second, archive_root=root, previous=first_dir)
    diff = _json(second_dir / "DIFF.json")
    assert diff["files"] == {"added": [], "removed": [], "changed": []}
    assert "sha256" in diff["installer"]["changed"]
    assert "size" in diff["installer"]["changed"]

    missing = tmp_path / "missing.exe"
    _make_installer(missing, {"new.py": "x = 1\n"})
    try:
        installer_archive.analyze_installer(
            missing,
            archive_root=root,
            previous=tmp_path / "does-not-exist",
        )
    except installer_archive.InstallerAnalysisError as exc:
        assert "上一版本归档不存在" in str(exc)
        assert not (root / "missing").exists()
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected an invalid --previous path to fail")

    try:
        installer_archive.analyze_installer(
            missing,
            archive_root=root,
            version="nan-limit",
            max_compression_ratio=float("nan"),
        )
    except installer_archive.InstallerAnalysisError as exc:
        assert "上限" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected NaN compression ratio to fail")


def test_outer_zip_is_preferred_over_nested_zip(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        for index in range(5):
            archive.writestr(f"inner-{index}.txt", "inner")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("actual.py", "def outer():\n    pass\n")
        archive.writestr("nested.zip", inner.getvalue())
    installer = tmp_path / "nested.exe"
    installer.write_bytes(b"prefix" + outer.getvalue() + b"suffix")
    archive_dir = installer_archive.analyze_installer(installer, archive_root=tmp_path / "archives")
    manifest = _json(archive_dir / "MANIFEST.json")
    assert [item["path"] for item in manifest["entries"]] == ["actual.py", "nested.zip"]


def test_payload_zip_input_keeps_source_copy_separate(tmp_path: Path) -> None:
    source = tmp_path / "payload.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("main.py", "def run():\n    pass\n")
    archive_dir = installer_archive.analyze_installer(source, archive_root=tmp_path / "archives")
    assert (archive_dir / "original/source-installer.zip").read_bytes() == source.read_bytes()
    assert (archive_dir / "original/payload.zip").read_bytes() == source.read_bytes()
