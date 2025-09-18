# Running the Agents

This document provides instructions on how to run the different agents of the Distributed Multi-Agent System.

## Prerequisites

Before running the agents, make sure you have:

1.  **Installed all the dependencies** from the `requirements.txt` file.
2.  **Set up the environment variables** in a `.env` file or directly in your terminal.
3.  **Started Kafka and Elasticsearch** services.
4.  **Created the Kafka topics** by running the `src/create_kafka_topics.py` script.

## Running the Agents

Each agent runs as a separate process. You will need to open a new terminal for each agent.

### 1. Retrieval Agent

This agent retrieves new error logs from Elasticsearch and publishes them to the `logs.anomalies` Kafka topic. It also stores the final remediation results back into Elasticsearch.

```bash
python src/agents/retrieval_agent.py
```

The Retrieval Agent will be available at `http://localhost:8000`.

### 2. RCA (Root Cause Analysis) Agent

This agent consumes log anomalies from the `logs.anomalies` topic, performs a root cause analysis using an LLM, and publishes the results to the `logs.rca.output` topic.

```bash
uvicorn src.agents.rca_agent:app --host 0.0.0.0 --port 8001
```

The RCA Agent will be available at `http://localhost:8001`.

### 3. Remediation Agent

This agent consumes RCA results from the `logs.rca.output` topic, generates a remediation plan using an LLM, and publishes the plan to the `logs.remediation` topic.

```bash
uvicorn src.agents.remediation_agent:app --host 0.0.0.0 --port 8002
```

The Remediation Agent will be available at `http://localhost:8002`.

### 4. Observibot Agent (V2)

This agent provides a conversational interface for querying and analyzing data from Elasticsearch.

```bash
uvicorn src.agents.langgraph_observibot2:app --host 0.0.0.0 --port 8003
```

The Observibot Agent will be available at `http://localhost:8003`.

### 5. Observibot Agent (V1)

This is the first version of the Observibot agent.

```bash
uvicorn src.agents.langgraph_observibot:app --host 0.0.0.0 --port 8004
```

The Observibot Agent (V1) will be available at `http://localhost:8004`.
