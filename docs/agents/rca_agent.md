# Root Cause Analysis (RCA) Agent Documentation

## Overview
The RCA Agent is responsible for analyzing system anomalies and determining their root causes. It uses a combination of LLM capabilities and predefined tools to process anomalies from Kafka and publish analysis results.

## Architecture

### Components
1. **Kafka Consumer**
   - Subscribes to: `logs.anomalies`
   - Group ID: `rca_group`
   - Manual offset control for reliability

2. **Kafka Producer**
   - Publishes to: `logs.rca.output`
   - Configured for reliable delivery (acks=all)
   - Automatic retries

3. **LLM Interface**
   - Uses configured LLM provider (default: Ollama)
   - Model specified by `LLM_MODEL_RCA`
   - Processes anomaly data for analysis

### Configuration

#### Kafka Settings
```python
kafka_config = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "rca_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
    "max.poll.interval.ms": 1800000,
    "session.timeout.ms": 300000,
    "heartbeat.interval.ms": 10000,
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
}
```

#### Environment Variables
- `KAFKA_BOOTSTRAP_SERVERS`: Kafka cluster address
- `LLM_PROVIDER`: LLM provider selection
- `LLM_MODEL_RCA`: Model for analysis
- `LLM_ENDPOINT`: LLM API endpoint

## Tools

### PublishRCAResults
Publishes analysis results to Kafka topic `logs.rca.output`

#### Input Format:
```json
{
    "log_id": "unique_identifier",
    "rca": {
        "summary": "Brief description",
        "detailed_analysis": "Comprehensive analysis",
        "root_causes": [
            {
                "cause": "Identified cause",
                "probability": "HIGH|MEDIUM|LOW",
                "impact_areas": ["area1", "area2"],
                "technical_details": "Technical explanation"
            }
        ]
    },
    "recommended_actions": ["action1", "action2"],
    "severity": "HIGH|MEDIUM|LOW"
}
```

## Prompt Engineering

### System Prompt
The agent uses a carefully crafted system prompt that:
- Defines the agent's role
- Specifies input validation requirements
- Provides response format guidelines
- Includes example interactions

### Response Format
```
Thought: [Analysis reasoning]
Action: PublishRCAResults
Action Input: [RCA JSON]
Observation: [Tool response]
Final Answer: [Status confirmation]
```

## Error Handling
- Validates JSON before publishing
- Reports publishing errors clearly
- No automatic retries on failure
- Logs all operations with DEBUG level

## Best Practices
1. **Monitoring**
   - Watch for consumer lag
   - Monitor processing times
   - Check error rates

2. **Maintenance**
   - Regularly update LLM models
   - Review and tune prompts
   - Adjust Kafka settings as needed

3. **Performance**
   - Use batch processing when possible
   - Monitor memory usage
   - Track LLM response times

## Dependencies
- confluent_kafka
- langchain
- llm_interface (local)
- logging

## Deployment Considerations
1. **Scaling**
   - Can run multiple instances
   - Kafka partitioning for parallelism
   - Consider LLM API limits

2. **Security**
   - Secure Kafka connections
   - API key management
   - Log sanitization

3. **Monitoring**
   - Use logging for debugging
   - Track success/failure rates
   - Monitor resource usage
