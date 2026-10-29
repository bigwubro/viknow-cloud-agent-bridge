#!/usr/bin/env python3
"""Apply bucket CORS so Playground can browser-PUT presigned URLs to the S3 gateway."""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from viknow3.storage import s3

ORIGINS = [
    o.strip()
    for o in (
        os.environ.get("VIKNOW_CORS_ORIGINS")
        or os.environ.get("VIKNOW_PLAYGROUND_CORS_ORIGINS")
        or "http://36.103.198.236:5198"
    ).split(",")
    if o.strip()
]


def main() -> None:
    cfg = s3._cfg()
    client, cfg = s3._client(endpoint_url=s3._signing_endpoint(cfg))
    bucket = cfg.get("bucket") or "viknow"
    cors = {
        "CORSRules": [
            {
                "AllowedOrigins": ORIGINS,
                "AllowedMethods": ["GET", "PUT", "HEAD", "POST"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag", "x-amz-request-id"],
                "MaxAgeSeconds": 3600,
            }
        ]
    }
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors)
    got = client.get_bucket_cors(Bucket=bucket)
    print(json.dumps(got.get("CORSRules", []), ensure_ascii=False))


if __name__ == "__main__":
    main()
