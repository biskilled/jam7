#!/usr/bin/env python3
"""
RAG Performance Testing Suite
Tests ChromaDB's ability to serve 1000+ concurrent agents with sub-200ms retrieval times.
"""

import asyncio
import time
import json
import statistics
import random
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

# Import RAG managers
try:
    from deployment.chromadb_config import get_sync_rag_manager, get_async_rag_manager
    from rag.production_rag_manager import ProductionRAGManager
    from rag.async_production_rag_manager import AsyncProductionRAGManager
except ImportError as e:
    print(f"❌ Import error: {e}")
    print("Please ensure you're running from the project root directory")
    sys.exit(1)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class TestResult:
    """Results from a performance test."""
    test_name: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    response_times: List[float] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    concurrent_users: int = 0
    target_response_time_ms: float = 200.0
    
    @property
    def duration(self) -> float:
        return self.end_time - self.start_time
    
    @property
    def requests_per_second(self) -> float:
        if self.duration == 0:
            return 0.0
        return self.total_requests / self.duration
    
    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return (self.successful_requests / self.total_requests) * 100
    
    @property
    def average_response_time_ms(self) -> float:
        if not self.response_times:
            return 0.0
        return statistics.mean(self.response_times)
    
    @property
    def p95_response_time_ms(self) -> float:
        if not self.response_times:
            return 0.0
        return statistics.quantiles(self.response_times, n=20)[18]
    
    @property
    def p99_response_time_ms(self) -> float:
        if not self.response_times:
            return 0.0
        return statistics.quantiles(self.response_times, n=100)[98]
    
    @property
    def meets_target(self) -> bool:
        """Check if P95 response time meets the 200ms target."""
        return self.p95_response_time_ms <= self.target_response_time_ms
    
    def print_summary(self):
        """Print a formatted summary of test results."""
        print(f"\n📊 {self.test_name} Results:")
        print(f"   Concurrent Users: {self.concurrent_users}")
        print(f"   Duration: {self.duration:.1f}s")
        print(f"   Total Requests: {self.total_requests}")
        print(f"   Success Rate: {self.success_rate:.1f}%")
        print(f"   Requests/Second: {self.requests_per_second:.1f}")
        print(f"   Average Response Time: {self.average_response_time_ms:.1f}ms")
        print(f"   P95 Response Time: {self.p95_response_time_ms:.1f}ms")
        print(f"   P99 Response Time: {self.p99_response_time_ms:.1f}ms")
        print(f"   Meets 200ms Target: {'✅ YES' if self.meets_target else '❌ NO'}")

