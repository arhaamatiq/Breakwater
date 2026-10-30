"""S3-compatible artifact store (MinIO locally). Sync boto3, called via asyncio.to_thread."""
import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import CFG

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=CFG.s3_endpoint,
            aws_access_key_id=CFG.s3_key,
            aws_secret_access_key=CFG.s3_secret,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
            region_name="us-east-1",
        )
    return _client


def ensure_bucket():
    c = client()
    try:
        c.head_bucket(Bucket=CFG.s3_bucket)
    except ClientError:
        c.create_bucket(Bucket=CFG.s3_bucket)


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    client().put_object(Bucket=CFG.s3_bucket, Key=key, Body=data, ContentType=content_type)
    return f"s3://{CFG.s3_bucket}/{key}"


def get_bytes(key: str) -> bytes:
    return client().get_object(Bucket=CFG.s3_bucket, Key=key)["Body"].read()
