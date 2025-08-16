#!/usr/bin/env python3
"""
Production-Ready RAG Manager for AWS ChromaDB Deployment
Features:
- Connection pooling for high concurrency
- Circuit breaker pattern with exponential backoff
- Comprehensive health checks and monitoring
- Automatic failover and recovery
- CloudWatch metrics integration
- Production-grade error handling
"""

import asyncio
import time
import json
import logging
import threading
from typing import Dict, List, Optional, Union, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics
from contextlib import contextmanager

import boto3
import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain.docstore.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Configure structured logging
# Get config folder for log file
config_folder = os.getenv('CONFIG_FOLDER', '.')
log_file_path = os.path.join(config_folder, 'rag_manager.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file_path)
    ]
)
logger = logging.getLogger(__name__)

class ConnectionState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class ConnectionMetrics:
    """Track connection performance metrics."""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_response_time: float = 0.0
    response_times: List[float] = field(default_factory=list)
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    
    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests
    
    @property
    def average_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        return statistics.mean(self.response_times)
    
    @property
    def p95_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        return statistics.quantiles(self.response_times, n=20)[18]  # 95th percentile

class CircuitBreaker:
    """Circuit breaker pattern implementation for fault tolerance."""
    
    def __init__(self, 
                 failure_threshold: int = 5,
                 recovery_timeout: float = 60.0,
                 expected_exception: type = Exception):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self._state = ConnectionState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._lock = threading.RLock()
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        if not self._can_execute():
            raise Exception(f"Circuit breaker is {self._state.value}")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e
    
    def _can_execute(self) -> bool:
        with self._lock:
            if self._state == ConnectionState.CLOSED:
                return True
            
            if self._state == ConnectionState.OPEN:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = ConnectionState.HALF_OPEN
                    return True
                return False
            
            return True  # HALF_OPEN
    
    def _on_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = ConnectionState.CLOSED
    
    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            
            if self._failure_count >= self.failure_threshold:
                self._state = ConnectionState.OPEN

class ConnectionPool:
    """Connection pool for managing ChromaDB client connections."""
    
    def __init__(self, 
                 max_connections: int = 20,
                 max_idle_time: float = 300.0,
                 health_check_interval: float = 30.0):
        self.max_connections = max_connections
        self.max_idle_time = max_idle_time
        self.health_check_interval = health_check_interval
        
        self._connections: List[Dict] = []
        self._lock = threading.RLock()
        self._metrics = ConnectionMetrics()
        
        # Start health check thread
        self._health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_check_thread.start()
    
    def get_connection(self, host: str, port: int) -> chromadb.HttpClient:
        """Get a connection from the pool or create a new one."""
        with self._lock:
            # Try to find an available connection
            for conn in self._connections:
                if (conn['host'] == host and 
                    conn['port'] == port and 
                    not conn['in_use'] and
                    time.time() - conn['last_used'] < self.max_idle_time):
                    conn['in_use'] = True
                    conn['last_used'] = time.time()
                    return conn['client']
            
            # Create new connection if pool not full
            if len(self._connections) < self.max_connections:
                client = chromadb.HttpClient(host=host, port=port)
                conn = {
                    'client': client,
                    'host': host,
                    'port': port,
                    'in_use': True,
                    'created': time.time(),
                    'last_used': time.time()
                }
                self._connections.append(conn)
                return client
            
            # Wait for a connection to become available
            raise Exception("Connection pool exhausted")
    
    def release_connection(self, client: chromadb.HttpClient):
        """Release a connection back to the pool."""
        with self._lock:
            for conn in self._connections:
                if conn['client'] == client:
                    conn['in_use'] = False
                    conn['last_used'] = time.time()
                    break
    
    def _health_check_loop(self):
        """Background thread for health checking connections."""
        while True:
            time.sleep(self.health_check_interval)
            self._health_check_connections()
    
    def _health_check_connections(self):
        """Check health of all connections in the pool."""
        with self._lock:
            for conn in self._connections[:]:  # Copy list to avoid modification during iteration
                try:
                    if not conn['in_use']:
                        conn['client'].heartbeat()
                except Exception as e:
                    logger.warning(f"Removing unhealthy connection: {e}")
                    self._connections.remove(conn)

