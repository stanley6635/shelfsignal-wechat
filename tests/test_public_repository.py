from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".gitignore"}
EXCLUDED_PARTS = {".git", ".venv", "dist", "build", ".build"}


def public_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name == ".gitignore":
            yield path


def test_public_repository_has_no_private_paths_or_credentials() -> None:
    private_user_root = "/" + "Users" + "/"
    cookie_header = "Coo" + "kie:"
    bearer_header = "Authori" + "zation: Bearer"
    private_project = "T" + "ARS"
    maintainer_name = "Stan" + "ley Sun"
    for path in public_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert private_user_root not in text, path
        assert cookie_header not in text, path
        assert bearer_header not in text, path
        assert private_project not in text, path
        assert maintainer_name not in text, path


def test_readme_documents_the_complete_public_workflow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_claims = (
        "Local-first WeChat Official Account article collection for local AI agents.",
        "Python 3.11",
        "playwright install chromium",
        "global Skill",
        "profile/interests.md",
        "profile/rubric.md",
        "profile/focus/",
        "--auth fresh",
        "--auth reuse",
        "--run-id",
        "- [ ]",
        "- [x]",
        "selected bundle",
        "read-only",
        "AuthRequired",
        "ShelfUnavailable",
        "ContentContractUnavailable",
        "visible",
        "no telemetry",
        "no LLM provider API",
        "shelfsignal doctor",
        "v0 non-goals",
    )
    for claim in required_claims:
        assert claim in readme, claim

    assert "Collection already writes" in readme
    assert "completed run is immutable" in readme
    assert "Do not routinely run `prepare-briefing`" in readme
    assert "newly created for that completed run" in readme


def test_package_metadata_and_shipped_resources_are_public_ready() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    assert project["name"] == "shelfsignal-wechat"
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["shelfsignal"] == "shelfsignal.cli:console_main"
    assert config["tool"]["setuptools"]["package-data"]["shelfsignal"] == [
        "resources/*.swift"
    ]
    assert (ROOT / "src/shelfsignal/resources/vision_ocr.swift").is_file()
    assert (ROOT / "skills/shelfsignal-wechat/SKILL.md").is_file()


def test_repository_has_no_runtime_artifacts() -> None:
    forbidden_names = {"state.db", "Cookies", "Local State"}
    forbidden_suffixes = {".sqlite", ".sqlite3"}
    for path in ROOT.rglob("*"):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        assert path.name not in forbidden_names, path
        assert path.suffix not in forbidden_suffixes, path
