import os
import time
import json
import sys
import asyncio
from datetime import datetime, UTC
from elasticsearch import AsyncElasticsearch, ApiError, TransportError
from elasticsearch.helpers import async_bulk
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
import logging
from fastapi import FastAPI, Response
import uvicorn
import uuid
from statistics import mean

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize AsyncElasticsearch client
es = AsyncElasticsearch(
    os.getenv("ELASTICSEARCH_URL", "http://localhost:9200"),
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
    "enable.auto.commit": False,
    "max.poll.interval.ms": "1800000",
    "session.timeout.ms": "300000",
    "heartbeat.interval.ms": "10000",
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500,
    "max.partition.fetch.bytes": 10485760,  # 10MB
    "fetch.max.bytes": 52428800  # 50MB
})
consumer.subscribe(["logs.remediation"])
producer = Producer(kafka_config)

# Metrics and processed IDs storage
metrics_file = "retrieval_metrics.json"
processed_ids_file = "processed_ids.json"
processed_remediation_ids_file = "processed_remediation_ids.json"
metrics = {
    "runs": [],
    "service_start_time": datetime.now(UTC).isoformat(),
    "total_logs_processed": 0,
    "total_errors": 0,
    "last_run_status": "running"
}
processed_ids = set()
processed_remediation_ids = set()

# Track last query timestamp
last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'

# FastAPI app
app = FastAPI()

def save_metrics():
    try:
        with open(metrics_file, 'w') as f:
            json.dump(metrics, f, indent=2)
        logger.debug("Metrics saved to %s", metrics_file)
    except Exception as e:
        logger.error("Failed to save metrics: %s", str(e))

def load_processed_ids():
    try:
        if os.path.exists(processed_ids_file):
            with open(processed_ids_file, 'r') as f:
                processed_ids.update(json.load(f))
            logger.info("Loaded %d processed IDs from %s", len(processed_ids), processed_ids_file)
        if os.path.exists(processed_remediation_ids_file):
            with open(processed_remediation_ids_file, 'r') as f:
                processed_remediation_ids.update(json.load(f))
            logger.info("Loaded %d processed remediation IDs from %s", len(processed_remediation_ids), processed_remediation_ids_file)
    except Exception as e:
        logger.error("Failed to load processed IDs: %s", str(e))

def save_processed_ids():
    try:
        with open(processed_ids_file, 'w') as f:
            json.dump(list(processed_ids), f)
        with open(processed_remediation_ids_file, 'w') as f:
            json.dump(list(processed_remediation_ids), f)
        logger.debug("Saved %d processed IDs and %d remediation IDs", len(processed_ids), len(processed_remediation_ids))
    except Exception as e:
        logger.error("Failed to save processed IDs: %s", str(e))

