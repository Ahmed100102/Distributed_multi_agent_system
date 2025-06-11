# Kafka Topics Setup Documentation

## Overview
This module is responsible for creating the required Kafka topics for the log analysis system. It ensures that all necessary topics exist with the correct configuration before the agents start processing.

## Topics Created

### 1. logs.anomalies
- Purpose: Receives initial log anomalies for analysis
- Consumers: RCA Agent
- Configuration:
  - Partitions: 1
  - Replication Factor: 1

### 2. logs.rca.output
- Purpose: Stores Root Cause Analysis results
- Publishers: RCA Agent
- Consumers: Remediation Agent
- Configuration:
  - Partitions: 1
  - Replication Factor: 1

### 3. logs.remediation
- Purpose: Stores remediation plans
- Publishers: Remediation Agent
- Consumers: Retrieval Agent
- Configuration:
  - Partitions: 1
  - Replication Factor: 1

## Usage

### Command Line
```bash
python src/create_kafka_topics.py
```

### Programmatic Usage
```python
from create_kafka_topics import create_kafka_topics
create_kafka_topics()
```

## Configuration
- Bootstrap Servers: localhost:9092 (default)
- Topic settings can be modified in the code

## Error Handling
- Attempts to create each topic independently
- Reports success/failure for each topic
- Continues with remaining topics if one fails

## Best Practices

### 1. Pre-deployment
- Run before starting any agents
- Verify topic creation success
- Check topic configurations

### 2. Production Settings
Consider adjusting for production:
- Increase replication factor
- Adjust partition count
- Set retention policies
- Configure cleanup policies

### 3. Monitoring
- Check topic existence
- Verify configurations
- Monitor partition count
- Check replication status

## Dependencies
- confluent_kafka.admin
- AdminClient
- NewTopic

## Security Considerations
1. **Access Control**
   - Set appropriate ACLs
   - Configure authentication
   - Implement authorization

2. **Network Security**
   - Secure connections
   - SSL/TLS configuration
   - Network isolation

## Troubleshooting

### Common Issues
1. **Connection Failures**
   - Check Kafka server status
   - Verify network connectivity
   - Check firewall settings

2. **Permission Issues**
   - Verify user permissions
   - Check ACL configuration
   - Review security settings

3. **Topic Existence**
   - Handle duplicate creation
   - Check cluster health
   - Verify broker status
