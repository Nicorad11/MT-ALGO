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