async def check_new_errors(from_time: str = "") -> str:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "check_new_errors",
        "steps": {}
    }
    
    global last_query_timestamp
    try:
        validate_start = time.time()
        search_time = from_time if from_time else last_query_timestamp
        if from_time:
            try:
                datetime.fromisoformat(from_time.replace('Z', '+00:00'))
            except ValueError:
                logger.error("Invalid from_time format: %s. Expected ISO 8601 UTC.", from_time)
                metrics_entry["status"] = "error"
                metrics_entry["error"] = "Invalid from_time format"
                metrics["runs"].append(metrics_entry)
                save_metrics()
                return json.dumps({"status": "error", "message": "Invalid from_time format. Use ISOF 8601 UTC."})
        metrics_entry["steps"]["validate_input"] = {"duration_ms": (time.time() - validate_start) * 1000}

        index_check_start = time.time()
        index_patterns = [
            "logstash-*"
        ]
        indices_exist = False
        for pattern in index_patterns:
            if await es.indices.exists(index=pattern):
                indices_exist = True
                break
        metrics_entry["steps"]["index_check"] = {"duration_ms": (time.time() - index_check_start) * 1000}
        if not indices_exist:
            logger.warning("No indices found for patterns: %s", ", ".join(index_patterns))
            metrics_entry["status"] = "success"
            metrics_entry["log_count"] = 0
            metrics["runs"].append(metrics_entry)
            save_metrics()
            return json.dumps({"status": "success", "logs": [], "total": 0})

        query_start = time.time()
        es_query = {
            "bool": {
                "filter": [
                    {"terms": {"log_level.keyword": ["ERROR", "WARN"]}},
                    {"range": {"@timestamp": {"gt": search_time, "lte": "now"}}},
                    {"terms": {"type.keyword": ["FISCD", "vgmlog", "netprobe", "gateway"]}}
                ],
                "must_not": [
                    {"terms": {"tags.keyword": ["_applog_grok_failure", "_vgm_grok_failure", "_grokparsefailure", "_gateway_grok_failure"]}}
                ]
            }
        }
        logger.debug("Elasticsearch query: %s", es_query)
        result = await es.search(
            index=",".join(index_patterns),
            query=es_query,
            sort=[{"@timestamp": {"order": "desc"}}],
            size=1000  # Reduced for efficiency
        )
        metrics_entry["steps"]["es_query"] = {"duration_ms": (time.time() - query_start) * 1000}

        parse_start = time.time()
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
        metrics_entry["steps"]["parse_results"] = {"duration_ms": (time.time() - parse_start) * 1000}
        metrics_entry["log_count"] = len(logs)

        produce_start = time.time()
        new_logs = []
        for log in logs:
            if log["_id"] not in processed_ids:
                await asyncio.to_thread(
                    producer.produce,
                    "logs.anomalies",
                    key=log["_id"].encode("utf-8"),
                    value=json.dumps(log).encode("utf-8")
                )
                processed_ids.add(log["_id"])
                new_logs.append(log)
        await asyncio.to_thread(producer.flush)
        metrics_entry["steps"]["kafka_produce"] = {"duration_ms": (time.time() - produce_start) * 1000}
        metrics_entry["new_logs_produced"] = len(new_logs)
        metrics["total_logs_processed"] += len(new_logs)

        last_query_timestamp = datetime.now(UTC).isoformat() + 'Z'
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        metrics["runs"].append(metrics_entry)
        save_metrics()

        logger.info("Found %d new errors across %s", len(new_logs), ", ".join(index_patterns))
        return json.dumps({"status": "success", "logs": new_logs, "total": len(new_logs)}, indent=2)

    except ApiError as e:
        logger.error("Elasticsearch API error: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return json.dumps({"status": "error", "message": str(e)})
    except TransportError as e:
        logger.error("Elasticsearch transport error: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        logger.error("Unexpected error querying Elasticsearch: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return json.dumps({"status": "error", "message": str(e)})

async def create_or_update_index_template():
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "create_or_update_index_template",
        "steps": {}
    }
    
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
        await es.indices.put_index_template(name=template_name, body=template_body)
        logger.info("Index template created/updated successfully")
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        metrics["runs"].append(metrics_entry)
        save_metrics()
    except Exception as e:
        logger.error("Failed to create/update index template: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()

async def store_remediations_bulk(messages):
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "store_remediations_bulk",
        "steps": {}
    }
    
    actions = []
    log_ids = []
    try:
        index_name = f"observix-results-{datetime.now(UTC).strftime('%Y.%m')}"
        if not await es.indices.exists(index=index_name):
            await es.indices.create(index=index_name)
            logger.info("Created new index: %s", index_name)
        
        parse_start = time.time()
        for msg in messages:
            data = json.loads(msg.value().decode("utf-8"))
            log_id = data.get("log_id", "unknown")
            if log_id in processed_remediation_ids:
                logger.info("Skipping duplicate remediation log_id: %s", log_id)
                await asyncio.to_thread(commit_message_offset, msg)
                continue
            if "timestamp" not in data:
                data["timestamp"] = datetime.now(UTC).isoformat()
            actions.append({
                "_index": index_name,
                "_id": log_id,
                "_source": data
            })
            log_ids.append((log_id, msg))
        metrics_entry["steps"]["parse_json"] = {"duration_ms": (time.time() - parse_start) * 1000}
        
        if not actions:
            metrics_entry["status"] = "skipped"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            metrics["runs"].append(metrics_entry)
            save_metrics()
            return "No new remediations to store"
        
        store_start = time.time()
        await async_bulk(es, actions)
        metrics_entry["steps"]["es_bulk_store"] = {"duration_ms": (time.time() - store_start) * 1000}
        
        for log_id, msg in log_ids:
            processed_remediation_ids.add(log_id)
            await asyncio.to_thread(commit_message_offset, msg)
        logger.info("Stored %d remediations in %s", len(actions), index_name)
        
        metrics_entry["status"] = "success"
        metrics_entry["log_count"] = len(actions)
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return f"Stored {len(actions)} remediations in {index_name}"
    
    except Exception as e:
        logger.error("Error storing remediations: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return f"Error storing remediations: {str(e)}"

def commit_message_offset(msg):
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "commit_message_offset",
        "steps": {}
    }
    
    try:
        commit_start = time.time()
        partitions = [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)]
        consumer.commit(offsets=partitions, asynchronous=False)
        logger.info("Committed offset %d for partition %d of topic %s",
                    msg.offset() + 1, msg.partition(), msg.topic())
        metrics_entry["status"] = "success"
        metrics_entry["steps"]["commit"] = {"duration_ms": (time.time() - commit_start) * 1000}
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return True
    except Exception as e:
        logger.error("Failed to commit offset for message: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return False

async def consume_remediation():
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "consume_remediation",
        "steps": {}
    }
    
    messages = []
    try:
        poll_start = time.time()
        while len(messages) < 100:  # Buffer up to 100 messages
            msg = await asyncio.to_thread(consumer.poll, timeout=5.0)
            if msg is None:
                break
            if msg.error():
                logger.error("Kafka consumer error: %s", msg.error())
                metrics_entry["status"] = "error"
                metrics_entry["error"] = str(msg.error())
                metrics["total_errors"] += 1
                metrics["runs"].append(metrics_entry)
                save_metrics()
                return f"Kafka error: {msg.error()}"
            messages.append(msg)
        metrics_entry["steps"]["kafka_poll"] = {"duration_ms": (time.time() - poll_start) * 1000}
        
        if not messages:
            metrics_entry["status"] = "no_messages"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            metrics["runs"].append(metrics_entry)
            save_metrics()
            return "No remediation messages"
        
        store_start = time.time()
        store_result = await store_remediations_bulk(messages)
        metrics_entry["steps"]["store_remediation"] = {"duration_ms": (time.time() - store_start) * 1000}
        metrics_entry["status"] = "success" if "Stored" in store_result else "error"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return store_result
    
    except Exception as e:
        logger.error("Error consuming remediation: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics["total_errors"] += 1
        metrics["runs"].append(metrics_entry)
        save_metrics()
        return f"Error consuming remediation: {str(e)}"

@app.get("/health")
async def health_check():
    try:
        es_health = await es.cluster.health()
        kafka_ok = await asyncio.to_thread(producer.list_topics, timeout=5)
        status = "healthy" if es_health["status"] in ["green", "yellow"] and kafka_ok else "unhealthy"
        
        recent_runs = metrics["runs"][-10:]
        error_runs = [r for r in metrics["runs"] if r.get("status") == "error"]
        check_errors_runs = [r for r in metrics["runs"] if r.get("function") == "check_new_errors"]
        consume_runs = [r for r in metrics["runs"] if r.get("function") == "consume_remediation"]
        
        avg_durations = {
            "check_new_errors": {
                "total": mean([r["total_duration_ms"] for r in check_errors_runs]) if check_errors_runs else 0,
                "steps": {
                    step: mean([r["steps"][step]["duration_ms"] for r in check_errors_runs if step in r["steps"]])
                    for step in ["validate_input", "index_check", "es_query", "parse_results", "kafka_produce"]
                    if any(step in r["steps"] for r in check_errors_runs)
                }
            },
            "consume_remediation": {
                "total": mean([r["total_duration_ms"] for r in consume_runs]) if consume_runs else 0,
                "steps": {
                    step: mean([r["steps"][step]["duration_ms"] for r in consume_runs if step in r["steps"]])
                    for step in ["kafka_poll", "parse_message", "store_remediation"]
                    if any(step in r["steps"] for r in consume_runs)
                }
            }
        }
        
        total_runs = len(metrics["runs"])
        error_rate = len(error_runs) / total_runs if total_runs > 0 else 0

        return {
            "status": status,
            "elasticsearch": es_health["status"],
            "kafka": "connected" if kafka_ok else "disconnected",
            "metrics": {
                "total_logs_processed": metrics["total_logs_processed"],
                "total_errors": metrics["total_errors"],
                "error_rate": error_rate,
                "service_uptime": (datetime.now(UTC) - datetime.fromisoformat(metrics["service_start_time"].replace('Z', '+00:00'))).total_seconds() / 3600,
                "last_run": {
                    "status": metrics["last_run_status"],
                    "timestamp": metrics["runs"][-1]["timestamp"] if metrics["runs"] else None,
                    "function": metrics["runs"][-1]["function"] if metrics["runs"] else None,
                    "total_duration_ms": metrics["runs"][-1]["total_duration_ms"] if metrics["runs"] else None
                },
                "recent_runs": [
                    {
                        "run_id": r["run_id"],
                        "timestamp": r["timestamp"],
                        "function": r["function"],
                        "status": r["status"],
                        "total_duration_ms": r["total_duration_ms"],
                        "steps": r["steps"],
                        "log_count": r.get("log_count", 0),
                        "new_logs_produced": r.get("new_logs_produced", 0),
                        "error": r.get("error", None)
                    } for r in recent_runs
                ],
                "average_durations": avg_durations
            }
        }
    except Exception as e:
        logger.error("Health check failed: %s", str(e))
        return {"status": "unhealthy", "error": str(e)}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

async def run_check_new_errors():
    while True:
        try:
            result = await check_new_errors()
            if result and "error" not in result.lower():
                logger.info("check_new_errors result: %s", result)
            log_count = json.loads(result).get("total", 0) if result else 0
            sleep_time = 10 if log_count > 100 else 60
            await asyncio.sleep(sleep_time)
        except Exception as e:
            logger.error("Error in check_new_errors loop: %s", str(e))
            metrics["total_errors"] += 1
            save_metrics()
            await asyncio.sleep(60)

async def run_consume_remediation():
    while True:
        try:
            result = await consume_remediation()
            if result != "No remediation messages":
                logger.info("consume_remediation result: %s", result)
            if "No remediation messages" in result:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error("Error in consume_remediation loop: %s", str(e))
            metrics["total_errors"] += 1
            save_metrics()
            await asyncio.sleep(1)

async def main():
    load_processed_ids()
    
    from_time = sys.argv[1] if len(sys.argv) > 1 else ""
    if from_time:
        logger.info("Running historical query with from_time: %s", from_time)
        error_result = await check_new_errors(from_time)
        if error_result and "error" not in error_result.lower():
            logger.info("Historical query result: %s", error_result)

    await create_or_update_index_template()
    index_name = f"observix-results-{datetime.now(UTC).strftime('%Y.%m')}"
    if not await es.indices.exists(index=index_name):
        await es.indices.create(index=index_name)
        logger.info("Created new index: %s", index_name)

    # Start FastAPI server in the same event loop
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        run_check_new_errors(),
        run_consume_remediation()
    )

    # Cleanup on shutdown
    save_processed_ids()
    await es.close()
    await asyncio.to_thread(consumer.close)
    await asyncio.to_thread(producer.flush)

if __name__ == "__main__":
    asyncio.run(main())