import os
import time
import json
import sys
from datetime import datetime, UTC
from elasticsearch import Elasticsearch, ApiError, TransportError
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
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
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,  # Manual offset control for remediation
    "max.poll.interval.ms": "1800000",  # 30 minutes for processing
    "session.timeout.ms": "300000",     # 5 minutes
    "heartbeat.interval.ms": "10000",   # 10 seconds
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
})
consumer.subscribe(["logs.remediation"])
producer = Producer(kafka_config)

# Track last successful query time and processed log IDs
last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'
processed_ids = set()
processed_remediation_ids = set()  # Separate set for remediation deduplication

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

        # Index patterns from Logstash config
        index_patterns = [
            "logstash-fiscd-*",
            "logstash-vgm-*",
            "logstash-netprobe-*",
            "logstash-gateway-*"
        ]

        # Build query for ERROR and WARN logs
        es_query = {
            "bool": {
                "filter": [
                    {"terms": {"log_level.keyword": ["ERROR", "WARN"]}},
                    {"range": {"@timestamp": {"gt": search_time}}},
                    {"terms": {"type.keyword": ["FISCD", "vgmlog", "netprobe", "gateway"]}}
                ],
                "must_not": [
                    {"terms": {"tags.keyword": ["_applog_grok_failure", "_vgm_grok_failure", "_grokparsefailure", "_gateway_grok_failure"]}}
                ]
            }
        }

        # Check if any index exists
        indices_exist = any(es.indices.exists(index=pattern) for pattern in index_patterns)
        if not indices_exist:
            logger.warning("No indices found for patterns: %s", ", ".join(index_patterns))
            return json.dumps({"status": "success", "logs": [], "total": 0})

        # Execute query across all index patterns
        logger.debug("Elasticsearch query: %s", es_query)
        result = es.search(
            index=",".join(index_patterns),
            query=es_query,
            sort=[{"@timestamp": {"order": "desc"}}],
            size=100
        )

        # Parse results
        hits = result.get("hits", {}).get("hits", [])
        logs = []
        for hit in hits:
            source = hit["_source"]
            log_type = source.get("type", "")
            log_entry = {
                "_id": hit["_id"],
                "timestamp": source.get("@timestamp", ""),
                "log_level": source.get("log_level", ""),
                "log_message": source.get("log_message", ""),
                "type": log_type,
                "tags": source.get("tags", [])
            }
            # Add type-specific fields
            if log_type == "FISCD":
                log_entry["java_class"] = source.get("class", "")
                log_entry["thread"] = source.get("thread", "")
                log_entry["summary"] = source.get("summary", "")
                log_entry["stack_trace"] = source.get("stack_trace", "")
            elif log_type == "vgmlog":
                log_entry["summary"] = source.get("log_message", "").split('\n')[0]
            elif log_type == "netprobe":
                log_entry["component"] = source.get("component", "")
            elif log_type == "gateway":
                log_entry["java_class"] = source.get("class", "")
                log_entry["thread"] = source.get("thread", "")

            logs.append(log_entry)

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
                    logger.info("Sent log to logs.anomalies: %s (type: %s)", log["_id"], log["type"])
                    new_logs.append(log)
                except KafkaError as e:
                    logger.error("Failed to send log to logs.anomalies: %s", str(e))
            else:
                logger.debug("Skipped duplicate log: %s (type: %s)", log["_id"], log["type"])

        # Update timestamp for next query
        last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'

        logger.info("Found %d new errors across %s", len(new_logs), ", ".join(index_patterns))
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
    """Create or update index template for logs, analysis, and remediation results"""
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
                    "log_level": {"type": "keyword"},
                    "log_message": {"type": "text", "analyzer": "log_analyzer"},
                    "type": {"type": "keyword"},
                    "java_class": {"type": "keyword"},
                    "thread": {"type": "keyword"},
                    "summary": {"type": "text", "analyzer": "log_analyzer"},
                    "stack_trace": {"type": "text", "analyzer": "log_analyzer"},
                    "component": {"type": "keyword"},
                    "rca_details": {
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
                            },
                            "system_state": {
                                "properties": {
                                    "affected_components": {"type": "keyword"},
                                    "error_patterns": {"type": "keyword"},
                                    "environmental_factors": {"type": "keyword"}
                                }
                            },
                            "java_class": {"type": "keyword"},
                            "thread": {"type": "keyword"},
                            "log_summary": {"type": "text", "analyzer": "log_analyzer"},
                            "stack_trace": {"type": "text", "analyzer": "log_analyzer"},
                            "component": {"type": "keyword"},
                            "recommended_actions": {"type": "text", "analyzer": "log_analyzer"},
                            "severity": {"type": "keyword"},
                            "confidence": {"type": "keyword"},
                            "category": {"type": "keyword"},
                            "metadata": {
                                "properties": {
                                    "analysis_timestamp": {"type": "date"},
                                    "log_type": {"type": "keyword"},
                                    "log_level": {"type": "keyword"}
                                }
                            }
                        }
                    },
                    "remediation_plan": {
                        "type": "nested",
                        "properties": {
                            "summary": {"type": "text", "analyzer": "log_analyzer"},
                            "steps": {
                                "type": "nested",
                                "properties": {
                                    "step_number": {"type": "integer"},
                                    "action": {"type": "text", "analyzer": "log_analyzer"},
                                    "purpose": {"type": "text", "analyzer": "log_analyzer"},
                                    "expected_outcome": {"type": "text", "analyzer": "log_analyzer"},
                                    "verification": {"type": "text", "analyzer": "log_analyzer"},
                                    "fallback": {"type": "text", "analyzer": "log_analyzer"}
                                }
                            },
                            "prerequisites": {"type": "text", "analyzer": "log_analyzer"},
                            "estimated_timeline": {
                                "properties": {
                                    "total_duration": {"type": "text"},
                                    "breakdown": {
                                        "type": "nested",
                                        "properties": {
                                            "phase": {"type": "text", "analyzer": "log_analyzer"},
                                            "duration": {"type": "text"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "error_details": {
                        "properties": {
                            "timestamp": {"type": "date"},
                            "log_level": {"type": "keyword"},
                            "log_message": {"type": "text", "analyzer": "log_analyzer"},
                            "log_type": {"type": "keyword"},
                            "summary": {"type": "text", "analyzer": "log_analyzer"},
                            "java_class": {"type": "keyword"},
                            "thread": {"type": "keyword"},
                            "stack_trace": {"type": "text", "analyzer": "log_analyzer"},
                            "component": {"type": "keyword"}
                        }
                    },
                    "priority": {"type": "keyword"},
                    "required_resources": {
                        "type": "nested",
                        "properties": {
                            "type": {"type": "keyword"},
                            "description": {"type": "text", "analyzer": "log_analyzer"},
                            "reason": {"type": "text", "analyzer": "log_analyzer"}
                        }
                    },
                    "risk_assessment": {
                        "properties": {
                            "impact_level": {"type": "keyword"},
                            "potential_risks": {
                                "type": "nested",
                                "properties": {
                                    "risk": {"type": "text", "analyzer": "log_analyzer"},
                                    "mitigation": {"type": "text", "analyzer": "log_analyzer"}
                                }
                            }
                        }
                    },
                    "remediation_metadata": {
                        "properties": {
                            "remediation_timestamp": {"type": "date"},
                            "llm_provider": {"type": "keyword"},
                            "llm_model": {"type": "keyword"}
                        }
                    },
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
        log_id = parsed_data.get("log_id", "unknown")
        
        # Skip if already processed
        if log_id in processed_remediation_ids:
            logger.info("Skipping duplicate remediation log_id: %s", log_id)
            return f"Skipped duplicate remediation log_id: {log_id}"
        
        # Create index name with prefix for better organization
        index_name = f"observix-results-{datetime.now(UTC).strftime('%Y.%m')}"
        
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
            id=log_id,
            document=parsed_data
        )
        
        processed_remediation_ids.add(log_id)
        logger.info("Stored remediation in %s with ID %s", index_name, log_id)
        return f"Stored remediation in {index_name}"
        
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in remediation data: %s", str(e))
        return f"Error storing remediation: Invalid JSON format - {str(e)}"
    except Exception as e:
        logger.error("Error storing remediation: %s", str(e))
        return f"Error storing remediation: {str(e)}"

def commit_message_offset(msg):
    """Commit offset for a specific message immediately"""
    try:
        partitions = [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)]
        consumer.commit(offsets=partitions, asynchronous=False)
        logger.info("Committed offset %d for partition %d of topic %s",
                    msg.offset() + 1, msg.partition(), msg.topic())
        return True
    except Exception as e:
        logger.error("Failed to commit offset for message: %s", str(e))
        return False

def consume_remediation():
    try:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            return "No remediation messages"
        if msg.error():
            logger.error("Kafka consumer error: %s", msg.error())
            return f"Kafka error: {msg.error()}"
        
        # Parse message to check log_id for deduplication
        try:
            result = json.loads(msg.value().decode("utf-8"))
            log_id = result.get("log_id", "unknown")
            if log_id in processed_remediation_ids:
                logger.info("Skipping duplicate remediation log_id: %s, committing offset", log_id)
                commit_message_offset(msg)
                return f"Skipped duplicate remediation log_id: {log_id}"
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in consumed message: %s", str(e))
            commit_message_offset(msg)
            return f"Error consuming remediation: Invalid JSON format - {str(e)}"

        # Store remediation and commit offset on success
        store_result = store_remediation(json.dumps(result))
        if "Stored remediation" in store_result:
            if commit_message_offset(msg):
                logger.info("Successfully processed and committed remediation message for log_id: %s", log_id)
            else:
                logger.error("Failed to commit offset for log_id: %s, will retry", log_id)
        return store_result
        
    except Exception as e:
        logger.error("Error consuming remediation: %s", str(e))
        return f"Error consuming remediation: {str(e)}"

def main():
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
        try:
            error_result = check_new_errors()
            if error_result and "error" not in error_result.lower():
                print(error_result)

            remediation_result = consume_remediation()
            if remediation_result != "No remediation messages":
                print(remediation_result)

            time.sleep(30)
        
        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            break
        except Exception as e:
            logger.error("Unexpected error in main loop: %s", str(e))
            time.sleep(30)

    # Cleanup
    logger.info("Shutting down Retrieval Agent...")
    try:
        consumer.close()
        producer.flush()
    except Exception as e:
        logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()