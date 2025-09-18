# Create Kafka Topics

The `create_kafka_topics.py` script is a utility for setting up the necessary Kafka topics for the Distributed Multi-Agent System. It ensures that the topics are created with the correct configurations for the agents to communicate with each other.

## Overview

This script connects to a Kafka cluster and creates the following topics:

-   `logs.anomalies`: For publishing new error logs retrieved from Elasticsearch.
-   `logs.rca.output`: For publishing the results of the Root Cause Analysis (RCA).
-   `logs.remediation`: For publishing the final remediation plans.

If the topics already exist, the script will verify their configuration.

## Usage

To run the script, simply execute it from the command line:

```bash
python src/create_kafka_topics.py
```

## Topics Created

The script creates the following topics with the specified configurations:

| Topic Name          | Partitions | Replication Factor | Cleanup Policy | Retention (ms) | Min In-sync Replicas |
| ------------------- | ---------- | ------------------ | -------------- | -------------- | -------------------- |
| `logs.anomalies`    | 1          | 1                  | `delete`       | 3600000        | 1                    |
| `logs.rca.output`   | 1          | 1                  | `delete`       | 3600000        | 1                    |
| `logs.remediation`  | 1          | 1                  | `delete`       | 3600000        | 1                    |

## Verification

After attempting to create the topics, the script will:

1.  **Verify Configuration:** If a topic already exists, it will print the existing configuration.
2.  **List Topics:** It will list all the topics in the cluster to confirm that the required topics are present.