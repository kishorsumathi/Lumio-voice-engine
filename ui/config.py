"""Load DATABASE_URL from env or RDS secret (same pattern as worker pipeline)."""
from __future__ import annotations

import json
import os

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
RDS_SECRET_NAME = os.getenv("RDS_SECRET_NAME", "anchor-voice/prd/rds-credentials")
S3_BUCKET = os.getenv("S3_PROCESSED_BUCKET", "")

_db_url: str | None = None


def _get_secret_json(secret_name: str) -> dict:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
        return json.loads(resp.get("SecretString") or "{}")
    except ClientError as e:
        raise RuntimeError(f"Secrets Manager error for {secret_name}: {e}") from e


def get_db_url() -> str:
    global _db_url
    if _db_url is None:
        direct = os.getenv("DATABASE_URL")
        if direct:
            _db_url = direct
        else:
            creds = _get_secret_json(RDS_SECRET_NAME)
            host = creds["host"]
            port = creds.get("port", 5432)
            dbname = creds.get("dbname", "anchorvoice")
            user = creds["username"]
            password = creds["password"]
            _db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    return _db_url