class ProductionRAGManager:
    """
    Production-ready RAG Manager with enterprise features:
    - Connection pooling for high concurrency
    - Circuit breaker pattern for fault tolerance
    - Comprehensive monitoring and metrics
    - Automatic failover and recovery
    """
    
    def __init__(self,
                 chroma_host: str = "localhost",
                 chroma_port: int = 8000,
                 max_connections: int = 20,
                 circuit_breaker_threshold: int = 5,
                 circuit_breaker_timeout: float = 60.0,
                 retry_attempts: int = 3,
                 retry_backoff_factor: float = 2.0,
                 enable_monitoring: bool = True):
        
        self.chroma_host = chroma_host
        self.chroma_port = chroma_port
        self.retry_attempts = retry_attempts
        self.retry_backoff_factor = retry_backoff_factor
        self.enable_monitoring = enable_monitoring
        
        # Initialize components
        self.connection_pool = ConnectionPool(max_connections=max_connections)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_breaker_threshold,
            recovery_timeout=circuit_breaker_timeout
        )
        
        # Initialize LangChain components
        self.embedding_fn = OpenAIEmbeddings(model="text-embedding-3-small")
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=3000,
            chunk_overlap=400,
        )
        
        # Internal state
        self._vector_stores: Dict[str, Chroma] = {}
        self._lock = threading.RLock()
        self._metrics = ConnectionMetrics()
        
        # CloudWatch monitoring
        if enable_monitoring:
            self.cloudwatch = boto3.client('cloudwatch')
            self._setup_monitoring()
        
        logger.info(f"Production RAG Manager initialized for {chroma_host}:{chroma_port}")
    
    def _setup_monitoring(self):
        """Setup CloudWatch metrics and monitoring."""
        self.namespace = "ChromaDB/RAGManager"
        self.dimensions = [
            {'Name': 'Host', 'Value': self.chroma_host},
            {'Name': 'Port', 'Value': str(self.chroma_port)}
        ]
    
    @contextmanager
    def _get_connection(self):
        """Context manager for getting and releasing connections."""
        client = None
        try:
            client = self.connection_pool.get_connection(self.chroma_host, self.chroma_port)
            yield client
        finally:
            if client:
                self.connection_pool.release_connection(client)
    
    def _retry_with_backoff(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with exponential backoff retry logic."""
        last_exception = None
        
        for attempt in range(self.retry_attempts):
            try:
                start_time = time.time()
                result = self.circuit_breaker.call(func, *args, **kwargs)
                
                # Record metrics
                response_time = time.time() - start_time
                self._record_success(response_time)
                
                return result
                
            except Exception as e:
                last_exception = e
                self._record_failure()
                
                if attempt < self.retry_attempts - 1:
                    backoff_time = self.retry_backoff_factor ** attempt
                    logger.warning(f"Attempt {attempt + 1} failed, retrying in {backoff_time}s: {e}")
                    time.sleep(backoff_time)
        
        raise last_exception
    
    def _record_success(self, response_time: float):
        """Record successful operation metrics."""
        self._metrics.total_requests += 1
        self._metrics.successful_requests += 1
        self._metrics.total_response_time += response_time
        self._metrics.response_times.append(response_time)
        self._metrics.last_success = time.time()
        
        # Keep only last 1000 response times for memory efficiency
        if len(self._metrics.response_times) > 1000:
            self._metrics.response_times = self._metrics.response_times[-1000:]
        
        # Send metrics to CloudWatch
        if self.enable_monitoring:
            self._send_metrics()
    
    def _record_failure(self):
        """Record failed operation metrics."""
        self._metrics.total_requests += 1
        self._metrics.failed_requests += 1
        self._metrics.last_failure = time.time()
        
        if self.enable_monitoring:
            self._send_metrics()
    
    def _send_metrics(self):
        """Send metrics to CloudWatch."""
        try:
            metrics = [
                {
                    'MetricName': 'TotalRequests',
                    'Value': self._metrics.total_requests,
                    'Unit': 'Count',
                    'Dimensions': self.dimensions
                },
                {
                    'MetricName': 'SuccessRate',
                    'Value': self._metrics.success_rate * 100,
                    'Unit': 'Percent',
                    'Dimensions': self.dimensions
                },
                {
                    'MetricName': 'AverageResponseTime',
                    'Value': self._metrics.average_response_time * 1000,  # Convert to ms
                    'Unit': 'Milliseconds',
                    'Dimensions': self.dimensions
                },
                {
                    'MetricName': 'P95ResponseTime',
                    'Value': self._metrics.p95_response_time * 1000,  # Convert to ms
                    'Unit': 'Milliseconds',
                    'Dimensions': self.dimensions
                }
            ]
            
            self.cloudwatch.put_metric_data(
                Namespace=self.namespace,
                MetricData=metrics
            )
        except Exception as e:
            logger.error(f"Failed to send metrics to CloudWatch: {e}")
    
    def health_check(self) -> Dict[str, Any]:
        """Comprehensive health check of the ChromaDB service."""
        try:
            with self._get_connection() as client:
                start_time = time.time()
                client.heartbeat()
                response_time = time.time() - start_time
                
                # Get collections info
                collections = client.list_collections()
                
                return {
                    'status': 'healthy',
                    'response_time_ms': response_time * 1000,
                    'collections_count': len(collections),
                    'circuit_breaker_state': self.circuit_breaker._state.value,
                    'connection_pool_size': len(self.connection_pool._connections),
                    'metrics': {
                        'success_rate': self._metrics.success_rate,
                        'average_response_time_ms': self._metrics.average_response_time * 1000,
                        'p95_response_time_ms': self._metrics.p95_response_time * 1000
                    }
                }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e),
                'circuit_breaker_state': self.circuit_breaker._state.value
            }
    
    def get_vector_store(self, store_label: str) -> Optional[Chroma]:
        """Get or create a vector store with production-ready error handling."""
        with self._lock:
            if store_label in self._vector_stores:
                return self._vector_stores[store_label]
            
            try:
                def _create_store():
                    with self._get_connection() as client:
                        return Chroma(
                            client=client,
                            collection_name=store_label,
                            embedding_function=self.embedding_fn
                        )
                
                store = self._retry_with_backoff(_create_store)
                self._vector_stores[store_label] = store
                return store
                
            except Exception as e:
                logger.error(f"Failed to create vector store '{store_label}': {e}")
                return None
    
    def add_documents(self, 
                     documents: List[Document], 
                     store_label: str,
                     ids: Optional[List[str]] = None) -> bool:
        """Add documents to the vector store with production error handling."""
        try:
            def _add_docs():
                store = self.get_vector_store(store_label)
                if not store:
                    raise Exception(f"Failed to get vector store '{store_label}'")
                
                if ids:
                    store.add_documents(documents, ids=ids)
                else:
                    store.add_documents(documents)
            
            self._retry_with_backoff(_add_docs)
            logger.info(f"Successfully added {len(documents)} documents to '{store_label}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add documents to '{store_label}': {e}")
            return False
    
    def similarity_search(self,
                         query: str,
                         store_label: str,
                         k: int = 4,
                         filter: Optional[Dict] = None) -> List[Document]:
        """Perform similarity search with production error handling."""
        try:
            def _search():
                store = self.get_vector_store(store_label)
                if not store:
                    raise Exception(f"Failed to get vector store '{store_label}'")
                
                return store.similarity_search(query, k=k, filter=filter)
            
            results = self._retry_with_backoff(_search)
            logger.info(f"Successfully performed similarity search in '{store_label}'")
            return results
            
        except Exception as e:
            logger.error(f"Failed to perform similarity search in '{store_label}': {e}")
            return []
    
    def get_collection_info(self, store_label: str) -> Dict[str, Any]:
        """Get information about a collection."""
        try:
            def _get_info():
                with self._get_connection() as client:
                    collection = client.get_collection(store_label)
                    return {
                        'name': collection.name,
                        'count': collection.count(),
                        'metadata': collection.metadata
                    }
            
            return self._retry_with_backoff(_get_info)
            
        except Exception as e:
            logger.error(f"Failed to get collection info for '{store_label}': {e}")
            return {}
    
    def delete_collection(self, store_label: str) -> bool:
        """Delete a collection with production error handling."""
        try:
            def _delete():
                with self._get_connection() as client:
                    client.delete_collection(store_label)
            
            self._retry_with_backoff(_delete)
            
            # Remove from local cache
            with self._lock:
                if store_label in self._vector_stores:
                    del self._vector_stores[store_label]
            
            logger.info(f"Successfully deleted collection '{store_label}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete collection '{store_label}': {e}")
            return False
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get comprehensive performance metrics."""
        return {
            'connection_pool': {
                'total_connections': len(self.connection_pool._connections),
                'active_connections': sum(1 for c in self.connection_pool._connections if c['in_use']),
                'idle_connections': sum(1 for c in self.connection_pool._connections if not c['in_use'])
            },
            'circuit_breaker': {
                'state': self.circuit_breaker._state.value,
                'failure_count': self.circuit_breaker._failure_count
            },
            'performance': {
                'total_requests': self._metrics.total_requests,
                'success_rate': self._metrics.success_rate,
                'average_response_time_ms': self._metrics.average_response_time * 1000,
                'p95_response_time_ms': self._metrics.p95_response_time * 1000,
                'last_success': self._metrics.last_success,
                'last_failure': self._metrics.last_failure
            },
            'vector_stores': {
                'cached_stores': len(self._vector_stores)
            }
        }
    
    def cleanup(self):
        """Cleanup resources."""
        logger.info("Cleaning up RAG Manager resources...")
        
        # Clear vector stores cache
        with self._lock:
            self._vector_stores.clear()
        
        # Clear connection pool
        with self.connection_pool._lock:
            self.connection_pool._connections.clear()

# Example usage and testing
if __name__ == "__main__":
    # Initialize production RAG manager
    rag_manager = ProductionRAGManager(
        chroma_host="your-alb-dns-name.us-east-1.elb.amazonaws.com",
        chroma_port=80,
        max_connections=20,
        circuit_breaker_threshold=5,
        circuit_breaker_timeout=60.0,
        retry_attempts=3,
        enable_monitoring=True
    )
    
    # Health check
    health = rag_manager.health_check()
    print(f"Health check: {health}")
    
    # Performance metrics
    metrics = rag_manager.get_performance_metrics()
    print(f"Performance metrics: {json.dumps(metrics, indent=2)}")
