# File location: /server/server/storage/client.py
"""
Async S3-compatible storage client backed by aioboto3.

Every backend (AWS S3, MinIO) speaks the same protocol.  MinIO is
just S3 with an ``ENDPOINT_URL``.
"""
import logging
from typing import Any, BinaryIO, Dict, List, Optional

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class StorageClient:
    """Thin async wrapper around a single S3 bucket via aioboto3."""

    def __init__(
        self,
        session: aioboto3.Session,
        config: Dict[str, Any],
        bucket: str,
    ) -> None:
        self._session = session
        self._config = config
        self._bucket = bucket
        self._boto_config = BotoConfig(
            connect_timeout=config.get('CONNECT_TIMEOUT', 5),
            read_timeout=config.get('READ_TIMEOUT', 15),
            retries={
                'max_attempts': config.get('MAX_RETRIES', 3),
                'mode': 'standard',
            },
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    # ------------------------------------------------------------------
    # Internal: build kwargs for ``session.client('s3', **kw)``
    # ------------------------------------------------------------------

    def _client_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {'config': self._boto_config}
        if self._config.get('REGION_NAME'):
            kw['region_name'] = self._config['REGION_NAME']
        if self._config.get('ENDPOINT_URL'):
            kw['endpoint_url'] = self._config['ENDPOINT_URL']
        return kw

    # ------------------------------------------------------------------
    # Bucket management
    # ------------------------------------------------------------------

    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist.

        Raises on auth failures or network errors so callers can detect
        misconfigured credentials during startup.  Only swallows the
        404/NoSuchBucket case to attempt creation.
        """
        async with self._session.client('s3', **self._client_kwargs()) as client:
            try:
                await client.head_bucket(Bucket=self._bucket)
                return  # bucket exists and we have access
            except ClientError as err:
                error_code = err.response.get('Error', {}).get('Code', '')
                http_status = err.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)

                # 403/AccessDenied = bad credentials — must propagate
                if error_code in ('403', 'AccessDenied') or http_status == 403:
                    raise

                # 404/NoSuchBucket = expected, try to create
                if error_code not in ('404', 'NoSuchBucket') and http_status != 404:
                    # Unknown error — propagate rather than masking
                    raise

            logger.info('Bucket %s not found — creating.', self._bucket)
            await client.create_bucket(Bucket=self._bucket)

    async def set_lifecycle_expiration(
        self, prefix: str, days: int, rule_id: str = 'auto-expire',
    ) -> None:
        """Apply an expiration lifecycle rule to *prefix* in the bucket."""
        lifecycle = {
            'Rules': [
                {
                    'ID': rule_id,
                    'Status': 'Enabled',
                    'Filter': {'Prefix': prefix},
                    'Expiration': {'Days': days},
                },
            ],
        }
        async with self._session.client('s3', **self._client_kwargs()) as client:
            try:
                await client.put_bucket_lifecycle_configuration(
                    Bucket=self._bucket,
                    LifecycleConfiguration=lifecycle,
                )
                logger.info(
                    'Set lifecycle expiration on %s/%s* → %d days.',
                    self._bucket, prefix, days,
                )
            except ClientError as err:
                logger.warning(
                    'Failed to set lifecycle on %s: %s', self._bucket, err,
                )

    async def set_public_read_policy(self) -> None:
        """Apply a public-read bucket policy (for static files bucket)."""
        import json

        policy = {
            'Version': '2012-10-17',
            'Statement': [
                {
                    'Sid': 'PublicRead',
                    'Effect': 'Allow',
                    'Principal': '*',
                    'Action': 's3:GetObject',
                    'Resource': f'arn:aws:s3:::{self._bucket}/*',
                },
            ],
        }
        async with self._session.client('s3', **self._client_kwargs()) as client:
            try:
                await client.put_bucket_policy(
                    Bucket=self._bucket,
                    Policy=json.dumps(policy),
                )
                logger.info('Set public-read policy on %s.', self._bucket)
            except ClientError as err:
                logger.warning(
                    'Failed to set public-read policy on %s: %s',
                    self._bucket, err,
                )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = 'application/octet-stream',
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload raw bytes.  Returns the full ``bucket/key`` path."""
        extra: Dict[str, Any] = {'ContentType': content_type}
        if metadata:
            extra['Metadata'] = metadata

        async with self._session.client('s3', **self._client_kwargs()) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                **extra,
            )
        logger.debug(
            'Uploaded %d bytes → %s/%s', len(data), self._bucket, key,
        )
        return f'{self._bucket}/{key}'

    async def upload_fileobj(
        self,
        key: str,
        fileobj: BinaryIO,
        length: int,
        content_type: str = 'application/octet-stream',
    ) -> str:
        """Upload from a file-like object."""
        async with self._session.client('s3', **self._client_kwargs()) as client:
            await client.upload_fileobj(
                fileobj,
                self._bucket,
                key,
                ExtraArgs={'ContentType': content_type},
            )
        logger.debug('Uploaded fileobj → %s/%s', self._bucket, key)
        return f'{self._bucket}/{key}'

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_bytes(self, key: str) -> bytes:
        """Download an object and return its body as bytes."""
        async with self._session.client('s3', **self._client_kwargs()) as client:
            response = await client.get_object(
                Bucket=self._bucket, Key=key,
            )
            body = await response['Body'].read()
        logger.debug(
            'Downloaded %d bytes ← %s/%s', len(body), self._bucket, key,
        )
        return body

    # ------------------------------------------------------------------
    # Signed URLs
    # ------------------------------------------------------------------

    async def generate_signed_url(
        self,
        key: str,
        expiry: int = 3600,
        *,
        response_content_disposition: str | None = None,
        response_content_type: str | None = None,
    ) -> str:
        """Generate a pre-signed GET URL."""
        params: Dict[str, Any] = {'Bucket': self._bucket, 'Key': key}
        if response_content_disposition:
            params['ResponseContentDisposition'] = response_content_disposition
        if response_content_type:
            params['ResponseContentType'] = response_content_type
        async with self._session.client('s3', **self._client_kwargs()) as client:
            url = await client.generate_presigned_url(
                'get_object',
                Params=params,
                ExpiresIn=expiry,
            )
        return url

    def build_public_url(self, key: str) -> str:
        """Build a plain (unsigned) public URL for the object.

        Used for static files served from a public-read bucket.
        """
        endpoint = self._config.get('ENDPOINT_URL')
        region = self._config.get('REGION_NAME')

        if endpoint:
            return f'{endpoint.rstrip("/")}/{self._bucket}/{key}'
        if region:
            return (
                f'https://{self._bucket}.s3.{region}.amazonaws.com/{key}'
            )
        return f'https://{self._bucket}.s3.amazonaws.com/{key}'

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_object(self, key: str) -> bool:
        """Delete a single object.  Returns True on success."""
        try:
            async with self._session.client('s3', **self._client_kwargs()) as client:
                await client.delete_object(
                    Bucket=self._bucket, Key=key,
                )
            logger.debug('Deleted %s/%s.', self._bucket, key)
            return True
        except ClientError as err:
            logger.error(
                'delete_object failed %s/%s: %s', self._bucket, key, err,
            )
            return False

    async def delete_objects(self, keys: List[str]) -> int:
        """Bulk-delete up to 1000 objects.  Returns count of successful deletes."""
        if not keys:
            return 0

        objects = [{'Key': k} for k in keys]
        deleted_count = 0

        async with self._session.client('s3', **self._client_kwargs()) as client:
            # S3 DeleteObjects supports max 1000 keys per call
            for i in range(0, len(objects), 1000):
                batch = objects[i:i + 1000]
                try:
                    resp = await client.delete_objects(
                        Bucket=self._bucket,
                        Delete={'Objects': batch, 'Quiet': True},
                    )
                    errors = resp.get('Errors', [])
                    deleted_count += len(batch) - len(errors)
                    for err in errors:
                        logger.error(
                            'Failed to delete %s/%s: %s',
                            self._bucket, err.get('Key'), err.get('Message'),
                        )
                except ClientError as err:
                    logger.error(
                        'delete_objects batch failed for %s: %s',
                        self._bucket, err,
                    )

        logger.debug(
            'Bulk-deleted %d/%d objects from %s.',
            deleted_count, len(keys), self._bucket,
        )
        return deleted_count

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def object_exists(self, key: str) -> bool:
        """Check whether an object exists in the bucket."""
        try:
            async with self._session.client('s3', **self._client_kwargs()) as client:
                await client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False
