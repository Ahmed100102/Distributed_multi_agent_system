# Retrieval Agent

The `retrieval_agent.py` script is the entry and exit point of the log processing pipeline. It is responsible for retrieving new error logs from Elasticsearch, initiating the analysis process, and storing the final remediation results back into Elasticsearch.

## Overview

The Retrieval Agent has two primary functions:

1.  **Error Log Retrieval:** It periodically queries Elasticsearch for new error and warning logs, formats them, and publishes them to the `logs.anomalies` Kafka topic to be picked up by the RCA Agent.

2.  **Remediation Storage:** It consumes the final remediation plans from the `logs.remediation` Kafka topic and stores them in a dedicated Elasticsearch index for persistence and future analysis.

## Architecture

The agent is built with the following components:

-   **Elasticsearch:** Used as both the source for new logs and the destination for the final results.
-   **Kafka:** For communication with the other agents in the pipeline.
-   **FastAPI:** To provide a `/health` endpoint for monitoring.
-   **Asyncio:** The agent is built on Python's `asyncio` library to handle concurrent operations efficiently, such as querying Elasticsearch and consuming from Kafka.

## Asynchronous Operations

The use of `asyncio` allows the agent to perform multiple tasks concurrently without blocking. The main functions, `run_check_new_errors` and `run_consume_remediation`, run in separate asynchronous loops, allowing the agent to continuously check for new logs while simultaneously processing incoming remediation results.

## Workflow

The agent operates in two parallel workflows:

### 1. Error Log Retrieval

-   The `check_new_errors` function queries Elasticsearch for new logs with `log_level` "ERROR" or "WARN".
-   It formats the logs into a standardized JSON format.
-   New logs are published to the `logs.anomalies` Kafka topic.
-   The agent keeps track of processed log IDs to avoid duplicates.

### 2. Remediation Storage

-   The `consume_remediation` function consumes messages from the `logs.remediation` topic.
-   It uses `async_bulk` to efficiently store the remediation results in a dedicated Elasticsearch index (`observix-results-*`).
-   The agent also creates and manages an Elasticsearch index template (`observix-template`) to ensure the data is indexed correctly.

## Elasticsearch Integration

The agent interacts with Elasticsearch in several ways:

-   **Querying:** It uses the `es.search` method to find new error logs.
-   **Indexing:** It uses `async_bulk` to store remediation results.
-   **Index Management:** It creates and updates an index template to define the mappings for the `observix-results-*` indices.

## Error Handling

The agent includes error handling for both Elasticsearch and Kafka operations. If an error occurs, it is logged, and the agent will continue to run. The `/health` endpoint will reflect the status of the connections.

## Metrics and Monitoring

Metrics are collected and saved to `retrieval_metrics.json`. The `/health` endpoint (running on port 8000) provides a detailed view of the agent's status.

### Generated Metrics

The following metrics are generated and can be viewed through the `/health` endpoint:

-   **`total_logs_processed`:** The total number of new logs processed by the agent.
-   **`total_errors`:** The total number of errors encountered by the agent.
-   **`error_rate`:** The rate of errors, calculated as `total_errors / total_runs`.
-   **`service_uptime`:** The number of hours the agent has been running.
-   **`last_run`:** Information about the last run, including its status, timestamp, function, and duration.
-   **`recent_runs`:** A list of the last 10 runs with detailed information for each.
-   **`average_durations`:** The average duration of the `check_new_errors` and `consume_remediation` functions, including a breakdown of the steps within each function.

## Configuration

The agent is configured through environment variables:

-   `ELASTICSEARCH_URL`: The URL of the Elasticsearch cluster.
-   `KAFKA_BOOTSTRAP_SERVERS`: The address of the Kafka bootstrap servers.

## Persistence

To avoid duplicate processing, the agent maintains two sets of processed IDs:

-   `processed_ids`: For logs retrieved from Elasticsearch.
-   `processed_remediation_ids`: For remediation results stored in Elasticsearch.

These sets are saved to `processed_ids.json` and `processed_remediation_ids.json` respectively, and are loaded on startup.
