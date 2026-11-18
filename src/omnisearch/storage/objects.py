from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put_text(self, key: str, text: str, content_type: str) -> str: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_text(self, key: str, text: str, content_type: str) -> str:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise ValueError("object key escapes configured object store")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target.as_uri()


class S3ObjectStore:
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError(
                "S3 object storage requires the OmniSearch cloud extra: "
                "pip install -e '.[cloud]'"
            ) from error

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put_text(self, key: str, text: str, content_type: str) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType=content_type,
        )
        return f"s3://{self.bucket}/{key}"
