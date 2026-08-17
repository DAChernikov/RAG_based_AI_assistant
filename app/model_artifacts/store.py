from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelManifest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(min_length=1, max_length=255)
    role: str = Field(pattern=r"^(generation|embedding|reranker)$")
    version: str = Field(min_length=1, max_length=100)
    format: str = Field(pattern=r"^(gguf|safetensors|onnx)$")
    quantization: str | None = Field(default=None, max_length=100)
    size: int = Field(gt=0, le=20_000_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_key: str = Field(min_length=1, max_length=1024)


class S3ModelArtifactStore:
    """Downloads reviewed formats into an atomic local cache; remote writes do not exist."""

    def __init__(self, bucket: str, cache_dir: Path, client=None):
        self.bucket = bucket
        self.cache_dir = cache_dir.resolve()
        if self.cache_dir == Path(self.cache_dir.anchor):
            raise ValueError("Model cache cannot be a filesystem root.")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def read_manifest(self, key: str) -> ModelManifest:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"].read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("Model manifest exceeds the maximum size.")
        return ModelManifest.model_validate(json.loads(body))

    def ensure_cached(self, manifest: ModelManifest) -> Path:
        suffix = f".{manifest.format}"
        target = self.cache_dir / manifest.model_id.replace("/", "--") / manifest.version
        target = target.with_suffix(suffix)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and self._valid(target, manifest):
            return target
        descriptor, raw_path = tempfile.mkstemp(prefix="model-", dir=target.parent)
        os.close(descriptor)
        staging = Path(raw_path)
        try:
            with staging.open("wb") as output:
                self.client.download_fileobj(self.bucket, manifest.object_key, output)
                output.flush()
                os.fsync(output.fileno())
            if not self._valid(staging, manifest):
                raise ValueError("Model artifact size or SHA-256 verification failed.")
            os.replace(staging, target)
            return target
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _valid(path: Path, manifest: ModelManifest) -> bool:
        if path.stat().st_size != manifest.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == manifest.sha256
