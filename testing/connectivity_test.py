#!/usr/bin/env python3
"""
Simple Connectivity Test for ChromaDB Production Environment
Tests basic connectivity, health checks, and service availability.
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

def load_deployment_info():
    """Load deployment information from aws_services.json."""
    try:
        config_folder = os.getenv('CONFIG_FOLDER', '../deployment')
        services_file_path = os.path.join(config_folder, 'aws_services.json')
        with open(services_file_path, 'r') as f:
            services = json.load(f)
            return {
                'load_balancer_dns': services['services']['load_balancer']['dns'],
                'cluster_name': services['services']['ecs']['cluster_name'],
                'service_name': services['services']['ecs']['service_name']
            }
    except Exception as e:
        print(f"❌ Error loading deployment info: {e}")
        return None

def run_command(command, description):
    """Run a shell command with error handling."""
    print(f"\n🔄 {description}")
    print(f"Running: {command}")
    
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Success")
            if result.stdout.strip():
                print(f"Output: {result.stdout.strip()}")
        else:
            print("❌ Failed")
            if result.stderr.strip():
                print(f"Error: {result.stderr.strip()}")
        return result.returncode == 0
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_aws_cli_connectivity():
    """Test AWS CLI connectivity and permissions."""
    print("\n🔗 Testing AWS CLI Connectivity")
    
    # Test AWS CLI configuration
    if not run_command("aws sts get-caller-identity", "Checking AWS credentials"):
        print("❌ AWS CLI not configured or credentials invalid")
        return False
    
    # Test ECS access
    if not run_command("aws ecs list-clusters", "Testing ECS access"):
        print("❌ No ECS access or no clusters found")
        return False
    
    return True

def test_load_balancer_health(deployment_info):
    """Test load balancer health and connectivity."""
    print(f"\n🌐 Testing Load Balancer: {deployment_info['load_balancer_dns']}")
    
    # Test basic connectivity
    if not run_command(f"ping -c 3 {deployment_info['load_balancer_dns']}", "Testing basic connectivity"):
        print("❌ Load balancer not reachable")
        return False
    
    # Test HTTP connectivity
    health_check_url = f"http://{deployment_info['load_balancer_dns']}/api/v1/heartbeat"
    if not run_command(f"curl -f -s {health_check_url}", "Testing ChromaDB health check"):
        print("❌ ChromaDB health check failed")
        return False
    
    return True

def test_ecs_service_status(deployment_info):
    """Test ECS service status."""
    print(f"\n🐳 Testing ECS Service: {deployment_info['service_name']}")
    
    # Check service status
    command = f"aws ecs describe-services --cluster {deployment_info['cluster_name']} --services {deployment_info['service_name']}"
    if not run_command(command, "Checking ECS service status"):
        print("❌ ECS service not found or not accessible")
        return False
    
    # Check running tasks
    command = f"aws ecs list-tasks --cluster {deployment_info['cluster_name']} --service-name {deployment_info['service_name']}"
    if not run_command(command, "Checking running tasks"):
        print("❌ No running tasks found")
        return False
    
    return True

def test_rag_manager_connectivity():
    """Test RAG manager connectivity."""
    print("\n🤖 Testing RAG Manager Connectivity")
    
    try:
        from deployment.chromadb_config import get_sync_rag_manager
        
        # Initialize RAG manager
        rag_manager = get_sync_rag_manager()
        print("✅ RAG manager initialized")
        
        # Test health check
        health = rag_manager.health_check()
        print(f"✅ Health check passed: {health}")
        
        # Test simple query
        start_time = time.time()
        results = rag_manager.similarity_search("test query", "test_collection", 1)
        response_time = (time.time() - start_time) * 1000
        
        print(f"✅ Query successful: {len(results)} results")
        print(f"   Response time: {response_time:.1f}ms")
        
        return True
        
    except Exception as e:
        print(f"❌ RAG manager test failed: {e}")
        return False

def test_cloudwatch_metrics():
    """Test CloudWatch metrics access."""
    print("\n📊 Testing CloudWatch Metrics")
    
    # Test CloudWatch access
    if not run_command("aws cloudwatch list-metrics --namespace AWS/ECS", "Testing CloudWatch access"):
        print("❌ CloudWatch access failed")
        return False
    
    return True

def main():
    """Run all connectivity tests."""
    print("🔍 ChromaDB Connectivity Test Suite")
    print("=" * 50)
    
    # Load deployment info
    deployment_info = load_deployment_info()
    if not deployment_info:
        print("❌ No deployment information found")
        print("Please ensure you have deployed the infrastructure first")
        return
    
    print(f"📋 Deployment Info:")
    print(f"   Load Balancer: {deployment_info['load_balancer_dns']}")
    print(f"   ECS Cluster: {deployment_info['cluster_name']}")
    print(f"   ECS Service: {deployment_info['service_name']}")
    
    # Run tests
    tests = [
        ("AWS CLI Connectivity", test_aws_cli_connectivity),
        ("Load Balancer Health", lambda: test_load_balancer_health(deployment_info)),
        ("ECS Service Status", lambda: test_ecs_service_status(deployment_info)),
        ("RAG Manager Connectivity", test_rag_manager_connectivity),
        ("CloudWatch Metrics", test_cloudwatch_metrics)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n{'='*20} {test_name} {'='*20}")
        if test_func():
            passed += 1
            print(f"✅ {test_name} PASSED")
        else:
            print(f"❌ {test_name} FAILED")
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"📋 CONNECTIVITY TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Tests Passed: {passed}/{total}")
    print(f"Success Rate: {(passed/total)*100:.1f}%")
    
    if passed == total:
        print("🎉 ALL TESTS PASSED: System is ready for performance testing!")
    else:
        print("⚠️  SOME TESTS FAILED: Please check the issues above before running performance tests.")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
