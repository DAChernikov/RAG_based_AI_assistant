from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from app.api.config import settings
from app.api.services.s3_client import S3ArtifactClient


class ArtifactManager:
    REQUIRED_FILES = (
        "corpus_emb.npy",
        "corpus.joblib",
        "meta.json",
        "retriever_model/config.json",
        "retriever_model/config_sentence_transformers.json",
    )

    def __init__(self, artifacts_dir: str):
        self.artifacts_dir = Path(artifacts_dir)

    def artifact_path(self, relative_path: str) -> Path:
        return self.artifacts_dir / relative_path

    def has_required_artifacts(self) -> bool:
        return all(self.artifact_path(path).exists() for path in self.REQUIRED_FILES)

    def validate(self) -> None:
        missing = [path for path in self.REQUIRED_FILES if not self.artifact_path(path).exists()]
        if missing:
            raise FileNotFoundError(f"Missing required artifacts: {', '.join(missing)}")

    def prepare(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if self.has_required_artifacts() and not settings.force_artifacts_download:
            return

        if settings.force_artifacts_download and self.artifacts_dir.exists():
            for child in self.artifacts_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

        if self.has_required_artifacts():
            return

        if not settings.s3_bucket:
            raise FileNotFoundError(
                "Artifacts are missing locally and S3 bucket is not configured."
            )

        zip_path = self.artifacts_dir / "artifacts_bundle.zip"

        client = S3ArtifactClient(
            bucket=settings.s3_bucket,
            object_key=settings.s3_artifact_key,
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_default_region,
        )
        client.download(zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(self.artifacts_dir)

        zip_path.unlink(missing_ok=True)
        self.validate()
