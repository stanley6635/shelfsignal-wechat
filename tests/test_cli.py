from pathlib import Path

from shelfsignal.cli import main


def test_version_command(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "shelfsignal 0.1.0"


def test_init_reports_workspace_error_without_traceback(
    tmp_path: Path, capsys
):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)

    assert main(["init", str(root)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "shelfsignal: refusing to initialize a private workspace inside a Git repository"
    )
    assert "Traceback" not in captured.err
