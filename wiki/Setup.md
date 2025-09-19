# Project Setup and Configuration Documentation

## Overview
This document covers the setup, configuration, and deployment of the Distributed Multi-Agent System. It includes environment setup, dependencies, and configuration files.

## Installation

### Prerequisites
- Python 3.8 or higher
- Kafka
- Elasticsearch
- An LLM provider (Ollama, OpenAI, or Google Gemini)

### Steps

1. **Clone Repository**
   ```bash
   git clone <repository-url>
   cd Distributed_multi_agent_system
   ```

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

## Dependencies

### Python Packages
From `requirements.txt`:
```
confluent-kafka
elasticsearch
langchain
langchain-community
langchain-openai
langchain-google-genai
pydantic
langchain-core
fastapi
uvicorn
python-dotenv
requests
```

## Environment Configuration

### Environment Variables

You can configure the application using a `.env` file in the root of the project or by setting environment variables directly.

Here is an example of a `.env` file:

```
KAFKA_BOOTSTRAP_SERVERS="localhost:9092"
ELASTICSEARCH_URL="http://localhost:9200"
MODEL_RUNTIME="gemini" # or ollama, groq, openrouter

# Add the API key for your chosen provider
GEMINI_API_KEY="your_gemini_api_key"
# GROQ_API_KEY="your_groq_api_key"
# OR_API_KEY="your_openrouter_api_key"
```

## Project Structure

```
Distributed_multi_agent_system/
├── config/
│   └── kafka_logstash.conf    # Logstash configuration for Kafka
├── docs/
│   └── agents/               # Agent documentation
├── src/
│   ├── agents/              # Agent implementations
│   │   ├── __init__.py
│   │   ├── langgraph_observibot.py
│   │   ├── langgraph_observibot2.py
│   │   ├── llm_interface.py
│   │   ├── rca_agent.py
│   │   ├── remediation_agent.py
│   │   └── retrieval_agent.py
│   └── create_kafka_topics.py # Kafka setup
├── tests/
│   └── test_agents.py      # Agent test suite
├── requirements.txt        # Python dependencies
└── observibot_metrics.json # Metrics file for Observibot
```

## Component Setup

### 1. Environment Setup
1. Create a `.env` file or set the environment variables.
2. Install dependencies from `requirements.txt`.
3. Verify access to your chosen LLM provider.

### 2. Kafka Setup
1. Start your Kafka server.
2. Create the required topics by running the `create_kafka_topics.py` script.
3. Verify that the topics have been created.

### 3. Elasticsearch Setup
1. Start your Elasticsearch server.
2. The Retrieval Agent will automatically create the necessary index templates and indices.
3. Verify connectivity by checking the health endpoint of the Retrieval Agent.

## Running the System

### 1. Start Services

Make sure Kafka and Elasticsearch are running.

### 2. Set Environment

If you are not using a `.env` file, make sure to set the environment variables in your terminal.

### 3. Create Kafka Topics

```bash
python src/create_kafka_topics.py
```

### 4. Start Agents

Run each agent in a separate terminal:

```bash
python src/agents/retrieval_agent.py
python src/agents/rca_agent.py
python src/agents/remediation_agent.py
python src/agents/langgraph_observibot2.py # Or langgraph_observibot.py
```

## Monitoring and Maintenance

### 1. Log Monitoring
- Each agent outputs logs to the console.
- You can also monitor the Kafka consumer group status and Elasticsearch indices.

### 2. Performance Monitoring
- Check the `/health` endpoint of each agent for performance metrics.
- Monitor consumer lag in Kafka.

### 3. Maintenance Tasks
- Manage log rotation and index cleanup in Elasticsearch.
- Implement a backup strategy for your data.

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
- Follow PEP 8 guidelines.
- Use type hints for function signatures.
- Add docstrings to functions and classes.

## Security Considerations

### 1. Network Security
- Secure your Kafka and Elasticsearch connections.
- Use firewalls to restrict access to the agent's API endpoints.

### 2. Data Security
- Enable encryption at rest for Elasticsearch.
- Securely manage your LLM API keys and other credentials.

## Troubleshooting

### Common Issues

1.  **Connection Issues:**
    -   Check the status of Kafka and Elasticsearch.
    -   Verify your network connectivity and firewall rules.
    -   Ensure your API keys and other credentials are correct.

2.  **Performance Issues:**
    -   Monitor the resource usage of each agent.
    -   Check for consumer lag in Kafka.
    -   Review the agent logs for any errors or warnings.

3.  **Data Issues:**
    -   Validate the format of the data being sent to Kafka.
    -   Check the index mappings in Elasticsearch.
    -   Verify the topic configurations in Kafka.