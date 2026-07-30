from app.api.config import settings


class S3Client:
    def __init__(self, client=None):
        self._client = client

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_default_region,
            )
        return self._client

    def download_file(
        self,
        bucket: str,
        key: str,
        destination: str,
        *,
        max_bytes: int,
    ) -> None:
        client = self._get_client()
        metadata = client.head_object(Bucket=bucket, Key=key)
        size = int(metadata.get("ContentLength", 0))
        if size <= 0 or size > max_bytes:
            raise RuntimeError("Retriever artifact archive size is outside the allowed limit.")
        client.download_file(bucket, key, destination)
