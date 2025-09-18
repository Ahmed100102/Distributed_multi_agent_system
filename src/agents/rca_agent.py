import os
import json
import re
import logging
from datetime import datetime, UTC
from typing import TypedDict, Optional, Dict
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
from langgraph.graph import StateGraph, END, START
from fastapi import FastAPI, Response
from threading import Thread
import uvicorn
import uuid
from statistics import mean
from llm_interface import LLMInterface
from langchain_core.exceptions import LangChainException
import time

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Metrics and state storage
metrics_file = "rca_metrics.json"

# Global metrics and processed_ids to persist across invoke calls
global_metrics = {
    "runs": [],
    "service_start_time": datetime.now(UTC).isoformat(),
    "total_logs_processed": 0,
    "total_errors": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "last_run_status": "running"
}
global_processed_ids = set()

class AgentState(TypedDict):
    log_data: Optional[Dict]
    rca_result: Optional[Dict]
    publish_result: Optional[str]
    metrics: Dict
    processed_ids: set
    model_status: str
    current_log_message: Optional[str]
    validation_passed: bool

def initialize_state():
    return AgentState(
        log_data=None,
        rca_result=None,
        publish_result=None,
        metrics=global_metrics,  # Reference global metrics
        processed_ids=global_processed_ids,  # Reference global processed_ids
        model_status="idle",
        current_log_message=None,
        validation_passed=False
    )

# Kafka setup
kafka_config = {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
logger.info("Kafka configuration: %s", kafka_config)

consumer = Consumer({
    **kafka_config,
    "group.id": "rca_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
    "max.poll.interval.ms": "1800000",
    "session.timeout.ms": "300000",
    "heartbeat.interval.ms": "10000",
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
})
consumer.subscribe(["logs.anomalies"])
logger.info("Kafka consumer subscribed to: logs.anomalies with manual offset control")

producer = Producer({
    **kafka_config,
    "acks": "all",
    "retries": 3,
    "delivery.timeout.ms": 30000
})
logger.info("Kafka producer initialized with reliable delivery settings")

# LLM setup
llm_interface = LLMInterface()
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            llm_interface.provider, llm_interface.model, llm_interface.endpoint or "default")

# FastAPI app
app = FastAPI()

def save_metrics():
    try:
        # Limit to last 1000 runs to manage memory
        global_metrics["runs"] = global_metrics["runs"][-1000:]
        with open(metrics_file, 'w') as f:
            json.dump(global_metrics, f, indent=2)
        logger.debug("Metrics saved to %s", metrics_file)
    except Exception as e:
        logger.error("Failed to save metrics: %s", str(e))

