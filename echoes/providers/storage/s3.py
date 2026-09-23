"""S3-compatible object storage (Cloudflare R2, Backblaze B2, MinIO, AWS).

Chosen over a mounted volume wherever it is available: it is durable across
redeploys by construction, and R2's free tier covers ten gigabytes with no
egress charge, which is roughly a week of finished documentaries.

boto3 is imported lazily so a deployment using local storage does not need
the dependency at all.
"""

from __future__ import annotations

from pathlib import Path

from ...errors import ConfigError, Permanent, ProviderUnavailable
from ...logging import get_logger

log = get_logger(__name__)


class S3Storage:
    name = "s3"

    def __init__(
        self, bucket: str, *, endpoint: str | None, access_key: str,
        secret_key: str, region: str = "auto",
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = None

    def _c(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise ConfigError(
                    "STORAGE_PROVIDER=s3 requires boto3; add it to requirements "
                    "or use STORAGE_PROVIDER=local with a mounted volume"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=Config(retries={"max_attempts": 4, "mode": "standard"},
                              signature_version="s3v4"),
            )
        return self._client

    def put(self, local_path: Path, key: str) -> str:
        try:
            self._c().upload_file(str(local_path), self._bucket, key)
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"s3 upload failed for {key}: {exc}") from exc
        return key

    def get(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._c().download_file(self._bucket, key, str(dest))
        except Exception as exc:  # noqa: BLE001
            raise Permanent(f"s3 object not retrievable: {key}: {exc}") from exc
        return dest

    def exists(self, key: str) -> bool:
        try:
            self._c().head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - any failure means "cannot confirm"
            return False

    def delete(self, key: str) -> None:
        try:
            self._c().delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            log.warning("s3 delete failed", extra={"key": key, "error": str(exc)})

    def durable(self) -> bool:
        return True
