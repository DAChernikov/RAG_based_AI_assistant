from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from app.api.config import settings
from app.api.services.s3_client import S3Client


class ArtifactManager:
    REQUIRED_FILES = [
        "corpus_emb.npy",
        "corpus.joblib",
        "meta.json",
        "retriever_model/config.json",
        "retriever_model/config_sentence_transformers.json",
    ]

    def __init__(self, artifacts_dir: str):
        self.artifacts_dir = Path(artifacts_dir)
        self.s3_client = S3Client()

    def has_required_artifacts(self) -> bool:
        return all((self.artifacts_dir / rel_path).exists() for rel_path in self.REQUIRED_FILES)

    def missing_required_artifacts(self) -> list[str]:
        return [
            rel_path
            for rel_path in self.REQUIRED_FILES
            if not (self.artifacts_dir / rel_path).exists()
        ]

    def prepare(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if settings.force_artifacts_download:
            self._reset_artifacts_dir()
            self._download_and_extract()
            self._validate_or_raise()
            return

        if self.has_required_artifacts():
            return

        self._download_and_extract()
        self._validate_or_raise()

    def _validate_or_raise(self) -> None:
        missing = self.missing_required_artifacts()
        if missing:
            raise RuntimeError(f"Missing required artifacts: {', '.join(missing)}")

    def _reset_artifacts_dir(self) -> None:
        if self.artifacts_dir.exists():
            shutil.rmtree(self.artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _download_and_extract(self) -> None:
        zip_path = self.artifacts_dir / "artifacts_bundle.zip"

        self.s3_client.download_file(
            bucket=settings.s3_bucket,
            key=settings.s3_artifact_key,
            destination=str(zip_path),
        )

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(self.artifacts_dir)

        if zip_path.exists():
            zip_path.unlink()

        self._cleanup_junk()
        self._normalize_single_nested_root()
        self._cleanup_junk()

    def _cleanup_junk(self) -> None:
        macosx_dir = self.artifacts_dir / "__MACOSX"
        if macosx_dir.exists():
            shutil.rmtree(macosx_dir, ignore_errors=True)

        for path in self.artifacts_dir.rglob(".DS_Store"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        for path in self.artifacts_dir.rglob("._*"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _normalize_single_nested_root(self) -> None:
        entries = [
            p for p in self.artifacts_dir.iterdir()
            if p.name not in {"__MACOSX"} and not p.name.startswith("._")
        ]

        if len(entries) != 1:
            return

        nested_root = entries[0]
        if not nested_root.is_dir():
            return

        nested_required_count = sum(
            (nested_root / rel_path).exists() for rel_path in self.REQUIRED_FILES
        )
        if nested_required_count == 0:
            return

        for child in list(nested_root.iterdir()):
            target = self.artifacts_dir / child.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), str(target))

        shutil.rmtree(nested_root, ignore_errors=True)