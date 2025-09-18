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
import time

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Metrics and processed IDs storage
metrics_file = "remediation_metrics.json"
processed_ids_file = "processed_ids.json"

# Global metrics and processed_ids
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
    remediation_result: Optional[Dict]
    publish_result: Optional[str]
    metrics: Dict
    processed_ids: set
    model_status: str
    current_log_message: Optional[str]
    validation_passed: bool

def initialize_state():
    return AgentState(
        log_data=None,
        remediation_result=None,
        publish_result=None,
        metrics=global_metrics,
        processed_ids=global_processed_ids,
        model_status="idle",
        current_log_message=None,
        validation_passed=False
    )

# Load processed IDs from file
def load_processed_ids():
    try:
        if os.path.exists(processed_ids_file):
            with open(processed_ids_file, 'r') as f:
                global_processed_ids.update(json.load(f))
            logger.info("Loaded %d processed IDs from %s", len(global_processed_ids), processed_ids_file)
    except Exception as e:
        logger.error("Failed to load processed IDs: %s", str(e))

# Save processed IDs to file
def save_processed_ids():
    try:
        with open(processed_ids_file, 'w') as f:
            json.dump(list(global_processed_ids), f, indent=2)
        logger.debug("Saved %d processed IDs to %s", len(global_processed_ids), processed_ids_file)
    except Exception as e:
        logger.error("Failed to save processed IDs: %s", str(e))

# Save metrics to file
def save_metrics():
    try:
        global_metrics["runs"] = global_metrics["runs"][-1000:]
        with open(metrics_file, 'w') as f:
            json.dump(global_metrics, f, indent=2)
        logger.debug("Metrics saved to %s", metrics_file)
    except Exception as e:
        logger.error("Failed to save metrics: %s", str(e))

# Load initial processed IDs
load_processed_ids()

# FastAPI app
app = FastAPI()