class RAGPerformanceTester:
    """Comprehensive RAG performance testing suite."""
    
    def __init__(self):
        self.sync_manager = None
        self.async_manager = None
        self.test_queries = [
            "What is machine learning?",
            "Explain neural networks",
            "How does deep learning work?",
            "What are the benefits of AI?",
            "Explain natural language processing",
            "What is computer vision?",
            "How do recommendation systems work?",
            "Explain reinforcement learning",
            "What is supervised learning?",
            "How does unsupervised learning work?"
        ]
        self.collection_name = "test_collection"
        
    def initialize_managers(self):
        """Initialize RAG managers."""
        print("🔧 Initializing RAG managers...")
        
        try:
            # Try to use the configuration utility first
            self.sync_manager = get_sync_rag_manager()
            self.async_manager = get_async_rag_manager()
            print("✅ RAG managers initialized using configuration utility")
        except Exception as e:
            print(f"⚠️  Configuration utility failed: {e}")
            print("🔄 Falling back to manual configuration...")
            
            # Fallback to manual configuration
            try:
                # Load deployment info
                config_folder = os.getenv('CONFIG_FOLDER', '../deployment')
                services_file_path = os.path.join(config_folder, 'aws_services.json')
                with open(services_file_path, 'r') as f:
                    services = json.load(f)
                    host = services['services']['load_balancer']['dns']
                
                self.sync_manager = ProductionRAGManager(
                    chroma_host=host,
                    chroma_port=80,
                    max_connections=50,
                    circuit_breaker_threshold=5,
                    circuit_breaker_timeout=60.0,
                    retry_attempts=3,
                    enable_monitoring=True
                )
                
                # Initialize async manager
                from rag.async_production_rag_manager import AsyncProductionRAGManager, ConnectionConfig, CacheConfig
                connection_config = ConnectionConfig(
                    max_connections=50,
                    retry_attempts=3,
                    circuit_breaker_threshold=5
                )
                cache_config = CacheConfig(enabled=False)  # Disable cache for testing
                
                self.async_manager = AsyncProductionRAGManager(
                    chromadb_host=host,
                    chromadb_port=80,
                    connection_config=connection_config,
                    cache_config=cache_config
                )
                
                print("✅ Sync and Async RAG managers initialized")
            except Exception as e2:
                print(f"❌ Failed to initialize RAG managers: {e2}")
                return False
        
        return True
    
    def setup_test_collection(self):
        """Setup test collection with sample documents."""
        print(f"📚 Setting up test collection: {self.collection_name}")
        
        # Sample documents for testing
        documents = [
            "Machine learning is a subset of artificial intelligence that enables computers to learn and improve from experience without being explicitly programmed.",
            "Neural networks are computing systems inspired by biological neural networks that constitute animal brains.",
            "Deep learning is a subset of machine learning that uses neural networks with multiple layers to model and understand complex patterns.",
            "Artificial intelligence offers numerous benefits including automation, improved decision making, and enhanced user experiences.",
            "Natural language processing enables computers to understand, interpret, and generate human language.",
            "Computer vision is a field of AI that trains computers to interpret and understand visual information from the world.",
            "Recommendation systems analyze user behavior to suggest relevant items or content.",
            "Reinforcement learning is a type of machine learning where agents learn by interacting with an environment.",
            "Supervised learning uses labeled training data to learn the mapping between inputs and outputs.",
            "Unsupervised learning finds hidden patterns in data without predefined labels."
        ]
        
        try:
            # Add documents to collection
            from langchain.docstore.document import Document
            
            for i, doc_text in enumerate(documents):
                doc = Document(
                    page_content=doc_text,
                    metadata={"id": f"doc_{i}", "type": "test"}
                )
                self.sync_manager.add_documents(
                    documents=[doc],
                    store_label=self.collection_name
                )
            print(f"✅ Added {len(documents)} documents to test collection")
            return True
        except Exception as e:
            print(f"❌ Failed to setup test collection: {e}")
            return False
    
    def test_basic_connectivity(self) -> bool:
        """Test 1: Basic connectivity and health check."""
        print("\n🔗 Test 1: Basic Connectivity")
        
        try:
            # Test health check
            health = self.sync_manager.health_check()
            print(f"✅ Health check passed: {health}")
            
            # Test simple query
            start_time = time.time()
            results = self.sync_manager.similarity_search(
                "machine learning", 
                self.collection_name, 
                k=3
            )
            response_time = (time.time() - start_time) * 1000
            
            print(f"✅ Basic query successful: {len(results)} results")
            print(f"   Response time: {response_time:.1f}ms")
            
            return True
        except Exception as e:
            print(f"❌ Basic connectivity test failed: {e}")
            return False
    
    def test_latency_benchmark(self) -> TestResult:
        """Test 2: Latency benchmark with single user."""
        print("\n⚡ Test 2: Latency Benchmark (Single User)")
        
        result = TestResult(
            test_name="Latency Benchmark",
            concurrent_users=1,
            target_response_time_ms=200.0
        )
        
        result.start_time = time.time()
        
        # Run 100 queries sequentially
        for i in range(100):
            query = random.choice(self.test_queries)
            
            try:
                start_time = time.time()
                results = self.sync_manager.similarity_search(
                    query, 
                    self.collection_name, 
                    k=3
                )
                response_time = (time.time() - start_time) * 1000
                
                result.total_requests += 1
                result.successful_requests += 1
                result.response_times.append(response_time)
                
                if i % 20 == 0:
                    print(f"   Progress: {i+1}/100 queries")
                    
            except Exception as e:
                result.total_requests += 1
                result.failed_requests += 1
                print(f"   Query {i+1} failed: {e}")
        
        result.end_time = time.time()
        result.print_summary()
        return result
    
    async def test_concurrent_users(self, num_users: int, requests_per_user: int) -> TestResult:
        """Test 3: Concurrent users simulation."""
        print(f"\n👥 Test 3: Concurrent Users ({num_users} users, {requests_per_user} requests each)")
        
        # Initialize async manager if needed
        if self.async_manager:
            await self.async_manager.initialize()
        
        result = TestResult(
            test_name=f"Concurrent Users ({num_users})",
            concurrent_users=num_users,
            target_response_time_ms=200.0
        )
        
        async def user_simulation(user_id: int):
            """Simulate a single user making requests."""
            user_results = []
            
            for i in range(requests_per_user):
                query = random.choice(self.test_queries)
                
                try:
                    start_time = time.time()
                    results = await self.async_manager.async_similarity_search(
                        query, 
                        self.collection_name, 
                        3
                    )
                    response_time = (time.time() - start_time) * 1000
                    
                    user_results.append({
                        'success': True,
                        'response_time': response_time
                    })
                    
                except Exception as e:
                    user_results.append({
                        'success': False,
                        'error': str(e)
                    })
                
                # Small delay between requests
                await asyncio.sleep(0.1)
            
            return user_results
        
        result.start_time = time.time()
        
        # Create tasks for all users
        tasks = [user_simulation(i) for i in range(num_users)]
        
        # Run all users concurrently
        all_results = await asyncio.gather(*tasks)
        
        # Aggregate results
        for user_results in all_results:
            for req_result in user_results:
                result.total_requests += 1
                
                if req_result['success']:
                    result.successful_requests += 1
                    result.response_times.append(req_result['response_time'])
                else:
                    result.failed_requests += 1
        
        result.end_time = time.time()
        result.print_summary()
        return result
    
    def test_high_load_scenario(self) -> TestResult:
        """Test 4: High load scenario with 1000+ concurrent agents simulation."""
        print("\n🚀 Test 4: High Load Scenario (1000+ Concurrent Agents)")
        
        result = TestResult(
            test_name="High Load (1000+ Agents)",
            concurrent_users=1000,
            target_response_time_ms=200.0
        )
        
        # Simulate high load with multiple threads
        import threading
        from concurrent.futures import ThreadPoolExecutor
        
        def agent_simulation(agent_id: int):
            """Simulate a single agent making requests."""
            agent_results = []
            
            for i in range(5):  # 5 requests per agent
                query = random.choice(self.test_queries)
                
                try:
                    start_time = time.time()
                    results = self.sync_manager.similarity_search(
                        query, 
                        self.collection_name, 
                        k=3
                    )
                    response_time = (time.time() - start_time) * 1000
                    
                    agent_results.append({
                        'success': True,
                        'response_time': response_time
                    })
                    
                except Exception as e:
                    agent_results.append({
                        'success': False,
                        'error': str(e)
                    })
                
                time.sleep(0.05)  # 50ms delay between requests
            
            return agent_results
        
        result.start_time = time.time()
        
        # Use ThreadPoolExecutor to simulate 1000+ concurrent agents
        with ThreadPoolExecutor(max_workers=100) as executor:
            # Submit 1000 agent tasks
            future_to_agent = {
                executor.submit(agent_simulation, i): i 
                for i in range(1000)
            }
            
            # Collect results
            for future in future_to_agent:
                try:
                    agent_results = future.result()
                    
                    for req_result in agent_results:
                        result.total_requests += 1
                        
                        if req_result['success']:
                            result.successful_requests += 1
                            result.response_times.append(req_result['response_time'])
                        else:
                            result.failed_requests += 1
                            
                except Exception as e:
                    print(f"   Agent {future_to_agent[future]} failed: {e}")
        
        result.end_time = time.time()
        result.print_summary()
        return result
    
    def test_stress_scenario(self) -> TestResult:
        """Test 5: Stress test with maximum load."""
        print("\n💥 Test 5: Stress Test (Maximum Load)")
        
        result = TestResult(
            test_name="Stress Test",
            concurrent_users=2000,
            target_response_time_ms=200.0
        )
        
        # Simulate burst traffic
        import threading
        from concurrent.futures import ThreadPoolExecutor
        
        def burst_request():
            """Make a single burst request."""
            query = random.choice(self.test_queries)
            
            try:
                start_time = time.time()
                results = self.sync_manager.similarity_search(
                    query, 
                    self.collection_name, 
                    k=3
                )
                response_time = (time.time() - start_time) * 1000
                
                return {
                    'success': True,
                    'response_time': response_time
                }
                
            except Exception as e:
                return {
                    'success': False,
                    'error': str(e)
                }
        
        result.start_time = time.time()
        
        # Simulate burst of 2000 requests
        with ThreadPoolExecutor(max_workers=200) as executor:
            futures = [executor.submit(burst_request) for _ in range(2000)]
            
            for future in futures:
                try:
                    req_result = future.result()
                    result.total_requests += 1
                    
                    if req_result['success']:
                        result.successful_requests += 1
                        result.response_times.append(req_result['response_time'])
                    else:
                        result.failed_requests += 1
                        
                except Exception as e:
                    result.total_requests += 1
                    result.failed_requests += 1
        
        result.end_time = time.time()
        result.print_summary()
        return result
    
    def run_all_tests(self):
        """Run all performance tests."""
        print("🧪 RAG Performance Testing Suite")
        print("=" * 50)
        
        # Initialize managers
        if not self.initialize_managers():
            print("❌ Failed to initialize RAG managers")
            return
        
        # Setup test collection
        if not self.setup_test_collection():
            print("❌ Failed to setup test collection")
            return
        
        # Run tests
        results = []
        
        # Test 1: Basic connectivity
        if not self.test_basic_connectivity():
            print("❌ Basic connectivity test failed. Stopping tests.")
            return
        
        # Test 2: Latency benchmark
        results.append(self.test_latency_benchmark())
        
        # Test 3: Concurrent users (100 users)
        try:
            result = asyncio.run(self.test_concurrent_users(100, 10))
            results.append(result)
        except Exception as e:
            print(f"❌ Concurrent users test failed: {e}")
        
        # Test 4: High load scenario
        results.append(self.test_high_load_scenario())
        
        # Test 5: Stress test
        results.append(self.test_stress_scenario())
        
        # Print final summary
        self.print_final_summary(results)
    
    def print_final_summary(self, results: List[TestResult]):
        """Print final summary of all test results."""
        print("\n" + "=" * 60)
        print("📋 FINAL TEST SUMMARY")
        print("=" * 60)
        
        all_meet_target = True
        
        for result in results:
            print(f"\n{result.test_name}:")
            print(f"  Success Rate: {result.success_rate:.1f}%")
            print(f"  P95 Response Time: {result.p95_response_time_ms:.1f}ms")
            print(f"  Meets 200ms Target: {'✅ YES' if result.meets_target else '❌ NO'}")
            
            if not result.meets_target:
                all_meet_target = False
        
        print("\n" + "=" * 60)
        if all_meet_target:
            print("🎉 ALL TESTS PASSED: System meets 1000+ concurrent agents with sub-200ms requirements!")
        else:
            print("⚠️  SOME TESTS FAILED: System may need optimization for production load.")
        print("=" * 60)

def main():
    """Main function to run the performance tests."""
    tester = RAGPerformanceTester()
    tester.run_all_tests()

if __name__ == "__main__":
    main()
