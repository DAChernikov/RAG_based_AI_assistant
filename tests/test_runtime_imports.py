import os
import subprocess
import sys


def test_queued_api_import_does_not_load_retriever_module():
    environment = os.environ.copy()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.api.main; "
                "print('app.api.services.retriever_loader' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.stdout.strip() == "False"
