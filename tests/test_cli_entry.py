import shutil
import subprocess


def test_console_script_is_installed() -> None:
    exe = shutil.which("epistrel")
    assert exe is not None, "the `epistrel` console script is not on PATH (run `uv sync`)"
    proc = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10, check=False)
    assert proc.returncode == 0
    assert "serve" in proc.stdout
