#!/usr/bin/env python3
"""
Async Production RAG Manager for ChromaDB
Enhanced version with async operations, Redis caching, and improved performance
for high-concurrency production environments.
"""

import asyncio
import aiohttp
import aioredis
import json
import logging
import time
import hashlib
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import os
from enum import Enum

# Load environment variables
def load_env():
    """Load environment variables from .env file."""
    try:
        # Load from project root (JAM7 folder)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file_path = os.path.join(project_root, '.env')
        with open(env_file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    try:
                        key, value = line.split('=', 1)
                        # Clean the key and value
                        key = key.strip()
                        value = value.strip()
                        
                        # Remove quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        # Validate key and value
                        if '\0' in key or '\0' in value:
                            print(f"Warning: Skipping line {line_num} - contains null characters")
                            continue
                        
                        if key and value is not None:
                            os.environ[key] = value
                    except Exception as e:
                        print(f"Warning: Error processing line {line_num}: {e}")
                        continue
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Warning: Error loading .env file: {e}")

load_env()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

@dataclass
class CacheConfig:
    """Configuration for Redis caching."""
    enabled: bool = True
    ttl_seconds: int = 120  # 2 minutes default TTL
    max_size: int = 10000   # Maximum cache entries
    enable_query_cache: bool = True
    enable_embedding_cache: bool = True

@dataclass
class ConnectionConfig:
    """Configuration for connection management."""
    max_connections: int = 100
    connection_timeout: float = 30.0
    read_timeout: float = 60.0
    retry_attempts: int = 3
    retry_delay: float = 1.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: float = 60.0

class AsyncConnectionPool:
    """Async connection pool for ChromaDB HTTP client."""
    
    def __init__(self, base_url: str, config: ConnectionConfig):
        self.base_url = base_url
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._circuit_breaker = AsyncCircuitBreaker(
            threshold=config.circuit_breaker_threshold,
            timeout=config.circuit_breaker_timeout
        )
    
    async def __aenter__(self):
        await self._ensure_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _ensure_session(self):
        """Ensure aiohttp session is created."""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.config.max_connections,
                limit_per_host=self.config.max_connections,
                ttl_dns_cache=300,
                use_dns_cache=True
            )
            timeout = aiohttp.ClientTimeout(
                total=self.config.connection_timeout,
                connect=self.config.connection_timeout / 2
            )
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={'Content-Type': 'application/json'}
            )
    
    async def close(self):
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make an async HTTP request with circuit breaker and retry logic."""
        url = f"{self.base_url}{endpoint}"
        
        async with self._circuit_breaker:
            for attempt in range(self.config.retry_attempts):
                try:
                    await self._ensure_session()
                    
                    async with self.session.request(method, url, **kwargs) as response:
                        if response.status >= 400:
                            raise aiohttp.ClientResponseError(
                                request_info=response.request_info,
                                history=response.history,
                                status=response.status,
                                message=f"HTTP {response.status}"
                            )
                        
                        data = await response.json()
                        self._circuit_breaker.record_success()
                        return data
                        
                except Exception as e:
                    self._circuit_breaker.record_failure()
                    if attempt == self.config.retry_attempts - 1:
                        raise
                    
                    await asyncio.sleep(self.config.retry_delay * (2 ** attempt))
    
    async def get(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make GET request."""
        return await self.request('GET', endpoint, **kwargs)
    
    async def post(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make POST request."""
        return await self.request('POST', endpoint, **kwargs)
    
    async def delete(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make DELETE request."""
        return await self.request('DELETE', endpoint, **kwargs)

class AsyncCircuitBreaker:
    """Async circuit breaker pattern implementation."""
    
    def __init__(self, threshold: int = 5, timeout: float = 60.0):
        self.threshold = threshold
        self.timeout = timeout
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0
        self._lock = asyncio.Lock()
    
    async def __aenter__(self):
        await self._check_state()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    
    async def _check_state(self):
        """Check and update circuit breaker state."""
        async with self._lock:
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time > self.timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker moved to HALF_OPEN state")
                else:
                    raise Exception("Circuit breaker is OPEN")
    
    def record_success(self):
        """Record a successful operation."""
        async def _record():
            async with self._lock:
                if self.state == CircuitState.HALF_OPEN:
                    self.state = CircuitState.CLOSED
                    logger.info("Circuit breaker moved to CLOSED state")
                self.failure_count = 0
        
        asyncio.create_task(_record())
    
    def record_failure(self):
        """Record a failed operation."""
        async def _record():
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                
                if self.failure_count >= self.threshold:
                    self.state = CircuitState.OPEN
                    logger.warning(f"Circuit breaker moved to OPEN state after {self.failure_count} failures")
        
        asyncio.create_task(_record())

class AsyncRedisCache:
    """Async Redis cache implementation."""
    
    def __init__(self, config: CacheConfig):
        self.config = config
        self.redis: Optional[aioredis.Redis] = None
        self._lock = asyncio.Lock()
    
    async def connect(self):
        """Connect to Redis."""
        if not self.config.enabled:
            return
        
        try:
            redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
            self.redis = aioredis.from_url(redis_url, decode_responses=True)
            await self.redis.ping()
            logger.info("Connected to Redis cache")
        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}")
            self.redis = None
    
    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
    
    def _generate_key(self, prefix: str, data: Any) -> str:
        """Generate cache key."""
        data_str = json.dumps(data, sort_keys=True)
        hash_obj = hashlib.md5(data_str.encode())
        return f"{prefix}:{hash_obj.hexdigest()}"
    
    async def get(self, prefix: str, data: Any) -> Optional[Dict[str, Any]]:
        """Get value from cache."""
        if not self.config.enabled or not self.redis:
            return None
        
        try:
            key = self._generate_key(prefix, data)
            value = await self.redis.get(key)
            if value:
                return json.loads(value)
        except Exception as e:
            logger.warning(f"Cache get error: {e}")
        
        return None
    
    async def set(self, prefix: str, data: Any, value: Dict[str, Any]):
        """Set value in cache."""
        if not self.config.enabled or not self.redis:
            return
        
        try:
            key = self._generate_key(prefix, data)
            await self.redis.setex(
                key,
                self.config.ttl_seconds,
                json.dumps(value)
            )
        except Exception as e:
            logger.warning(f"Cache set error: {e}")
    
    async def invalidate_pattern(self, pattern: str):
        """Invalidate cache entries matching pattern."""
        if not self.config.enabled or not self.redis:
            return
        
        try:
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} cache entries")
        except Exception as e:
            logger.warning(f"Cache invalidation error: {e}")

