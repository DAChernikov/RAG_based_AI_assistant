import hashlib
import io
import json

import pytest

from app.model_artifacts.store import ModelManifest, S3ModelArtifactStore


class FakeS3:
    def __init__(self, artifact: bytes, manifest: dict):
        self.artifact = artifact
        self.manifest = manifest

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.manifest).encode())}

    def download_fileobj(self, bucket, key, output):
        output.write(self.artifact)


def test_verified_model_artifact_is_activated_atomically(tmp_path):
    payload = b"GGUF-safe-test"
    manifest = {
        "model_id": "local/generator",
        "role": "generation",
        "version": "1",
        "format": "gguf",
        "quantization": "Q4_K_M",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "object_key": "models/generator.gguf",
    }
    store = S3ModelArtifactStore("bucket", tmp_path / "cache", FakeS3(payload, manifest))
    loaded = store.read_manifest("models/generator.manifest.json")
    path = store.ensure_cached(loaded)
    assert path.read_bytes() == payload
    assert store.ensure_cached(loaded) == path


def test_model_artifact_rejects_unsafe_format_and_bad_checksum(tmp_path):
    with pytest.raises(ValueError):
        ModelManifest(
            model_id="bad",
            role="generation",
            version="1",
            format="pickle",
            size=1,
            sha256="0" * 64,
            object_key="bad.pkl",
        )
    payload = b"bad"
    manifest = ModelManifest(
        model_id="safe",
        role="embedding",
        version="1",
        format="safetensors",
        size=len(payload),
        sha256="0" * 64,
        object_key="safe.safetensors",
    )
    store = S3ModelArtifactStore("bucket", tmp_path / "cache", FakeS3(payload, {}))
    with pytest.raises(ValueError, match="SHA-256"):
        store.ensure_cached(manifest)
