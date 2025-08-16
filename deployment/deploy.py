#!/usr/bin/env python3
"""
ChromaDB AWS Deployment Script
Handles deployment and management of ChromaDB infrastructure on AWS.
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional

# Add the deployment directory to the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from aws_infrastructure import ChromaDBInfrastructure

def check_env_configuration():
    """Check if required environment variables are configured."""
    print("🔍 Checking environment configuration...")
    
    # Load .env file from project root (JAM7 folder)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = Path(os.path.join(project_root, '.env'))
    
    if not env_path.exists():
        print("❌ .env file not found!")
        print("Please create a .env file in the project root (JAM7 folder) with your AWS credentials and configuration.")
        print("Example .env file:")
        print("AWS_ACCESS_KEY_ID=your_access_key")
        print("AWS_SECRET_ACCESS_KEY=your_secret_key")
        print("AWS_REGION=us-east-1")
        print("PROJECT_NAME=chromadb-production")
        return False
    
    # Load environment variables manually with robust error handling
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    try:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        if '\0' in key or '\0' in value:
                            print(f"Warning: Skipping line {line_num} - contains null characters")
                            continue
                        if key and value is not None:
                            os.environ[key] = value
                    except Exception as e:
                        print(f"Warning: Error processing line {line_num}: {e}")
                        continue
    except Exception as e:
        print(f"Warning: Error loading .env file: {e}")
        return False
    
    # Check required variables
    required_vars = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    print("✅ Environment configuration is valid")
    return True

def check_existing_services():
    """Check if services already exist and load from aws_services.json."""
    config_folder = os.getenv('CONFIG_FOLDER', '.')
    services_file_path = os.path.join(config_folder, 'aws_services.json')
    
    if os.path.exists(services_file_path):
        try:
            with open(services_file_path, 'r') as f:
                services_data = json.load(f)
            print("📋 Found existing services configuration:")
            for service_name, service_info in services_data.get('services', {}).items():
                status = service_info.get('status', 'UNKNOWN')
                print(f"   • {service_name}: {status}")
            return services_data
        except Exception as e:
            print(f"⚠️  Error reading existing services: {e}")
    
    return None

def interactive_menu():
    """Display interactive menu for deployment actions."""
    print("\n🚀 ChromaDB AWS Deployment Manager")
    print("=" * 50)
    print("1. Deploy Infrastructure")
    print("2. Delete All Resources")
    print("3. Check Service Status")
    print("4. Fix Service Issues (Redis + ECS)")
    print("5. Diagnose ECS Issues")
    print("6. Force ECS Update")
    print("7. Fix Docker Image Issues")
    print("8. Exit")
    print("=" * 50)

    while True:
        try:
            choice = input("\nSelect an option (1-7): ").strip()
            if choice in ['1', '2', '3', '4', '5', '6', '7']:
                return choice
            else:
                print("❌ Invalid choice. Please enter 1, 2, 3, 4, 5, 6, or 7.")
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            sys.exit(0)

def deploy_infrastructure():
    """Deploy the ChromaDB infrastructure with service tracking."""
    print("\n🚀 Starting ChromaDB infrastructure deployment...")
    
    # Initialize infrastructure
    infrastructure = ChromaDBInfrastructure()
    
    # Load existing services
    existing_services = check_existing_services()
    services_data = existing_services or {
        'project_name': infrastructure.project_name,
        'region': infrastructure.region,
        'deployment_timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'services': {}
    }
    
    try:
        # Deploy infrastructure with service tracking
        deployment_result = infrastructure.deploy_with_tracking(services_data)
        
        if deployment_result['success']:
            print("\n✅ Deployment completed successfully!")
            print(f"🌐 ChromaDB endpoint: http://{deployment_result['load_balancer_dns']}")
            print(f"📊 ECS Cluster: {deployment_result['cluster_name']}")
            print(f"🔧 ECS Service: {deployment_result['service_name']}")
        else:
            print("\n⚠️  Deployment completed with some failures.")
            print("Check the aws_services.json file for details on which services failed.")
        
        return deployment_result
        
    except Exception as e:
        print(f"\n❌ Deployment failed: {str(e)}")
        return {'success': False, 'error': str(e)}

def delete_resources():
    """Delete all ChromaDB infrastructure resources."""
    print("\n🗑️  Starting resource deletion...")
    
    confirm = input("Are you sure you want to delete ALL ChromaDB infrastructure resources? This action cannot be undone. (yes/no): ")
    if confirm.lower() != 'yes':
        print("❌ Deletion cancelled.")
        return
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.delete_all_resources()
        print("✅ All resources deleted successfully!")
    except Exception as e:
        print(f"❌ Error during deletion: {str(e)}")

def check_service_status():
    """Check the status of deployed services."""
    print("\n📊 Checking service status...")
    
    existing_services = check_existing_services()
    if not existing_services:
        print("❌ No services found. Deploy infrastructure first.")
        return
    
    infrastructure = ChromaDBInfrastructure()
    
    # Check each service status
    for service_name, service_info in existing_services.get('services', {}).items():
        status = service_info.get('status', 'UNKNOWN')
        print(f"\n🔍 {service_name}: {status}")
        
        if status == 'DEPLOYED':
            # Add specific status checks here if needed
            print(f"   • Service is deployed and running")
        elif status == 'FAILED':
            error = service_info.get('error', 'Unknown error')
            print(f"   • Failed to deploy: {error}")

def fix_service_issues():
    """Fix common service issues like Redis endpoint and ECS desired count."""
    print("\n🔧 Fixing service issues...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        
        # Fix Redis endpoint issues
        print("\n📋 Step 1: Checking Redis cluster...")
        try:
            # Load existing services to get VPC info
            existing_services = check_existing_services()
            if existing_services and 'vpc' in existing_services.get('services', {}):
                vpc_info = existing_services['services']['vpc']
                if vpc_info.get('status') == 'DEPLOYED':
                    # Try to manually fix Redis endpoint detection
                    cluster_name = f'{infrastructure.project_name}-redis-cluster'
                    print(f"Checking Redis cluster: {cluster_name}")
                    
                    # Get cluster details directly
                    elasticache = infrastructure.session.client('elasticache')
                    try:
                        response = elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                        if response['CacheClusters']:
                            cluster = response['CacheClusters'][0]
                            cluster_status = cluster['CacheClusterStatus']
                            print(f"Cluster status: {cluster_status}")
                            
                            if cluster_status == 'available':
                                # Try to get endpoint from different possible locations
                                redis_endpoint = None
                                redis_port = None
                                
                                # Check for ConfigurationEndpoint (multi-node)
                                if 'ConfigurationEndpoint' in cluster:
                                    redis_endpoint = cluster['ConfigurationEndpoint']['Address']
                                    redis_port = cluster['ConfigurationEndpoint']['Port']
                                    print(f"Found multi-node endpoint: {redis_endpoint}:{redis_port}")
                                
                                # Check for CacheNodes (single-node)
                                elif 'CacheNodes' in cluster and cluster['CacheNodes']:
                                    cache_node = cluster['CacheNodes'][0]
                                    node_status = cache_node.get('CacheNodeStatus', 'UNKNOWN')
                                    print(f"Cache node status: {node_status}")
                                    
                                    if node_status == 'available' and 'Endpoint' in cache_node:
                                        redis_endpoint = cache_node['Endpoint']['Address']
                                        redis_port = cache_node['Endpoint']['Port']
                                        print(f"Found single-node endpoint: {redis_endpoint}:{redis_port}")
                                    else:
                                        print(f"Cache node not ready, waiting...")
                                        # Wait for cache node to become available
                                        import time
                                        max_attempts = 30
                                        for attempt in range(max_attempts):
                                            time.sleep(10)
                                            updated_response = elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                                            if updated_response['CacheClusters']:
                                                updated_cluster = updated_response['CacheClusters'][0]
                                                if 'CacheNodes' in updated_cluster and updated_cluster['CacheNodes']:
                                                    updated_node = updated_cluster['CacheNodes'][0]
                                                    if updated_node['CacheNodeStatus'] == 'available' and 'Endpoint' in updated_node:
                                                        redis_endpoint = updated_node['Endpoint']['Address']
                                                        redis_port = updated_node['Endpoint']['Port']
                                                        print(f"Cache node now available: {redis_endpoint}:{redis_port}")
                                                        break
                                                    else:
                                                        print(f"Cache node still not ready: {updated_node.get('CacheNodeStatus', 'UNKNOWN')} (attempt {attempt + 1}/{max_attempts})")
                                                else:
                                                    print("No cache nodes found in updated response")
                                            else:
                                                print("No cluster found in updated response")
                                        else:
                                            print("Cache node did not become available after waiting")
                                
                                if redis_endpoint and redis_port:
                                    print(f"✅ Redis endpoint found: {redis_endpoint}:{redis_port}")
                                    # Update the services data with the working endpoint
                                    if 'redis' in existing_services.get('services', {}):
                                        existing_services['services']['redis'].update({
                                            'endpoint': redis_endpoint,
                                            'port': redis_port,
                                            'status': 'DEPLOYED',
                                            'fixed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                                        })
                                        # Save updated services data
                                        config_folder = os.getenv('CONFIG_FOLDER', '.')
                                        services_file_path = os.path.join(config_folder, 'aws_services.json')
                                        with open(services_file_path, 'w') as f:
                                            json.dump(existing_services, f, indent=2)
                                        print("✅ Redis endpoint updated in services file")
                                else:
                                    print("❌ Could not find Redis endpoint")
                            elif cluster_status == 'creating':
                                print("Redis cluster is still being created, waiting...")
                                # Wait for cluster to become available
                                waiter = elasticache.get_waiter('cache_cluster_available')
                                waiter.wait(CacheClusterId=cluster_name)
                                print("Redis cluster is now available, retrying endpoint detection...")
                                # Recursively call this function to retry
                                return fix_service_issues()
                            else:
                                print(f"Redis cluster is in unexpected status: {cluster_status}")
                        else:
                            print("No Redis cluster found")
                    except Exception as cluster_error:
                        print(f"Error checking Redis cluster: {cluster_error}")
                        # Try to recreate Redis with better endpoint detection
                        redis_config = infrastructure.create_elasticache_redis(
                            vpc_info['vpc_id'],
                            vpc_info['security_group_id'],
                            [vpc_info['private_subnet_1'], vpc_info['private_subnet_2']]
                        )
                        if redis_config:
                            print("✅ Redis cluster recreated successfully")
                        else:
                            print("⚠️  Redis cluster recreation failed")
                else:
                    print("❌ VPC not deployed, cannot fix Redis")
            else:
                print("❌ No VPC information found, cannot fix Redis")
        except Exception as e:
            print(f"⚠️  Error fixing Redis: {e}")
        
        # Fix ECS service desired count
        print("\n📋 Step 2: Checking ECS service desired count...")
        try:
            infrastructure.fix_service_desired_count()
            print("✅ ECS service desired count check completed")
        except Exception as e:
            print(f"⚠️  Error fixing ECS service: {e}")
        
        print("\n✅ Service issue fixes completed!")
        
    except Exception as e:
        print(f"❌ Error during service fixes: {e}")

def diagnose_ecs_issues():
    """Diagnose ECS service issues in detail."""
    print("\n🔍 Diagnosing ECS service issues...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.diagnose_ecs_issues()
        print("\n✅ ECS diagnosis completed!")
        
    except Exception as e:
        print(f"❌ Error during ECS diagnosis: {e}")

def force_ecs_update():
    """Force update ECS service to restart tasks."""
    print("\n🔄 Force updating ECS service...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.force_service_update()
        print("\n✅ ECS force update completed!")
        
    except Exception as e:
        print(f"❌ Error during ECS force update: {e}")

def fix_docker_image_issues():
    """Fix Docker image pull issues."""
    print("\n🐳 Fixing Docker image pull issues...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.fix_docker_image_issues()
        print("\n✅ Docker image fix completed!")
        
    except Exception as e:
        print(f"❌ Error fixing Docker image issues: {e}")

def main():
    """Main deployment function."""
    print("🔧 ChromaDB AWS Infrastructure Deployment")
    print("=" * 60)
    
    # Check environment configuration
    if not check_env_configuration():
        print("\n❌ Environment configuration check failed.")
        print("Please fix the issues above and try again.")
        return
    
    # Check prerequisites
    print("\n🔍 Checking prerequisites...")
    try:
        import boto3
        print("✅ boto3 is available")
    except ImportError:
        print("❌ boto3 is not installed. Please install it with: pip install boto3")
        return
    
    # Interactive menu
    while True:
        choice = interactive_menu()
        
        if choice == '1':
            deploy_infrastructure()
        elif choice == '2':
            delete_resources()
        elif choice == '3':
            check_service_status()
        elif choice == '4':
            fix_service_issues()
        elif choice == '5':
            diagnose_ecs_issues()
        elif choice == '6':
            force_ecs_update()
        elif choice == '7':
            fix_docker_image_issues()
        elif choice == '8':
            print("\n👋 Goodbye!")
            break

        if choice in ['1', '2', '3', '4', '5', '6', '7']:
            input("\nPress Enter to continue...")

def diagnose_ecs_issues():
    """Diagnose ECS service issues in detail."""
    print("\n🔍 Diagnosing ECS service issues...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.diagnose_ecs_issues()
        print("\n✅ ECS diagnosis completed!")
        
    except Exception as e:
        print(f"❌ Error during ECS diagnosis: {e}")

def force_ecs_update():
    """Force update ECS service to restart tasks."""
    print("\n🔄 Force updating ECS service...")
    
    try:
        infrastructure = ChromaDBInfrastructure()
        infrastructure.force_service_update()
        print("\n✅ ECS force update completed!")
        
    except Exception as e:
        print(f"❌ Error during ECS force update: {e}")

if __name__ == "__main__":
    main()
