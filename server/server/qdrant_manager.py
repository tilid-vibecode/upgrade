# File location: /server/server/qdrant_manager.py
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.conf import settings as django_settings
from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

logger = logging.getLogger(__name__)

_qdrant_manager_instance: Optional['QdrantManager'] = None
_POINT_ID_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
_PAYLOAD_INDEXES = [
    ('org_id', qdrant_models.PayloadSchemaType.KEYWORD),
    ('workspace_slug', qdrant_models.PayloadSchemaType.KEYWORD),
    ('doc_type', qdrant_models.PayloadSchemaType.KEYWORD),
    ('source_type', qdrant_models.PayloadSchemaType.KEYWORD),
    ('source_kind', qdrant_models.PayloadSchemaType.KEYWORD),
    ('source_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('parsed_source_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('employee_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('blueprint_run_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('cycle_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('pack_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('question_id', qdrant_models.PayloadSchemaType.KEYWORD),
    ('skill_key', qdrant_models.PayloadSchemaType.KEYWORD),
    ('evidence_row_uuid', qdrant_models.PayloadSchemaType.KEYWORD),
    ('evidence_category', qdrant_models.PayloadSchemaType.KEYWORD),
    ('generation_id', qdrant_models.PayloadSchemaType.KEYWORD),
    ('node_id', qdrant_models.PayloadSchemaType.KEYWORD),
    ('chunk_family', qdrant_models.PayloadSchemaType.KEYWORD),
    ('language_code', qdrant_models.PayloadSchemaType.KEYWORD),
    ('embedding_model', qdrant_models.PayloadSchemaType.KEYWORD),
    ('index_version', qdrant_models.PayloadSchemaType.KEYWORD),
    ('confidence', qdrant_models.PayloadSchemaType.FLOAT),
]

def string_to_uuid(string_id: str) -> str:
    # Keep this namespace stable so deterministic point IDs do not drift across re-index runs.
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, string_id))

class QdrantManager:
    def __init__(self, config: Dict[str, Any], context_config: Dict[str, Any]):
        self.config = config
        self.context_config = context_config

        self._async_client: Optional[AsyncQdrantClient] = None
        self._sync_client: Optional[QdrantClient] = None
        self._async_payload_indexes_ready = False
        self._sync_payload_indexes_ready = False

        self.host = config.get('HOST', 'localhost')
        self.port = config.get('PORT', 6333)
        self.grpc_port = config.get('GRPC_PORT', 6334)
        self.api_key = config.get('API_KEY')
        self.https = config.get('HTTPS', False)
        self.timeout = config.get('TIMEOUT', 30.0)
        self.prefer_grpc = config.get('PREFER_GRPC', False)

        self.collection_name = context_config.get('COLLECTION_NAME', 'org_context_documents')
        self.vector_size = context_config.get('VECTOR_SIZE', 1536)

    def _client_kwargs(self) -> Dict[str, Any]:
        kwargs = {
            'host': self.host,
            'port': self.port,
            'grpc_port': self.grpc_port,
            'https': self.https,
            'timeout': self.timeout,
            'prefer_grpc': self.prefer_grpc,
        }
        if self.api_key:
            kwargs['api_key'] = self.api_key
        return kwargs

    async def initialize(self) -> None:
        if self._async_client is None:
            self._async_client = AsyncQdrantClient(**self._client_kwargs())
            collections = await self._async_client.get_collections()
            logger.info(
                'Qdrant async client connected (%s:%s) — %d collections found.',
                self.host, self.port, len(collections.collections),
            )
        await self._ensure_collection()

    def initialize_sync(self) -> None:
        if self._sync_client is None:
            self._sync_client = QdrantClient(**self._client_kwargs())
            collections = self._sync_client.get_collections()
            logger.info(
                'Qdrant sync client connected (%s:%s) — %d collections.',
                self.host, self.port, len(collections.collections),
            )
        self._ensure_collection_sync()

    def _get_async_client(self) -> AsyncQdrantClient:
        if self._async_client is None:
            raise RuntimeError('Async Qdrant client not initialised.')
        return self._async_client

    def _get_sync_client(self) -> QdrantClient:
        if self._sync_client is None:
            self.initialize_sync()
        return self._sync_client

    async def _ensure_collection(self) -> None:
        client = self._get_async_client()
        collections = await client.get_collections()
        names = [c.name for c in collections.collections]

        if self.collection_name in names:
            info = await client.get_collection(self.collection_name)
            existing_size = info.config.params.vectors.size
            if existing_size != self.vector_size:
                logger.warning(
                    'Collection %s has vector size %d, expected %d.',
                    self.collection_name, existing_size, self.vector_size,
                )
            if not self._async_payload_indexes_ready:
                await self._create_payload_indexes(client)
            return

        try:
            await client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=self.vector_size,
                    distance=qdrant_models.Distance.COSINE,
                    on_disk=False,
                ),
                hnsw_config=qdrant_models.HnswConfigDiff(
                    m=16, ef_construct=100, full_scan_threshold=10000,
                ),
                optimizers_config=qdrant_models.OptimizersConfigDiff(indexing_threshold=20000),
            )
            await self._create_payload_indexes(client)
            logger.info('Created collection %s (vector_size=%d).', self.collection_name, self.vector_size)
        except UnexpectedResponse as err:
            if 'already exists' in str(err).lower():
                logger.debug('Collection %s already exists (race condition).', self.collection_name)
                if not self._async_payload_indexes_ready:
                    await self._create_payload_indexes(client)
            else:
                raise

    def _ensure_collection_sync(self) -> None:
        client = self._get_sync_client()
        collections = client.get_collections()
        names = [c.name for c in collections.collections]

        if self.collection_name in names:
            info = client.get_collection(self.collection_name)
            existing_size = info.config.params.vectors.size
            if existing_size != self.vector_size:
                logger.warning(
                    'Collection %s has vector size %d, expected %d.',
                    self.collection_name, existing_size, self.vector_size,
                )
            if not self._sync_payload_indexes_ready:
                self._create_payload_indexes_sync(client)
            return

        try:
            client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=self.vector_size,
                    distance=qdrant_models.Distance.COSINE,
                    on_disk=False,
                ),
                hnsw_config=qdrant_models.HnswConfigDiff(
                    m=16, ef_construct=100, full_scan_threshold=10000,
                ),
                optimizers_config=qdrant_models.OptimizersConfigDiff(indexing_threshold=20000),
            )
            self._create_payload_indexes_sync(client)
            logger.info('Created collection %s (sync, vector_size=%d).', self.collection_name, self.vector_size)
        except UnexpectedResponse as err:
            if 'already exists' in str(err).lower():
                logger.debug('Collection %s already exists (sync race condition).', self.collection_name)
                if not self._sync_payload_indexes_ready:
                    self._create_payload_indexes_sync(client)
            else:
                raise

    async def _create_payload_indexes(self, client: AsyncQdrantClient) -> None:
        had_error = False
        for field, schema in _PAYLOAD_INDEXES:
            try:
                await client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field, field_schema=schema,
                )
            except UnexpectedResponse as err:
                if 'already exists' not in str(err).lower():
                    logger.warning('Index creation for %s failed: %s', field, err)
                    had_error = True
        self._async_payload_indexes_ready = not had_error

    def _create_payload_indexes_sync(self, client: QdrantClient) -> None:
        had_error = False
        for field, schema in _PAYLOAD_INDEXES:
            try:
                client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=schema,
                )
            except UnexpectedResponse as err:
                if 'already exists' not in str(err).lower():
                    logger.warning('Sync index creation for %s failed: %s', field, err)
                    had_error = True
        self._sync_payload_indexes_ready = not had_error

    def _build_filter(
        self,
        *,
        org_id: Optional[str] = None,
        doc_types: Optional[List[str]] = None,
        additional_filters: Optional[Dict[str, Any]] = None,
    ) -> qdrant_models.Filter:
        must = []
        if org_id:
            must.append(qdrant_models.FieldCondition(
                key='org_id',
                match=qdrant_models.MatchValue(value=org_id),
            ))
        if doc_types:
            must.append(qdrant_models.FieldCondition(
                key='doc_type',
                match=qdrant_models.MatchAny(any=doc_types),
            ))
        if additional_filters:
            for key, value in additional_filters.items():
                if value in (None, '', []):
                    continue
                match = (
                    qdrant_models.MatchAny(any=value) if isinstance(value, list)
                    else qdrant_models.MatchValue(value=value)
                )
                must.append(qdrant_models.FieldCondition(key=key, match=match))
        return qdrant_models.Filter(must=must)

    async def upsert_document(
        self, doc_id: str, vector: List[float], payload: Dict[str, Any],
    ) -> bool:
        if 'org_id' not in payload:
            raise ValueError('payload must include org_id.')
        client = self._get_async_client()
        payload['_original_id'] = doc_id
        try:
            await client.upsert(
                collection_name=self.collection_name,
                points=[qdrant_models.PointStruct(
                    id=string_to_uuid(doc_id), vector=vector, payload=payload,
                )],
            )
            return True
        except Exception as err:
            logger.error('upsert_document failed for %s: %s', doc_id, err, exc_info=True)
            return False

    async def upsert_documents_batch(
        self, documents: List[Dict[str, Any]], batch_size: int = 100,
    ) -> int:
        client = self._get_async_client()
        total = 0

        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            points = []
            for doc in batch:
                pl = doc.get('payload', {})
                if 'org_id' not in pl:
                    continue
                pl = pl.copy()
                pl['_original_id'] = doc['id']
                points.append(qdrant_models.PointStruct(
                    id=string_to_uuid(doc['id']), vector=doc['vector'], payload=pl,
                ))
            if not points:
                continue
            try:
                await client.upsert(collection_name=self.collection_name, points=points)
                total += len(points)
            except Exception as err:
                logger.error('Batch upsert failed at index %d: %s', i, err)

        logger.info('Batch upsert: %d/%d succeeded.', total, len(documents))
        return total

    async def search(
        self,
        org_id: str,
        query_vector: List[float],
        doc_types: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        additional_filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        client = self._get_async_client()
        top_k = top_k if top_k is not None else self.context_config.get('DEFAULT_TOP_K', 10)
        min_score = min_score if min_score is not None else self.context_config.get('MIN_SCORE', 0.3)

        try:
            results = await client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=self._build_filter(
                    org_id=org_id,
                    doc_types=doc_types,
                    additional_filters=additional_filters,
                ),
                limit=top_k, score_threshold=min_score, with_payload=True,
            )
            return [
                {'id': p.id, 'score': p.score, 'payload': p.payload}
                for p in results.points
            ]
        except Exception as err:
            logger.error('Search failed for org %s: %s', org_id, err, exc_info=True)
            return []

    async def delete_document(self, doc_id: str) -> bool:
        client = self._get_async_client()
        try:
            await client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.PointIdsList(points=[string_to_uuid(doc_id)]),
            )
            return True
        except Exception as err:
            logger.error('delete_document failed for %s: %s', doc_id, err)
            return False

    async def delete_by_org(self, org_id: str) -> bool:
        return await self.delete_by_filters(org_id=org_id)

    async def delete_by_filters(
        self,
        *,
        org_id: Optional[str] = None,
        doc_types: Optional[List[str]] = None,
        additional_filters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        client = self._get_async_client()
        filter_conditions = self._build_filter(
            org_id=org_id,
            doc_types=doc_types,
            additional_filters=additional_filters,
        )
        if not filter_conditions.must:
            raise ValueError('delete_by_filters requires at least one filter condition.')
        try:
            await client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.FilterSelector(filter=filter_conditions),
            )
            logger.info(
                'Deleted documents from %s using filters org_id=%s doc_types=%s extra=%s.',
                self.collection_name,
                org_id,
                doc_types,
                additional_filters,
            )
            return True
        except Exception as err:
            logger.error(
                'delete_by_filters failed for org=%s doc_types=%s extra=%s: %s',
                org_id,
                doc_types,
                additional_filters,
                err,
            )
            return False

    async def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        client = self._get_async_client()
        try:
            results = await client.retrieve(
                collection_name=self.collection_name,
                ids=[string_to_uuid(doc_id)], with_payload=True, with_vectors=False,
            )
            if results:
                return {'id': results[0].id, 'payload': results[0].payload}
            return None
        except Exception as err:
            logger.error('get_document failed for %s: %s', doc_id, err)
            return None

    async def count_documents(self, org_id: Optional[str] = None) -> int:
        client = self._get_async_client()
        try:
            kwargs: Dict[str, Any] = {'collection_name': self.collection_name}
            if org_id:
                kwargs['count_filter'] = qdrant_models.Filter(must=[
                    qdrant_models.FieldCondition(
                        key='org_id', match=qdrant_models.MatchValue(value=org_id),
                    ),
                ])
            return (await client.count(**kwargs)).count
        except Exception as err:
            logger.error('count_documents failed: %s', err)
            return 0

    async def get_collection_info(self) -> Optional[Dict[str, Any]]:
        client = self._get_async_client()
        try:
            info = await client.get_collection(self.collection_name)
            vectors_cfg = info.config.params.vectors
            size = getattr(vectors_cfg, 'size', None)
            if size is None and isinstance(vectors_cfg, dict):
                first = next(iter(vectors_cfg.values()), None)
                size = getattr(first, 'size', None) if first else None
            return {
                'name': self.collection_name,
                'points_count': info.points_count,
                'status': info.status.value if info.status else 'unknown',
                'vector_size': size,
            }
        except Exception as err:
            logger.error('get_collection_info failed: %s', err)
            return None

    def upsert_document_sync(
        self, doc_id: str, vector: List[float], payload: Dict[str, Any],
    ) -> bool:
        if 'org_id' not in payload:
            raise ValueError('payload must include org_id.')
        client = self._get_sync_client()
        payload['_original_id'] = doc_id
        try:
            client.upsert(
                collection_name=self.collection_name,
                points=[qdrant_models.PointStruct(
                    id=string_to_uuid(doc_id), vector=vector, payload=payload,
                )],
            )
            return True
        except Exception as err:
            logger.error('upsert_document_sync failed for %s: %s', doc_id, err)
            return False

    def upsert_documents_batch_sync(
        self,
        documents: List[Dict[str, Any]],
        batch_size: int = 100,
    ) -> int:
        client = self._get_sync_client()
        total = 0

        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            points = []
            for doc in batch:
                payload = dict(doc.get('payload', {}) or {})
                if 'org_id' not in payload:
                    continue
                payload['_original_id'] = doc['id']
                points.append(qdrant_models.PointStruct(
                    id=string_to_uuid(doc['id']),
                    vector=doc['vector'],
                    payload=payload,
                ))
            if not points:
                continue
            try:
                client.upsert(collection_name=self.collection_name, points=points)
                total += len(points)
            except Exception as err:
                logger.error('Sync batch upsert failed at index %d: %s', i, err)

        logger.info('Sync batch upsert: %d/%d succeeded.', total, len(documents))
        return total

    def search_sync(
        self,
        org_id: str,
        query_vector: List[float],
        doc_types: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        additional_filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        client = self._get_sync_client()
        top_k = top_k if top_k is not None else self.context_config.get('DEFAULT_TOP_K', 10)
        min_score = min_score if min_score is not None else self.context_config.get('MIN_SCORE', 0.3)

        try:
            results = client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=self._build_filter(
                    org_id=org_id,
                    doc_types=doc_types,
                    additional_filters=additional_filters,
                ),
                limit=top_k, score_threshold=min_score, with_payload=True,
            )
            return [
                {'id': p.id, 'score': p.score, 'payload': p.payload}
                for p in results.points
            ]
        except Exception as err:
            logger.error('search_sync failed for org %s: %s', org_id, err)
            return []

    def delete_by_filters_sync(
        self,
        *,
        org_id: Optional[str] = None,
        doc_types: Optional[List[str]] = None,
        additional_filters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        client = self._get_sync_client()
        filter_conditions = self._build_filter(
            org_id=org_id,
            doc_types=doc_types,
            additional_filters=additional_filters,
        )
        if not filter_conditions.must:
            raise ValueError('delete_by_filters_sync requires at least one filter condition.')
        try:
            client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.FilterSelector(filter=filter_conditions),
            )
            return True
        except Exception as err:
            logger.error(
                'delete_by_filters_sync failed for org=%s doc_types=%s extra=%s: %s',
                org_id,
                doc_types,
                additional_filters,
                err,
            )
            return False

    async def health_check(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'service': 'qdrant',
            'healthy': False,
            'host': f'{self.host}:{self.port}',
            'collection': self.collection_name,
        }
        try:
            client = self._get_async_client()
            cols = await client.get_collections()
            names = [c.name for c in cols.collections]
            if self.collection_name in names:
                info = await self.get_collection_info()
                result['healthy'] = (info or {}).get('status') == 'green'
                result['points_count'] = (info or {}).get('points_count', 0)
            else:
                result['error'] = 'collection missing'
        except Exception as err:
            result['error'] = str(err)
        return result

    async def close(self) -> None:
        if self._async_client:
            await self._async_client.close()
            self._async_client = None
        if self._sync_client:
            self._sync_client.close()
            self._sync_client = None
        self._async_payload_indexes_ready = False
        self._sync_payload_indexes_ready = False
        logger.info('Qdrant clients closed.')

