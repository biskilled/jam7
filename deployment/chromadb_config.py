#!/usr/bin/env python3
"""
ChromaDB Configuration Utility
Loads deployment information from aws_services.json and provides configuration
for both synchronous and asynchronous RAG managers.
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

class ChromaDBConfig:
    """Configuration manager for ChromaDB deployment."""
    
    def __init__(self, config_file: str = None):
        """
        Initialize configuration from aws_services.json file.
        
        Args:
            config_file: Path to the aws_services.json file (optional, uses CONFIG_FOLDER)
        """
        if config_file is None:
            config_folder = os.getenv('CONFIG_FOLDER', '.')
            self.config_file = os.path.join(config_folder, "aws_services.json")
        else:
            self.config_file = config_file
        self.deployment_info = self._load_deployment_info()
        
        # Default settings
        self.chroma_port = 80
        self.aws_region = "us-east-1"
        self.max_connections = 20
        self.circuit_breaker_threshold = 5
        self.circuit_breaker_timeout = 60.0
        self.retry_attempts = 3
        self.enable_monitoring = True
        
        # Cache settings for async manager
        self.cache_enabled = True
        self.cache_ttl = 300  # 5 minutes
        self.cache_max_size = 1000
    
    def _load_deployment_info(self) -> Optional[Dict[str, Any]]:
        """Load deployment information from aws_services.json."""
        config_path = Path(self.config_file)
        
        if not config_path.exists():
            print(f"⚠️  Configuration file {self.config_file} not found.")
            print("Please ensure you have deployed the infrastructure first.")
            return None
        
        try:
            with open(config_path, 'r') as f:
                services = json.load(f)
            
            # Extract key information
            deployment_info = {
                'load_balancer_dns': services['services']['load_balancer']['dns'],
                'cluster_name': services['services']['ecs']['cluster_name'],
                'service_name': services['services']['ecs']['service_name'],
                'project_name': services.get('project_name'),
                'region': services.get('region'),
                'deployment_timestamp': services.get('deployment_timestamp')
            }
            
            print(f"✅ Loaded configuration from {self.config_file}")
            return deployment_info
            
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            return None
    
    @property
    def chroma_host(self) -> str:
        """Get ChromaDB host from deployment info."""
        if self.deployment_info:
            return self.deployment_info['load_balancer_dns']
        return "localhost"  # fallback
    
    def get_sync_config(self) -> Dict[str, Any]:
        """Get configuration for synchronous RAG manager."""
        return {
            'chroma_host': self.chroma_host,
            'chroma_port': self.chroma_port,
            'max_connections': self.max_connections,
            'circuit_breaker_threshold': self.circuit_breaker_threshold,
            'circuit_breaker_timeout': self.circuit_breaker_timeout,
            'retry_attempts': self.retry_attempts,
            'enable_monitoring': self.enable_monitoring
        }
    
    def get_async_config(self) -> Dict[str, Any]:
        """Get configuration for asynchronous RAG manager."""
        return {
            'connection_config': {
                'host': self.chroma_host,
                'port': self.chroma_port,
                'max_connections': self.max_connections,
                'circuit_breaker_threshold': self.circuit_breaker_threshold,
                'circuit_breaker_timeout': self.circuit_breaker_timeout,
                'retry_attempts': self.retry_attempts,
                'enable_monitoring': self.enable_monitoring
            },
            'cache_config': {
                'enabled': self.cache_enabled,
                'ttl': self.cache_ttl,
                'max_size': self.cache_max_size
            }
        }
    
    def create_sync_manager(self):
        """Create and return a synchronous RAG manager instance."""
        try:
            from rag.production_rag_manager import ProductionRAGManager
            config = self.get_sync_config()
            return ProductionRAGManager(**config)
        except ImportError as e:
            print(f"❌ Error importing ProductionRAGManager: {e}")
            return None
    
    def create_async_manager(self):
        """Create and return an asynchronous RAG manager instance."""
        try:
            from rag.async_production_rag_manager import AsyncProductionRAGManager, ConnectionConfig, CacheConfig
            config = self.get_async_config()
            
            connection_config = ConnectionConfig(**config['connection_config'])
            cache_config = CacheConfig(**config['cache_config'])
            
            return AsyncProductionRAGManager(
                chromadb_host=self.chroma_host,
                chromadb_port=self.chroma_port,
                connection_config=connection_config,
                cache_config=cache_config
            )
        except ImportError as e:
            print(f"❌ Error importing AsyncProductionRAGManager: {e}")
            return None
    
    def print_config(self):
        """Print current configuration."""
        print("🔧 ChromaDB Configuration:")
        print(f"  Host: {self.chroma_host}")
        print(f"  Port: {self.chroma_port}")
        print(f"  Region: {self.aws_region}")
        print(f"  Max Connections: {self.max_connections}")
        print(f"  Circuit Breaker Threshold: {self.circuit_breaker_threshold}")
        print(f"  Circuit Breaker Timeout: {self.circuit_breaker_timeout}s")
        print(f"  Retry Attempts: {self.retry_attempts}")
        print(f"  Monitoring Enabled: {self.enable_monitoring}")
        print(f"  Cache Enabled: {self.cache_enabled}")
        print(f"  Cache TTL: {self.cache_ttl}s")
        print(f"  Cache Max Size: {self.cache_max_size}")

# Global configuration instance
config = ChromaDBConfig()

# Convenience functions
def get_sync_rag_manager():
    """Get a synchronous RAG manager instance."""
    return config.create_sync_manager()

def get_async_rag_manager():
    """Get an asynchronous RAG manager instance."""
    return config.create_async_manager()

def get_config():
    """Get the configuration instance."""
    return config

# Example usage
if __name__ == "__main__":
    # Print current configuration
    config.print_config()
    
    # Example: Create sync manager
    print("\n🔄 Creating synchronous RAG manager...")
    sync_manager = get_sync_rag_manager()
    if sync_manager:
        print("✅ Synchronous RAG manager created successfully")
    
    # Example: Create async manager
    print("\n🔄 Creating asynchronous RAG manager...")
    async_manager = get_async_rag_manager()
    if async_manager:
        print("✅ Asynchronous RAG manager created successfully")
    
    # Example: Using async manager
    if async_manager:
        print("\n📝 Example async usage:")
        print("""
import asyncio

async def example_search():
    manager = get_async_rag_manager()
    await manager.initialize()  # Initialize the manager first
    result = await manager.async_similarity_search(
        query="your query here",
        collection_name="your_collection",
        n_results=5
    )
    await manager.close()  # Clean up
    return result

# Run the example
result = asyncio.run(example_search())
print(result)
        """)