class AsyncProductionRAGManager:
    """Async Production RAG Manager with Redis caching and high-performance features."""
    
    def __init__(self, 
                 chromadb_host: str = None,
                 chromadb_port: int = None,
                 connection_config: ConnectionConfig = None,
                 cache_config: CacheConfig = None):
        
        # Load configuration from environment
        self.chromadb_host = chromadb_host or os.getenv('CHROMADB_HOST', 'localhost')
        self.chromadb_port = chromadb_port or int(os.getenv('CHROMADB_PORT', '8000'))
        self.base_url = f"http://{self.chromadb_host}:{self.chromadb_port}/api/v1"
        
        # Initialize configurations
        self.connection_config = connection_config or ConnectionConfig()
        self.cache_config = cache_config or CacheConfig()
        
        # Initialize components
        self.connection_pool: Optional[AsyncConnectionPool] = None
        self.cache: Optional[AsyncRedisCache] = None
        self._executor = ThreadPoolExecutor(max_workers=10)
        self._lock = asyncio.Lock()
        
        # Performance metrics
        self.metrics = {
            'requests_total': 0,
            'requests_success': 0,
            'requests_failed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'avg_response_time': 0.0
        }
    
    async def initialize(self):
        """Initialize the RAG manager."""
        logger.info("Initializing Async Production RAG Manager...")
        
        # Initialize connection pool
        self.connection_pool = AsyncConnectionPool(self.base_url, self.connection_config)
        
        # Initialize cache
        self.cache = AsyncRedisCache(self.cache_config)
        await self.cache.connect()
        
        # Test connection
        await self.health_check()
        logger.info("Async Production RAG Manager initialized successfully")
    
    async def close(self):
        """Close all connections."""
        if self.connection_pool:
            await self.connection_pool.close()
        if self.cache:
            await self.cache.close()
        self._executor.shutdown(wait=True)
    
    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            async with self.connection_pool as pool:
                response = await pool.get('/heartbeat')
                return response.get('status') == 'ok'
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
    
    async def get_collections(self) -> List[Dict[str, Any]]:
        """Get all collections."""
        cache_key = 'collections'
        
        # Try cache first
        if self.cache_config.enabled:
            cached = await self.cache.get('collections', cache_key)
            if cached:
                self.metrics['cache_hits'] += 1
                return cached
        
        # Fetch from ChromaDB
        start_time = time.time()
        try:
            async with self.connection_pool as pool:
                response = await pool.get('/collections')
                self.metrics['requests_success'] += 1
                
                # Cache the result
                if self.cache_config.enabled:
                    await self.cache.set('collections', cache_key, response)
                
                self.metrics['cache_misses'] += 1
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to get collections: {e}")
            raise
    
    async def create_collection(self, name: str, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create a new collection."""
        start_time = time.time()
        try:
            async with self.connection_pool as pool:
                response = await pool.post('/collections', json={
                    'name': name,
                    'metadata': metadata or {}
                })
                self.metrics['requests_success'] += 1
                
                # Invalidate collections cache
                if self.cache_config.enabled:
                    await self.cache.invalidate_pattern('collections:*')
                
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to create collection {name}: {e}")
            raise
    
    async def add_documents(self, 
                          collection_name: str, 
                          documents: List[str],
                          metadatas: List[Dict[str, Any]] = None,
                          ids: List[str] = None) -> Dict[str, Any]:
        """Add documents to collection with async processing."""
        start_time = time.time()
        
        # Generate IDs if not provided
        if ids is None:
            ids = [f"doc_{i}_{int(time.time())}" for i in range(len(documents))]
        
        # Prepare batch data
        batch_data = {
            'collection_name': collection_name,
            'documents': documents,
            'metadatas': metadatas or [{}] * len(documents),
            'ids': ids
        }
        
        try:
            async with self.connection_pool as pool:
                response = await pool.post('/collections/add', json=batch_data)
                self.metrics['requests_success'] += 1
                
                # Invalidate relevant caches
                if self.cache_config.enabled:
                    await self.cache.invalidate_pattern(f'collection:{collection_name}:*')
                
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to add documents to collection {collection_name}: {e}")
            raise
    
    async def similarity_search(self,
                              collection_name: str,
                              query_texts: List[str],
                              n_results: int = 10,
                              where: Dict[str, Any] = None,
                              where_document: Dict[str, Any] = None) -> Dict[str, Any]:
        """Perform similarity search with caching."""
        start_time = time.time()
        
        # Prepare search parameters
        search_params = {
            'collection_name': collection_name,
            'query_texts': query_texts,
            'n_results': n_results,
            'where': where,
            'where_document': where_document
        }
        
        # Try cache first for query cache
        if self.cache_config.enable_query_cache:
            cache_key = f'search:{collection_name}'
            cached = await self.cache.get(cache_key, search_params)
            if cached:
                self.metrics['cache_hits'] += 1
                return cached
        
        try:
            async with self.connection_pool as pool:
                response = await pool.post('/collections/query', json=search_params)
                self.metrics['requests_success'] += 1
                
                # Cache the result
                if self.cache_config.enable_query_cache:
                    await self.cache.set(cache_key, search_params, response)
                
                self.metrics['cache_misses'] += 1
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to perform similarity search: {e}")
            raise
    
    async def get_collection_info(self, collection_name: str) -> Dict[str, Any]:
        """Get collection information."""
        cache_key = f'collection_info:{collection_name}'
        
        # Try cache first
        if self.cache_config.enabled:
            cached = await self.cache.get('collection_info', collection_name)
            if cached:
                self.metrics['cache_hits'] += 1
                return cached
        
        start_time = time.time()
        try:
            async with self.connection_pool as pool:
                response = await pool.get(f'/collections/{collection_name}')
                self.metrics['requests_success'] += 1
                
                # Cache the result
                if self.cache_config.enabled:
                    await self.cache.set('collection_info', collection_name, response)
                
                self.metrics['cache_misses'] += 1
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to get collection info for {collection_name}: {e}")
            raise
    
    async def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """Delete a collection."""
        start_time = time.time()
        try:
            async with self.connection_pool as pool:
                response = await pool.delete(f'/collections/{collection_name}')
                self.metrics['requests_success'] += 1
                
                # Invalidate all related caches
                if self.cache_config.enabled:
                    await self.cache.invalidate_pattern(f'*{collection_name}*')
                
                self._update_metrics(time.time() - start_time)
                return response
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Failed to delete collection {collection_name}: {e}")
            raise
    
    async def batch_operations(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute multiple operations concurrently."""
        tasks = []
        
        for operation in operations:
            op_type = operation.get('type')
            if op_type == 'search':
                task = self.similarity_search(**operation['params'])
            elif op_type == 'add':
                task = self.add_documents(**operation['params'])
            elif op_type == 'get_info':
                task = self.get_collection_info(**operation['params'])
            else:
                logger.warning(f"Unknown operation type: {op_type}")
                continue
            
            tasks.append(task)
        
        # Execute all operations concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Operation {i} failed: {result}")
                processed_results.append({'error': str(result)})
            else:
                processed_results.append(result)
        
        return processed_results
    
    def _update_metrics(self, response_time: float):
        """Update performance metrics."""
        self.metrics['requests_total'] += 1
        
        # Update average response time
        total_requests = self.metrics['requests_total']
        current_avg = self.metrics['avg_response_time']
        self.metrics['avg_response_time'] = (
            (current_avg * (total_requests - 1) + response_time) / total_requests
        )
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current performance metrics."""
        return {
            **self.metrics,
            'cache_hit_rate': (
                self.metrics['cache_hits'] / 
                (self.metrics['cache_hits'] + self.metrics['cache_misses'])
                if (self.metrics['cache_hits'] + self.metrics['cache_misses']) > 0 else 0
            ),
            'success_rate': (
                self.metrics['requests_success'] / 
                self.metrics['requests_total']
                if self.metrics['requests_total'] > 0 else 0
            )
        }
    
    async def clear_cache(self):
        """Clear all cache entries."""
        if self.cache:
            await self.cache.invalidate_pattern('*')
    
    async def async_similarity_search(self, 
                                    query: str, 
                                    collection_name: str, 
                                    n_results: int = 5,
                                    where: Dict[str, Any] = None,
                                    where_document: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Perform similarity search asynchronously.
        
        Args:
            query: Search query text
            collection_name: Name of the collection to search
            n_results: Number of results to return
            where: Filter conditions for metadata
            where_document: Filter conditions for document content
            
        Returns:
            List of search results
        """
        cache_key = f"search:{hash(query)}:{collection_name}:{n_results}"
        
        # Try cache first
        if self.cache_config.enabled and self.cache_config.enable_query_cache:
            cached = await self.cache.get('search', cache_key)
            if cached:
                self.metrics['cache_hits'] += 1
                return cached
        
        # Perform search
        start_time = time.time()
        try:
            search_params = {
                'query_texts': [query],
                'n_results': n_results
            }
            
            if where:
                search_params['where'] = where
            if where_document:
                search_params['where_document'] = where_document
            
            async with self.connection_pool as pool:
                response = await pool.post(f'/collections/{collection_name}/query', json=search_params)
                self.metrics['requests_success'] += 1
                
                # Cache the result
                if self.cache_config.enabled and self.cache_config.enable_query_cache:
                    await self.cache.set('search', cache_key, response)
                
                self.metrics['cache_misses'] += 1
                self._update_metrics(time.time() - start_time)
                return response.get('results', [])
                
        except Exception as e:
            self.metrics['requests_failed'] += 1
            logger.error(f"Similarity search failed: {e}")
            raise

# Example usage
async def main():
    """Example usage of Async Production RAG Manager."""
    
    # Initialize RAG manager
    rag_manager = AsyncProductionRAGManager(
        connection_config=ConnectionConfig(
            max_connections=50,
            retry_attempts=3,
            circuit_breaker_threshold=5
        ),
        cache_config=CacheConfig(
            enabled=True,
            ttl_seconds=120,
            enable_query_cache=True
        )
    )
    
    try:
        await rag_manager.initialize()
        
        # Example operations
        collections = await rag_manager.get_collections()
        print(f"Found {len(collections)} collections")
        
        # Batch operations example
        operations = [
            {
                'type': 'search',
                'params': {
                    'collection_name': 'test_collection',
                    'query_texts': ['test query'],
                    'n_results': 5
                }
            },
            {
                'type': 'get_info',
                'params': {
                    'collection_name': 'test_collection'
                }
            }
        ]
        
        results = await rag_manager.batch_operations(operations)
        print(f"Batch operations completed: {len(results)} results")
        
        # Get metrics
        metrics = rag_manager.get_metrics()
        print(f"Performance metrics: {metrics}")
        
    finally:
        await rag_manager.close()

if __name__ == "__main__":
    asyncio.run(main())