async def initialize_qdrant_manager() -> None:
    global _qdrant_manager_instance
    if _qdrant_manager_instance is not None:
        return

    cfg = django_settings.QDRANT_CONFIG
    ctx = django_settings.ORG_CONTEXT_CONFIG
    _qdrant_manager_instance = QdrantManager(cfg, ctx)
    await _qdrant_manager_instance.initialize()
    logger.info('QdrantManager initialised (async).')

def initialize_qdrant_manager_sync() -> None:
    global _qdrant_manager_instance
    if _qdrant_manager_instance is not None:
        _qdrant_manager_instance.initialize_sync()
        return

    cfg = django_settings.QDRANT_CONFIG
    ctx = django_settings.ORG_CONTEXT_CONFIG
    _qdrant_manager_instance = QdrantManager(cfg, ctx)
    _qdrant_manager_instance.initialize_sync()
    logger.info('QdrantManager initialised (sync).')

async def get_qdrant_manager() -> QdrantManager:
    if _qdrant_manager_instance is None:
        raise RuntimeError('QdrantManager not initialised.')
    return _qdrant_manager_instance

def get_qdrant_manager_sync() -> QdrantManager:
    if _qdrant_manager_instance is None:
        raise RuntimeError('QdrantManager not initialised.')
    return _qdrant_manager_instance

async def close_qdrant_manager() -> None:
    global _qdrant_manager_instance
    if _qdrant_manager_instance:
        await _qdrant_manager_instance.close()
        _qdrant_manager_instance = None
