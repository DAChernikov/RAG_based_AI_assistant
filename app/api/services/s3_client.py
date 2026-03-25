from pathlib import Path

import boto3


class S3ArtifactClient:
    def __init__(
        self,
        bucket: str,
        object_key: str,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
    ):
        self.bucket = bucket
        self.object_key = object_key
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
        )

    def download(self, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self.object_key, str(target_path))
        return target_path