@app.get("/health")
async def health_check():
    try:
        kafka_ok = producer.list_topics(timeout=5) is not None
        llm_ok = llm_interface.llm is not None
        status = "healthy" if kafka_ok and llm_ok else "unhealthy"
        
        recent_runs = global_metrics["runs"][-10:]
        error_runs = [r for r in recent_runs if r.get("status") == "error"]
        rca_runs = [r for r in recent_runs if r.get("function") == "perform_rca"]
        publish_runs = [r for r in recent_runs if r.get("function") == "publish_to_kafka_rca"]
        
        total_logs = global_metrics["total_logs_processed"]
        avg_tokens_per_log = {
            "input": global_metrics["total_input_tokens"] / total_logs if total_logs > 0 else 0,
            "output": global_metrics["total_output_tokens"] / total_logs if total_logs > 0 else 0
        }
        
        valid_rca_runs = [r for r in rca_runs if "total_duration_ms" in r]
        valid_publish_runs = [r for r in publish_runs if "total_duration_ms" in r]
        
        if not valid_rca_runs and rca_runs:
            logger.warning("Some RCA runs missing total_duration_ms: %s", [r["run_id"] for r in rca_runs if "total_duration_ms" not in r])
        if not valid_publish_runs and publish_runs:
            logger.warning("Some publish runs missing total_duration_ms: %s", [r["run_id"] for r in publish_runs if "total_duration_ms" not in r])
        
        avg_durations = {
            "perform_rca": {
                "total": mean([r["total_duration_ms"] for r in valid_rca_runs]) if valid_rca_runs else 0,
                "steps": {
                    step: mean([r["steps"][step]["duration_ms"] for r in rca_runs if step in r["steps"]])
                    for step in ["llm_call", "clean_response"]
                    if any(step in r["steps"] for r in rca_runs)
                }
            },
            "publish_to_kafka_rca": {
                "total": mean([r["total_duration_ms"] for r in valid_publish_runs]) if valid_publish_runs else 0,
                "steps": {
                    step: mean([r["steps"][step]["duration_ms"] for r in publish_runs if step in r["steps"]])
                    for step in ["validate_json", "kafka_produce"]
                    if any(step in r["steps"] for r in publish_runs)
                }
            }
        }
        
        error_rate = len(error_runs) / len(global_metrics["runs"]) if global_metrics["runs"] else 0

        return {
            "status": status,
            "kafka": "connected" if kafka_ok else "disconnected",
            "llm": "initialized" if llm_ok else "uninitialized",
            "model_status": initialize_state()["model_status"],  # Use current model_status
            "current_log_message": initialize_state()["current_log_message"],
            "metrics": {
                "total_logs_processed": total_logs,
                "total_errors": global_metrics["total_errors"],
                "error_rate": error_rate,
                "total_input_tokens": global_metrics["total_input_tokens"],
                "total_output_tokens": global_metrics["total_output_tokens"],
                "avg_tokens_per_log": avg_tokens_per_log,
                "service_uptime_hours": (datetime.now(UTC) - datetime.fromisoformat(global_metrics["service_start_time"].replace('Z', '+00:00'))).total_seconds() / 3600,
                "last_run": {
                    "status": global_metrics["last_run_status"],
                    "timestamp": global_metrics["runs"][-1]["timestamp"] if global_metrics["runs"] else None,
                    "function": global_metrics["runs"][-1]["function"] if global_metrics["runs"] else None,
                    "total_duration_ms": global_metrics["runs"][-1].get("total_duration_ms", None) if global_metrics["runs"] else None
                },
                "recent_runs": [
                    {
                        "run_id": r["run_id"],
                        "timestamp": r["timestamp"],
                        "function": r["function"],
                        "status": r["status"],
                        "total_duration_ms": r.get("total_duration_ms", None),
                        "steps": r["steps"],
                        "log_id": r.get("log_id", None),
                        "input_tokens": r.get("input_tokens", 0),
                        "output_tokens": r.get("output_tokens", 0),
                        "error": r.get("error", None)
                    } for r in recent_runs
                ],
                "average_durations": avg_durations
            }
        }
    except Exception as e:
        logger.error("Health endpoint error: %s", str(e))
        return {
            "status": "unhealthy",
            "error": str(e),
            "model_status": initialize_state()["model_status"],
            "current_log_message": initialize_state()["current_log_message"],
            "metrics": {
                "total_logs_processed": global_metrics["total_logs_processed"],
                "total_errors": global_metrics["total_errors"],
                "error_rate": error_rate,
                "total_input_tokens": global_metrics["total_input_tokens"],
                "total_output_tokens": global_metrics["total_output_tokens"]
            }
        }

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")

