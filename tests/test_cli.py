"""Tests for the command-line entry point."""

from pathlib import Path

import pytest

from aiosxm.cli import main


class TestCli:
    """Argument handling and credential resolution."""

    def test_missing_credentials_exits_with_guidance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.delenv("SXM_USERNAME", raising=False)
        monkeypatch.delenv("SXM_PASSWORD", raising=False)
        monkeypatch.setattr("sys.argv", ["aiosxm-proxy", "--env-file", str(tmp_path / "none.env")])

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 2
        message = capsys.readouterr().err
        assert "No SiriusXM credentials found" in message
        assert "SXM_USERNAME" in message

    def test_credentials_from_flags_reach_the_app(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("SXM_USERNAME", raising=False)
        monkeypatch.delenv("SXM_PASSWORD", raising=False)
        captured: dict = {}

        def fake_run_app(app_coro, **kwargs) -> None:
            captured["kwargs"] = kwargs
            app_coro.close()

        monkeypatch.setattr("aiosxm.cli.web.run_app", fake_run_app)
        monkeypatch.setattr(
            "sys.argv",
            [
                "aiosxm-proxy",
                "--env-file",
                str(tmp_path / "none.env"),
                "--username",
                "flag@example.com",
                "--password",
                "flagpass",
                "--port",
                "9999",
                "--host",
                "0.0.0.0",  # noqa: S104
            ],
        )
        main()
        assert captured["kwargs"]["port"] == 9999
        assert captured["kwargs"]["host"] == "0.0.0.0"  # noqa: S104

    def test_env_file_supplies_credentials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("SXM_USERNAME", raising=False)
        monkeypatch.delenv("SXM_PASSWORD", raising=False)
        env = tmp_path / ".env"
        env.write_text("SXM_USERNAME=file@example.com\nSXM_PASSWORD=filepass\n")

        started = False

        def fake_run_app(app_coro, **_kwargs) -> None:
            nonlocal started
            started = True
            app_coro.close()

        monkeypatch.setattr("aiosxm.cli.web.run_app", fake_run_app)
        monkeypatch.setattr("sys.argv", ["aiosxm-proxy", "--env-file", str(env)])
        main()
        assert started, "the server should start when .env supplies credentials"
