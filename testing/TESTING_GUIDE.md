# ChromaDB Testing Guide

This guide explains how to run tests and what each test validates in the ChromaDB production environment.

## 🧪 Testing Overview

The testing suite validates that your ChromaDB deployment can handle **1000+ concurrent agents with sub-200ms retrieval times** in production.

## 📋 Test Files

### **1. `connectivity_test.py`** - Health Checks
**Purpose**: Verify basic connectivity and system health

**What it tests:**
- AWS CLI connectivity and permissions
- Load balancer health and reachability
- ECS service status and running tasks
- RAG manager connectivity
- CloudWatch metrics access

**When to run:**
- After deployment to verify everything is working
- Before running performance tests
- When troubleshooting connectivity issues

### **2. `rag_performance_test.py`** - Performance Validation
**Purpose**: Test RAG agent performance under various load conditions

**What it tests:**
- **Test 1**: Basic connectivity and health check
- **Test 2**: Latency benchmark (single user, 100 queries)
- **Test 3**: Concurrent users (100 users, 10 requests each)
- **Test 4**: High load scenario (1000+ concurrent agents)
- **Test 5**: Stress test (2000 burst requests)

**When to run:**
- After connectivity tests pass
- To validate performance requirements
- Before going to production
- For capacity planning

## 🚀 How to Run Tests

### **Prerequisites**
1. **Deploy infrastructure first**: `python deploy.py`
2. **Wait for service to be ready**: 5-10 minutes after deployment
3. **Ensure AWS credentials are configured**

### **Step 1: Run Connectivity Tests**
```bash
# From project root directory
python testing/connectivity_test.py
```

**Expected Output:**
```
🔍 ChromaDB Connectivity Test Suite
==================================================
📋 Deployment Info:
   Load Balancer: chromadb-production-alb-1234567890.us-east-1.elb.amazonaws.com
   ECS Cluster: chromadb-production-cluster
   ECS Service: chromadb-production-service

==================== AWS CLI Connectivity ====================
🔄 Checking AWS credentials
✅ Success

==================== Load Balancer Health ====================
🔄 Testing basic connectivity
✅ Success

==================== ECS Service Status ====================
🔄 Checking ECS service status
✅ Success

==================== RAG Manager Connectivity ====================
✅ RAG manager initialized
✅ Health check passed: {'status': 'healthy'}
✅ Query successful: 1 results
   Response time: 45.2ms

==================== CloudWatch Metrics ====================
🔄 Testing CloudWatch access
✅ Success

📋 CONNECTIVITY TEST SUMMARY
============================================================
Tests Passed: 5/5
Success Rate: 100.0%
🎉 ALL TESTS PASSED: System is ready for performance testing!
```

### **Step 2: Run Performance Tests**
```bash
# From project root directory
python testing/rag_performance_test.py
```

**Expected Output:**
```
🧪 RAG Performance Testing Suite
==================================================
🔧 Initializing RAG managers...
✅ RAG managers initialized using configuration utility
📚 Setting up test collection: test_collection
✅ Added 10 documents to test collection

🔗 Test 1: Basic Connectivity
✅ Health check passed: {'status': 'healthy'}
✅ Basic query successful: 3 results
   Response time: 45.2ms

⚡ Test 2: Latency Benchmark (Single User)
   Progress: 20/100 queries
   Progress: 40/100 queries
   Progress: 60/100 queries
   Progress: 80/100 queries
   Progress: 100/100 queries

📊 Latency Benchmark Results:
   Concurrent Users: 1
   Duration: 12.3s
   Total Requests: 100
   Success Rate: 100.0%
   Requests/Second: 8.1
   Average Response Time: 45.2ms
   P95 Response Time: 67.8ms
   P99 Response Time: 89.3ms
   Meets 200ms Target: ✅ YES

👥 Test 3: Concurrent Users (100 users, 10 requests each)
📊 Concurrent Users (100) Results:
   Concurrent Users: 100
   Duration: 15.7s
   Total Requests: 1000
   Success Rate: 99.8%
   Requests/Second: 63.7
   Average Response Time: 78.9ms
   P95 Response Time: 145.2ms
   P99 Response Time: 189.7ms
   Meets 200ms Target: ✅ YES

🚀 Test 4: High Load Scenario (1000+ Concurrent Agents)
📊 High Load (1000+ Agents) Results:
   Concurrent Users: 1000
   Duration: 45.2s
   Total Requests: 5000
   Success Rate: 99.5%
   Requests/Second: 110.6
   Average Response Time: 125.3ms
   P95 Response Time: 185.7ms
   P99 Response Time: 245.2ms
   Meets 200ms Target: ✅ YES

💥 Test 5: Stress Test (Maximum Load)
📊 Stress Test Results:
   Concurrent Users: 2000
   Duration: 32.1s
   Total Requests: 2000
   Success Rate: 98.7%
   Requests/Second: 62.3
   Average Response Time: 156.8ms
   P95 Response Time: 234.5ms
   P99 Response Time: 298.7ms
   Meets 200ms Target: ❌ NO

============================================================
📋 FINAL TEST SUMMARY
============================================================

Latency Benchmark:
  Success Rate: 100.0%
  P95 Response Time: 67.8ms
  Meets 200ms Target: ✅ YES

Concurrent Users (100):
  Success Rate: 99.8%
  P95 Response Time: 145.2ms
  Meets 200ms Target: ✅ YES

High Load (1000+ Agents):
  Success Rate: 99.5%
  P95 Response Time: 185.7ms
  Meets 200ms Target: ✅ YES

Stress Test:
  Success Rate: 98.7%
  P95 Response Time: 234.5ms
  Meets 200ms Target: ❌ NO

============================================================
⚠️  SOME TESTS FAILED: System may need optimization for production load.
============================================================
```

