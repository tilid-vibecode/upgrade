# File location: /server/server/embedding_manager.py
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import openai
from openai import AsyncOpenAI, OpenAI

from django.conf import settings as django_settings

logger = logging.getLogger(__name__)

_embedding_manager_instance: Optional['EmbeddingManager'] = None

class BaseEmbeddingProvider(ABC):

    @abstractmethod
    async def embed_text(self, text: str) -> List[float]: ...

    @abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]: ...

    @abstractmethod
    def embed_text_sync(self, text: str) -> List[float]: ...

    @abstractmethod
    def embed_texts_sync(self, texts: List[str]) -> List[List[float]]: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

class OpenAIEmbeddingProvider(BaseEmbeddingProvider):

    def __init__(self, config: Dict[str, Any], api_key: str):
        self.model = config.get('OPENAI_MODEL', 'text-embedding-3-small')
        self._dimensions = config.get('OPENAI_DIMENSIONS', 1536)
        self.batch_size = config.get('OPENAI_BATCH_SIZE', 100)
        self._async_client: Optional[AsyncOpenAI] = None
        self._sync_client: Optional[OpenAI] = None
        self._api_key = api_key

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self.model

    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(api_key=self._api_key)
        return self._async_client

    def _get_sync_client(self) -> OpenAI:
        if self._sync_client is None:
            self._sync_client = OpenAI(api_key=self._api_key)
        return self._sync_client

    def _dims_kwarg(self) -> Optional[int]:
        return self._dimensions if 'text-embedding-3' in self.model else None

    @staticmethod
    def _prepare(text: str, max_chars: int = 32_000) -> str:
        text = ' '.join(text.strip().split())
        if len(text) > max_chars:
            logger.warning('Truncating text from %d to %d chars.', len(text), max_chars)
            text = text[:max_chars]
        return text

    async def embed_text(self, text: str) -> List[float]:
        client = self._get_async_client()
        try:
            resp = await client.embeddings.create(
                model=self.model, input=self._prepare(text), dimensions=self._dims_kwarg(),
            )
            return resp.data[0].embedding
        except openai.RateLimitError:
            logger.warning('OpenAI rate limit — retrying in 1s.')
            await asyncio.sleep(1)
            return await self.embed_text(text)

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        client = self._get_async_client()
        prepared = [self._prepare(t) for t in texts]
        all_embeddings: List[List[float]] = []

        for i in range(0, len(prepared), self.batch_size):
            batch = prepared[i:i + self.batch_size]
            try:
                resp = await client.embeddings.create(
                    model=self.model, input=batch, dimensions=self._dims_kwarg(),
                )
                sorted_data = sorted(resp.data, key=lambda x: x.index)
                all_embeddings.extend([d.embedding for d in sorted_data])
            except openai.RateLimitError:
                logger.warning('Rate limit on batch — retrying in 2s.')
                await asyncio.sleep(2)
                resp = await client.embeddings.create(
                    model=self.model, input=batch, dimensions=self._dims_kwarg(),
                )
                sorted_data = sorted(resp.data, key=lambda x: x.index)
                all_embeddings.extend([d.embedding for d in sorted_data])
            except Exception as err:
                logger.error('Batch embed failed at index %d: %s', i, err)
                all_embeddings.extend([[0.0] * self._dimensions] * len(batch))

        return all_embeddings

    def embed_text_sync(self, text: str) -> List[float]:
        client = self._get_sync_client()
        try:
            resp = client.embeddings.create(
                model=self.model, input=self._prepare(text), dimensions=self._dims_kwarg(),
            )
            return resp.data[0].embedding
        except openai.RateLimitError:
            logger.warning('Rate limit (sync) — retrying in 1s.')
            time.sleep(1)
            return self.embed_text_sync(text)

    def embed_texts_sync(self, texts: List[str]) -> List[List[float]]:
        client = self._get_sync_client()
        prepared = [self._prepare(t) for t in texts]
        all_embeddings: List[List[float]] = []

        for i in range(0, len(prepared), self.batch_size):
            batch = prepared[i:i + self.batch_size]
            try:
                resp = client.embeddings.create(
                    model=self.model, input=batch, dimensions=self._dims_kwarg(),
                )
                sorted_data = sorted(resp.data, key=lambda x: x.index)
                all_embeddings.extend([d.embedding for d in sorted_data])
            except Exception as err:
                logger.error('Sync batch embed failed at index %d: %s', i, err)
                all_embeddings.extend([[0.0] * self._dimensions] * len(batch))

        return all_embeddings

