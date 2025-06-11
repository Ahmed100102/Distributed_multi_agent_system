# Retrieval Agent Documentation

## Overview
The Retrieval Agent manages the storage and retrieval of log analysis data using Elasticsearch. It processes remediation plans from Kafka and maintains a searchable index of all analysis results.

## Architecture

### Components
1. **Elasticsearch Client**
   - Manages document storage
   - Handles search operations
   - Maintains index templates

2. **Kafka Consumer**
   - Topic: `logs.remediation`
   - Group: `retrieval_group`
   - Processes remediation plans

### Elasticsearch Configuration

#### Index Template
```json
{
    "index_patterns": ["observix-*"],
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1,
            "refresh_interval": "1s",
            "analysis": {
                "analyzer": {
                    "log_analyzer": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["lowercase", "stop"]
                    }
                }
            }
        }
    }
}
```

#### Document Mapping
```json
{
    "properties": {
        "timestamp": {"type": "date"},
        "log_id": {"type": "keyword"},
        "rca": {
            "properties": {
                "summary": {"type": "text", "analyzer": "log_analyzer"},
                "detailed_analysis": {"type": "text", "analyzer": "log_analyzer"},
                "root_causes": {
                    "type": "nested",
                    "properties": {
                        "cause": {"type": "text", "analyzer": "log_analyzer"},
                        "probability": {"type": "keyword"},
                        "impact_areas": {"type": "keyword"},
                        "technical_details": {"type": "text", "analyzer": "log_analyzer"}
                    }
                }
            }
        },
        "remediation_plan": {
            "type": "nested",
            "properties": {
                "steps": {
                    "type": "nested",
                    "properties": {
                        "step_number": {"type": "integer"},
                        "action": {"type": "text", "analyzer": "log_analyzer"},
                        "purpose": {"type": "text", "analyzer": "log_analyzer"}
                    }
                }
            }
        },
        "severity": {"type": "keyword"},
        "category": {"type": "keyword"},
        "metadata": {
            "properties": {
                "source_system": {"type": "keyword"},
                "log_level": {"type": "keyword"}
            }
        }
    }
}
```

## Core Functions

### check_new_errors(from_time: str = "") -> str
Queries Elasticsearch for new error entries since the specified time.

#### Parameters:
- `from_time` (str): ISO format timestamp

#### Returns:
- JSON string with query results or error message

### create_or_update_index_template()
Creates or updates the Elasticsearch index template for log data.

### store_remediation(data: str) -> str
Stores remediation plans in Elasticsearch.

#### Parameters:
- `data` (str): JSON string containing remediation plan

#### Returns:
- Success/failure status as JSON string

## Error Handling
1. **Elasticsearch Errors**
   - ApiError handling
   - TransportError handling
   - Connection retry logic

2. **Kafka Errors**
   - Consumer errors
   - Message processing errors
   - Offset management

## Example Queries

### High-Severity Issues
```json
GET observix-remediation-*/_search
{
  "query": {
    "bool": {
      "must": [
        { "term": { "severity": "HIGH" } }
      ]
    }
  }
}
```

### Root Cause Search
```json
GET observix-remediation-*/_search
{
  "query": {
    "nested": {
      "path": "rca.root_causes",
      "query": {
        "match": {
          "rca.root_causes.cause": "network partition"
        }
      }
    }
  }
}
```

## Best Practices

### 1. Index Management
- Regular index cleanup
- Optimize refresh intervals
- Monitor shard size
- Set appropriate replicas

### 2. Query Optimization
- Use appropriate field types
- Leverage cached queries
- Implement pagination
- Use scroll for large results

### 3. Data Management
- Implement data retention
- Use index lifecycle policies
- Regular backup strategy
- Monitor disk usage

## Environment Variables
- `ELASTICSEARCH_URL`: Elasticsearch endpoint
- `KAFKA_BOOTSTRAP_SERVERS`: Kafka connection

## Dependencies
- elasticsearch
- confluent_kafka
- datetime
- logging

## Deployment Considerations

### 1. Scaling
- Elasticsearch cluster sizing
- Shard allocation strategy
- Consumer group planning

### 2. Monitoring
- Index health
- Search performance
- Consumer lag
- Disk usage

### 3. Security
- Elasticsearch authentication
- Index-level security
- Network security
- Data encryption

## Performance Tips
1. Bulk indexing when possible
2. Optimize refresh intervals
3. Use appropriate mapping types
4. Monitor and adjust JVM heap
5. Regular performance testing