## 📊 Understanding Test Results

### **Pass/Fail Criteria**
- **✅ PASS**: P95 response time ≤ 200ms
- **❌ FAIL**: P95 response time > 200ms

### **Performance Targets**
- **Success Rate**: > 99%
- **P95 Response Time**: < 200ms
- **P99 Response Time**: < 500ms
- **Throughput**: > 1000 RPS

### **Test Scenarios Explained**

#### **Test 1: Basic Connectivity**
- **Purpose**: Verify the system is working
- **Load**: Single request
- **Expected**: < 100ms response time

#### **Test 2: Latency Benchmark**
- **Purpose**: Measure baseline performance
- **Load**: 1 user, 100 sequential requests
- **Expected**: < 200ms P95 response time

#### **Test 3: Concurrent Users**
- **Purpose**: Test moderate concurrent load
- **Load**: 100 users, 10 requests each (1000 total)
- **Expected**: < 200ms P95 response time

#### **Test 4: High Load (1000+ Agents)**
- **Purpose**: Validate production requirements
- **Load**: 1000 agents, 5 requests each (5000 total)
- **Expected**: < 200ms P95 response time

#### **Test 5: Stress Test**
- **Purpose**: Find system limits
- **Load**: 2000 burst requests
- **Expected**: May exceed 200ms (stress test)

## 🔧 Test Configuration

### **Automatic Configuration**
Tests automatically:
- Load deployment info from `aws_services.json`
- Initialize RAG managers using `chromadb_config.py`
- Setup test collections with sample documents
- Handle both sync and async operations

### **Test Data**
- **Sample Documents**: 10 AI/ML related documents
- **Test Queries**: 10 diverse queries for realistic testing
- **Collection Name**: `test_collection`

## 🚨 Troubleshooting

### **Common Issues**

#### **Import Errors**
```bash
❌ Import error: No module named 'deployment.chromadb_config'
```
**Solution**: Run from project root directory, not from testing folder.

#### **Deployment Not Found**
```bash
❌ No deployment information found!
```
**Solution**: Deploy infrastructure first using `python deploy.py`

#### **Connection Failures**
```bash
❌ RAG manager test failed: Connection refused
```
**Solution**: Wait for ECS service to be fully ready (5-10 minutes after deployment).

#### **High Response Times**
```bash
❌ P95 Response Time: 234.5ms
```
**Solutions**:
- Check if auto-scaling is working
- Verify connection pool settings
- Monitor CPU/memory utilization
- Consider increasing task count

### **Debug Commands**
```bash
# Check ECS service status
aws ecs describe-services --cluster chromadb-production-cluster

# Check service logs
aws logs tail /ecs/chromadb-production-task --follow

# Check load balancer health
aws elbv2 describe-target-health --target-group-arn <target-group-arn>
```

## 📈 Interpreting Results

### **Good Results**
- All connectivity tests pass
- P95 response time < 200ms for normal load tests
- Success rate > 99%
- System handles 1000+ concurrent agents

### **Needs Optimization**
- P95 response time > 200ms
- Success rate < 99%
- High error rates
- Slow response times under load

### **Action Items**
1. **If connectivity fails**: Check deployment and AWS credentials
2. **If performance is poor**: Review auto-scaling and resource allocation
3. **If stress test fails**: This is expected - stress test pushes limits
4. **If all tests pass**: System is ready for production!

## 🎯 Success Criteria

### **Production Ready**
- ✅ All connectivity tests pass
- ✅ P95 response time < 200ms for high load test
- ✅ Success rate > 99%
- ✅ System handles 1000+ concurrent agents

### **Ready to Deploy**
```bash
# 1. Run connectivity tests
python testing/connectivity_test.py

# 2. If all pass, run performance tests
python testing/rag_performance_test.py

# 3. If performance tests pass, system is ready!
```

## 📋 Test Execution Checklist

- [ ] Infrastructure deployed (`python deploy.py`)
- [ ] Service is ready (5-10 minutes after deployment)
- [ ] AWS credentials configured
- [ ] Running from project root directory
- [ ] Connectivity tests pass
- [ ] Performance tests meet targets
- [ ] System ready for production

---

**Ready to test?** Start with `python testing/connectivity_test.py`!
