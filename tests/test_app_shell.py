import subprocess
import sys

from opus_trader.app import InstanceLock


def test_second_instance_is_refused(tmp_path):
    first = InstanceLock(tmp_path / "opus_trader.lock")
    assert first.acquire()
    first.note("http://127.0.0.1:8765/")
    # A second process can't take the lock while the first holds it.
    code = subprocess.run([sys.executable, "-c",
                           f"from opus_trader.app import InstanceLock; import sys; sys.exit(0 if InstanceLock(r'{tmp_path / 'opus_trader.lock'}').acquire() else 3)"],
                          capture_output=True).returncode
    assert code == 3
    assert InstanceLock(tmp_path / "opus_trader.lock").holder() == "http://127.0.0.1:8765/"
    first.fh.close()  # released when the first instance exits (or crashes)
    code = subprocess.run([sys.executable, "-c",
                           f"from opus_trader.app import InstanceLock; import sys; sys.exit(0 if InstanceLock(r'{tmp_path / 'opus_trader.lock'}').acquire() else 3)"],
                          capture_output=True).returncode
    assert code == 0


def test_cli_output_survives_a_non_utf8_console(tmp_path):
    out = subprocess.run([sys.executable, "-m", "opus_trader", "flips", "--csv", "tests/fixtures/eurusd_m15_fixture.csv", "--count", "3"],
                         capture_output=True, env={"PYTHONIOENCODING": "ascii", "PATH": ""})
    assert out.returncode == 0, out.stderr.decode(errors="replace")
    assert b"bar OPEN times in UTC" in out.stdout


def _fake_webview(monkeypatch, winforms):
    import types

    pkg, platforms = types.ModuleType("webview"), types.ModuleType("webview.platforms")
    pkg.platforms = platforms
    monkeypatch.setitem(sys.modules, "webview", pkg)
    monkeypatch.setitem(sys.modules, "webview.platforms", platforms)
    if winforms is None:
        platforms.__path__ = []  # an empty package: importing the backend fails
        monkeypatch.delitem(sys.modules, "webview.platforms.winforms", raising=False)
    else:
        platforms.winforms = winforms
        monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms)


def test_window_falls_back_when_webview2_is_missing(monkeypatch):
    import types

    from opus_trader import app

    monkeypatch.setattr(app.os, "name", "nt")
    _fake_webview(monkeypatch, types.SimpleNamespace(renderer="mshtml"))  # what pywebview picks without WebView2
    assert app.windows_renderer() == "mshtml"
    _fake_webview(monkeypatch, types.SimpleNamespace(renderer="edgechromium"))
    assert app.windows_renderer() == "edgechromium"
    _fake_webview(monkeypatch, None)  # .NET / pythonnet couldn't load the backend
    assert app.windows_renderer() == "unavailable"


def test_first_run_asks_for_the_key_and_saves_it(monkeypatch, tmp_path):
    from opus_trader import app

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-...\n# MT5_TERMINAL_PATH=C:\\MT5\\terminal64.exe\n")  # a copy of .env.example
    monkeypatch.setattr(app, "ENV_PATH", env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    app.ensure_api_key(interactive=True, ask=lambda prompt: "  sk-ant-api03-abc_DEF-123  ")
    assert env.read_text() == "ANTHROPIC_API_KEY=sk-ant-api03-abc_DEF-123\n# MT5_TERMINAL_PATH=C:\\MT5\\terminal64.exe\n"
    assert app.os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-abc_DEF-123"


def test_first_run_key_prompt_can_be_skipped_or_refused(monkeypatch, tmp_path):
    from opus_trader import app

    env = tmp_path / ".env"
    monkeypatch.setattr(app, "ENV_PATH", env)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    app.ensure_api_key(interactive=True, ask=lambda prompt: "")
    app.ensure_api_key(interactive=True, ask=lambda prompt: "my mt5 password")  # not a key: never saved
    assert not env.exists()
    app.ensure_api_key(interactive=False, ask=lambda prompt: 1 / 0)  # no console: never asks
    app.ensure_api_key(interactive=True, ask=lambda prompt: "sk-ant-api03-xyz")
    assert env.read_text().splitlines()[-1] == "ANTHROPIC_API_KEY=sk-ant-api03-xyz"


def test_env_txt_is_read_when_explorer_added_an_extension(monkeypatch, tmp_path):
    from opus_trader import app

    (tmp_path / ".env.txt").write_text("ANTHROPIC_API_KEY=sk-ant-api03-from-txt\n")
    monkeypatch.setattr(app, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # recorded, so the key loaded below is undone after the test
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    app.load_env()
    assert app.os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-from-txt"
