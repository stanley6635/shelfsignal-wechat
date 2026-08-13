from __future__ import annotations

import io
import os
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from release_audit import audit_distribution, audit_repository, repository_files

ROOT = Path(__file__).parents[1]


def test_public_repository_has_no_private_or_runtime_artifacts() -> None:
    files = repository_files(ROOT)
    assert files
    assert audit_repository(ROOT) == ()


def test_default_readme_is_chinese_and_links_to_english() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_claims = (
        "从微信读书书架采集公众号全文",
        "README.en.md",
        "docs/assets/wechat-logo.png",
        "docs/assets/weread-logo.png",
        "与腾讯、微信及微信读书不存在官方关联",
        "快速开始",
        "工作流程",
        "隐私边界",
    )
    for claim in required_claims:
        assert claim in readme, claim

    assert not (ROOT / "README.zh-CN.md").exists()
    assert (ROOT / "README.en.md").is_file()
    assert (ROOT / "docs/assets/wechat-logo.png").is_file()
    assert (ROOT / "docs/assets/weread-logo.png").is_file()


def test_english_readme_documents_the_complete_public_workflow() -> None:
    readme = (ROOT / "README.en.md").read_text(encoding="utf-8")
    required_claims = (
        "Local-first WeChat Official Account article collection for local AI agents.",
        "Python 3.11",
        "playwright install chromium",
        "global Skill",
        'export SHELFSIGNAL_WORKSPACE="$HOME/ShelfSignal-Data"',
        "current shell",
        "outside any Git repository",
        "在微信读书中打开",
        "latest three articles",
        "--auth fresh",
        "--auth reuse",
        "--run-id",
        "stored `source.md`",
        "read-only",
        "AuthRequired",
        "ShelfUnavailable",
        "ContentContractUnavailable",
        "visible",
        "no telemetry",
        "no LLM provider API",
        "shelfsignal doctor",
        "MIT License",
        "SHELFSIGNAL_REQUIRE_DIST=1 python -m pytest -q tests/test_public_repository.py",
    )
    for claim in required_claims:
        assert claim in readme, claim

    assert "Collection already writes" in readme
    assert "completed run is immutable" in readme
    assert "Do not routinely run `prepare-briefing`" in readme
    assert "item number or title" in readme


def test_package_metadata_and_shipped_resources_are_public_ready() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    assert project["name"] == "shelfsignal-wechat"
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["shelfsignal"] == "shelfsignal.cli:console_main"
    assert config["tool"]["setuptools"]["package-data"]["shelfsignal"] == [
        "resources/*.swift"
    ]
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")
    assert (ROOT / "src/shelfsignal/resources/vision_ocr.swift").is_file()
    assert (ROOT / "skills/shelfsignal-wechat/SKILL.md").is_file()


def _write_synthetic_wheel(path: Path, unsafe: bytes | None = None) -> None:
    members = {
        "shelfsignal/__init__.py": b"",
        "shelfsignal/resources/vision_ocr.swift": b"import Vision\n",
        "example.dist-info/METADATA": b"Metadata-Version: 2.4\nLicense-Expression: MIT\n",
        "example.dist-info/licenses/LICENSE": b"MIT License\n",
    }
    if unsafe is not None:
        members["shelfsignal/binary.bin"] = b"\x00\xff" + unsafe
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _write_synthetic_sdist(path: Path, unsafe_name: str | None = None) -> None:
    members = {
        "example-0.1.0/LICENSE": b"MIT License\n",
        "example-0.1.0/PKG-INFO": b"Metadata-Version: 2.4\nLicense-Expression: MIT\n",
        "example-0.1.0/README.md": b"# Example\n",
        "example-0.1.0/pyproject.toml": b"[project]\n",
        "example-0.1.0/src/shelfsignal/__init__.py": b"",
        "example-0.1.0/src/shelfsignal/resources/vision_ocr.swift": b"import Vision\n",
        "example-0.1.0/tests/release_audit.py": b"# release audit\n",
    }
    if unsafe_name is not None:
        members[unsafe_name] = b"private"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_archive_audit_handles_binary_bytes_and_member_names(tmp_path: Path) -> None:
    wheel = tmp_path / "example-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "example-0.1.0.tar.gz"
    _write_synthetic_wheel(wheel)
    _write_synthetic_sdist(sdist)
    assert audit_distribution(wheel) == ()
    assert audit_distribution(sdist) == ()

    private_root = b"/" + b"Users" + b"/private"
    _write_synthetic_wheel(wheel, unsafe=private_root)
    assert any("forbidden marker" in item for item in audit_distribution(wheel))

    credential_headers = (
        b"cOo" + b"KiE\t: session=value",
        b"authori" + b"zation: Basic dXNlcjpwYXNz",
        b"AUTHORI" + b"ZATION : Bearer token-value",
    )
    for credential_header in credential_headers:
        _write_synthetic_wheel(wheel, unsafe=credential_header)
        assert any("header" in item for item in audit_distribution(wheel))

    _write_synthetic_sdist(sdist, "example-0.1.0/browser/session.bin")
    assert any("runtime or credential" in item for item in audit_distribution(sdist))


def test_repository_audit_detects_case_insensitive_headers_without_word_false_positives(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "notes.bin").write_bytes(
        b"cookie policy; authorization model; bearer market; basic access"
    )
    assert audit_repository(safe) == ()

    credential_headers = (
        b"coo" + b"kie: session=value",
        b"AuThOrI" + b"zAtIoN:\tBasic dXNlcjpwYXNz",
        b"authori" + b"zation :  Bearer token-value",
    )
    for index, credential_header in enumerate(credential_headers):
        path = safe / f"secret-{index}.bin"
        path.write_bytes(b"\x00\xff" + credential_header)
        assert any("header" in item for item in audit_repository(safe))
        path.unlink()


def test_built_distribution_archives_pass_release_audit() -> None:
    if os.environ.get("SHELFSIGNAL_REQUIRE_DIST") != "1":
        pytest.skip("set SHELFSIGNAL_REQUIRE_DIST=1 for the built-artifact release gate")
    archives = sorted((ROOT / "dist").glob("shelfsignal_wechat-0.1.0*"))
    assert {path.suffix for path in archives} >= {".whl", ".gz"}
    assert len(archives) == 2
    for archive in archives:
        assert audit_distribution(archive) == (), archive