@app.get("/health")
async def health_check():
    try:
        kafka_ok = producer.list_topics(timeout=5) is not None
        llm_ok = llm_interface.llm is not None
        status = "healthy" if kafka_ok and llm_ok else "unhealthy"
        
        recent_runs = global_metrics["runs"][-10:]
        error_runs = [r for r in recent_runs if r.get("status") == "error"]
        remediation_runs = [r for r in recent_runs if r.get("function") == "perform_remediation"]
        publish_runs = [r for r in recent_runs if r.get("function") == "publish_to_kafka_remediation"]
        
        total_logs = global_metrics["total_logs_processed"]
        avg_tokens_per_log = {
            "input": global_metrics["total_input_tokens"] / total_logs if total_logs > 0 else 0,
            "output": global_metrics["total_output_tokens"] / total_logs if total_logs > 0 else 0
        }
        
        valid_remediation_runs = [r for r in remediation_runs if "total_duration_ms" in r]
        valid_publish_runs = [r for r in publish_runs if "total_duration_ms" in r]
        
        if not valid_remediation_runs and remediation_runs:
            logger.warning("Some remediation runs missing total_duration_ms: %s", [r["run_id"] for r in remediation_runs if "total_duration_ms" not in r])
        if not valid_publish_runs and publish_runs:
            logger.warning("Some publish runs missing total_duration_ms: %s", [r["run_id"] for r in publish_runs if "total_duration_ms" not in r])
        
        avg_durations = {
            "perform_remediation": {
                "total": mean([r["total_duration_ms"] for r in valid_remediation_runs]) if valid_remediation_runs else 0,
                "steps": {
                    step: mean([r["steps"][step]["duration_ms"] for r in remediation_runs if step in r["steps"]])
                    for step in ["llm_call", "clean_response"]
                    if any(step in r["steps"] for r in remediation_runs)
                }
            },
            "publish_to_kafka_remediation": {
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
            "model_status": initialize_state()["model_status"],
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
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")

llm_interface = LLMInterface()
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            llm_interface.provider, llm_interface.model, llm_interface.endpoint or "default")

# Kafka setup
kafka_config = {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
logger.info("Kafka configuration: %s", kafka_config)

consumer = Consumer({
    **kafka_config,
    "group.id": "remediation_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
    "max.poll.interval.ms": "1800000",
    "session.timeout.ms": "300000",
    "heartbeat.interval.ms": "10000",
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
})
consumer.subscribe(["logs.rca.output"])
logger.info("Kafka consumer subscribed to: logs.rca.output with manual offset control")

producer = Producer({
    **kafka_config,
    "acks": "all",
    "retries": 3,
    "delivery.timeout.ms": 30000
})
logger.info("Kafka producer initialized with reliable delivery settings")

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
            logger.debug("No new messages in logs.rca.output")
            metrics_entry["status"] = "skipped"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["validation_passed"] = False
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
        
        required_fields = ["log_id", "rca", "recommended_actions", "severity"]
        missing_fields = [f for f in required_fields if f not in log_data]
        if missing_fields:
            logger.error("Missing required fields in RCA: %s", missing_fields)
            commit_message_offset(state)
            metrics_entry["status"] = "error"
            metrics_entry["error"] = f"Missing fields: {missing_fields}"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["log_data"] = None
            state["validation_passed"] = False
            return state
        
        log_id = log_data["log_id"]
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
        state["current_log_message"] = log_data.get("rca", {}).get("log_summary", "")
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

def perform_remediation(state: AgentState) -> AgentState:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "perform_remediation",
        "steps": {},
        "input_tokens": 0,
        "output_tokens": 0
    }
    state["model_status"] = "analyzing"
    
    try:
        rca_data = state["log_data"]["parsed"]
        log_id = rca_data.get("log_id", "unknown")
        rca = rca_data.get("rca", {})
        metadata = rca_data.get("metadata", {})
        recommended_actions = rca_data.get("recommended_actions", [])
        severity = rca_data.get("severity", "MEDIUM")
        confidence = rca_data.get("confidence", "MEDIUM")
        category = rca_data.get("category", "OTHER")
        
        state["processed_ids"].add(log_id)
        state["metrics"]["total_logs_processed"] += 1
        save_processed_ids()
        
        system_prompt = f"""You are a remediation specialist focusing on system issues.

CONTEXT:
- Log ID: {log_id}
- RCA Summary: {rca.get('summary', '')}
- Detailed Analysis: {rca.get('detailed_analysis', '')}
- Root Causes: {json.dumps(rca.get('root_causes', []))}
- System State: {json.dumps(rca.get('system_state', {}))}
- Recommended Actions: {json.dumps(recommended_actions)}
- Severity: {severity}
- Confidence: {confidence}
- Category: {category}
- Log Type: {metadata.get('log_type', 'unknown')}
- Log Level: {metadata.get('log_level', 'unknown')}
- Analysis Timestamp: {metadata.get('analysis_timestamp', 'unknown')}

Generate a comprehensive remediation plan based on the RCA details.
Return ONLY a valid JSON object with exactly these fields:
{{
    "remediation_plan": {{
        "summary": "Brief overview of remediation approach",
        "steps": [
            {{
                "step_number": 1,
                "action": "Detailed action to take",
                "purpose": "Why this step is necessary",
                "expected_outcome": "What should happen after this step",
                "verification": "How to verify step success",
                "fallback": "What to do if step fails"
            }}
        ],
        "prerequisites": ["Required conditions or resources"],
        "estimated_timeline": {{
            "total_duration": "Expected total time",
            "breakdown": [
                {{
                    "phase": "Phase description",
                    "duration": "Expected duration"
                }}
            ]
        }}
    }},
    "priority": "HIGH|MEDIUM|LOW",
    "required_resources": [
        {{
            "type": "TEAM|TOOL|ACCESS|OTHER",
            "description": "Specific resource needed",
            "reason": "Why this resource is needed"
        }}
    ],
    "risk_assessment": {{
        "impact_level": "HIGH|MEDIUM|LOW",
        "potential_risks": [
            {{
                "risk": "Description of risk",
                "mitigation": "How to mitigate this risk"
            }}
        ]
    }}
}}"""

        user_prompt = """Generate a detailed remediation plan based on the RCA results and recommendations provided.
Ensure the plan is actionable, prioritized based on severity, and includes risk assessment."""
        
        logger.info("Starting remediation planning for log_id: %s", log_id)
        
        llm_start = time.time()
        plan, token_counts = llm_interface.call(system_prompt, user_prompt, timeout=30)
        metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.time() - llm_start) * 1000}
        metrics_entry["input_tokens"] = token_counts.get("input_tokens", 0)
        metrics_entry["output_tokens"] = token_counts.get("output_tokens", 0)
        state["metrics"]["total_input_tokens"] += token_counts.get("input_tokens", 0)
        state["metrics"]["total_output_tokens"] += token_counts.get("output_tokens", 0)
        
        clean_start = time.time()
        plan = clean_llm_response(plan)
        parsed = json.loads(plan)
        metrics_entry["steps"]["clean_response"] = {"duration_ms": (time.time() - clean_start) * 1000}
        
        # Extract error details from RCA metadata and rca
        error_details = {
            "timestamp": metadata.get("analysis_timestamp", "unknown"),
            "log_level": metadata.get("log_level", "unknown"),
            "log_message": rca.get("log_summary", ""),
            "log_type": metadata.get("log_type", "unknown"),
            "summary": rca.get("log_summary", ""),
            "java_class": rca.get("java_class", ""),
            "thread": rca.get("thread", ""),
            "stack_trace": rca.get("stack_trace", ""),
            "component": rca.get("component", "")
        }

        # Construct full remediation result
        result = {
            "log_id": log_id,
            "error_details": error_details,
            "rca_details": {
                "summary": rca.get("summary", ""),
                "detailed_analysis": rca.get("detailed_analysis", ""),
                "root_causes": rca.get("root_causes", []),
                "system_state": rca.get("system_state", {}),
                "java_class": rca.get("java_class", ""),
                "thread": rca.get("thread", ""),
                "log_summary": rca.get("log_summary", ""),
                "stack_trace": rca.get("stack_trace", ""),
                "component": rca.get("component", ""),
                "recommended_actions": recommended_actions,
                "severity": severity,
                "confidence": confidence,
                "category": category,
                "metadata": metadata
            },
            "remediation_plan": parsed.get("remediation_plan", {}),
            "priority": parsed.get("priority", "MEDIUM").upper(),
            "required_resources": parsed.get("required_resources", [{"type": "TEAM", "description": "Operations Team", "reason": "Execute remediation steps"}]),
            "risk_assessment": parsed.get("risk_assessment", {"impact_level": "MEDIUM", "potential_risks": []}),
            "remediation_metadata": {
                "remediation_timestamp": datetime.now(UTC).isoformat() + "Z"
            }
        }

        # Validate priority
        if result["priority"] not in ["HIGH", "MEDIUM", "LOW"]:
            result["priority"] = "MEDIUM"

        state["remediation_result"] = result
        metrics_entry["status"] = "success"
        metrics_entry["log_id"] = log_id
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["model_status"] = "idle"
        state["current_log_message"] = None
        return state

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response for log_id %s: %s", log_id, str(e))
        error_response = json.dumps(create_error_result(log_id, rca, metadata, recommended_actions, severity, confidence, category, str(e)))
        token_counts = {
            "input_tokens": llm_interface._estimate_tokens(system_prompt + user_prompt),
            "output_tokens": llm_interface._estimate_tokens(error_response)
        }
        state["remediation_result"] = json.loads(error_response)
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
    except Exception as e:
        logger.error("Error generating remediation for log_id %s: %s", log_id, str(e))
        error_response = json.dumps(create_error_result(log_id, rca, metadata, recommended_actions, severity, confidence, category, str(e)))
        token_counts = {
            "input_tokens": llm_interface._estimate_tokens(system_prompt + user_prompt),
            "output_tokens": llm_interface._estimate_tokens(error_response)
        }
        state["remediation_result"] = json.loads(error_response)
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

