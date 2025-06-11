# Remediation Agent Documentation

## Overview
The Remediation Agent is responsible for creating and publishing remediation plans based on Root Cause Analysis (RCA) results. It processes RCA outputs and generates actionable steps for issue resolution.

## Architecture

### Components
1. **Kafka Consumer**
   - Subscribes to: `logs.rca.output`
   - Group ID: `remediation_group`
   - Manual offset control

2. **Kafka Producer**
   - Publishes to: `logs.remediation`
   - Reliable delivery configuration
   - Automatic retries

3. **LLM Interface**
   - Uses specified LLM provider
   - Model: `LLM_MODEL_REMEDIATION`
   - Generates remediation plans

### Configuration

#### Kafka Settings
```python
kafka_config = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "remediation_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
    "max.poll.interval.ms": 1800000,
    "session.timeout.ms": 300000,
    "heartbeat.interval.ms": 10000
}
```

#### Producer Settings
```python
producer_config = {
    "acks": "all",
    "retries": 3,
    "delivery.timeout.ms": 30000
}
```

## Tools

### PublishKafkaRemediation
Publishes remediation plans to Kafka topic `logs.remediation`

#### Input Format:
```json
{
    "log_id": "unique_identifier",
    "remediation": {
        "steps": [
            {
                "step_number": 1,
                "action": "Specific action to take",
                "purpose": "Why this action helps"
            }
        ],
        "estimated_time": "30m",
        "required_skills": ["skill1", "skill2"],
        "precautions": ["precaution1", "precaution2"]
    },
    "priority": "HIGH|MEDIUM|LOW"
}
```

## Prompt Engineering

### System Prompt
The agent uses a structured prompt that:
- Defines the agent's purpose
- Specifies validation requirements
- Provides format guidelines
- Includes example interactions

### Response Format
```
Thought: [Remediation planning process]
Action: PublishKafkaRemediation
Action Input: [Remediation JSON]
Observation: [Tool response]
Final Answer: [Status confirmation]
```

## Error Handling
1. **Input Validation**
   - JSON schema validation
   - Required field checks
   - Data type verification

2. **Publishing Errors**
   - Clear error reporting
   - No automatic retries
   - Error logging

3. **LLM Errors**
   - Timeout handling
   - Response validation
   - Fallback strategies

## Best Practices

### 1. Remediation Plan Quality
- Include clear, actionable steps
- Specify prerequisites
- Note potential risks
- Estimate time requirements

### 2. Performance
- Batch processing when possible
- Monitor LLM response times
- Track message processing rates

### 3. Reliability
- Validate all outputs
- Handle edge cases
- Maintain idempotency

## Environment Variables
- `KAFKA_BOOTSTRAP_SERVERS`: Kafka connection
- `LLM_PROVIDER`: LLM service provider
- `LLM_MODEL_REMEDIATION`: Specific model
- `LLM_ENDPOINT`: API endpoint

## Dependencies
- confluent_kafka
- langchain
- llm_interface (local)
- logging

## Deployment Considerations

### 1. Scaling
- Multiple instances possible
- Kafka partition assignment
- LLM API capacity planning

### 2. Monitoring
- Consumer lag
- Processing times
- Error rates
- Resource usage

### 3. Security
- Secure Kafka connections
- API key management
- Input sanitization

## Testing
1. **Unit Tests**
   - Tool functionality
   - JSON validation
   - Error handling

2. **Integration Tests**
   - Kafka connectivity
   - LLM interactions
   - End-to-end flows

3. **Load Tests**
   - Message throughput
   - Response times
   - Resource utilization