class EmbeddingManager:

    def __init__(self, config: Dict[str, Any], api_key: Optional[str] = None):
        self.config = config
        self.provider_name = config.get('PROVIDER', 'openai')
        self._provider: Optional[BaseEmbeddingProvider] = None
        self._api_key = api_key

    def initialize(self) -> None:
        if self._provider is not None:
            return

        if self.provider_name == 'openai':
            if not self._api_key:
                raise ValueError('OPENAI_API_KEY required for OpenAI embeddings.')
            self._provider = OpenAIEmbeddingProvider(self.config, self._api_key)
            logger.info(
                'OpenAI embedding provider ready (model=%s, dims=%d).',
                self._provider.model, self._provider.dimensions,
            )
        else:
            raise ValueError(f'Unknown embedding provider: {self.provider_name}')

    def _get_provider(self) -> BaseEmbeddingProvider:
        if self._provider is None:
            raise RuntimeError('EmbeddingManager not initialised.')
        return self._provider

    @property
    def dimensions(self) -> int:
        return self._get_provider().dimensions

    @property
    def model_name(self) -> str:
        return self._get_provider().model_name

    async def embed(self, text: str) -> List[float]:
        return await self._get_provider().embed_text(text)

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return await self._get_provider().embed_texts(texts)

    def embed_sync(self, text: str) -> List[float]:
        return self._get_provider().embed_text_sync(text)

    def embed_batch_sync(self, texts: List[str]) -> List[List[float]]:
        return self._get_provider().embed_texts_sync(texts)

    async def health_check(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'service': 'embedding', 'healthy': False, 'provider': self.provider_name,
        }
        try:
            provider = self._get_provider()
            result['model'] = provider.model_name
            result['dimensions'] = provider.dimensions
            test = await provider.embed_text('health check')
            result['healthy'] = len(test) == provider.dimensions
        except Exception as err:
            result['error'] = str(err)
        return result

async def initialize_embedding_manager() -> None:
    global _embedding_manager_instance
    if _embedding_manager_instance is not None:
        return

    config = django_settings.EMBEDDING_CONFIG
    api_key = django_settings.OPENAI_API_KEY
    _embedding_manager_instance = EmbeddingManager(config, api_key)
    _embedding_manager_instance.initialize()

    test = await _embedding_manager_instance.embed('init test')
    logger.info('EmbeddingManager initialised (dims=%d).', len(test))

def initialize_embedding_manager_sync() -> None:
    global _embedding_manager_instance
    if _embedding_manager_instance is not None:
        return

    config = django_settings.EMBEDDING_CONFIG
    api_key = django_settings.OPENAI_API_KEY
    _embedding_manager_instance = EmbeddingManager(config, api_key)
    _embedding_manager_instance.initialize()

    test = _embedding_manager_instance.embed_sync('init test')
    logger.info('EmbeddingManager initialised sync (dims=%d).', len(test))

async def get_embedding_manager() -> EmbeddingManager:
    if _embedding_manager_instance is None:
        raise RuntimeError('EmbeddingManager not initialised.')
    return _embedding_manager_instance

def get_embedding_manager_sync() -> EmbeddingManager:
    if _embedding_manager_instance is None:
        raise RuntimeError('EmbeddingManager not initialised.')
    return _embedding_manager_instance

async def embed_text(text: str) -> List[float]:
    return await (await get_embedding_manager()).embed(text)

async def embed_texts(texts: List[str]) -> List[List[float]]:
    return await (await get_embedding_manager()).embed_batch(texts)

def embed_text_sync(text: str) -> List[float]:
    return get_embedding_manager_sync().embed_sync(text)

def embed_texts_sync(texts: List[str]) -> List[List[float]]:
    return get_embedding_manager_sync().embed_batch_sync(texts)
