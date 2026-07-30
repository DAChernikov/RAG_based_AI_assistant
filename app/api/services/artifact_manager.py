from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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

    def __init__(
        self,
        artifacts_dir: str,
        *,
        s3_client=None,
        s3_client_factory: Callable[[], object] = S3Client,
        smoke_validator: Callable[[Path], None] | None = None,
        max_archive_bytes: int | None = None,
        max_extracted_bytes: int | None = None,
    ):
        self.artifacts_dir = Path(artifacts_dir).expanduser().resolve()
        self._injected_s3_client = s3_client
        self._s3_client_factory = s3_client_factory
        self._smoke_validator = smoke_validator or self._default_smoke_validation
        self.max_archive_bytes = max_archive_bytes or settings.artifacts_max_archive_bytes
        self.max_extracted_bytes = max_extracted_bytes or settings.artifacts_max_extracted_bytes
        self._validate_target()

    def _validate_target(self) -> None:
        if self.artifacts_dir == Path(self.artifacts_dir.anchor):
            raise ValueError("Artifacts directory cannot be a filesystem root.")
        if not self.artifacts_dir.name or self.artifacts_dir.name in {".", ".."}:
            raise ValueError("Artifacts directory must have a concrete directory name.")

    def has_required_artifacts(self, root: Path | None = None) -> bool:
        base = root or self.artifacts_dir
        return all((base / rel_path).is_file() for rel_path in self.REQUIRED_FILES)

    def missing_required_artifacts(self, root: Path | None = None) -> list[str]:
        base = root or self.artifacts_dir
        return [rel_path for rel_path in self.REQUIRED_FILES if not (base / rel_path).is_file()]

    def prepare(self) -> None:
        if not settings.force_artifacts_download and self.has_required_artifacts():
            return

        self._validate_s3_configuration()
        self.artifacts_dir.parent.mkdir(parents=True, exist_ok=True)

        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".{self.artifacts_dir.name}.staging-",
                dir=self.artifacts_dir.parent,
            )
        )
        try:
            archive_path = staging_root / "artifacts_bundle.zip"
            extracted_root = staging_root / "extracted"
            extracted_root.mkdir()
            self._download_archive(archive_path)
            self._safe_extract(archive_path, extracted_root)
            candidate = self._normalized_root(extracted_root)
            self._validate_or_raise(candidate)
            self._smoke_validator(candidate)
            self._activate(candidate)
        finally:
            self._remove_managed_tree(staging_root, kind="staging")

    def _validate_s3_configuration(self) -> None:
        required = (
            settings.s3_endpoint_url,
            settings.s3_bucket,
            settings.s3_artifact_key,
            settings.aws_access_key_id,
            settings.aws_secret_access_key,
        )
        if self._injected_s3_client is None and not all(required):
            raise RuntimeError(
                "Retriever artifacts are missing and S3 download configuration is incomplete."
            )

    def _download_archive(self, destination: Path) -> None:
        client = self._injected_s3_client or self._s3_client_factory()
        client.download_file(
            bucket=settings.s3_bucket,
            key=settings.s3_artifact_key,
            destination=str(destination),
            max_bytes=self.max_archive_bytes,
        )
        size = destination.stat().st_size
        if size <= 0 or size > self.max_archive_bytes:
            raise RuntimeError("Downloaded retriever artifact archive exceeds the allowed size.")

    @staticmethod
    def _is_symlink(info: zipfile.ZipInfo) -> bool:
        return stat.S_ISLNK(info.external_attr >> 16)

    def _safe_extract(self, archive_path: Path, destination: Path) -> None:
        destination_resolved = destination.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            total_size = sum(entry.file_size for entry in entries)
            if total_size > self.max_extracted_bytes:
                raise RuntimeError("Retriever artifacts exceed the extracted size limit.")

            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                posix_path = PurePosixPath(normalized_name)
                if (
                    posix_path.is_absolute()
                    or ".." in posix_path.parts
                    or (posix_path.parts and posix_path.parts[0].endswith(":"))
                    or self._is_symlink(entry)
                ):
                    raise RuntimeError("Retriever artifact archive contains an unsafe entry.")

                target = (destination / Path(*posix_path.parts)).resolve()
                if target != destination_resolved and destination_resolved not in target.parents:
                    raise RuntimeError("Retriever artifact archive escapes the staging directory.")

            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                target = destination / Path(*PurePosixPath(normalized_name).parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

        self._cleanup_junk(destination)

    @staticmethod
    def _cleanup_junk(root: Path) -> None:
        macosx_dir = root / "__MACOSX"
        if macosx_dir.exists():
            shutil.rmtree(macosx_dir)
        for pattern in (".DS_Store", "._*"):
            for path in root.rglob(pattern):
                if path.is_file():
                    path.unlink()

    def _normalized_root(self, extracted_root: Path) -> Path:
        if self.has_required_artifacts(extracted_root):
            return extracted_root
        directories = [item for item in extracted_root.iterdir() if item.is_dir()]
        files = [item for item in extracted_root.iterdir() if item.is_file()]
        if not files and len(directories) == 1 and self.has_required_artifacts(directories[0]):
            return directories[0]
        return extracted_root

    def _validate_or_raise(self, root: Path) -> None:
        missing = self.missing_required_artifacts(root)
        if missing:
            raise RuntimeError(f"Missing required retriever artifacts: {', '.join(missing)}")

    @staticmethod
    def _default_smoke_validation(root: Path) -> None:
        import joblib
        import numpy as np

        embeddings = np.load(root / "corpus_emb.npy", mmap_mode="r")
        corpus = joblib.load(root / "corpus.joblib")
        if embeddings.ndim != 2 or len(corpus) != embeddings.shape[0]:
            raise RuntimeError("Retriever artifact corpus and embeddings are inconsistent.")
        for path in (
            root / "meta.json",
            root / "retriever_model/config.json",
            root / "retriever_model/config_sentence_transformers.json",
        ):
            with path.open(encoding="utf-8") as file:
                json.load(file)

    def _activate(self, candidate: Path) -> None:
        backup = self.artifacts_dir.parent / (
            f".{self.artifacts_dir.name}.backup-{uuid.uuid4().hex}"
        )
        had_active = self.artifacts_dir.exists()
        try:
            if had_active:
                os.replace(self.artifacts_dir, backup)
            os.replace(candidate, self.artifacts_dir)
        except Exception:
            if had_active and backup.exists() and not self.artifacts_dir.exists():
                os.replace(backup, self.artifacts_dir)
            raise
        else:
            if backup.exists():
                self._remove_managed_tree(backup, kind="backup")

    def _remove_managed_tree(self, path: Path, *, kind: str) -> None:
        resolved = path.resolve()
        expected_prefix = f".{self.artifacts_dir.name}.{kind}-"
        if resolved.parent != self.artifacts_dir.parent or not resolved.name.startswith(
            expected_prefix
        ):
            raise RuntimeError("Refusing to remove an unmanaged artifacts path.")
        if resolved.exists():
            shutil.rmtree(resolved)
