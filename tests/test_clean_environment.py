import os
import subprocess
import sys
from pathlib import Path


def test_bot_modules_import_without_env_or_telegram_token(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TELEGRAM_BOT_TOKEN", "API_KEY"}
    }
    environment["PYTHONPATH"] = str(project_root)

    result = subprocess.run(
        [sys.executable, "-c", "import app.bot.config; import app.bot.handlers"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