def consume_kafka(state: AgentState) -> AgentState:
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "consume_kafka",
        "steps": {}
    }
    start_time = time.time()
    
    try:
        msg = consumer.poll(timeout=5.0)
        if msg is None:
            logger.debug("No new messages in logs.anomalies")
            metrics_entry["status"] = "skipped"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["validation_passed"] = False  # Ensure no loop back
            return state
        
        if msg.error():
            logger.error("Kafka consumer error: %s", msg.error())
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(msg.error())
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["total_errors"] += 1
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["validation_passed"] = False
            return state
        
        state["log_data"] = {"message": msg, "offset": msg.offset(), "partition": msg.partition(), "topic": msg.topic()}
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        return state
    except Exception as e:
        logger.error("Error in consume_kafka: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["validation_passed"] = False
        return state

def validate_input(state: AgentState) -> AgentState:
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "validate_input",
        "steps": {}
    }
    start_time = time.time()
    
    try:
        if not state["log_data"] or not state["log_data"].get("message"):
            logger.error("No log data to validate")
            metrics_entry["status"] = "error"
            metrics_entry["error"] = "No log data"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["validation_passed"] = False
            return state
        
        msg = state["log_data"]["message"]
        log = msg.value().decode("utf-8")
        log_data = json.loads(log)
        
        required_fields = ["_id", "log_message"]
        missing_fields = [f for f in required_fields if f not in log_data]
        if missing_fields:
            logger.error("Missing required fields in log: %s", missing_fields)
            commit_message_offset(state)
            metrics_entry["status"] = "error"
            metrics_entry["error"] = f"Missing fields: {missing_fields}"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["log_data"] = None
            state["validation_passed"] = False
            return state
        
        log_id = log_data["_id"]
        if log_id in state["processed_ids"]:
            logger.info(f"Log_id {log_id} already processed, skipping")
            commit_message_offset(state)
            metrics_entry["status"] = "skipped"
            metrics_entry["log_id"] = log_id
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["log_data"] = None
            state["validation_passed"] = False
            return state
        
        state["log_data"]["parsed"] = log_data
        state["current_log_message"] = log_data.get("log_message", "")
        state["validation_passed"] = True
        metrics_entry["status"] = "success"
        metrics_entry["log_id"] = log_id
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        return state
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in consumed message: %s", str(e))
        commit_message_offset(state)
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Invalid JSON: {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["log_data"] = None
        state["validation_passed"] = False
        return state
    except Exception as e:
        logger.error("Error in validate_input: %s", str(e))
        commit_message_offset(state)
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["log_data"] = None
        state["validation_passed"] = False
        return state

def perform_rca(state: AgentState) -> AgentState:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "perform_rca",
        "steps": {},
        "input_tokens": 0,
        "output_tokens": 0
    }
    state["model_status"] = "analyzing"
    
    try:
        log_data = state["log_data"]["parsed"]
        message = log_data.get("log_message", "")
        log_id = log_data.get("_id", "unknown")
        timestamp = log_data.get("timestamp", "unknown")
        log_type = log_data.get("type", "unknown")
        level = log_data.get("log_level", "unknown")
        java_class = log_data.get("java_class", "")
        thread = log_data.get("thread", "")
        summary = log_data.get("summary", "")
        stack_trace = log_data.get("stack_trace", "")
        component = log_data.get("component", "")
        
        state["processed_ids"].add(log_id)
        state["metrics"]["total_logs_processed"] += 1
        
        system_prompt = f"""You are an expert Root Cause Analysis (RCA) specialist with extensive experience in system analysis.

ANALYSIS CONTEXT:
- Log ID: {log_id}
- Timestamp: {timestamp}
- Log Type: {log_type}
- Log Level: {level}
- Java Class: {java_class}
- Thread: {thread}
- Summary: {summary}
- Stack Trace: {stack_trace}
- Component: {component}

LOG TYPE DETAILS:
- FISCD: Contains log_level, log_message, java_class, thread, summary, stack_trace
- vgmlog: Contains log_level, log_message, summary
- netprobe: Contains log_level, log_message, component
- gateway: Contains log_level, log_message, java_class, thread

ANALYSIS REQUIREMENTS:
1. Perform deep technical analysis of the log message
2. Break down the issue into component parts
3. Identify all potential root causes with probabilities
4. Map dependencies and impact areas
5. Assess business impact and urgency
6. Provide actionable recommendations to resolve the issue
7. Include relevant fields (java_class, thread, summary, stack_trace, component) based on log type

OUTPUT FORMAT (JSON ONLY):
{{
    "log_id": "{log_id}",
    "rca": {{
        "summary": "Brief summary of the issue",
        "detailed_analysis": "In-depth technical analysis",
        "root_causes": [
            {{
                "cause": "Description of root cause",
                "probability": "HIGH|MEDIUM|LOW",
                "impact_areas": ["Area1", "Area2"],
                "technical_details": "Technical explanation"
            }}
        ],
        "system_state": {{
            "affected_components": ["Component1", "Component2"],
            "error_patterns": ["Pattern1", "Pattern2"],
            "environmental_factors": ["Factor1", "Factor2"]
        }},
        "java_class": "{java_class}",
        "thread": "{thread}",
        "log_summary": "{summary}",
        "stack_trace": "{stack_trace}",
        "component": "{component}"
    }},
    "recommended_actions": ["Action1", "Action2"],
    "severity": "HIGH|MEDIUM|LOW",
    "confidence": "HIGH|MEDIUM|LOW",
    "category": "INFRASTRUCTURE|APPLICATION|NETWORK|DATA|SECURITY|OTHER",
    "metadata": {{
        "analysis_timestamp": "{timestamp}",
        "log_type": "{log_type}",
        "log_level": "{level}"
    }}
}}"""
        
        user_prompt = f"""Analyze this log message and provide comprehensive root cause analysis:

LOG MESSAGE: {message}

Additional context from log entry:
- Log Type: {log_type}
- Log Level: {level}
- Java Class: {java_class}
- Thread: {thread}
- Summary: {summary}
- Stack Trace: {stack_trace}
- Component: {component}

Perform thorough technical analysis, provide actionable recommendations, and return the JSON response."""
        
        logger.info("Starting RCA analysis for log_id: %s (type: %s)", log_id, log_type)
        
        llm_start = time.time()
        analysis, token_counts = llm_interface.call(system_prompt, user_prompt, timeout=30)
        metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.time() - llm_start) * 1000}
        metrics_entry["input_tokens"] = token_counts.get("input_tokens", 0)
        metrics_entry["output_tokens"] = token_counts.get("output_tokens", 0)
        state["metrics"]["total_input_tokens"] += token_counts.get("input_tokens", 0)
        state["metrics"]["total_output_tokens"] += token_counts.get("output_tokens", 0)
        
        clean_start = time.time()
        analysis = clean_llm_response(analysis)
        parsed = json.loads(analysis)
        metrics_entry["steps"]["clean_response"] = {"duration_ms": (time.time() - clean_start) * 1000}
        
        if isinstance(parsed.get("rca"), str):
            try:
                parsed["rca"] = json.loads(parsed["rca"].replace("'", '"'))
            except json.JSONDecodeError:
                logger.warning("Could not parse RCA string as JSON, leaving as is")
        
        if "analysis_timestamp" not in parsed.get("metadata", {}):
            parsed["metadata"] = parsed.get("metadata", {})
            parsed["metadata"]["analysis_timestamp"] = timestamp
        if "log_type" not in parsed.get("metadata", {}):
            parsed["metadata"]["log_type"] = log_type
            
        if "recommended_actions" not in parsed:
            logger.warning("LLM did not provide recommended_actions, adding default")
            parsed["recommended_actions"] = ["Review and update instrument database", "Validate client request parameters"]
            
        state["rca_result"] = parsed
        metrics_entry["status"] = "success"
        metrics_entry["log_id"] = log_id
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["model_status"] = "idle"
        state["current_log_message"] = None
        return state
        
    except Exception as e:
        logger.error("RCA analysis failed for log_id %s: %s", log_id, str(e))
        # Estimate token counts for error response
        error_response = json.dumps({
            "log_id": str(log_id),
            "rca": {
                "summary": f"RCA analysis failed: {str(e)}",
                "detailed_analysis": "Analysis could not be completed due to an error",
                "root_causes": [],
                "system_state": {
                    "affected_components": [],
                    "error_patterns": [],
                    "environmental_factors": []
                },
                "java_class": java_class,
                "thread": thread,
                "log_summary": summary,
                "stack_trace": stack_trace,
                "component": component
            },
            "recommended_actions": ["Review LLM configuration"],
            "severity": "LOW",
            "confidence": "LOW",
            "category": "OTHER",
            "metadata": {
                "analysis_timestamp": timestamp,
                "log_type": log_type,
                "log_level": level
            }
        })
        token_counts = {
            "input_tokens": llm_interface._estimate_tokens(system_prompt + user_prompt),
            "output_tokens": llm_interface._estimate_tokens(error_response)
        }
        state["rca_result"] = json.loads(error_response)
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["input_tokens"] = token_counts["input_tokens"]
        metrics_entry["output_tokens"] = token_counts["output_tokens"]
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["total_input_tokens"] += token_counts["input_tokens"]
        state["metrics"]["total_output_tokens"] += token_counts["output_tokens"]
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["model_status"] = "idle"
        state["current_log_message"] = None
        return state

