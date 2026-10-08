import os
import subprocess
import sys


def test_serve_refuses_to_start_without_database_url() -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")}
    proc = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "serve"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    assert proc.returncode == 78
    first_line = proc.stderr.strip().splitlines()[0]
    assert "EPISTREL_DATABASE_URL" in first_line
    assert "Traceback" not in proc.stderr


def test_bare_command_prints_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "epistrel.cli"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2
    assert "serve" in proc.stderr
