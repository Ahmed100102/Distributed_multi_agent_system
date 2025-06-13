import os
import time
import json
from datetime import datetime, UTC
from elasticsearch import Elasticsearch, ApiError, TransportError
from confluent_kafka import Consumer, Producer, KafkaError
import logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Elasticsearch client
es = Elasticsearch(
    os.getenv("ELASTICSEARCH_URL", "http://10.254.117.52:9200"),
    verify_certs=False,
    ssl_show_warn=False
)

# Kafka config
kafka_config = {
    "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
}
consumer = Consumer({
    **kafka_config,
    "group.id": "retrieval_group",
    "auto.offset.reset": "earliest"
})
consumer.subscribe(["logs.remediation"])
producer = Producer(kafka_config)

# Track last successful query time and processed log IDs
last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'
processed_ids = set()

def check_new_errors(from_time: str = "") -> str:
    global last_query_timestamp
    try:
        # Use provided from_time or last_query_timestamp
        search_time = from_time if from_time else last_query_timestamp
        if from_time:
            try:
                datetime.fromisoformat(from_time.replace('Z', '+00:00'))
            except ValueError:
                logger.error("Invalid from_time format: %s. Expected ISO 8601 UTC.", from_time)
                return json.dumps({"status": "error", "message": "Invalid from_time format. Use ISO 8601 UTC."})

        # Build query based on Logstash config
        es_query = {
            "bool": {
                "filter": [
                    {"terms": {"log_level.keyword": ["ERROR", "WARN"]}},
                    {"range": {"@timestamp": {"gt": search_time}}},
                    {"term": {"type.keyword": "vgmlog"}}
                ],
                "must_not": [
                    {"term": {"tags.keyword": "_kafka_grok_failure"}}
                ]
            }
        }

        # Check if index exists
        index_pattern = "logstash-*"
        if not es.indices.exists(index=index_pattern):
            logger.warning("No indices found for pattern: %s", index_pattern)
            return json.dumps({"status": "success", "logs": [], "total": 0})

        # Execute query
        logger.debug("Elasticsearch query: %s", es_query)
        result = es.search(
            index=index_pattern,
            query=es_query,
            sort=[{"@timestamp": {"order": "desc"}}],
            size=100
        )

        # Parse results
        hits = result.get("hits", {}).get("hits", [])
        logs = [
            {
                "_id": hit["_id"],
                "timestamp": hit["_source"].get("@timestamp", ""),
                "log_level": hit["_source"].get("log_level", ""),
                "log_message": hit["_source"].get("log_message", ""),
                "java_class": hit["_source"].get("java_class", ""),
                "type": hit["_source"].get("type", ""),
                "anomaly_type": hit["_source"].get("anomaly_type", ""),
                "tags": hit["_source"].get("tags", [])
            }
            for hit in hits
        ]

        # Send new ERROR/WARN logs to logs.anomalies
        new_logs = []
        for log in logs:
            if log["_id"] not in processed_ids:
                try:
                    producer.produce(
                        "logs.anomalies",
                        key=log["_id"].encode("utf-8"),
                        value=json.dumps(log).encode("utf-8")
                    )
                    producer.flush()
                    processed_ids.add(log["_id"])
                    logger.info("Sent log to logs.anomalies: %s", log["_id"])
                    new_logs.append(log)
                except KafkaError as e:
                    logger.error("Failed to send log to logs.anomalies: %s", str(e))
            else:
                logger.debug("Skipped duplicate log: %s", log["_id"])

        # Update timestamp for next query
        last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'

        logger.info("Found %d new errors", len(new_logs))
        return json.dumps({"status": "success", "logs": new_logs, "total": len(new_logs)}, indent=2)

    except ApiError as e:
        logger.error("Elasticsearch API error: %s", str(e))
        return json.dumps({"status": "error", "message": str(e)})
    except TransportError as e:
        logger.error("Elasticsearch transport error: %s", str(e))
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        logger.error("Unexpected error querying Elasticsearch: %s", str(e))
        return json.dumps({"status": "error", "message": str(e)})

def create_or_update_index_template():
    """Create or update index template for logs and analysis results"""
    template_name = "observix-template"
    template_body = {
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
            },
            "mappings": {
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
        }
    }
    
    try:
        es.indices.put_index_template(name=template_name, body=template_body)
        logger.info("Index template created/updated successfully")
    except Exception as e:
        logger.error("Failed to create/update index template: %s", str(e))

def store_remediation(data: str) -> str:
    try:
        parsed_data = json.loads(data)
        
        # Create index name with prefix for better organization
        index_name = f"observix-remediation-{datetime.now(UTC).strftime('%Y.%m')}"
        
        # Add timestamp if not present
        if "timestamp" not in parsed_data:
            parsed_data["timestamp"] = datetime.now(UTC).isoformat()
        
        # Create index if it doesn't exist (template will be applied automatically)
        if not es.indices.exists(index=index_name):
            es.indices.create(index=index_name)
            logger.info("Created new index: %s", index_name)
        
        # Store document with log_id as document ID for deduplication
        es.index(
            index=index_name,
            id=parsed_data["log_id"],
            document=parsed_data
        )
        
        logger.info("Stored remediation in %s with ID %s", index_name, parsed_data["log_id"])
        return f"Stored remediation in {index_name}"
        
    except Exception as e:
        logger.error("Error storing remediation: %s", str(e))
        return f"Error storing remediation: {str(e)}"

def consume_remediation():
    try:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            return "No remediation messages"
        if msg.error():
            return f"Kafka error: {msg.error()}"
        result = json.loads(msg.value().decode("utf-8"))
        return store_remediation(json.dumps(result))
    except Exception as e:
        logger.error("Error consuming remediation: %s", str(e))
        return f"Error consuming remediation: {str(e)}"

def main():
    import sys
    from_time = sys.argv[1] if len(sys.argv) > 1 else ""
    if from_time:
        # Run one-time historical query
        logger.info("Running historical query with from_time: %s", from_time)
        error_result = check_new_errors(from_time)
        if error_result and "error" not in error_result.lower():
            print(error_result)
        return

    # Create/update index template on startup
    create_or_update_index_template()

    # Run live loop
    logger.info("Starting live retrieval loop")
    while True:
        error_result = check_new_errors()
        if error_result and "error" not in error_result.lower():
            print(error_result)

        remediation_result = consume_remediation()
        if remediation_result != "No remediation messages":
            print(remediation_result)

        time.sleep(30)

if __name__ == "__main__":
    main()