from pathlib import Path

import pytest

from app.api.services.s3_client import S3Client


class FakeBotoClient:
    def __init__(self, size: int):
        self.size = size
        self.downloaded = False

    def head_object(self, **kwargs):
        return {"ContentLength": self.size}

    def download_file(self, bucket, key, destination):
        self.downloaded = True
        Path(destination).write_bytes(b"archive")


def test_injected_s3_client_checks_size_before_download(tmp_path):
    boto_client = FakeBotoClient(size=7)

    S3Client(client=boto_client).download_file(
        "bucket",
        "key",
        str(tmp_path / "archive.zip"),
        max_bytes=10,
    )

    assert boto_client.downloaded is True


def test_s3_client_rejects_oversized_object_before_download(tmp_path):
    boto_client = FakeBotoClient(size=11)

    with pytest.raises(RuntimeError, match="size"):
        S3Client(client=boto_client).download_file(
            "bucket",
            "key",
            str(tmp_path / "archive.zip"),
            max_bytes=10,
        )

    assert boto_client.downloaded is False
