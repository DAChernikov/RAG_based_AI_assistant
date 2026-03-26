import boto3

from app.api.config import settings


class S3Client:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_default_region,
        )

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        self.client.download_file(bucket, key, destination)
