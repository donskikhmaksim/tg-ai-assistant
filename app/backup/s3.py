"""Minimal S3-compatible client (Cloudflare R2, AWS S3, MinIO, ...) built on a
hand-rolled AWS Signature Version 4 (SigV4) over `aiohttp`.

WHY NOT boto3: boto3 pulls in botocore (JSON service models for every AWS
service, plus python-dateutil / s3transfer / jmespath) — tens of MB for the
exactly THREE operations this backup job needs (put one object, list objects
under a prefix, delete an object). `aiohttp` is already a project dependency
(used everywhere else network-facing), and SigV4 for path-style PUT/GET/DELETE
is a small, well-specified algorithm (AWS's "Signature Version 4 signing
process"). Trade-off, noted honestly: this is ~150 lines of crypto-adjacent
code we own instead of a battle-tested library. It's kept small, isolated to
this one module (swapping to boto3 later is a one-file change), and exercised
by `tests/test_mongo_backup.py`.

Uses PATH-STYLE addressing (`https://<endpoint>/<bucket>/<key>`), which is
what Cloudflare R2's docs recommend for S3-compatible access (no need for
bucket-specific virtual-host DNS).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

import aiohttp

_ALGORITHM = "AWS4-HMAC-SHA256"
_S3_XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _parse_s3_datetime(value: str) -> datetime | None:
    """LastModified comes back as ISO-8601 UTC, with or without fractional seconds."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class S3Object:
    key: str
    last_modified: datetime | None


@dataclass
class S3Client:
    """A tiny SigV4-signed client scoped to one bucket."""

    endpoint: str       # e.g. https://<accountid>.r2.cloudflarestorage.com
    bucket: str
    access_key: str
    secret_key: str
    region: str = "auto"
    service: str = "s3"

    def _host(self) -> str:
        return urlparse(self.endpoint).netloc

    def _base_url(self) -> str:
        parsed = urlparse(self.endpoint)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _canonical_path(self, key: str | None) -> str:
        path = "/" + quote(self.bucket, safe="")
        if key:
            path += "/" + quote(key, safe="/")
        return path

    @staticmethod
    def _canonical_query(params: dict[str, str]) -> str:
        return "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items())
        )

    def _sign_request(
        self, method: str, key: str | None, query: dict[str, str], body: bytes
    ) -> tuple[dict[str, str], str]:
        """Build the signed headers + full URL for a path-style S3 request."""
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = self._host()

        canonical_uri = self._canonical_path(key)
        canonical_query = self._canonical_query(query)
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
        )

        credential_scope = f"{date_stamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _signing_key(self.secret_key, date_stamp, self.region, self.service)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = (
            f"{_ALGORITHM} Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": authorization,
        }
        url = self._base_url() + canonical_uri
        if canonical_query:
            url += "?" + canonical_query
        return headers, url

    async def put_object(
        self, key: str, body: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        headers, url = self._sign_request("PUT", key, {}, body)
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        async with aiohttp.ClientSession() as session:
            async with session.put(url, data=body, headers=headers) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"S3 PUT {key!r} failed: {resp.status} {text[:500]}")

    async def delete_object(self, key: str) -> None:
        headers, url = self._sign_request("DELETE", key, {}, b"")
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    raise RuntimeError(f"S3 DELETE {key!r} failed: {resp.status} {text[:500]}")

    async def list_objects(self, prefix: str) -> list[S3Object]:
        """A single ListObjectsV2 page (no continuation) — plenty for a daily
        backup's object count under sane retention windows."""
        query = {"list-type": "2", "prefix": prefix}
        headers, url = self._sign_request("GET", None, query, b"")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"S3 LIST {prefix!r} failed: {resp.status} {text[:500]}")
        return _parse_list_objects(text)


def _parse_list_objects(xml_text: str) -> list[S3Object]:
    root = ET.fromstring(xml_text)
    ns = {"s3": _S3_XMLNS}
    # Some S3-compatible servers omit the namespace on the root; try both.
    contents = root.findall("s3:Contents", ns) or root.findall("Contents")
    results: list[S3Object] = []
    for node in contents:
        key = node.findtext("s3:Key", default=None, namespaces=ns) or node.findtext("Key")
        last_modified_raw = (
            node.findtext("s3:LastModified", default=None, namespaces=ns)
            or node.findtext("LastModified")
        )
        if key:
            results.append(S3Object(key=key, last_modified=_parse_s3_datetime(last_modified_raw or "")))
    return results