def create_error_result(log_id: str, rca: dict, metadata: dict, recommended_actions: list, severity: str, confidence: str, category: str, error: str) -> dict:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "create_error_result",
        "steps": {},
        "log_id": log_id,
        "total_duration_ms": 0
    }
    
    error_details = {
        "timestamp": metadata.get("analysis_timestamp", "unknown"),
        "log_level": metadata.get("log_level", "unknown"),
        "log_message": rca.get("log_summary", ""),
        "log_type": metadata.get("log_type", "unknown"),
        "summary": rca.get("log_summary", ""),
        "java_class": rca.get("java_class", ""),
        "thread": rca.get("thread", ""),
        "stack_trace": rca.get("stack_trace", ""),
        "component": rca.get("component", "")
    }
    result = {
        "log_id": log_id,
        "error_details": error_details,
        "rca_details": {
            "summary": rca.get("summary", ""),
            "detailed_analysis": rca.get("detailed_analysis", ""),
            "root_causes": rca.get("root_causes", []),
            "system_state": rca.get("system_state", {}),
            "java_class": rca.get("java_class", ""),
            "thread": rca.get("thread", ""),
            "log_summary": rca.get("log_summary", ""),
            "stack_trace": rca.get("stack_trace", ""),
            "component": rca.get("component", ""),
            "recommended_actions": recommended_actions,
            "severity": severity,
            "confidence": confidence,
            "category": category,
            "metadata": metadata
        },
        "remediation_plan": {"summary": f"Remediation generation failed: {error}", "steps": [], "prerequisites": [], "estimated_timeline": {"total_duration": "Unknown", "breakdown": []}},
        "priority": "LOW",
        "required_resources": [{"type": "TEAM", "description": "Operations Team", "reason": "Investigate remediation failure"}],
        "risk_assessment": {"impact_level": "LOW", "potential_risks": [{"risk": "Remediation failure", "mitigation": "Manual intervention"}]},
        "remediation_metadata": {
            "remediation_timestamp": datetime.now(UTC).isoformat() + "Z"
        }
    }
    
    metrics_entry["status"] = "success"
    metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
    state["metrics"]["runs"].append(metrics_entry)
    save_metrics()
    return result

