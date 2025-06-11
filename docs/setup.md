# Project Setup and Configuration Documentation

## Overview
This document covers the setup, configuration, and deployment of the Observix log analysis system. It includes environment setup, dependencies, and configuration files.

## Installation

### Prerequisites
- Python 3.8 or higher
- Kafka
- Elasticsearch
- LLM provider (Ollama, OpenAI, or Google Gemini)

### Steps

1. **Clone Repository**
   ```bash
   git clone <repository-url>
   cd Project0
   ```

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   
   Or using setup.py:
   ```bash
   pip install -e .
   ```

## Dependencies

### Python Packages
From `requirements.txt`:
```
confluent-kafka==2.10.0
elasticsearch==8.15.0
langchain==0.3.25
langchain-community==0.3.24
langchain-openai==0.3.19
langchain-google-genai==2.1.5
pydantic==2.11.5
langchain-core==0.3.63
langchain-text-splitters==0.3.8
```

## Environment Configuration

### Environment Variables
Set using `set_env.sh`:
```bash
KAFKA_BOOTSTRAP_SERVERS="localhost:9092"
ELASTICSEARCH_URL="http://localhost:9200"
LLM_PROVIDER="ollama"
LLM_ENDPOINT="http://localhost:11434"
LLM_MODEL_RCA="qwen3:4b"
LLM_MODEL_REMEDIATION="llama3.2:3b"
```

### Usage
```bash
source set_env.sh
```

## Project Structure

```
Project0/
├── config/
│   └── kafka_logstash.conf    # Logstash configuration for Kafka
├── docs/
│   └── agents/               # Agent documentation
├── src/
│   ├── agents/              # Agent implementations
│   ├── api.py              # API endpoints
│   └── create_kafka_topics.py # Kafka setup
├── tests/
│   └── test_agents.py      # Agent test suite
├── requirements.txt        # Python dependencies
├── setup.py               # Package setup
└── set_env.sh            # Environment configuration
```

## Component Setup

### 1. Environment Setup
1. Configure environment variables
2. Install dependencies
3. Verify LLM provider access

### 2. Kafka Setup
1. Start Kafka server
2. Create required topics
3. Verify topic creation

### 3. Elasticsearch Setup
1. Start Elasticsearch
2. Create indices
3. Verify connectivity

## Running the System

### 1. Start Services
```bash
# Start Kafka and Elasticsearch (adjust based on your installation)
systemctl start kafka
systemctl start elasticsearch
```

### 2. Set Environment
```bash
source set_env.sh
```

### 3. Create Kafka Topics
```bash
python src/create_kafka_topics.py
```

### 4. Start Agents
```bash
# In separate terminals:
python src/agents/rca_agent.py
python src/agents/remediation_agent.py
python src/agents/retrieval_agent.py
```

## Monitoring and Maintenance

### 1. Log Monitoring
- Agent logs in debug mode
- Kafka consumer group status
- Elasticsearch indices

### 2. Performance Monitoring
- Consumer lag
- Processing times
- Resource usage

### 3. Maintenance Tasks
- Log rotation
- Index cleanup
- Backup strategy

## Development Setup

### 1. Development Dependencies
```bash
pip install -r requirements.txt
```

### 2. Running Tests
```bash
python -m pytest tests/
```

### 3. Code Style
- Follow PEP 8
- Use type hints
- Document functions

## Security Considerations

### 1. Network Security
- Secure Kafka connections
- Elasticsearch security
- API authentication

### 2. Data Security
- Encryption at rest
- Secure credentials
- Access control

### 3. API Security
- Rate limiting
- Input validation
- Authentication

## Troubleshooting

### Common Issues

1. **Connection Issues**
   - Check service status
   - Verify network connectivity
   - Check credentials

2. **Performance Issues**
   - Monitor resource usage
   - Check consumer lag
   - Review log files

3. **Data Issues**
   - Validate input format
   - Check index mappings
   - Verify topic configurations

## Support and Resources

### Documentation
- Agent documentation in `/docs/agents/`
- API documentation
- Configuration guides

### Logging
- Debug level logging available
- Structured log format
- Centralized log collection

### Monitoring
- Health check endpoints
- Performance metrics
- System statistics
