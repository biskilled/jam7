#!/usr/bin/env python3
"""
AWS Infrastructure Deployment for ChromaDB Production Environment
Streamlined version - deploys ChromaDB on ECS Fargate with auto-scaling and monitoring.
"""

import boto3
import json
import time
import logging
import os
from typing import Dict, Any
from botocore.exceptions import ClientError

# Load environment variables
def load_env_file():
    """Load environment variables from .env file."""
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file_path = os.path.join(project_root, '.env')
        with open(env_file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    try:
                        key, value = line.split('=', 1)
                        key, value = key.strip(), value.strip()
                        
                        # Remove quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        # Skip null characters
                        if '\0' in key or '\0' in value:
                            print(f"Warning: Skipping line {line_num} - contains null characters")
                            continue
                        
                        if key and value is not None:
                            os.environ[key] = value
                    except Exception as e:
                        print(f"Warning: Error processing line {line_num}: {e}")
                        continue
    except FileNotFoundError:
        print("Warning: .env file not found. Using default environment variables.")
    except Exception as e:
        print(f"Warning: Error loading .env file: {e}")

load_env_file()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ChromaDBInfrastructure:
    def __init__(self, region: str = None):
        self.region = region or os.getenv('AWS_REGION', 'us-east-1')
        
        # Initialize AWS session
        self.session = boto3.Session(
            region_name=self.region,
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            aws_session_token=os.getenv('AWS_SESSION_TOKEN')
        )
        
        # Initialize AWS clients
        self.ecs = self.session.client('ecs')
        self.ec2 = self.session.client('ec2')
        self.elbv2 = self.session.client('elbv2')
        self.efs = self.session.client('efs')
        self.cloudwatch = self.session.client('cloudwatch')
        self.iam = self.session.client('iam')
        self.logs = self.session.client('logs')
        self.elasticache = self.session.client('elasticache')
        
        # Configuration
        self.project_name = os.getenv('PROJECT_NAME', 'chromadb-production')
        self.cluster_name = f"{self.project_name}-cluster"
        self.service_name = f"{self.project_name}-service"
        self.task_family = f"{self.project_name}-task"
        self.load_balancer_name = f"{self.project_name}-alb"
        self.target_group_name = f"{self.project_name}-tg"
        
    def create_vpc_and_networking(self) -> Dict[str, str]:
        """Create VPC, subnets, and security groups."""
        logger.info("Creating VPC and networking...")
        
        # Create VPC
        vpc_response = self.ec2.create_vpc(
            CidrBlock='10.0.0.0/16',
            TagSpecifications=[{
                'ResourceType': 'vpc',
                'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-vpc'}]
            }]
        )
        vpc_id = vpc_response['Vpc']['VpcId']
        
        # Enable DNS
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})
        
        # Create Internet Gateway
        igw_response = self.ec2.create_internet_gateway()
        igw_id = igw_response['InternetGateway']['InternetGatewayId']
        self.ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)
        
        # Create subnets
        public_subnet_1 = self.ec2.create_subnet(
            VpcId=vpc_id, CidrBlock='10.0.1.0/24', AvailabilityZone=f'{self.region}a',
            TagSpecifications=[{'ResourceType': 'subnet', 'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-public-1'}]}]
        )
        public_subnet_2 = self.ec2.create_subnet(
            VpcId=vpc_id, CidrBlock='10.0.2.0/24', AvailabilityZone=f'{self.region}b',
            TagSpecifications=[{'ResourceType': 'subnet', 'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-public-2'}]}]
        )
        private_subnet_1 = self.ec2.create_subnet(
            VpcId=vpc_id, CidrBlock='10.0.3.0/24', AvailabilityZone=f'{self.region}a',
            TagSpecifications=[{'ResourceType': 'subnet', 'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-private-1'}]}]
        )
        private_subnet_2 = self.ec2.create_subnet(
            VpcId=vpc_id, CidrBlock='10.0.4.0/24', AvailabilityZone=f'{self.region}b',
            TagSpecifications=[{'ResourceType': 'subnet', 'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-private-2'}]}]
        )
        
        # Create NAT Gateway for private subnets
        # Allocate Elastic IP for NAT Gateway
        eip_response = self.ec2.allocate_address(Domain='vpc')
        eip_allocation_id = eip_response['AllocationId']
        
        # Create NAT Gateway
        nat_gateway = self.ec2.create_nat_gateway(
            SubnetId=public_subnet_1['Subnet']['SubnetId'],
            AllocationId=eip_allocation_id,
            TagSpecifications=[{
                'ResourceType': 'natgateway',
                'Tags': [{'Key': 'Name', 'Value': f'{self.project_name}-nat-gateway'}]
            }]
        )
        nat_gateway_id = nat_gateway['NatGateway']['NatGatewayId']
        
        # Wait for NAT Gateway to be available
        logger.info("Waiting for NAT Gateway to be available...")
        waiter = self.ec2.get_waiter('nat_gateway_available')
        waiter.wait(NatGatewayIds=[nat_gateway_id])
        
        # Create route table for public subnets
        public_route_table = self.ec2.create_route_table(VpcId=vpc_id)
        self.ec2.create_route(
            RouteTableId=public_route_table['RouteTable']['RouteTableId'],
            DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id
        )
        
        # Associate public subnets with public route table
        self.ec2.associate_route_table(
            RouteTableId=public_route_table['RouteTable']['RouteTableId'],
            SubnetId=public_subnet_1['Subnet']['SubnetId']
        )
        self.ec2.associate_route_table(
            RouteTableId=public_route_table['RouteTable']['RouteTableId'],
            SubnetId=public_subnet_2['Subnet']['SubnetId']
        )
        
        # Create route table for private subnets
        private_route_table = self.ec2.create_route_table(VpcId=vpc_id)
        self.ec2.create_route(
            RouteTableId=private_route_table['RouteTable']['RouteTableId'],
            DestinationCidrBlock='0.0.0.0/0', NatGatewayId=nat_gateway_id
        )
        
        # Associate private subnets with private route table
        self.ec2.associate_route_table(
            RouteTableId=private_route_table['RouteTable']['RouteTableId'],
            SubnetId=private_subnet_1['Subnet']['SubnetId']
        )
        self.ec2.associate_route_table(
            RouteTableId=private_route_table['RouteTable']['RouteTableId'],
            SubnetId=private_subnet_2['Subnet']['SubnetId']
        )
        
        # Create security group
        security_group = self.ec2.create_security_group(
            GroupName=f'{self.project_name}-chromadb-sg',
            Description='Security group for ChromaDB service',
            VpcId=vpc_id
        )
        
        self.ec2.authorize_security_group_ingress(
            GroupId=security_group['GroupId'],
            IpPermissions=[
                {'IpProtocol': 'tcp', 'FromPort': 8000, 'ToPort': 8000, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
            ]
        )
        
        return {
            'vpc_id': vpc_id,
            'public_subnet_1': public_subnet_1['Subnet']['SubnetId'],
            'public_subnet_2': public_subnet_2['Subnet']['SubnetId'],
            'private_subnet_1': private_subnet_1['Subnet']['SubnetId'],
            'private_subnet_2': private_subnet_2['Subnet']['SubnetId'],
            'security_group_id': security_group['GroupId'],
            'nat_gateway_id': nat_gateway_id,
            'eip_allocation_id': eip_allocation_id
        }
    
    def create_efs_storage(self, vpc_id: str, security_group_id: str, private_subnet_ids: list) -> Dict[str, str]:
        """Create EFS file system for persistent storage."""
        logger.info("Creating EFS storage...")
        
        # Create EFS file system
        efs_response = self.efs.create_file_system(
            PerformanceMode='generalPurpose',
            ThroughputMode='provisioned',
            ProvisionedThroughputInMibps=100,
            Encrypted=True,
            Tags=[
                {'Key': 'Name', 'Value': f'{self.project_name}-chromadb-storage'},
                {'Key': 'Purpose', 'Value': 'ChromaDB persistent storage'}
            ]
        )
        file_system_id = efs_response['FileSystemId']
        
        # Wait for EFS to be available
        try:
            waiter = self.efs.get_waiter('file_system_available')
            waiter.wait(FileSystemId=file_system_id)
        except Exception:
            # Fallback polling
            max_attempts = 30
            for attempt in range(max_attempts):
                response = self.efs.describe_file_systems(FileSystemId=file_system_id)
                if response['FileSystems'][0]['LifeCycleState'] == 'available':
                    break
                time.sleep(10)
        
        # Create mount targets
        mount_targets = []
        for subnet_id in private_subnet_ids:
            mount_target = self.efs.create_mount_target(
                FileSystemId=file_system_id,
                SubnetId=subnet_id,
                SecurityGroups=[security_group_id]
            )
            mount_targets.append(mount_target['MountTargetId'])
        
        # Wait for mount targets
        for mount_target_id in mount_targets:
            try:
                waiter = self.efs.get_waiter('mount_target_available')
                waiter.wait(MountTargetId=mount_target_id)
            except Exception:
                # Fallback polling
                max_attempts = 30
                for attempt in range(max_attempts):
                    response = self.efs.describe_mount_targets(MountTargetId=mount_target_id)
                    if response['MountTargets'][0]['LifeCycleState'] == 'available':
                        break
                    time.sleep(10)
        
        return {
            'file_system_id': file_system_id,
            'mount_targets': mount_targets
        }
    
    def create_elasticache_redis(self, vpc_id: str, security_group_id: str, private_subnets: list) -> Dict[str, str]:
        """Create ElastiCache Redis cluster for caching."""
        logger.info("Creating Redis cluster...")
        
        if os.getenv('ENABLE_REDIS_CACHE', 'true').lower() != 'true':
            logger.info("Redis caching disabled")
            return {}
        
        # Create subnet group
        subnet_group_name = f'{self.project_name}-redis-subnet-group'
        try:
            self.elasticache.create_cache_subnet_group(
                CacheSubnetGroupName=subnet_group_name,
                CacheSubnetGroupDescription=f'Subnet group for {self.project_name} Redis cluster',
                SubnetIds=private_subnets
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'CacheSubnetGroupAlreadyExists':
                raise
        
        # Create parameter group
        parameter_group_name = f'{self.project_name}-redis-params'
        try:
            self.elasticache.create_cache_parameter_group(
                CacheParameterGroupFamily='redis7',
                CacheParameterGroupName=parameter_group_name,
                Description=f'Parameter group for {self.project_name} Redis cluster'
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'CacheParameterGroupAlreadyExists':
                raise
        
        # Create Redis cluster
        cluster_name = f'{self.project_name}-redis-cluster'
        node_type = os.getenv('REDIS_NODE_TYPE', 'cache.t3.micro')
        num_cache_nodes = int(os.getenv('REDIS_NUM_NODES', '1'))
        
        # Check if cluster exists
        try:
            existing_response = self.elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
            if existing_response['CacheClusters']:
                existing_cluster = existing_response['CacheClusters'][0]
                cluster_status = existing_cluster['CacheClusterStatus']
                
                if cluster_status == 'available':
                    # Get endpoint
                    redis_endpoint = None
                    redis_port = None
                    
                    if 'ConfigurationEndpoint' in existing_cluster:
                        redis_endpoint = existing_cluster['ConfigurationEndpoint']['Address']
                        redis_port = existing_cluster['ConfigurationEndpoint']['Port']
                    elif 'CacheNodes' in existing_cluster and existing_cluster['CacheNodes']:
                        cache_node = existing_cluster['CacheNodes'][0]
                        if cache_node['CacheNodeStatus'] == 'available' and 'Endpoint' in cache_node:
                            redis_endpoint = cache_node['Endpoint']['Address']
                            redis_port = cache_node['Endpoint']['Port']
                        else:
                            # Wait for cache node
                            max_attempts = 30
                            for attempt in range(max_attempts):
                                time.sleep(10)
                                updated_response = self.elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                                if updated_response['CacheClusters']:
                                    updated_cluster = updated_response['CacheClusters'][0]
                                    if 'CacheNodes' in updated_cluster and updated_cluster['CacheNodes']:
                                        updated_node = updated_cluster['CacheNodes'][0]
                                        if updated_node['CacheNodeStatus'] == 'available' and 'Endpoint' in updated_node:
                                            redis_endpoint = updated_node['Endpoint']['Address']
                                            redis_port = updated_node['Endpoint']['Port']
                                            break
                    
                    if redis_endpoint and redis_port:
                        return {
                            'cluster_id': cluster_name,
                            'endpoint': redis_endpoint,
                            'port': redis_port,
                            'subnet_group_name': subnet_group_name,
                            'parameter_group_name': parameter_group_name
                        }
                elif cluster_status == 'creating':
                    waiter = self.elasticache.get_waiter('cache_cluster_available')
                    waiter.wait(CacheClusterId=cluster_name)
                    return self.create_elasticache_redis(vpc_id, security_group_id, private_subnets)
        except ClientError as e:
            if e.response['Error']['Code'] != 'CacheClusterNotFound':
                raise
        
        # Create new cluster
        try:
            redis_response = self.elasticache.create_cache_cluster(
                CacheClusterId=cluster_name,
                CacheNodeType=node_type,
                Engine='redis',
                NumCacheNodes=num_cache_nodes,
                CacheSubnetGroupName=subnet_group_name,
                CacheParameterGroupName=parameter_group_name,
                SecurityGroupIds=[security_group_id],
                EngineVersion='7.0',
                Port=6379,
                Tags=[
                    {'Key': 'Name', 'Value': f'{self.project_name}-redis'},
                    {'Key': 'Purpose', 'Value': 'ChromaDB caching layer'}
                ]
            )
        except ClientError as e:
            if e.response['Error']['Code'] in ['CacheClusterAlreadyExistsFault', 'CacheClusterAlreadyExists']:
                response = self.elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                existing_cluster = response['CacheClusters'][0]
                
                if 'ConfigurationEndpoint' in existing_cluster:
                    redis_endpoint = existing_cluster['ConfigurationEndpoint']['Address']
                    redis_port = existing_cluster['ConfigurationEndpoint']['Port']
                elif 'CacheNodes' in existing_cluster and existing_cluster['CacheNodes']:
                    redis_endpoint = existing_cluster['CacheNodes'][0]['Endpoint']['Address']
                    redis_port = existing_cluster['CacheNodes'][0]['Endpoint']['Port']
                
                return {
                    'cluster_id': cluster_name,
                    'endpoint': redis_endpoint,
                    'port': redis_port,
                    'subnet_group_name': subnet_group_name,
                    'parameter_group_name': parameter_group_name
                }
            else:
                raise
        
        # Wait for cluster
        try:
            waiter = self.elasticache.get_waiter('cache_cluster_available')
            waiter.wait(CacheClusterId=cluster_name)
        except Exception:
            max_attempts = 30
            for attempt in range(max_attempts):
                response = self.elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                if response['CacheClusters'][0]['CacheClusterStatus'] == 'available':
                    break
                time.sleep(30)
        
        # Get endpoint
        if 'ConfigurationEndpoint' in redis_response['CacheCluster']:
            redis_endpoint = redis_response['CacheCluster']['ConfigurationEndpoint']['Address']
            redis_port = redis_response['CacheCluster']['ConfigurationEndpoint']['Port']
        elif 'CacheNodes' in redis_response['CacheCluster'] and redis_response['CacheCluster']['CacheNodes']:
            cache_node = redis_response['CacheCluster']['CacheNodes'][0]
            if cache_node['CacheNodeStatus'] == 'available' and 'Endpoint' in cache_node:
                redis_endpoint = cache_node['Endpoint']['Address']
                redis_port = cache_node['Endpoint']['Port']
            else:
                time.sleep(30)
                updated_response = self.elasticache.describe_cache_clusters(CacheClusterId=cluster_name)
                if updated_response['CacheClusters']:
                    updated_cluster = updated_response['CacheClusters'][0]
                    if 'CacheNodes' in updated_cluster and updated_cluster['CacheNodes']:
                        updated_node = updated_cluster['CacheNodes'][0]
                        if 'Endpoint' in updated_node:
                            redis_endpoint = updated_node['Endpoint']['Address']
                            redis_port = updated_node['Endpoint']['Port']
        
        return {
            'cluster_id': cluster_name,
            'endpoint': redis_endpoint,
            'port': redis_port,
            'subnet_group_name': subnet_group_name,
            'parameter_group_name': parameter_group_name
        }
    
    def create_ecs_cluster(self) -> str:
        """Create ECS cluster."""
        logger.info("Creating ECS cluster...")
        
        # Ensure service-linked role exists
        try:
            self.iam.get_role(RoleName='AWSServiceRoleForECS')
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchEntity':
                try:
                    self.iam.create_service_linked_role(
                        AWSServiceName='ecs.amazonaws.com',
                        Description='Service-linked role for Amazon ECS'
                    )
                except ClientError as create_error:
                    if create_error.response['Error']['Code'] != 'InvalidInput':
                        raise create_error
        
        response = self.ecs.create_cluster(
            clusterName=self.cluster_name,
            capacityProviders=['FARGATE', 'FARGATE_SPOT'],
            defaultCapacityProviderStrategy=[{'capacityProvider': 'FARGATE', 'weight': 1}],
            settings=[{'name': 'containerInsights', 'value': 'enabled'}],
            tags=[
                {'key': 'Name', 'value': f'{self.project_name}-cluster'},
                {'key': 'Purpose', 'value': 'ChromaDB production cluster'}
            ]
        )
        
        return response['cluster']['clusterArn']
    
    def _create_execution_role(self) -> str:
        """Create IAM role for ECS task execution."""
        role_name = f'{self.project_name}-execution-role'
        
        try:
            self.iam.get_role(RoleName=role_name)
            return f'arn:aws:iam::{self.session.client("sts").get_caller_identity()["Account"]}:role/{role_name}'
        except ClientError:
            pass
        
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }
        
        response = self.iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description='ECS task execution role for ChromaDB'
        )
        
        self.iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy'
        )
        
        return response['Role']['Arn']
    
    def _create_task_role(self) -> str:
        """Create IAM role for ECS task."""
        role_name = f'{self.project_name}-task-role'
        
        try:
            self.iam.get_role(RoleName=role_name)
            return f'arn:aws:iam::{self.session.client("sts").get_caller_identity()["Account"]}:role/{role_name}'
        except ClientError:
            pass
        
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }
        
        response = self.iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description='ECS task role for ChromaDB'
        )
        
        return response['Role']['Arn']
    
    def create_task_definition(self, efs_file_system_id: str) -> str:
        """Create ECS task definition."""
        logger.info("Creating ECS task definition...")
        
        task_definition = {
            'family': self.task_family,
            'networkMode': 'awsvpc',
            'requiresCompatibilities': ['FARGATE'],
            'cpu': '2048',
            'memory': '4096',
            'executionRoleArn': self._create_execution_role(),
            'taskRoleArn': self._create_task_role(),
            'containerDefinitions': [{
                'name': 'chromadb',
                'image': 'ghcr.io/chroma-core/chroma:latest',
                'portMappings': [{'containerPort': 8000, 'protocol': 'tcp'}],
                'environment': [
                    {'name': 'CHROMA_SERVER_HOST', 'value': '0.0.0.0'},
                    {'name': 'CHROMA_SERVER_PORT', 'value': '8000'},
                    {'name': 'CHROMA_SERVER_CORS_ALLOW_ORIGINS', 'value': '*'},
                    {'name': 'CHROMA_SERVER_AUTH_CREDENTIALS_FILE', 'value': ''},
                    {'name': 'CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER', 'value': ''}
                ],
                'mountPoints': [{
                    'sourceVolume': 'chromadb-data',
                    'containerPath': '/chroma/chroma',
                    'readOnly': False
                }],
                'logConfiguration': {
                    'logDriver': 'awslogs',
                    'options': {
                        'awslogs-group': f'/ecs/{self.task_family}',
                        'awslogs-region': self.region,
                        'awslogs-stream-prefix': 'ecs'
                    }
                },
                'healthCheck': {
                    'command': ['CMD-SHELL', 'curl -f http://localhost:8000/api/v1/heartbeat || exit 1'],
                    'interval': 30,
                    'timeout': 5,
                    'retries': 3,
                    'startPeriod': 60
                }
            }],
            'volumes': [{
                'name': 'chromadb-data',
                'efsVolumeConfiguration': {
                    'fileSystemId': efs_file_system_id,
                    'rootDirectory': '/',
                    'transitEncryption': 'ENABLED'
                }
            }]
        }
        
        response = self.ecs.register_task_definition(**task_definition)
        return response['taskDefinition']['taskDefinitionArn']
    
    def create_alternative_task_definition(self, efs_file_system_id: str) -> str:
        """Create alternative ECS task definition with different image."""
        logger.info("Creating alternative ECS task definition...")
        
        # Try a different ChromaDB image
        alternative_images = [
            'chromadb/chroma:0.4.22',
            'chromadb/chroma:0.4.21',
            'chromadb/chroma:0.4.20'
        ]
        
        for image in alternative_images:
            try:
                logger.info(f"Trying alternative image: {image}")
                
                task_definition = {
                    'family': f'{self.task_family}-alt',
                    'networkMode': 'awsvpc',
                    'requiresCompatibilities': ['FARGATE'],
                    'cpu': '2048',
                    'memory': '4096',
                    'executionRoleArn': self._create_execution_role(),
                    'taskRoleArn': self._create_task_role(),
                    'containerDefinitions': [{
                        'name': 'chromadb',
                        'image': image,
                        'portMappings': [{'containerPort': 8000, 'protocol': 'tcp'}],
                        'environment': [
                            {'name': 'CHROMA_SERVER_HOST', 'value': '0.0.0.0'},
                            {'name': 'CHROMA_SERVER_PORT', 'value': '8000'},
                            {'name': 'CHROMA_SERVER_CORS_ALLOW_ORIGINS', 'value': '*'},
                            {'name': 'CHROMA_SERVER_AUTH_CREDENTIALS_FILE', 'value': ''},
                            {'name': 'CHROMA_SERVER_AUTH_CREDENTIALS_PROVIDER', 'value': ''}
                        ],
                        'mountPoints': [{
                            'sourceVolume': 'chromadb-data',
                            'containerPath': '/chroma/chroma',
                            'readOnly': False
                        }],
                        'logConfiguration': {
                            'logDriver': 'awslogs',
                            'options': {
                                'awslogs-group': f'/ecs/{self.task_family}-alt',
                                'awslogs-region': self.region,
                                'awslogs-stream-prefix': 'ecs'
                            }
                        },
                        'healthCheck': {
                            'command': ['CMD-SHELL', 'curl -f http://localhost:8000/api/v1/heartbeat || exit 1'],
                            'interval': 30,
                            'timeout': 5,
                            'retries': 3,
                            'startPeriod': 60
                        }
                    }],
                    'volumes': [{
                        'name': 'chromadb-data',
                        'efsVolumeConfiguration': {
                            'fileSystemId': efs_file_system_id,
                            'rootDirectory': '/',
                            'transitEncryption': 'ENABLED'
                        }
                    }]
                }
                
                response = self.ecs.register_task_definition(**task_definition)
                logger.info(f"✅ Alternative task definition created with image: {image}")
                return response['taskDefinition']['taskDefinitionArn']
                
            except Exception as e:
                logger.warning(f"Failed to create task definition with {image}: {e}")
                continue
        
        raise Exception("All alternative images failed to create task definition")
    
    def create_load_balancer(self, vpc_id: str, public_subnets: list, security_group_id: str) -> Dict[str, str]:
        """Create Application Load Balancer."""
        logger.info("Creating Application Load Balancer...")
        
        # Create ALB
        alb_response = self.elbv2.create_load_balancer(
            Name=self.load_balancer_name,
            Subnets=public_subnets,
            SecurityGroups=[security_group_id],
            Scheme='internet-facing',
            Type='application',
            IpAddressType='ipv4',
            Tags=[
                {'Key': 'Name', 'Value': f'{self.project_name}-alb'},
                {'Key': 'Purpose', 'Value': 'ChromaDB load balancer'}
            ]
        )
        
        alb_arn = alb_response['LoadBalancers'][0]['LoadBalancerArn']
        alb_dns = alb_response['LoadBalancers'][0]['DNSName']
        
        # Create target group
        tg_response = self.elbv2.create_target_group(
            Name=self.target_group_name,
            Protocol='HTTP',
            Port=8000,
            VpcId=vpc_id,
            TargetType='ip',
            HealthCheckProtocol='HTTP',
            HealthCheckPath='/api/v1/heartbeat',
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=5,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Tags=[
                {'Key': 'Name', 'Value': f'{self.project_name}-tg'},
                {'Key': 'Purpose', 'Value': 'ChromaDB target group'}
            ]
        )
        
        target_group_arn = tg_response['TargetGroups'][0]['TargetGroupArn']
        
        # Create listener
        listener_response = self.elbv2.create_listener(
            LoadBalancerArn=alb_arn,
            Protocol='HTTP',
            Port=80,
            DefaultActions=[{'Type': 'forward', 'TargetGroupArn': target_group_arn}]
        )
        
        return {
            'alb_arn': alb_arn,
            'alb_dns': alb_dns,
            'target_group_arn': target_group_arn,
            'listener_arn': listener_response['Listeners'][0]['ListenerArn']
        }
    
    def create_ecs_service(self, task_definition_arn: str, target_group_arn: str, 
                           private_subnets: list, security_group_id: str) -> str:
        """Create ECS service."""
        logger.info("Creating ECS service...")
        
        desired_tasks = max(int(os.getenv('ECS_DESIRED_TASKS', '3')), 3)
        
        service_response = self.ecs.create_service(
            cluster=self.cluster_name,
            serviceName=self.service_name,
            taskDefinition=task_definition_arn,
            desiredCount=desired_tasks,
            launchType='FARGATE',
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': private_subnets,
                    'securityGroups': [security_group_id],
                    'assignPublicIp': 'DISABLED'
                }
            },
            loadBalancers=[{
                'targetGroupArn': target_group_arn,
                'containerName': 'chromadb',
                'containerPort': 8000
            }],
            deploymentConfiguration={
                'maximumPercent': 200,
                'minimumHealthyPercent': 100,
                'deploymentCircuitBreaker': {'enable': True, 'rollback': True}
            },
            enableECSManagedTags=True,
            propagateTags='SERVICE',
            tags=[
                {'key': 'Name', 'value': f'{self.project_name}-service'},
                {'key': 'Purpose', 'value': 'ChromaDB production service'}
            ]
        )
        
        return service_response['service']['serviceArn']
    
    def setup_auto_scaling(self, service_arn: str, target_group_arn: str):
        """Set up auto-scaling."""
        logger.info("Setting up auto-scaling...")
        
        min_tasks = int(os.getenv('ECS_MIN_TASKS', '3'))
        max_tasks = int(os.getenv('ECS_MAX_TASKS', '10'))
        cpu_target = float(os.getenv('ECS_CPU_TARGET', '70.0'))
        memory_target = float(os.getenv('ECS_MEMORY_TARGET', '80.0'))
        scale_out_cooldown = int(os.getenv('ECS_SCALE_OUT_COOLDOWN', '60'))
        scale_in_cooldown = int(os.getenv('ECS_SCALE_IN_COOLDOWN', '120'))
        
        autoscaling = self.session.client('application-autoscaling')
        
        # Ensure service ARN format
        if not service_arn.startswith('service/'):
            service_name = service_arn.split('/')[-1]
            service_arn = f"service/{self.cluster_name}/{service_name}"
        
        try:
            autoscaling.register_scalable_target(
                ServiceNamespace='ecs',
                ResourceId=service_arn,
                ScalableDimension='ecs:service:DesiredCount',
                MinCapacity=min_tasks,
                MaxCapacity=max_tasks
            )
        except Exception as e:
            logger.error(f"Failed to register auto-scaling target: {e}")
            try:
                alternative_arn = f"service/{self.cluster_name}/{self.service_name}"
                autoscaling.register_scalable_target(
                    ServiceNamespace='ecs',
                    ResourceId=alternative_arn,
                    ScalableDimension='ecs:service:DesiredCount',
                    MinCapacity=min_tasks,
                    MaxCapacity=max_tasks
                )
                service_arn = alternative_arn
            except Exception as e2:
                raise Exception(f"Auto-scaling setup failed: {e2}")
        
        # Create scaling policies
        policies = [
            {
                'name': f'{self.project_name}-cpu-scaling',
                'target_value': cpu_target,
                'metric_type': 'ECSServiceAverageCPUUtilization'
            },
            {
                'name': f'{self.project_name}-memory-scaling',
                'target_value': memory_target,
                'metric_type': 'ECSServiceAverageMemoryUtilization'
            }
        ]
        
        for policy in policies:
            try:
                autoscaling.put_scaling_policy(
                    ServiceNamespace='ecs',
                    ResourceId=service_arn,
                    ScalableDimension='ecs:service:DesiredCount',
                    PolicyName=policy['name'],
                    PolicyType='TargetTrackingScaling',
                    TargetTrackingScalingPolicyConfiguration={
                        'TargetValue': policy['target_value'],
                        'PredefinedMetricSpecification': {
                            'PredefinedMetricType': policy['metric_type']
                        },
                        'ScaleOutCooldown': scale_out_cooldown,
                        'ScaleInCooldown': scale_in_cooldown,
                        'DisableScaleIn': False
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to create {policy['name']}: {e}")
        
        logger.info("✅ Auto-scaling setup completed")
    
    def fix_service_desired_count(self):
        """Fix ECS service desired count if it's set to 0."""
        logger.info("Checking and fixing ECS service desired count...")
        
        try:
            service_response = self.ecs.describe_services(
                cluster=self.cluster_name,
                services=[self.service_name]
            )
            
            if service_response['services']:
                service = service_response['services'][0]
                current_desired = service.get('desiredCount', 0)
                
                if current_desired == 0:
                    logger.info("Service desired count is 0, updating to 3...")
                    
                    self.ecs.update_service(
                        cluster=self.cluster_name,
                        service=self.service_name,
                        desiredCount=3
                    )
                    
                    logger.info("✅ Service desired count updated to 3")
                    
                    # Wait for service to stabilize
                    try:
                        waiter = self.ecs.get_waiter('services_stable')
                        waiter.wait(
                            cluster=self.cluster_name,
                            services=[self.service_name],
                            WaiterConfig={'Delay': 30, 'MaxAttempts': 40}
                        )
                        logger.info("✅ Service is now stable with 3 tasks")
                    except Exception as e:
                        logger.warning(f"Service did not stabilize within expected time: {e}")
                else:
                    logger.info(f"Service desired count is already {current_desired}, no update needed")
            else:
                logger.warning("Service not found")
                
        except Exception as e:
            logger.error(f"Error fixing service desired count: {e}")
    
    def diagnose_ecs_issues(self):
        """Diagnose common ECS service issues."""
        logger.info("🔍 Diagnosing ECS service issues...")
        
        try:
            # Check service status
            service_response = self.ecs.describe_services(
                cluster=self.cluster_name,
                services=[self.service_name]
            )
            
            if not service_response['services']:
                logger.error("❌ Service not found!")
                return
            
            service = service_response['services'][0]
            logger.info(f"Service status: {service.get('status', 'UNKNOWN')}")
            logger.info(f"Desired count: {service.get('desiredCount', 0)}")
            logger.info(f"Running count: {service.get('runningCount', 0)}")
            logger.info(f"Pending count: {service.get('pendingCount', 0)}")
            
            # Check deployments
            deployments = service.get('deployments', [])
            for deployment in deployments:
                logger.info(f"Deployment: {deployment.get('status', 'UNKNOWN')}")
                logger.info(f"  - Desired: {deployment.get('desiredCount', 0)}")
                logger.info(f"  - Running: {deployment.get('runningCount', 0)}")
                logger.info(f"  - Pending: {deployment.get('pendingCount', 0)}")
                logger.info(f"  - Failed: {deployment.get('failedTasks', 0)}")
            
            # Check recent events
            events = service.get('events', [])
            logger.info("Recent service events:")
            for event in events[:3]:
                logger.info(f"  - {event.get('message', 'No message')}")
            
            # Check task failures
            tasks_response = self.ecs.list_tasks(
                cluster=self.cluster_name,
                serviceName=self.service_name,
                desiredStatus='STOPPED'
            )
            
            if tasks_response['taskArns']:
                tasks_detail = self.ecs.describe_tasks(
                    cluster=self.cluster_name,
                    tasks=tasks_response['taskArns']
                )
                
                logger.info("Recent task failures:")
                for task in tasks_detail['tasks']:
                    stopped_reason = task.get('stoppedReason', 'Unknown')
                    logger.error(f"  - Task stopped: {stopped_reason}")
                    
                    # Check container details
                    containers = task.get('containers', [])
                    for container in containers:
                        if 'exitCode' in container:
                            logger.error(f"    Container {container.get('name', 'unknown')} exit code: {container['exitCode']}")
                        if 'reason' in container:
                            logger.error(f"    Reason: {container['reason']}")
            
            # Check if there are any running tasks
            running_tasks = self.ecs.list_tasks(
                cluster=self.cluster_name,
                serviceName=self.service_name,
                desiredStatus='RUNNING'
            )
            
            if running_tasks['taskArns']:
                logger.info(f"Found {len(running_tasks['taskArns'])} running tasks")
            else:
                logger.warning("No running tasks found")
                
        except Exception as e:
            logger.error(f"Error diagnosing ECS issues: {e}")
    
    def force_service_update(self):
        """Force update the ECS service to restart tasks."""
        logger.info("🔄 Force updating ECS service...")
        
        try:
            # Get current task definition
            task_definitions = self.ecs.list_task_definitions(
                familyPrefix=self.task_family,
                status='ACTIVE'
            )
            
            if not task_definitions['taskDefinitionArns']:
                logger.error("No active task definitions found")
                return
            
            latest_task_def = task_definitions['taskDefinitionArns'][-1]
            
            # Force new deployment
            self.ecs.update_service(
                cluster=self.cluster_name,
                service=self.service_name,
                taskDefinition=latest_task_def,
                forceNewDeployment=True
            )
            
            logger.info("✅ Service force update initiated")
            
            # Wait for deployment to complete
            try:
                waiter = self.ecs.get_waiter('services_stable')
                waiter.wait(
                    cluster=self.cluster_name,
                    services=[self.service_name],
                    WaiterConfig={'Delay': 30, 'MaxAttempts': 40}
                )
                logger.info("✅ Service is now stable after force update")
            except Exception as e:
                logger.warning(f"Service did not stabilize after force update: {e}")
                
        except Exception as e:
            logger.error(f"Error force updating service: {e}")
    
    def fix_docker_image_issues(self):
        """Fix Docker image pull issues by trying alternative images."""
        logger.info("🐳 Fixing Docker image pull issues...")
        
        try:
            # Load existing services to get EFS info
            services_file = self._load_services_from_file()
            if not services_file or 'efs' not in services_file.get('services', {}):
                logger.error("No EFS information found. Cannot create alternative task definition.")
                return
            
            efs_info = services_file['services']['efs']
            if efs_info.get('status') != 'DEPLOYED':
                logger.error("EFS not deployed. Cannot create alternative task definition.")
                return
            
            # Create alternative task definition
            logger.info("Creating alternative task definition with different image...")
            alternative_task_def_arn = self.create_alternative_task_definition(efs_info['file_system_id'])
            
            # Update service to use alternative task definition
            logger.info("Updating service to use alternative task definition...")
            self.ecs.update_service(
                cluster=self.cluster_name,
                service=self.service_name,
                taskDefinition=alternative_task_def_arn,
                forceNewDeployment=True
            )
            
            logger.info("✅ Service updated with alternative task definition")
            
            # Wait for service to stabilize
            try:
                waiter = self.ecs.get_waiter('services_stable')
                waiter.wait(
                    cluster=self.cluster_name,
                    services=[self.service_name],
                    WaiterConfig={'Delay': 30, 'MaxAttempts': 40}
                )
                logger.info("✅ Service is now stable with alternative image")
            except Exception as e:
                logger.warning(f"Service did not stabilize with alternative image: {e}")
                
        except Exception as e:
            logger.error(f"Error fixing Docker image issues: {e}")
    
    def setup_monitoring(self):
        """Set up CloudWatch monitoring."""
        logger.info("Setting up CloudWatch monitoring...")
        
        # Create CloudWatch dashboard
        dashboard_body = {
            "widgets": [
                {
                    "type": "metric",
                    "x": 0, "y": 0, "width": 12, "height": 6,
                    "properties": {
                        "metrics": [
                            ["AWS/ECS", "CPUUtilization", "ServiceName", self.service_name, "ClusterName", self.cluster_name],
                            [".", "MemoryUtilization", ".", ".", ".", "."]
                        ],
                        "view": "timeSeries",
                        "stacked": False,
                        "region": self.region,
                        "title": "ChromaDB Service CPU and Memory Utilization"
                    }
                },
                {
                    "type": "metric",
                    "x": 12, "y": 0, "width": 12, "height": 6,
                    "properties": {
                        "metrics": [
                            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", self.load_balancer_name],
                            [".", "RequestCount", ".", "."]
                        ],
                        "view": "timeSeries",
                        "stacked": False,
                        "region": self.region,
                        "title": "Load Balancer Response Time and Request Count"
                    }
                }
            ]
        }
        
        self.cloudwatch.put_dashboard(
            DashboardName=f'{self.project_name}-dashboard',
            DashboardBody=json.dumps(dashboard_body)
        )
        
        # Create CloudWatch log group
        try:
            self.logs.create_log_group(logGroupName=f'/ecs/{self.task_family}')
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                raise
    
    def _save_services_to_file(self, services_data: Dict[str, Any]) -> None:
        """Save AWS services information to aws_services.json file."""
        config_folder = os.getenv('CONFIG_FOLDER', '.')
        services_file_path = os.path.join(config_folder, 'aws_services.json')
        
        os.makedirs(config_folder, exist_ok=True)
        
        # Load existing services if file exists
        existing_services = {}
        if os.path.exists(services_file_path):
            try:
                with open(services_file_path, 'r') as f:
                    existing_services = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load existing services file: {e}")
        
        # Merge existing services with new services data
        if 'services' not in existing_services:
            existing_services['services'] = {}
        
        # Update or add new services
        for service_name, service_info in services_data['services'].items():
            existing_services['services'][service_name] = service_info
        
        # Update metadata
        existing_services['project_name'] = self.project_name
        existing_services['region'] = self.region
        existing_services['last_updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        if 'deployment_timestamp' not in existing_services:
            existing_services['deployment_timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        # Save the updated services file
        with open(services_file_path, 'w') as f:
            json.dump(existing_services, f, indent=2)
        
        logger.info(f"AWS services information updated in {services_file_path}")

    def _load_services_from_file(self) -> Dict[str, Any]:
        """Load AWS services information from aws_services.json file."""
        config_folder = os.getenv('CONFIG_FOLDER', '.')
        services_file_path = os.path.join(config_folder, 'aws_services.json')
        
        try:
            with open(services_file_path, 'r') as f:
                services_file = json.load(f)
            logger.info(f"AWS services information loaded from {services_file_path}")
            return services_file
        except FileNotFoundError:
            logger.warning(f"{services_file_path} file not found. Cannot perform targeted deletion.")
            return {}
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in {services_file_path} file.")
            return {}
    
    def deploy_with_tracking(self, services_data: Dict[str, Any]) -> Dict[str, Any]:
        """Deploy the complete ChromaDB infrastructure with service tracking."""
        logger.info("Starting ChromaDB infrastructure deployment with tracking...")
        
        deployment_result = {
            'success': True,
            'load_balancer_dns': None,
            'cluster_name': self.cluster_name,
            'service_name': self.service_name,
            'errors': []
        }
        
        try:
            # Step 1: Create VPC and networking
            logger.info("Step 1: Creating VPC and networking...")
            try:
                if 'vpc' not in services_data['services'] or services_data['services']['vpc'].get('status') != 'DEPLOYED':
                    networking = self.create_vpc_and_networking()
                    services_data['services']['vpc'] = {
                        'vpc_id': networking['vpc_id'],
                        'public_subnet_1': networking['public_subnet_1'],
                        'public_subnet_2': networking['public_subnet_2'],
                        'private_subnet_1': networking['private_subnet_1'],
                        'private_subnet_2': networking['private_subnet_2'],
                        'security_group_id': networking['security_group_id'],
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ VPC and networking created successfully")
                else:
                    logger.info("⏭️  VPC and networking already deployed, skipping...")
                    networking = {
                        'vpc_id': services_data['services']['vpc']['vpc_id'],
                        'public_subnet_1': services_data['services']['vpc']['public_subnet_1'],
                        'public_subnet_2': services_data['services']['vpc']['public_subnet_2'],
                        'private_subnet_1': services_data['services']['vpc']['private_subnet_1'],
                        'private_subnet_2': services_data['services']['vpc']['private_subnet_2'],
                        'security_group_id': services_data['services']['vpc']['security_group_id']
                    }
            except Exception as e:
                error_msg = f"Failed to create VPC and networking: {str(e)}"
                logger.error(error_msg)
                services_data['services']['vpc'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 2: Create EFS storage
            logger.info("Step 2: Creating EFS storage...")
            try:
                if 'efs' not in services_data['services'] or services_data['services']['efs'].get('status') != 'DEPLOYED':
                    storage = self.create_efs_storage(
                        networking['vpc_id'], 
                        networking['security_group_id'],
                        [networking['private_subnet_1'], networking['private_subnet_2']]
                    )
                    services_data['services']['efs'] = {
                        'file_system_id': storage['file_system_id'],
                        'mount_targets': storage['mount_targets'],
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ EFS storage created successfully")
                else:
                    logger.info("⏭️  EFS storage already deployed, skipping...")
                    storage = {
                        'file_system_id': services_data['services']['efs']['file_system_id'],
                        'mount_targets': services_data['services']['efs']['mount_targets']
                    }
            except Exception as e:
                error_msg = f"Failed to create EFS storage: {str(e)}"
                logger.error(error_msg)
                services_data['services']['efs'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 3: Create ElastiCache Redis
            logger.info("Step 3: Creating ElastiCache Redis...")
            try:
                if 'redis' not in services_data['services'] or services_data['services']['redis'].get('status') != 'DEPLOYED':
                    redis_config = self.create_elasticache_redis(
                        networking['vpc_id'], 
                        networking['security_group_id'],
                        [networking['private_subnet_1'], networking['private_subnet_2']]
                    )
                    if redis_config:
                        services_data['services']['redis'] = {
                            **redis_config,
                            'status': 'DEPLOYED',
                            'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                        }
                    else:
                        services_data['services']['redis'] = {
                            'status': 'SKIPPED',
                            'reason': 'Redis disabled or failed to create',
                            'skipped_at': time.strftime('%Y-%m-%d %H:%M:%S')
                        }
                    self._save_services_to_file(services_data)
                    logger.info("✅ Redis cluster created successfully")
                else:
                    logger.info("⏭️  Redis cluster already deployed, skipping...")
                    redis_config = services_data['services']['redis']
            except Exception as e:
                error_msg = f"Failed to create Redis cluster: {str(e)}"
                logger.error(error_msg)
                services_data['services']['redis'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                redis_config = {}
            
            # Step 4: Create ECS cluster
            logger.info("Step 4: Creating ECS cluster...")
            try:
                if 'ecs' not in services_data['services'] or services_data['services']['ecs'].get('status') != 'DEPLOYED':
                    cluster_arn = self.create_ecs_cluster()
                    services_data['services']['ecs'] = {
                        'cluster_name': self.cluster_name,
                        'cluster_arn': cluster_arn,
                        'service_name': self.service_name,
                        'task_family': self.task_family,
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ ECS cluster created successfully")
                else:
                    logger.info("⏭️  ECS cluster already deployed, skipping...")
                    cluster_arn = services_data['services']['ecs']['cluster_arn']
            except Exception as e:
                error_msg = f"Failed to create ECS cluster: {str(e)}"
                logger.error(error_msg)
                services_data['services']['ecs'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 5: Create task definition
            logger.info("Step 5: Creating ECS task definition...")
            try:
                if 'task_definition' not in services_data['services'] or services_data['services']['task_definition'].get('status') != 'DEPLOYED':
                    task_definition_arn = self.create_task_definition(storage['file_system_id'])
                    services_data['services']['task_definition'] = {
                        'task_definition_arn': task_definition_arn,
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ ECS task definition created successfully")
                else:
                    logger.info("⏭️  ECS task definition already deployed, skipping...")
                    task_definition_arn = services_data['services']['task_definition']['task_definition_arn']
            except Exception as e:
                error_msg = f"Failed to create ECS task definition: {str(e)}"
                logger.error(error_msg)
                services_data['services']['task_definition'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 6: Create load balancer
            logger.info("Step 6: Creating Application Load Balancer...")
            try:
                if 'load_balancer' not in services_data['services'] or services_data['services']['load_balancer'].get('status') != 'DEPLOYED':
                    lb_config = self.create_load_balancer(
                        networking['vpc_id'],
                        [networking['public_subnet_1'], networking['public_subnet_2']],
                        networking['security_group_id']
                    )
                    services_data['services']['load_balancer'] = {
                        'name': self.load_balancer_name,
                        'arn': lb_config['alb_arn'],
                        'dns': lb_config['alb_dns'],
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    services_data['services']['target_group'] = {
                        'name': self.target_group_name,
                        'arn': lb_config['target_group_arn'],
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ Load balancer created successfully")
                    deployment_result['load_balancer_dns'] = lb_config['alb_dns']
                else:
                    logger.info("⏭️  Load balancer already deployed, skipping...")
                    lb_config = {
                        'alb_arn': services_data['services']['load_balancer']['arn'],
                        'alb_dns': services_data['services']['load_balancer']['dns'],
                        'target_group_arn': services_data['services']['target_group']['arn']
                    }
                    deployment_result['load_balancer_dns'] = lb_config['alb_dns']
            except Exception as e:
                error_msg = f"Failed to create load balancer: {str(e)}"
                logger.error(error_msg)
                services_data['services']['load_balancer'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 7: Create ECS service
            logger.info("Step 7: Creating ECS service...")
            try:
                if 'ecs_service' not in services_data['services'] or services_data['services']['ecs_service'].get('status') != 'DEPLOYED':
                    service_arn = self.create_ecs_service(
                        task_definition_arn,
                        lb_config['target_group_arn'],
                        [networking['private_subnet_1'], networking['private_subnet_2']],
                        networking['security_group_id']
                    )
                    services_data['services']['ecs_service'] = {
                        'service_arn': service_arn,
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    services_data['services']['ecs']['service_arn'] = service_arn
                    self._save_services_to_file(services_data)
                    logger.info("✅ ECS service created successfully")
                else:
                    logger.info("⏭️  ECS service already deployed, skipping...")
                    service_arn = services_data['services']['ecs_service']['service_arn']
            except Exception as e:
                error_msg = f"Failed to create ECS service: {str(e)}"
                logger.error(error_msg)
                services_data['services']['ecs_service'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
                deployment_result['success'] = False
                return deployment_result
            
            # Step 8: Setup auto-scaling
            logger.info("Step 8: Setting up auto-scaling...")
            try:
                if 'auto_scaling' not in services_data['services'] or services_data['services']['auto_scaling'].get('status') != 'DEPLOYED':
                    self.setup_auto_scaling(service_arn, lb_config['target_group_arn'])
                    services_data['services']['auto_scaling'] = {
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ Auto-scaling configured successfully")
                else:
                    logger.info("⏭️  Auto-scaling already configured, skipping...")
            except Exception as e:
                error_msg = f"Failed to setup auto-scaling: {str(e)}"
                logger.error(error_msg)
                services_data['services']['auto_scaling'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
            
            # Step 9: Setup monitoring
            logger.info("Step 9: Setting up monitoring...")
            try:
                if 'monitoring' not in services_data['services'] or services_data['services']['monitoring'].get('status') != 'DEPLOYED':
                    self.setup_monitoring()
                    services_data['services']['monitoring'] = {
                        'dashboard_name': f'{self.project_name}-dashboard',
                        'log_group_name': f'/ecs/{self.task_family}',
                        'status': 'DEPLOYED',
                        'deployed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    self._save_services_to_file(services_data)
                    logger.info("✅ Monitoring configured successfully")
                else:
                    logger.info("⏭️  Monitoring already configured, skipping...")
            except Exception as e:
                error_msg = f"Failed to setup monitoring: {str(e)}"
                logger.error(error_msg)
                services_data['services']['monitoring'] = {
                    'status': 'FAILED',
                    'error': error_msg,
                    'failed_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_services_to_file(services_data)
                deployment_result['errors'].append(error_msg)
            
            # Step 9.5: Fix service desired count if needed
            logger.info("Step 9.5: Checking and fixing service desired count...")
            try:
                self.fix_service_desired_count()
                logger.info("✅ Service desired count check completed")
            except Exception as e:
                error_msg = f"Failed to fix service desired count: {str(e)}"
                logger.error(error_msg)
                deployment_result['errors'].append(error_msg)
            
            # Step 10: Wait for service to be stable
            logger.info("Step 10: Waiting for ECS service to be stable...")
            try:
                # First, let's check the current service status
                service_response = self.ecs.describe_services(
                    cluster=self.cluster_name,
                    services=[self.service_name]
                )
                
                if service_response['services']:
                    service = service_response['services'][0]
                    logger.info(f"Service status: {service.get('status', 'UNKNOWN')}")
                    logger.info(f"Desired count: {service.get('desiredCount', 0)}")
                    logger.info(f"Running count: {service.get('runningCount', 0)}")
                    logger.info(f"Pending count: {service.get('pendingCount', 0)}")
                    
                    # Check for deployment issues
                    deployments = service.get('deployments', [])
                    for deployment in deployments:
                        logger.info(f"Deployment status: {deployment.get('status', 'UNKNOWN')}")
                        logger.info(f"Deployment desired: {deployment.get('desiredCount', 0)}")
                        logger.info(f"Deployment running: {deployment.get('runningCount', 0)}")
                        logger.info(f"Deployment pending: {deployment.get('pendingCount', 0)}")
                        logger.info(f"Deployment failed: {deployment.get('failedTasks', 0)}")
                
                # Check for recent task failures
                tasks_response = self.ecs.list_tasks(
                    cluster=self.cluster_name,
                    serviceName=self.service_name
                )
                
                if tasks_response['taskArns']:
                    tasks_detail = self.ecs.describe_tasks(
                        cluster=self.cluster_name,
                        tasks=tasks_response['taskArns']
                    )
                    
                    for task in tasks_detail['tasks']:
                        task_status = task.get('lastStatus', 'UNKNOWN')
                        task_health = task.get('healthStatus', 'UNKNOWN')
                        logger.info(f"Task {task['taskArn'].split('/')[-1]}: {task_status} (health: {task_health})")
                        
                        # Check for stopped tasks and their reasons
                        if task_status == 'STOPPED':
                            stopped_reason = task.get('stoppedReason', 'Unknown')
                            logger.error(f"Task stopped: {stopped_reason}")
                            
                            # Check container status
                            containers = task.get('containers', [])
                            for container in containers:
                                container_status = container.get('lastStatus', 'UNKNOWN')
                                container_reason = container.get('reason', 'No reason provided')
                                logger.error(f"Container {container.get('name', 'unknown')}: {container_status} - {container_reason}")
                
                # Now try to wait for stability
                waiter = self.ecs.get_waiter('services_stable')
                waiter.wait(
                    cluster=self.cluster_name,
                    services=[self.service_name],
                    WaiterConfig={'Delay': 30, 'MaxAttempts': 40}
                )
                logger.info("✅ ECS service is stable")
            except Exception as e:
                error_msg = f"Service did not become stable: {str(e)}"
                logger.warning(error_msg)
                
                # Additional debugging for task failures
                try:
                    logger.info("🔍 Additional debugging information:")
                    
                    # Check service events
                    service_response = self.ecs.describe_services(
                        cluster=self.cluster_name,
                        services=[self.service_name]
                    )
                    
                    if service_response['services']:
                        service = service_response['services'][0]
                        events = service.get('events', [])
                        logger.info("Recent service events:")
                        for event in events[:5]:  # Show last 5 events
                            logger.info(f"  - {event.get('createdAt', 'Unknown time')}: {event.get('message', 'No message')}")
                    
                    # Check if there are any recent task failures
                    tasks_response = self.ecs.list_tasks(
                        cluster=self.cluster_name,
                        serviceName=self.service_name,
                        desiredStatus='STOPPED'
                    )
                    
                    if tasks_response['taskArns']:
                        tasks_detail = self.ecs.describe_tasks(
                            cluster=self.cluster_name,
                            tasks=tasks_response['taskArns']
                        )
                        
                        logger.info("Recent stopped tasks:")
                        for task in tasks_detail['tasks']:
                            stopped_reason = task.get('stoppedReason', 'Unknown')
                            logger.info(f"  - Task {task['taskArn'].split('/')[-1]}: {stopped_reason}")
                            
                            # Check container exit codes
                            containers = task.get('containers', [])
                            for container in containers:
                                if 'exitCode' in container:
                                    logger.info(f"    Container {container.get('name', 'unknown')} exit code: {container['exitCode']}")
                                    if 'reason' in container:
                                        logger.info(f"    Reason: {container['reason']}")
                
                except Exception as debug_error:
                    logger.warning(f"Could not get additional debugging info: {debug_error}")
                
                deployment_result['errors'].append(error_msg)
            
            # Update final status
            services_data['last_updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
            self._save_services_to_file(services_data)
            
            logger.info("✅ Deployment with tracking completed!")
            return deployment_result
            
        except Exception as e:
            error_msg = f"Deployment failed: {str(e)}"
            logger.error(error_msg)
            deployment_result['errors'].append(error_msg)
            deployment_result['success'] = False
            return deployment_result
    
    def delete_all_resources(self) -> None:
        """Delete all AWS resources created by this infrastructure."""
        logger.info("Starting deletion of all ChromaDB infrastructure resources...")
        
        services_file = self._load_services_from_file()
        if not services_file:
            logger.error("No services information found. Cannot perform deletion.")
            return
        
        services = services_file.get('services', {})
        
        try:
            # Delete ECS Service
            logger.info("Deleting ECS service...")
            try:
                ecs_info = services.get('ecs', {})
                cluster_name = ecs_info.get('cluster_name', self.cluster_name)
                service_name = ecs_info.get('service_name', self.service_name)
                
                self.ecs.update_service(cluster=cluster_name, service=service_name, desiredCount=0)
                waiter = self.ecs.get_waiter('services_stable')
                waiter.wait(cluster=cluster_name, services=[service_name])
                self.ecs.delete_service(cluster=cluster_name, service=service_name)
                logger.info("ECS service deleted successfully")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ServiceNotFoundException':
                    logger.warning(f"Error deleting ECS service: {e}")
            
            # Delete Application Load Balancer
            logger.info("Deleting Application Load Balancer...")
            try:
                lb_info = services.get('load_balancer', {})
                alb_arn = lb_info.get('arn')
                if alb_arn:
                    self.elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
                    logger.info("Application Load Balancer deleted successfully")
            except ClientError as e:
                if e.response['Error']['Code'] != 'LoadBalancerNotFound':
                    logger.warning(f"Error deleting ALB: {e}")
            
            # Delete Target Group
            logger.info("Deleting Target Group...")
            try:
                tg_info = services.get('target_group', {})
                tg_arn = tg_info.get('arn')
                if tg_arn:
                    self.elbv2.delete_target_group(TargetGroupArn=tg_arn)
                    logger.info("Target Group deleted successfully")
            except ClientError as e:
                if e.response['Error']['Code'] != 'TargetGroupNotFound':
                    logger.warning(f"Error deleting Target Group: {e}")
            
            # Delete Auto Scaling policies
            logger.info("Deleting Auto Scaling policies...")
            try:
                asg_client = self.session.client('application-autoscaling')
                ecs_info = services.get('ecs', {})
                service_arn = ecs_info.get('service_arn')
                if service_arn:
                    asg_client.deregister_scalable_target(
                        ServiceNamespace='ecs',
                        ResourceId=service_arn,
                        ScalableDimension='ecs:service:DesiredCount'
                    )
                    logger.info("Auto scaling target deregistered")
            except ClientError as e:
                logger.warning(f"Error deleting auto scaling policies: {e}")
            
            # Delete ECS Task Definition
            logger.info("Deleting ECS Task Definition...")
            try:
                ecs_info = services.get('ecs', {})
                task_family = ecs_info.get('task_family', self.task_family)
                task_definitions = self.ecs.list_task_definitions(familyPrefix=task_family)
                for task_def_arn in task_definitions['taskDefinitionArns']:
                    self.ecs.deregister_task_definition(taskDefinition=task_def_arn)
                    logger.info(f"Deregistered task definition: {task_def_arn}")
            except ClientError as e:
                logger.warning(f"Error deleting task definitions: {e}")
            
            # Delete ECS Cluster
            logger.info("Deleting ECS Cluster...")
            try:
                ecs_info = services.get('ecs', {})
                cluster_name = ecs_info.get('cluster_name', self.cluster_name)
                self.ecs.delete_cluster(cluster=cluster_name)
                logger.info("ECS Cluster deleted successfully")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ClusterNotFoundException':
                    logger.warning(f"Error deleting ECS cluster: {e}")
            
            # Delete ElastiCache Redis
            logger.info("Deleting ElastiCache Redis cluster...")
            try:
                redis_info = services.get('redis', {})
                cluster_id = redis_info.get('cluster_id')
                subnet_group_name = redis_info.get('subnet_group_name')
                parameter_group_name = redis_info.get('parameter_group_name')
                
                if cluster_id:
                    self.elasticache.delete_cache_cluster(CacheClusterId=cluster_id)
                    logger.info(f"Deleted Redis cluster: {cluster_id}")
                    time.sleep(30)
                
                if subnet_group_name:
                    try:
                        self.elasticache.delete_cache_subnet_group(CacheSubnetGroupName=subnet_group_name)
                        logger.info(f"Deleted Redis subnet group: {subnet_group_name}")
                    except ClientError as e:
                        logger.warning(f"Error deleting Redis subnet group: {e}")
                
                if parameter_group_name:
                    try:
                        self.elasticache.delete_cache_parameter_group(CacheParameterGroupName=parameter_group_name)
                        logger.info(f"Deleted Redis parameter group: {parameter_group_name}")
                    except ClientError as e:
                        logger.warning(f"Error deleting Redis parameter group: {e}")
            except ClientError as e:
                logger.warning(f"Error deleting Redis: {e}")
            
            # Delete EFS File System
            logger.info("Deleting EFS File System...")
            try:
                efs_info = services.get('efs', {})
                file_system_id = efs_info.get('file_system_id')
                mount_targets = efs_info.get('mount_targets', [])
                
                if file_system_id:
                    # Delete mount targets first
                    for mount_target_id in mount_targets:
                        try:
                            self.efs.delete_mount_target(MountTargetId=mount_target_id)
                            logger.info(f"Deleted mount target: {mount_target_id}")
                        except ClientError as e:
                            logger.warning(f"Error deleting mount target {mount_target_id}: {e}")
                    
                    if mount_targets:
                        time.sleep(30)
                    
                    # Delete file system
                    self.efs.delete_file_system(FileSystemId=file_system_id)
                    logger.info(f"Deleted EFS file system: {file_system_id}")
            except ClientError as e:
                logger.warning(f"Error deleting EFS: {e}")
            
            # Delete CloudWatch resources
            logger.info("Deleting CloudWatch resources...")
            try:
                cloudwatch_info = services.get('cloudwatch', {})
                dashboard_name = cloudwatch_info.get('dashboard_name')
                if dashboard_name:
                    self.cloudwatch.delete_dashboards(DashboardNames=[dashboard_name])
                    logger.info("CloudWatch dashboard deleted")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFound':
                    logger.warning(f"Error deleting CloudWatch dashboard: {e}")
            
            try:
                cloudwatch_info = services.get('cloudwatch', {})
                log_group_name = cloudwatch_info.get('log_group_name')
                if log_group_name:
                    self.logs.delete_log_group(logGroupName=log_group_name)
                    logger.info("CloudWatch log group deleted")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    logger.warning(f"Error deleting log group: {e}")
            
            # Delete IAM roles
            logger.info("Deleting IAM roles...")
            try:
                iam_info = services.get('iam_roles', {})
                task_execution_role_name = iam_info.get('task_execution_role')
                if task_execution_role_name:
                    try:
                        self.iam.delete_role_policy(RoleName=task_execution_role_name, PolicyName=f'{self.project_name}-task-execution-policy')
                    except ClientError:
                        pass
                    try:
                        self.iam.detach_role_policy(RoleName=task_execution_role_name, PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy')
                    except ClientError:
                        pass
                    self.iam.delete_role(RoleName=task_execution_role_name)
                    logger.info("Task execution role deleted")
            except ClientError as e:
                logger.warning(f"Error deleting task execution role: {e}")
            
            try:
                iam_info = services.get('iam_roles', {})
                task_role_name = iam_info.get('task_role')
                if task_role_name:
                    try:
                        self.iam.delete_role_policy(RoleName=task_role_name, PolicyName=f'{self.project_name}-task-policy')
                    except ClientError:
                        pass
                    self.iam.delete_role(RoleName=task_role_name)
                    logger.info("Task role deleted")
            except ClientError as e:
                logger.warning(f"Error deleting task role: {e}")
            
            # Delete VPC and networking resources
            logger.info("Deleting VPC and networking resources...")
            try:
                vpc_info = services.get('vpc', {})
                vpc_id = vpc_info.get('vpc_id')
                
                if vpc_id:
                    # Delete subnets
                    subnets = self.ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                    for subnet in subnets['Subnets']:
                        self.ec2.delete_subnet(SubnetId=subnet['SubnetId'])
                        logger.info(f"Deleted subnet: {subnet['SubnetId']}")
                    
                    # Delete security groups
                    security_groups = self.ec2.describe_security_groups(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                    for sg in security_groups['SecurityGroups']:
                        if sg['GroupName'] != 'default':
                            self.ec2.delete_security_group(GroupId=sg['GroupId'])
                            logger.info(f"Deleted security group: {sg['GroupId']}")
                    
                    # Delete route tables
                    route_tables = self.ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
                    for rt in route_tables['RouteTables']:
                        if not rt['Associations'] or not any(assoc['Main'] for assoc in rt['Associations']):
                            self.ec2.delete_route_table(RouteTableId=rt['RouteTableId'])
                            logger.info(f"Deleted route table: {rt['RouteTableId']}")
                    
                    # Delete internet gateway
                    igws = self.ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])
                    for igw in igws['InternetGateways']:
                        self.ec2.detach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw['InternetGatewayId'])
                        self.ec2.delete_internet_gateway(InternetGatewayId=igw['InternetGatewayId'])
                        logger.info(f"Deleted internet gateway: {igw['InternetGatewayId']}")
                    
                    # Delete VPC
                    self.ec2.delete_vpc(VpcId=vpc_id)
                    logger.info(f"Deleted VPC: {vpc_id}")
            except ClientError as e:
                logger.warning(f"Error deleting VPC resources: {e}")
            
            # Delete the services file after successful deletion
            try:
                config_folder = os.getenv('CONFIG_FOLDER', '.')
                services_file_path = os.path.join(config_folder, 'aws_services.json')
                os.remove(services_file_path)
                logger.info(f"{services_file_path} file deleted")
            except FileNotFoundError:
                pass
            
            logger.info("All ChromaDB infrastructure resources deleted successfully!")
            
        except Exception as e:
            logger.error(f"Error during resource deletion: {str(e)}")
            raise

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='ChromaDB AWS Infrastructure Management')
    parser.add_argument('--action', choices=['deploy', 'delete'], default='deploy',
                       help='Action to perform: deploy (default) or delete')
    parser.add_argument('--region', default=None,
                       help='AWS region (default: from .env file or us-east-1)')
    
    args = parser.parse_args()
    
    # Check if AWS credentials are configured
    if not os.getenv('AWS_ACCESS_KEY_ID') or not os.getenv('AWS_SECRET_ACCESS_KEY'):
        print("Error: AWS credentials not found in .env file or environment variables.")
        print("Please update your .env file with valid AWS credentials:")
        print("AWS_ACCESS_KEY_ID=your_access_key")
        print("AWS_SECRET_ACCESS_KEY=your_secret_key")
        exit(1)
    
    infrastructure = ChromaDBInfrastructure(region=args.region)
    
    if args.action == 'deploy':
        print(f"Deploying ChromaDB infrastructure in region: {infrastructure.region}")
        print(f"Project name: {infrastructure.project_name}")
        
        # Initialize services data
        services_data = {'services': {}}
        
        # Deploy the infrastructure
        deployment_result = infrastructure.deploy_with_tracking(services_data)
        
        if deployment_result['success']:
            print("Deployment completed successfully!")
            if deployment_result['load_balancer_dns']:
                print(f"ChromaDB endpoint: http://{deployment_result['load_balancer_dns']}")
        else:
            print("Deployment completed with errors:")
            for error in deployment_result['errors']:
                print(f"  - {error}")
    
    elif args.action == 'delete':
        # Delete all resources
        confirm = input("Are you sure you want to delete ALL ChromaDB infrastructure resources? This action cannot be undone. (yes/no): ")
        if confirm.lower() == 'yes':
            infrastructure.delete_all_resources()
            print("All resources deleted successfully!")
        else:
            print("Deletion cancelled.") 