def publish_to_kafka_remediation(state: AgentState) -> AgentState:
    start_time = time.time()
    metrics_entry = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "function": "publish_to_kafka_remediation",
        "steps": {}
    }
    
    try:
        if not state["remediation_result"]:
            logger.error("No remediation result to publish")
            metrics_entry["status"] = "error"
            metrics_entry["error"] = "No remediation result"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["publish_result"] = "ERROR: No remediation result"
            return state
        
        data = json.dumps(state["remediation_result"], indent=2)
        logger.debug("Publishing remediation to logs.remediation: %s", data[:200] + "..." if len(data) > 200 else data)
        
        validate_start = time.time()
        parsed = json.loads(data)
        required_fields = ["log_id", "error_details", "rca_details", "remediation_plan", "priority"]
        missing_fields = [field for field in required_fields if field not in parsed]
        if missing_fields:
            logger.error("Missing required fields in remediation JSON: %s", missing_fields)
            metrics_entry["status"] = "error"
            metrics_entry["error"] = f"Missing required fields - {', '.join(missing_fields)}"
            metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
            state["metrics"]["runs"].append(metrics_entry)
            save_metrics()
            state["publish_result"] = f"ERROR: Missing required fields - {', '.join(missing_fields)}"
            return state
        metrics_entry["steps"]["validate_json"] = {"duration_ms": (time.time() - validate_start) * 1000}
        metrics_entry["log_id"] = parsed.get("log_id", "unknown")
        
        produce_start = time.time()
        def delivery_callback(err, msg):
            if err:
                logger.error("Failed to deliver remediation message: %s", err)
            else:
                logger.info("Remediation message delivered to partition %d at offset %d",
                           msg.partition(), msg.offset())
        
        producer.produce(
            "logs.remediation",
            value=data.encode("utf-8"),
            callback=delivery_callback
        )
        producer.flush(timeout=10.0)
        metrics_entry["steps"]["kafka_produce"] = {"duration_ms": (time.time() - produce_start) * 1000}
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = "SUCCESS: Published remediation to Kafka"
        return state
        
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in remediation data: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Invalid JSON format - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = f"ERROR: Invalid JSON format - {str(e)}"
        return state
    except KafkaError as e:
        logger.error("Kafka error publishing remediation: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Kafka publishing failed - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
        state["metrics"]["runs"].append(metrics_entry)
        save_metrics()
        state["publish_result"] = f"ERROR: Kafka publishing failed - {str(e)}"
        return state
    except Exception as e:
        logger.error("Unexpected error publishing remediation: %s", str(e))
        metrics_entry["status"] = "error"
        metrics_entry["error"] = f"Unexpected error - {str(e)}"
        metrics_entry["total_duration_ms"] = (time.time() - start_time) * 1000
        state["metrics"]["total_errors"] += 1
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
workflow.add_node("perform_remediation", perform_remediation)
workflow.add_node("publish_to_kafka_remediation", publish_to_kafka_remediation)
workflow.add_node("commit_message_offset", commit_message_offset)
workflow.add_conditional_edges(
    "kafka_subgraph",
    lambda state: "perform_remediation" if state["validation_passed"] else END
)
workflow.add_edge("perform_remediation", "publish_to_kafka_remediation")
workflow.add_conditional_edges(
    "publish_to_kafka_remediation",
    lambda state: "commit_message_offset" if state["publish_result"] and "SUCCESS" in state["publish_result"] else END
)
workflow.add_edge("commit_message_offset", END)
workflow.set_entry_point("kafka_subgraph")

graph = workflow.compile()

def main():
    logger.info("Starting LangGraph Remediation Agent")
    api_thread = Thread(target=run_api, daemon=True)
    api_thread.start()
    
    try:
        while True:
            state = initialize_state()
            graph.invoke(state, config={"recursion_limit": 1000})
            global_metrics["last_run_status"] = "running"
            save_metrics()
            if state["log_data"] is None and not state["validation_passed"]:
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        global_metrics["last_run_status"] = "stopped"
        save_metrics()
        save_processed_ids()
    finally:
        logger.info("Shutting down Remediation Agent...")
        try:
            consumer.close()
            producer.flush()
        except Exception as e:
            logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()