def publish_to_kafka_rca(state: AgentState) -> AgentState:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "publish_to_kafka_rca",
        "steps": {}
    }
    
    try:
        if not state["rca_result"]:
            logger.error("No RCA result to publish")
            metrics_entry["status"] = "error"
            metrics_entry["error"] = "No RCA result"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["publish_result"] = "ERROR: No RCA result"
            return state
        
        data = json.dumps(state["rca_result"], indent=2)
        logger.debug("Publishing RCA to logs.rca.output: %s", data[:200] + "..." if len(data) > 200 else data)
        
        validate_start = time.time()
        parsed = json.loads(data)
        required_fields = ["log_id", "rca", "recommended_actions", "severity"]
        missing_fields = [field for field in required_fields if field not in parsed]
        if missing_fields:
            logger.error("Missing required fields in RCA JSON: %s", missing_fields)
            metrics_entry["status"] = "error"
            metrics_entry["error"] = f"Missing required fields - {', '.join(missing_fields)}"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["publish_result"] = f"ERROR: Missing required fields - {', '.join(missing_fields)}"
            return state
        metrics_entry["steps"]["validate_json"] = {"duration_ms": (time.time() - validate_start) * 1000}
        
        produce_start = time.time()
        def delivery_callback(err, msg):
            if err:
                logger.error("Failed to deliver RCA message: %s", err)
            else:
                logger.info("RCA message delivered to partition %d at offset %d", 
                           msg.partition(), msg.offset())
        
        producer.produce(
            "logs.rca.output", 
            value=data.encode("utf-8"),
            callback=delivery_callback
        )
        producer.flush(timeout=10.0)
        metrics_entry["steps"]["kafka_produce"] = {"duration_ms": (time.time() - produce_start) * 1000}
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = "SUCCESS: Published RCA to Kafka"
        return state
        
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in RCA data: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Invalid JSON format - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = f"ERROR: Invalid JSON format - {str(e)}"
        return state
    except KafkaError as e:
        logger.error("Kafka error publishing RCA: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Kafka publishing failed - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = f"ERROR: Kafka publishing failed - {str(e)}"
        return state
    except Exception as e:
        logger.error("Unexpected error publishing RCA: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Unexpected error - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = f"ERROR: Unexpected error - {str(e)}"
        return state

def commit_message_offset(state: AgentState) -> AgentState:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "commit_message_offset",
        "steps": {}
    }
    
    try:
        msg = state["log_data"]["message"]
        commit_start = time.time()
        partitions = [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)]
        consumer.commit(offsets=partitions, asynchronous=False)
        logger.info("Committed offset %d for partition %d of topic %s", 
                   msg.offset() + 1, msg.partition(), msg.topic())
        metrics_entry["status"] = "success"
        metrics_entry["steps"]["commit"] = {"duration_ms": (time.time() - commit_start) * 1000}
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["log_data"] = None
        state["validation_passed"] = False
        return state
    except Exception as e:
        logger.error("Failed to commit offset for message: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["log_data"] = None
        state["validation_passed"] = False
        return state

def clean_llm_response(response: str) -> str:
    response = re.sub(r'```json\s*', '', response)
    response = re.sub(r'```\s*$', '', response)
    response = re.sub(r'<[^>]+>.*?</[^>]+>', '', response, flags=re.DOTALL)
    json_match = re.search(r'\{.*\}', response, re.DOTALL)
    if json_match:
        response = json_match.group(0)
    return response.strip()

# LangGraph setup
kafka_subgraph = StateGraph(AgentState)
kafka_subgraph.add_node("consume_kafka", consume_kafka)
kafka_subgraph.add_node("validate_input", validate_input)
kafka_subgraph.add_edge(START, "consume_kafka")
kafka_subgraph.add_edge("consume_kafka", "validate_input")
kafka_subgraph.add_conditional_edges(
    "validate_input",
    lambda state: END if state["validation_passed"] or state["log_data"] is None else "consume_kafka"
)
kafka_subgraph.set_entry_point("consume_kafka")

workflow = StateGraph(AgentState)
workflow.add_node("kafka_subgraph", kafka_subgraph.compile())
workflow.add_node("perform_rca", perform_rca)
workflow.add_node("publish_to_kafka_rca", publish_to_kafka_rca)
workflow.add_node("commit_message_offset", commit_message_offset)
workflow.add_conditional_edges(
    "kafka_subgraph",
    lambda state: "perform_rca" if state["validation_passed"] else END
)
workflow.add_edge("perform_rca", "publish_to_kafka_rca")
workflow.add_conditional_edges(
    "publish_to_kafka_rca",
    lambda state: "commit_message_offset" if state["publish_result"] and "SUCCESS" in state["publish_result"] else END
)
workflow.add_edge("commit_message_offset", END)
workflow.set_entry_point("kafka_subgraph")

graph = workflow.compile()

def main():
    logger.info("Starting LangGraph RCA Agent")
    api_thread = Thread(target=run_api, daemon=True)
    api_thread.start()
    
    try:
        while True:
            state = initialize_state()
            graph.invoke(state, config={"recursion_limit": 1000})  # Increased recursion limit
            global_metrics["last_run_status"] = "running"
            save_metrics()
            if state["log_data"] is None and not state["validation_passed"]:
                time.sleep(1)  # Brief pause when no messages to avoid tight loop
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        global_metrics["last_run_status"] = "stopped"
        save_metrics()
    finally:
        logger.info("Shutting down RCA Agent...")
        try:
            consumer.close()
            producer.flush()
        except Exception as e:
            logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()