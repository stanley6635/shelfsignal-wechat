from shelfsignal.cli import main


def test_version_command(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "shelfsignal 0.1.0"
