import os
import json
import re
import logging
import traceback
from uuid import uuid4
from datetime import datetime, timezone
from typing import TypedDict, List, Optional, Dict, Any
import time
import asyncio
from collections import defaultdict
import threading
import concurrent.futures
from queue import Queue

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from elasticsearch import AsyncElasticsearch
from langchain.tools import Tool
from langchain.prompts import PromptTemplate as LCPromptTemplate
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage
from langchain.chains.summarize import load_summarize_chain
from langchain_core.documents import Document
from langgraph.graph import StateGraph, END
import tiktoken
from contextlib import asynccontextmanager
from src.agents.llm_interface import LLMInterface, runtime_configs

# Logger Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Observibot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (if any)
    logger.info("Starting up Observibot...")
    yield  # This is where the application runs
    # Shutdown logic
    logger.info("Shutting down Observibot...")
    try:
        await es.close()
        logger.info("Elasticsearch connection closed.")
    except Exception as e:
        logger.error(f"Error closing Elasticsearch connection: {str(e)}")
    with metrics_lock:
        global_metrics["runs"].append({
            "run_id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "function": "shutdown",
            "status": "success",
            "total_duration_ms": 0
        })
        save_metrics()
    logger.info("Shutdown complete.")
    
# Global Metrics
global_metrics = {
    "total_queries_processed": 0,
    "total_errors": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "runs": [],
    "start_time": time.time(),
    "node_counts": defaultdict(int),
    "node_durations": defaultdict(float),
    "node_errors": defaultdict(int)
}
metrics_lock = threading.Lock()

def save_metrics():
    try:
        with metrics_lock:
            with open("observibot_metrics.json", "w") as f:
                json.dump(global_metrics, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save metrics: {str(e)}")

def log_and_truncate_response(response: Any, max_length: int = 800) -> None:
    msg = json.dumps(response, indent=2) if isinstance(response, (dict, list)) else str(response)
    if len(msg) > max_length:
        logger.info("Response: %s... [TRUNCATED %d chars]", msg[:max_length], len(msg))
    else:
        logger.info("Response: %s", msg)

def clean_llm_response(text: Any) -> str:
    if not text or not isinstance(text, str):
        logger.warning(f"Non-string response in clean_llm_response: {str(text)[:200]}")
        return ""
    text = re.sub(r"<think>.*?</think>|\.\.\.", "", text, flags=re.DOTALL)
    return text.strip()

# FastAPI Setup
app = FastAPI(
    title="Observibot",
    description="LLM agent system for Observix platform using LangGraph.",
    version="1.0.0",
    lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Request Models
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user query to process")
    session_id: Optional[str] = Field(None, description="Session ID for conversation history")

class TraceRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user query to trace")
    session_id: Optional[str] = Field(None, description="Session ID for conversation history")

# Elasticsearch Connection
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
INDEX = os.getenv("ELASTICSEARCH_INDEX", "observix-results-*")
es = AsyncElasticsearch(
    ES_URL,
    verify_certs=os.getenv("ELASTICSEARCH_VERIFY_CERTS", "true").lower() == "true",
    retry_on_timeout=True,
    max_retries=3,
    request_timeout=30
)

# LLM Configuration
config = runtime_configs.get(os.getenv("MODEL_RUNTIME", "gemini").lower(), runtime_configs["gemini"])
llm_interface = LLMInterface()
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            llm_interface.provider, llm_interface.model, llm_interface.endpoint or "default")
llm = llm_interface.llm

# Token Calculation
def count_tokens(text: str, provider: str, model: str, max_length: int = 500) -> int:
    try:
        return llm_interface._estimate_tokens(text)
    except Exception as e:
        logger.warning(f"Token counting failed: {str(e)}")
        return min(len(text) // 4, max_length)

# State Schema
class ObservibotState(TypedDict):
    query: str
    session_id: str
    query_type: Optional[str]
    filters: Optional[Dict]
    requested_fields: Optional[List[str]]
    intermediate_output: Optional[str]
    final_output: Optional[str]
    token_usage: Dict[str, Dict[str, int]]
    chat_history: List[Any]
    trace: List[Dict]
    agent_scratchpad: List[Dict]
    node_durations: Dict[str, float]

# Utility Functions
def extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*?\}|\[.*?\])', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON-like content: {match.group(1)}")
        logger.error(f"Unable to extract JSON from: {text[:200]}...")
        raise ValueError("Unable to extract JSON")

def build_filters(user_filters: dict) -> dict:
    mapped = {}
    for k, v in user_filters.items():
        key = k.lower()
        if key == "severity":
            mapped["rca_details.severity"] = v.upper()
        elif key == "date":
            mapped["timestamp"] = {"gte": v.get("gte", "now-30d"), "lte": v.get("lte", "now")}
        elif key == "source":
            type_map = {
                "cpuvgm": "CPUVGM",
                "infralog": "INFRA",
                "fiscd": "FISCD",
                "vgmlog": "VGM",
                "vgm": "VGM",
                "netprobe": "NETPROBE",
                "gateway": "GATEWAY"
            }
            mapped["error_details.log_type"] = type_map.get(v.lower(), v.upper())
        else:
            mapped[k] = v
    return mapped

def build_es_query(filters: dict) -> dict:
    query = {"bool": {"filter": []}}
    for field, value in filters.items():
        if isinstance(value, dict) and "gte" in value:
            query["bool"]["filter"].append({"range": {field: value}})
        else:
            query["bool"]["filter"].append({"term": {field: value}})
    return query

MINIMAL_FIELDS = [
    "log_id", "timestamp", "error_details.summary",
    "rca_details.summary", "rca_details.severity", "rca_details.category", "error_details.log_type"
]
REMEDIATION_FIELDS = ["remediation_plan.summary", "remediation_plan.steps.action"]
ROOT_CAUSE_FIELDS = ["rca_details.root_causes.cause"]
DETAILED_ANALYSIS_FIELDS = ["rca_details.detailed_analysis"]
ALL_FIELDS = MINIMAL_FIELDS + REMEDIATION_FIELDS + ROOT_CAUSE_FIELDS + DETAILED_ANALYSIS_FIELDS

async def get_fields_by_request(user_input: str, explicit_fields: Optional[List[str]] = None) -> List[str]:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "get_fields_by_request",
        "status": "running",
        "steps": {}
    }
    final = set(explicit_fields or MINIMAL_FIELDS)
    query_lower = user_input.lower()
    
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', query_lower)
    is_full_details = "full details" in query_lower and log_id_match
    is_root_cause = any(k in query_lower for k in ["root cause", "cause of", "why did"])
    is_remediation = any(k in query_lower for k in ["how to fix", "remediation", "solution", "fix it"])
    is_health_query = any(k in query_lower for k in ["health", "status", "overview"])
    
    if is_full_details:
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return ALL_FIELDS
    elif is_root_cause:
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return list(set(MINIMAL_FIELDS + ROOT_CAUSE_FIELDS))
    elif is_remediation:
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return list(set(MINIMAL_FIELDS + REMEDIATION_FIELDS))
    elif is_health_query:
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return ALL_FIELDS
    
    prompt = f"""System: Return a JSON list of field names relevant to the query. Include all MINIMAL_FIELDS and add fields based on query intent. Do not include explanations or tags like <think>.

**Query**: {user_input}
**Available Fields**:
- MINIMAL_FIELDS: {', '.join(MINIMAL_FIELDS)}
- REMEDIATION_FIELDS: {', '.join(REMEDIATION_FIELDS)}
- ROOT_CAUSE_FIELDS: {', '.join(ROOT_CAUSE_FIELDS)}
- DETAILED_ANALYSIS_FIELDS: {', '.join(DETAILED_ANALYSIS_FIELDS)}

**Instructions**:
1. Always include MINIMAL_FIELDS.
2. Add REMEDIATION_FIELDS if the query mentions fixes, remediation, or solutions.
3. Add ROOT_CAUSE_FIELDS if the query asks about causes or reasons.
4. Add DETAILED_ANALYSIS_FIELDS if the query requests detailed analysis, traces, or full details.
5. For health/status queries, include all fields.
6. Return only a JSON list of field names.

Example: ["log_id", "timestamp", "error_details.summary", ...]
"""
    llm_start = time.perf_counter()
    try:
        response, token_counts = await llm_interface.call("", prompt, timeout=30)
        response_text = clean_llm_response(response)
        fields = extract_json(response_text)
        metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
        metrics_entry["input_tokens"] = token_counts.get("input_tokens", 0)
        metrics_entry["output_tokens"] = token_counts.get("output_tokens", 0)
        with metrics_lock:
            global_metrics["total_input_tokens"] += token_counts.get("input_tokens", 0)
            global_metrics["total_output_tokens"] += token_counts.get("output_tokens", 0)
        if not isinstance(fields, list):
            logger.warning(f"LLM returned non-list fields: {response_text}")
            fields = list(final)
        fields = list(set(fields) | set(MINIMAL_FIELDS))
        metrics_entry["status"] = "success"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return fields
    except Exception as e:
        logger.error(f"[get_fields_by_request Error] {str(e)}. Raw response: {'N/A' if 'response_text' not in locals() else response_text[:200]}...")
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["total_errors"] += 1
            global_metrics["node_errors"]["get_fields_by_request"] += 1
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["get_fields_by_request"] += 1
            global_metrics["node_durations"]["get_fields_by_request"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return list(final)

def format_issue(issue: dict, fields: List[str], is_health_query: bool = False) -> str:
    def show(f): return not fields or f in fields
    lines = ["## Issue Details"]
    if is_health_query:
        if show("log_id"):
            lines.append(f"- **Log ID**: {issue.get('log_id', 'N/A')}")
        if show("rca_details.severity"):
            lines.append(f"- **Severity**: {issue.get('rca_details', {}).get('severity', 'N/A')}")
        if show("error_details.log_type"):
            lines.append(f"- **Log Type**: {issue.get('error_details', {}).get('log_type', 'N/A')}")
    else:
        if show("log_id"):
            lines.append(f"- **Log ID**: {issue.get('log_id', 'N/A')}")
        if show("timestamp"):
            lines.append(f"- **Timestamp**: {issue.get('timestamp', 'N/A')}")
        if show("rca_details.severity"):
            lines.append(f"- **Severity**: {issue.get('rca_details', {}).get('severity', 'N/A')}")
        if show("rca_details.category"):
            lines.append(f"- **Category**: {issue.get('rca_details', {}).get('category', 'N/A')}")
        if show("error_details.log_type"):
            lines.append(f"- **Log Type**: {issue.get('error_details', {}).get('log_type', 'N/A')}")
        summary = issue.get("error_details", {}).get("summary") or issue.get("rca_details", {}).get("summary")
        if summary and show("error_details.summary"):
            lines.append(f"- **Summary**: {summary}")
        causes = issue.get("rca_details", {}).get("root_causes", [])
        if causes and show("rca_details.root_causes.cause"):
            lines.append("- **Root Cause(s)**:")
            for c in causes:
                lines.append(f"  - {c.get('cause', 'N/A')}")
        if show("rca_details.detailed_analysis"):
            analysis = issue.get("rca_details", {}).get("detailed_analysis", "N/A")
            lines.append(f"- **Detailed Analysis**: {analysis}")
        if show("remediation_plan.summary") and "remediation_plan.summary" in fields:
            rem = issue.get("remediation_plan", {})
            if rem.get("summary"):
                lines.append(f"- **Remediation Plan**: {rem.get('summary', 'N/A')}")
        if show("remediation_plan.steps.action") and "remediation_plan.steps.action" in fields:
            rem = issue.get("remediation_plan", {})
            if rem.get("steps"):
                lines.append("- **Remediation Steps**:")
                lines.extend([f"  - Step {idx+1}: {step.get('action', 'N/A')}" for idx, step in enumerate(rem["steps"])])
    return "\n".join(lines) or "## Issue Details\n\nNo relevant information available."

def format_final_response(response: str, is_health_query: bool = False) -> str:
    if response == "NO_RESULTS_FOUND":
        return "# System Response\n\nNo matching records found for the query."
    if not response.startswith("#"):
        return f"# System Response\n\n{response}"
    return response

# Tool Functions
async def tool_filter(params: str) -> str:
    start_time = time.perf_counter()
    try:
        data = extract_json(params)
        filters = build_filters(data.get("filters", {}))
        fields = data.get("fields") or MINIMAL_FIELDS
        is_health_query = data.get("is_health_query", False)
        body = {"query": build_es_query(filters), "_source": fields, "size": data.get("size", 100 if is_health_query else 5)}
        results = await es.search(index=INDEX, body=body)
        hits = [hit["_source"] for hit in results["hits"]["hits"]]
        formatted = [format_issue(doc, fields, is_health_query) for doc in hits]
        logger.info(f"Filter matched {len(formatted)} record(s).")
        result = json.dumps(hits) if hits else "NO_RESULTS_FOUND"
        with metrics_lock:
            global_metrics["node_counts"]["tool_filter"] += 1
            global_metrics["node_durations"]["tool_filter"] += (time.perf_counter() - start_time) * 1000
        return result
    except Exception as e:
        logger.error(f"[Filter Tool Error] {str(e)}")
        with metrics_lock:
            global_metrics["node_errors"]["tool_filter"] += 1
            global_metrics["node_counts"]["tool_filter"] += 1
            global_metrics["node_durations"]["tool_filter"] += (time.perf_counter() - start_time) * 1000
        return json.dumps({"error": str(e)})

async def tool_get_by_id(params: str) -> str:
    start_time = time.perf_counter()
    try:
        data = extract_json(params)
        log_id = data.get("log_id")
        if not log_id:
            raise ValueError("log_id is required")
        fields = data.get("fields") or MINIMAL_FIELDS
        result = await es.search(
            index=INDEX,
            body={"query": {"term": {"log_id": log_id}}, "_source": fields, "size": 1}
        )
        if not result["hits"]["hits"]:
            return json.dumps({"error": f"No record found for log_id: {log_id}"})
        with metrics_lock:
            global_metrics["node_counts"]["tool_get_by_id"] += 1
            global_metrics["node_durations"]["tool_get_by_id"] += (time.perf_counter() - start_time) * 1000
        return json.dumps(result["hits"]["hits"][0]["_source"])
    except Exception as e:
        logger.error(f"[GetErrorById Error] {str(e)}")
        with metrics_lock:
            global_metrics["node_errors"]["tool_get_by_id"] += 1
            global_metrics["node_counts"]["tool_get_by_id"] += 1
            global_metrics["node_durations"]["tool_get_by_id"] += (time.perf_counter() - start_time) * 1000
        return json.dumps({"error": str(e)})

async def tool_summarize_results(results: str) -> str:
    start_time = time.perf_counter()
    if not results or results == "NO_RESULTS_FOUND":
        with metrics_lock:
            global_metrics["node_counts"]["tool_summarize_results"] += 1
            global_metrics["node_durations"]["tool_summarize_results"] += (time.perf_counter() - start_time) * 1000
        return "# System Response\n\nNo results to summarize."
    template = LCPromptTemplate(
        input_variables=["text"],
        template="System: Return a concise Markdown summary (150–200 words) without explanations or tags like <think>.\n\nRead the issues and provide a summary in Markdown format:\n\n{text}\n\n# Summary\n"
    )
    chain = load_summarize_chain(llm, chain_type="stuff", prompt=template)
    documents = [Document(page_content=results)]
    try:
        response = await chain.ainvoke({"input_documents": documents})
        logger.info(f"Summarize response type: {type(response)}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
        if isinstance(response, dict):
            summary = clean_llm_response(response.get("output_text", response.get("content", "Summary could not be generated.")))
        elif isinstance(response, str):
            summary = clean_llm_response(response)
        elif hasattr(response, "content"):
            summary = clean_llm_response(response.content)
        else:
            logger.error(f"Unexpected response format in summarize_results: {type(response)}, content: {str(response)[:200]}")
            summary = "Summary could not be generated due to unexpected response format."
        if not summary.startswith("#"):
            summary = f"# Summary\n\n{summary}"
        with metrics_lock:
            global_metrics["node_counts"]["tool_summarize_results"] += 1
            global_metrics["node_durations"]["tool_summarize_results"] += (time.perf_counter() - start_time) * 1000
        return summary
    except Exception as e:
        logger.error(f"[SummarizeResults Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
        with metrics_lock:
            global_metrics["node_errors"]["tool_summarize_results"] += 1
            global_metrics["node_counts"]["tool_summarize_results"] += 1
            global_metrics["node_durations"]["tool_summarize_results"] += (time.perf_counter() - start_time) * 1000
        return f"# System Response\n\nSummary failed: {str(e)}."

# LangGraph Nodes
async def classify_query(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    query = state["query"].lower()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "classify_query",
        "status": "running",
        "steps": {}
    }
    fields = await get_fields_by_request(query)
    state["requested_fields"] = fields
    filter_patterns = r'^(list|show|find|give\s+me|all)\b.*(issues|errors|logs)'
    health_patterns = r'\b(health|status|overview|system\s*(health|status))\b'
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', query)
    if log_id_match:
        query_type = "log_id"
        state["token_usage"][config["provider"]] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
        logger.info(f"Pre-LLM classification: {query_type}")
    elif re.match(health_patterns, query):
        query_type = "health"
        state["token_usage"][config["provider"]] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
        logger.info(f"Pre-LLM classification: {query_type}")
    elif re.match(filter_patterns, query):
        query_type = "filter"
        state["token_usage"][config["provider"]] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
        logger.info(f"Pre-LLM classification: {query_type}")
    else:
        prompt = f"""System: Return only the final query type (log_id, filter, health, summarize, greeting, unknown) without explanations, reasoning, or tags like <think>.

Classify the query into one of: log_id, filter, health, summarize, greeting, unknown.
Rules:
- If it contains a log_id (20-24 alphanumeric chars, e.g., 's2GkZJcB4rxvVeed6fc1'), choose log_id.
- If it starts with 'list', 'show', 'find', 'give me', or 'all <condition>' (e.g., 'high severity platform errors', 'vgmlog errors') without a log_id, choose filter.
- If it contains 'health', 'status', or 'overview' (e.g., 'system health'), choose health.
- If it explicitly requests a summary (e.g., 'summarize NETWORK issues'), choose summarize.
- If it is a greeting (e.g., 'hi', 'hello'), choose greeting.
- If none of the above, choose unknown.
Query: {query}
"""
        llm_start = time.perf_counter()
        try:
            response, token_counts = await llm_interface.call("", prompt, timeout=30)
            query_type = clean_llm_response(response)
            query_type_match = re.search(r'(log_id|filter|health|summarize|greeting|unknown)', query_type, re.IGNORECASE)
            query_type = query_type_match.group(0).lower() if query_type_match else "unknown"
            metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
            metrics_entry["input_tokens"] = token_counts.get("input_tokens", 0)
            metrics_entry["output_tokens"] = token_counts.get("output_tokens", 0)
            state["token_usage"][config["provider"]] = {
                "prompt_tokens": token_counts.get("input_tokens", 0),
                "completion_tokens": token_counts.get("output_tokens", 0),
                "total_tokens": token_counts.get("input_tokens", 0) + token_counts.get("output_tokens", 0)
            }
            with metrics_lock:
                global_metrics["total_input_tokens"] += token_counts.get("input_tokens", 0)
                global_metrics["total_output_tokens"] += token_counts.get("output_tokens", 0)
        except Exception as e:
            logger.error(f"[classify_query Error] Failed to parse LLM response: {str(e)}. Raw response: {'N/A' if 'response' not in locals() else response[:200]}")
            query_type = "unknown"
            state["token_usage"][config["provider"]] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    state["query_type"] = query_type
    state["trace"].append({
        "node": "classify_query",
        "input": query,
        "output": query_type,
        "fields_used": state.get("requested_fields", []),
        "token_usage": state["token_usage"][config["provider"]],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["classify_query"] = time.perf_counter() - start_time
    metrics_entry["status"] = metrics_entry.get("status", "success")
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["classify_query"] += 1
        global_metrics["node_durations"]["classify_query"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["classify_query"] += 1
    save_metrics()
    return state

async def parse_filters(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    query = state["query"].lower()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "parse_filters",
        "status": "success",
        "steps": {}
    }
    filters = {"date": {"gte": "now-30d", "lte": "now"}} if state["query_type"] == "health" else {}
    if state["query_type"] != "health":
        if "high severity" in query:
            filters["severity"] = "HIGH"
        if any(t in query for t in ["platform", "cpuvgm"]):
            filters["source"] = "CPUVGM"
        if "infralog" in query:
            filters["source"] = "InfraLog"
        if "fiscd" in query:
            filters["source"] = "FISCD"
        if any(t in query for t in ["vgmlog", "vgm"]):
            filters["source"] = "vgmlog"
        if "netprobe" in query:
            filters["source"] = "netprobe"
        if "gateway" in query:
            filters["source"] = "gateway"
        if "network" in query:
            filters["category"] = "NETWORK"
        if "application" in query:
            filters["category"] = "APPLICATION"
        if "last 30 days" in query:
            filters["date"] = {"gte": "now-30d", "lte": "now"}
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(query)
        state["requested_fields"] = fields
    else:
        fields = state["requested_fields"]
    state["filters"] = filters
    state["trace"].append({
        "node": "parse_filters",
        "input": query,
        "output": filters,
        "fields_used": state.get("requested_fields", []),
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["parse_filters"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["parse_filters"] += 1
        global_metrics["node_durations"]["parse_filters"] += metrics_entry["total_duration_ms"]
    save_metrics()
    return state

async def execute_filter(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "execute_filter",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    params = json.dumps({
        "filters": state["filters"],
        "user_request": state["query"],
        "fields": fields,
        "is_health_query": state["query_type"] == "health"
    })
    try:
        results = await tool_filter(params)
        state["intermediate_output"] = results
    except Exception as e:
        logger.error(f"[execute_filter Error] {str(e)}")
        state["intermediate_output"] = json.dumps({"error": str(e)})
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
    state["trace"].append({
        "node": "execute_filter",
        "input": params,
        "fields_used": state.get("requested_fields", []),
        "output": state["intermediate_output"],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_filter"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["execute_filter"] += 1
        global_metrics["node_durations"]["execute_filter"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["execute_filter"] += 1
    save_metrics()
    return state

async def execute_get_by_id(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "execute_get_by_id",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', state["query"])
    log_id = log_id_match.group(0) if log_id_match else None
    if not log_id:
        state["intermediate_output"] = json.dumps({"error": "No valid log_id found in query."})
        metrics_entry["status"] = "error"
        metrics_entry["error"] = "No valid log_id found in query."
    else:
        params = json.dumps({
            "log_id": log_id,
            "user_request": state["query"],
            "fields": fields
        })
        try:
            result = await tool_get_by_id(params)
            state["intermediate_output"] = result
        except Exception as e:
            logger.error(f"[execute_get_by_id Error] {str(e)}")
            state["intermediate_output"] = json.dumps({"error": str(e)})
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    state["trace"].append({
        "node": "execute_get_by_id",
        "input": params if log_id else state["query"],
        "fields_used": state.get("requested_fields", []),
        "output": state["intermediate_output"],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_get_by_id"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["execute_get_by_id"] += 1
        global_metrics["node_durations"]["execute_get_by_id"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["execute_get_by_id"] += 1
    save_metrics()
    return state

async def execute_health_check(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "execute_health_check",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    params = json.dumps({
        "filters": state["filters"],
        "user_request": state["query"],
        "fields": fields,
        "is_health_query": True
    })
    try:
        results = await tool_filter(params)
        state["intermediate_output"] = results
    except Exception as e:
        logger.error(f"[execute_health_check Error] {str(e)}")
        state["intermediate_output"] = json.dumps({"error": str(e)})
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
    state["trace"].append({
        "node": "execute_health_check",
        "input": params,
        "fields_used": state.get("requested_fields", []),
        "output": state["intermediate_output"],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_health_check"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["execute_health_check"] += 1
        global_metrics["node_durations"]["execute_health_check"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["execute_health_check"] += 1
    save_metrics()
    return state

async def transform_output(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "transform_output",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    
    if not state.get("intermediate_output") or state["intermediate_output"] == "NO_RESULTS_FOUND":
        state["final_output"] = "# System Response\n\nNo matching records found for the query."
        state["trace"].append({
            "node": "transform_output",
            "input": state["intermediate_output"],
            "fields_used": state.get("requested_fields", []),
            "output": state["final_output"],
            "token_usage": state["token_usage"][config["provider"]],
            "duration": time.perf_counter() - start_time
        })
        state["node_durations"]["transform_output"] = time.perf_counter() - start_time
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["transform_output"] += 1
            global_metrics["node_durations"]["transform_output"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return state
    
    try:
        result = json.loads(state["intermediate_output"])
        if isinstance(result, dict) and "error" in result:
            state["final_output"] = f"# System Response\n\n{result['error']}"
            state["trace"].append({
                "node": "transform_output",
                "input": state["intermediate_output"],
                "fields_used": state.get("requested_fields", []),
                "output": state["final_output"],
                "token_usage": state["token_usage"][config["provider"]],
                "duration": time.perf_counter() - start_time
            })
            state["node_durations"]["transform_output"] = time.perf_counter() - start_time
            metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
            with metrics_lock:
                global_metrics["runs"].append(metrics_entry)
                global_metrics["node_counts"]["transform_output"] += 1
                global_metrics["node_durations"]["transform_output"] += metrics_entry["total_duration_ms"]
            save_metrics()
            return state
        logs = [result] if isinstance(result, dict) else result
        limited_input = json.dumps(logs)
    except json.JSONDecodeError:
        logger.warning(f"Non-JSON intermediate_output: {state['intermediate_output'][:200]}...")
        state["final_output"] = "# System Response\n\nError: Invalid response format from query execution."
        state["trace"].append({
            "node": "transform_output",
            "input": state["intermediate_output"],
            "fields_used": state.get("requested_fields", []),
            "output": state["final_output"],
            "token_usage": state["token_usage"][config["provider"]],
            "duration": time.perf_counter() - start_time
        })
        state["node_durations"]["transform_output"] = time.perf_counter() - start_time
        metrics_entry["status"] = "error"
        metrics_entry["error"] = "Invalid response format from query execution"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["transform_output"] += 1
            global_metrics["node_durations"]["transform_output"] += metrics_entry["total_duration_ms"]
            global_metrics["node_errors"]["transform_output"] += 1
        save_metrics()
        return state
    
    query_type = state["query_type"].lower()
    llm_start = time.perf_counter()
    if query_type == "filter":
        formatted_logs = [format_issue(log, fields, is_health_query=False) for log in logs]
        summary = f"# Query Results: {state['query']}\n\n"
        if not formatted_logs:
            summary += "No matching records found for the query."
        else:
            summary += "\n".join(f"### Result {idx + 1}\n\n{log}" for idx, log in enumerate(formatted_logs))
        prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
        completion_tokens = count_tokens(summary, config["provider"], config["model"])
    elif query_type == "health":
        prompt_template = LCPromptTemplate(
            input_variables=["text"],
            template="""System: You are a health analytics agent. Return a concise Markdown summary (150–200 words) without explanations or tags like <think>.

Provide:
- **Error Types**: Count and types of major errors (e.g., QueueFull, session errors).
- **Clients Affected**: Most impacted client/session IDs.
- **Categories**: Network, application, or infrastructure issues.
- **Time Clusters**: Dates of high-severity errors.
- **Severity**: Overall impact severity.

Logs:
{text}

# System Health Summary
"""
        )
        chain = load_summarize_chain(llm, chain_type="stuff", prompt=prompt_template)
        documents = [Document(page_content=limited_input)]
        try:
            response = await chain.ainvoke({"input_documents": documents})
            logger.info(f"Transform response type: {type(response)}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
            if isinstance(response, dict):
                summary = clean_llm_response(response.get("output_text", response.get("content", "Summary could not be generated.")))
            elif isinstance(response, str):
                summary = clean_llm_response(response)
            elif hasattr(response, "content"):
                summary = clean_llm_response(response.content)
            else:
                logger.error(f"Unexpected response format in transform_output: {type(response)}, content: {str(response)[:200]}")
                summary = "Summary could not be generated due to unexpected response format."
            if not summary.startswith("#"):
                summary = f"# System Health Summary\n\n{summary}"
            prompt = prompt_template.template.format(text=limited_input)
            prompt_tokens = count_tokens(prompt, config["provider"], config["model"])
            completion_tokens = count_tokens(summary, config["provider"], config["model"])
            metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
            metrics_entry["input_tokens"] = prompt_tokens
            metrics_entry["output_tokens"] = completion_tokens
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nSummary failed: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    elif query_type == "log_id":
        formatted_logs = [format_issue(log, fields, is_health_query=False) for log in logs]
        summary = f"# Log Details: {state['query']}\n\n"
        if not formatted_logs:
            summary += "No matching records found for the query."
        else:
            summary += formatted_logs[0]  # Single log expected for log_id query
        prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
        completion_tokens = count_tokens(summary, config["provider"], config["model"])
    elif query_type == "summarize":
        prompt_template = LCPromptTemplate(
            input_variables=["text", "query"],
            template="""System: Return a concise Markdown summary (150–200 words) for the query '{query}'.

Provide:
- **Summary**: Key insights from the logs, focusing on errors, severity, and categories.
- **Query Relevance**: How the summary addresses the query.

Logs:
{text}

# Summary for Query: {query}
"""
        )
        chain = load_summarize_chain(llm, chain_type="stuff", prompt=prompt_template)
        documents = [Document(page_content=limited_input)]
        try:
            response = await chain.ainvoke({"input_documents": documents, "text": limited_input, "query": state["query"]})
            logger.info(f"Transform response type: {type(response)}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
            if isinstance(response, dict):
                summary = clean_llm_response(response.get("output_text", response.get("content", "Summary could not be generated.")))
            elif isinstance(response, str):
                summary = clean_llm_response(response)
            elif hasattr(response, "content"):
                summary = clean_llm_response(response.content)
            else:
                logger.error(f"Unexpected response format in transform_output: {type(response)}, content: {str(response)[:200]}")
                summary = "Summary could not be generated due to unexpected response format."
            if not summary.startswith("#"):
                summary = f"# Summary for Query: {state['query']}\n\n{summary}"
            prompt = prompt_template.template.format(text=limited_input, query=state["query"])
            prompt_tokens = count_tokens(prompt, config["provider"], config["model"])
            completion_tokens = count_tokens(summary, config["provider"], config["model"])
            metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
            metrics_entry["input_tokens"] = prompt_tokens
            metrics_entry["output_tokens"] = completion_tokens
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nSummary failed: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    elif query_type == "greeting":
        summary = "# System Response\n\nHello! How can I assist you with Observix analytics today?"
        prompt_tokens = count_tokens("greeting", config["provider"], config["model"])
        completion_tokens = count_tokens(summary, config["provider"], config["model"])
    else:  # unknown
        prompt_template = LCPromptTemplate(
            input_variables=["text", "query"],
            template="""System: Return a concise Markdown response (150–200 words) for the query '{query}'.

Provide a clear, professional answer addressing the query directly.

Input:
{text}

# Response to Query: {query}
"""
        )
        try:
            prompt = prompt_template.format(text=limited_input, query=state["query"])
            response, token_counts = await llm_interface.call("", prompt, timeout=30)
            summary = clean_llm_response(response)
            if not summary.startswith("#"):
                summary = f"# Response to Query: {state['query']}\n\n{summary}"
            prompt_tokens = token_counts.get("input_tokens", 0)
            completion_tokens = token_counts.get("output_tokens", 0)
            metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
            metrics_entry["input_tokens"] = prompt_tokens
            metrics_entry["output_tokens"] = completion_tokens
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nError: Unable to process query: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    state["token_usage"][config["provider"]] = {
        "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + prompt_tokens,
        "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + completion_tokens,
        "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + prompt_tokens + completion_tokens
    }
    with metrics_lock:
        global_metrics["total_input_tokens"] += prompt_tokens
        global_metrics["total_output_tokens"] += completion_tokens
    state["final_output"] = summary
    state["trace"].append({
        "node": "transform_output",
        "input": limited_input,
        "fields_used": state.get("requested_fields", []),
        "output": summary,
        "token_usage": state["token_usage"][config["provider"]],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["transform_output"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["transform_output"] += 1
        global_metrics["node_durations"]["transform_output"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["transform_output"] += 1
    save_metrics()
    return state

async def summarize_results(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "summarize_results",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    if not state["intermediate_output"] or state["intermediate_output"] == "NO_RESULTS_FOUND":
        state["final_output"] = "# System Response\n\nNo results to summarize."
        state["trace"].append({
            "node": "summarize_results",
            "input": state["intermediate_output"],
            "fields_used": state.get("requested_fields", []),
            "output": state["final_output"],
            "token_usage": state["token_usage"][config["provider"]],
            "duration": time.perf_counter() - start_time
        })
        state["node_durations"]["summarize_results"] = time.perf_counter() - start_time
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["summarize_results"] += 1
            global_metrics["node_durations"]["summarize_results"] += metrics_entry["total_duration_ms"]
        save_metrics()
        return state
    results_text = state["intermediate_output"]
    try:
        summary = await tool_summarize_results(results_text)
        prompt_tokens = count_tokens(results_text, config["provider"], config["model"])
        completion_tokens = count_tokens(summary, config["provider"], config["model"])
    except Exception as e:
        logger.error(f"[summarize_results Error] {str(e)}")
        summary = f"# System Response\n\nSummary failed: {str(e)}."
        prompt_tokens = count_tokens(results_text, config["provider"], config["model"])
        completion_tokens = 0
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
    state["token_usage"][config["provider"]] = {
        "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + prompt_tokens,
        "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + completion_tokens,
        "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + prompt_tokens + completion_tokens
    }
    with metrics_lock:
        global_metrics["total_input_tokens"] += prompt_tokens
        global_metrics["total_output_tokens"] += completion_tokens
    state["final_output"] = summary
    state["trace"].append({
        "node": "summarize_results",
        "input": results_text,
        "fields_used": state.get("requested_fields", []),
        "output": summary,
        "token_usage": state["token_usage"][config["provider"]],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["summarize_results"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["summarize_results"] += 1
        global_metrics["node_durations"]["summarize_results"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["summarize_results"] += 1
    save_metrics()
    return state

async def direct_answer(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "direct_answer",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    state["final_output"] = "# System Response\n\nHello! How can I assist you with Observix analytics today?"
    state["trace"].append({
        "node": "direct_answer",
        "input": state["query"],
        "fields_used": state.get("requested_fields", []),
        "output": state["final_output"],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["direct_answer"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["direct_answer"] += 1
        global_metrics["node_durations"]["direct_answer"] += metrics_entry["total_duration_ms"]
    save_metrics()
    return state

async def handle_error(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "handle_error",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    state["final_output"] = state.get("final_output", "# System Response\n\nError: An unexpected issue occurred. Please try again.")
    state["trace"].append({
        "node": "handle_error",
        "input": state.get("intermediate_output", []),
        "fields_used": state.get("requested_fields", []),
        "output": state["final_output"],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["handle_error"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["handle_error"] += 1
        global_metrics["node_durations"]["handle_error"] += metrics_entry["total_duration_ms"]
    save_metrics()
    return state

async def dynamic_reasoning(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "dynamic_reasoning",
        "status": "success",
        "steps": {}
    }
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    if len(state["agent_scratchpad"]) >= 10:
        state["intermediate_output"] = json.dumps({"error": "Maximum reasoning iterations reached."})
        state["trace"].append({
            "node": "dynamic_reasoning",
            "input": state["query"],
            "fields_used": state.get("requested_fields", []),
            "output": state["intermediate_output"],
            "duration": time.perf_counter() - start_time
        })
        state["node_durations"]["dynamic_reasoning"] = time.perf_counter() - start_time
        metrics_entry["status"] = "error"
        metrics_entry["error"] = "Maximum reasoning iterations reached"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["dynamic_reasoning"] += 1
            global_metrics["node_durations"]["dynamic_reasoning"] += metrics_entry["total_duration_ms"]
            global_metrics["node_errors"]["dynamic_reasoning"] += 1
        save_metrics()
        return state
    tools = ["FilterObservix", "GetErrorById", "SummarizeResults", "DirectAnswer"]
    subgraphs = ["log_id_subgraph", "filter_subgraph", "health_subgraph", "summarize_subgraph", "greeting_subgraph"]
    prompt = f"""System: Return only a JSON object with "action", "input", and "reasoning" fields. Do not include explanations, reasoning text, or tags like <think>.

You are Observibot, an expert Observix analyst. Handle a query that doesn't match predefined patterns by selecting the appropriate tool or subgraph.

**Query**: {state["query"]}
**Available Tools**:
- FilterObservix: Filters issues by criteria (e.g., severity, category, source like CPUVGM, InfraLog, FISCD, vgmlog, netprobe, gateway).
- GetErrorById: Retrieves a specific issue by log_id.
- SummarizeResults: Summarizes a list of issues.
- DirectAnswer: Provides a simple reply for greetings or basic queries.
**Available Subgraphs**:
- log_id_subgraph: For log_id-based queries.
- filter_subgraph: For filtering queries.
- health_subgraph: For system health/status queries.
- summarize_subgraph: For summarization requests.
- greeting_subgraph: For greetings.

**History**:
{state["agent_scratchpad"]}

**Instructions**:
1. Analyze the query and select a tool, subgraph, or direct answer.
2. Prefer tools for direct actions to minimize latency.
3. Return a JSON object with:
   - "action": Tool or subgraph name (or "none" for direct answer).
   - "input": Input for the tool/subgraph or final answer.
   - "reasoning": Brief explanation of the choice.
"""
    llm_start = time.perf_counter()
    try:
        response, token_counts = await llm_interface.call("", prompt, timeout=30)
        decision = extract_json(clean_llm_response(response))
        metrics_entry["steps"]["llm_call"] = {"duration_ms": (time.perf_counter() - llm_start) * 1000}
        metrics_entry["input_tokens"] = token_counts.get("input_tokens", 0)
        metrics_entry["output_tokens"] = token_counts.get("output_tokens", 0)
        state["token_usage"][config["provider"]] = {
            "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + token_counts.get("input_tokens", 0),
            "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + token_counts.get("output_tokens", 0),
            "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + token_counts.get("input_tokens", 0) + token_counts.get("output_tokens", 0)
        }
        with metrics_lock:
            global_metrics["total_input_tokens"] += token_counts.get("input_tokens", 0)
            global_metrics["total_output_tokens"] += token_counts.get("output_tokens", 0)
    except Exception as e:
        logger.error(f"[dynamic_reasoning Error] Failed to parse LLM response: {str(e)}. Raw response: {'N/A' if 'response' in locals() else response[:200]}")
        decision = {"action": "none", "input": json.dumps({"error": "Unable to process query due to LLM response parsing failure."}), "reasoning": str(e)}
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
    action = decision.get("action", "none")
    action_input = decision.get("input", "")
    reasoning = decision.get("reasoning", "N/A")
    state["agent_scratchpad"].append({
        "thought": reasoning,
        "action": action,
        "input": action_input
    })
    if action == "none":
        state["intermediate_output"] = action_input
        try:
            parsed_input = json.loads(action_input)
            if isinstance(parsed_input, dict) and "error" in parsed_input:
                state["intermediate_output"] = f"# System Response\n\n{parsed_input['error']}"
            else:
                state["intermediate_output"] = f"# System Response\n\n{action_input}"
        except json.JSONDecodeError:
            state["intermediate_output"] = f"# System Response\n\n{action_input}"
    elif action in tools:
        tool_map = {
            "FilterObservix": tool_filter,
            "GetErrorById": tool_get_by_id,
            "SummarizeResults": tool_summarize_results,
            "DirectAnswer": lambda x: f"# System Response\n\n{x}"
        }
        try:
            result = await tool_map[action](action_input)
            state["intermediate_output"] = result
            state["agent_scratchpad"].append({"observation": result})
        except Exception as e:
            logger.error(f"[dynamic_reasoning Tool Error] {str(e)}")
            state["intermediate_output"] = json.dumps({"error": str(e)})
            state["agent_scratchpad"].append({"observation": f"Error: {str(e)}"})
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
    elif action in subgraphs:
        state["query_type"] = action.replace("_subgraph", "")
        state["agent_scratchpad"].append({"observation": f"Routed to {action}"})
    state["trace"].append({
        "node": "dynamic_reasoning",
        "input": prompt,
        "fields_used": state.get("requested_fields", []),
        "output": decision,
        "token_usage": state["token_usage"][config["provider"]],
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["dynamic_reasoning"] = time.perf_counter() - start_time
    metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
    with metrics_lock:
        global_metrics["runs"].append(metrics_entry)
        global_metrics["node_counts"]["dynamic_reasoning"] += 1
        global_metrics["node_durations"]["dynamic_reasoning"] += metrics_entry["total_duration_ms"]
        if metrics_entry.get("status") == "error":
            global_metrics["node_errors"]["dynamic_reasoning"] += 1
    save_metrics()
    return state

# Subgraphs
def create_log_id_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("execute_get_by_id", execute_get_by_id)
    workflow.add_node("transform_output", transform_output)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("execute_get_by_id")
    workflow.add_edge("execute_get_by_id", "transform_output")
    workflow.add_edge("transform_output", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

def create_filter_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("parse_filters", parse_filters)
    workflow.add_node("execute_filter", execute_filter)
    workflow.add_node("transform_output", transform_output)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("parse_filters")
    workflow.add_edge("parse_filters", "execute_filter")
    workflow.add_edge("execute_filter", "transform_output")
    workflow.add_edge("transform_output", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

def create_health_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("parse_filters", parse_filters)
    workflow.add_node("execute_health_check", execute_health_check)
    workflow.add_node("transform_output", transform_output)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("parse_filters")
    workflow.add_edge("parse_filters", "execute_health_check")
    workflow.add_edge("execute_health_check", "transform_output")
    workflow.add_edge("transform_output", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

def create_summarize_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("parse_filters", parse_filters)
    workflow.add_node("execute_filter", execute_filter)
    workflow.add_node("summarize_results", summarize_results)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("parse_filters")
    workflow.add_edge("parse_filters", "execute_filter")
    workflow.add_edge("execute_filter", "summarize_results")
    workflow.add_edge("summarize_results", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

def create_greeting_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("direct_answer", direct_answer)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("direct_answer")
    workflow.add_edge("direct_answer", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

def create_dynamic_subgraph():
    workflow = StateGraph(ObservibotState)
    workflow.add_node("dynamic_reasoning", dynamic_reasoning)
    workflow.add_node("transform_output", transform_output)
    workflow.add_node("handle_error", handle_error)
    workflow.set_entry_point("dynamic_reasoning")
    workflow.add_conditional_edges(
        "dynamic_reasoning",
        lambda state: state["query_type"] if state["query_type"] in ["log_id", "filter", "health", "summarize", "greeting"] else "transform",
        {
            "log_id": END,
            "filter": END,
            "health": END,
            "summarize": END,
            "greeting": END,
            "transform": "transform_output"
        }
    )
    workflow.add_edge("transform_output", "handle_error")
    workflow.add_edge("handle_error", END)
    return workflow.compile()

# Main Workflow
main_workflow = StateGraph(ObservibotState)
main_workflow.add_node("classify_query", classify_query)
main_workflow.add_node("log_id_subgraph", create_log_id_subgraph())
main_workflow.add_node("filter_subgraph", create_filter_subgraph())
main_workflow.add_node("health_subgraph", create_health_subgraph())
main_workflow.add_node("summarize_subgraph", create_summarize_subgraph())
main_workflow.add_node("greeting_subgraph", create_greeting_subgraph())
main_workflow.add_node("dynamic_subgraph", create_dynamic_subgraph())
main_workflow.set_entry_point("classify_query")
main_workflow.add_conditional_edges(
    "classify_query",
    lambda state: state["query_type"],
    {
        "log_id": "log_id_subgraph",
        "filter": "filter_subgraph",
        "health": "health_subgraph",
        "summarize": "summarize_subgraph",
        "greeting": "greeting_subgraph",
        "unknown": "dynamic_subgraph"
    }
)
main_workflow.add_conditional_edges(
    "dynamic_subgraph",
    lambda state: state["query_type"] if state["query_type"] in ["log_id", "filter", "health", "summarize", "greeting"] else END,
    {
        "log_id": "log_id_subgraph",
        "filter": "filter_subgraph",
        "health": "health_subgraph",
        "summarize": "summarize_subgraph",
        "greeting": "greeting_subgraph"
    }
)
main_workflow.add_edge("log_id_subgraph", END)
main_workflow.add_edge("filter_subgraph", END)
main_workflow.add_edge("health_subgraph", END)
main_workflow.add_edge("summarize_subgraph", END)
main_workflow.add_edge("greeting_subgraph", END)
compiled_workflow = main_workflow.compile()

# Session History Management
session_histories = {}
def get_session_history(sid: str) -> ChatMessageHistory:
    with metrics_lock:
        if sid not in session_histories:
            session_histories[sid] = ChatMessageHistory()
            global_metrics["node_counts"]["session_creation"] += 1
    return session_histories[sid]

def get_or_generate_session_id(session_id: Optional[str]) -> str:
    return session_id or str(uuid4())

# Health Endpoint
@app.get("/health")
async def health():
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "health_check",
        "status": "success",
        "steps": {}
    }
    try:
        # Elasticsearch Health Check
        es_start = time.perf_counter()
        try:
            es_status = await es.ping()
            es_status_str = "connected" if es_status else "disconnected"
        except Exception as e:
            logger.error(f"[Elasticsearch Health Error] {str(e)}")
            es_status_str = "disconnected"
            metrics_entry["status"] = "error"
            metrics_entry["error"] = metrics_entry.get("error", "") + f" Elasticsearch: {str(e)}"
        metrics_entry["steps"]["elasticsearch"] = {"duration_ms": (time.perf_counter() - es_start) * 1000}
        
        # LLM Endpoint Availability Check
        llm_start = time.perf_counter()
        llm_status_str = "reachable"
        endpoint = llm_interface.endpoint or "default"
        
        # Providers that don't require a custom endpoint (e.g., Gemini, Groq)
        if llm_interface.provider in ["gemini", "groq"] and endpoint == "default":
            # For Gemini and Groq, assume API is reachable if API key is provided
            if llm_interface.api_key:
                llm_status_str = "reachable (API key provided)"
            else:
                llm_status_str = "unreachable (API key missing)"
                metrics_entry["status"] = "error"
                metrics_entry["error"] = metrics_entry.get("error", "") + " LLM: API key missing"
        else:
            # Providers requiring an endpoint (e.g., ollama, llama_cpp, openrouter)
            try:
                if not endpoint.startswith(("http://", "https://")):
                    raise ValueError("Request URL is missing an 'http://' or 'https://' protocol.")
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(endpoint)  # Use HEAD if supported by the API
                    if response.status_code in (200, 201, 204):
                        llm_status_str = "reachable"
                    else:
                        llm_status_str = f"unreachable (status: {response.status_code})"
                        metrics_entry["status"] = "error"
                        metrics_entry["error"] = metrics_entry.get("error", "") + f" LLM endpoint returned {response.status_code}"
            except Exception as e:
                logger.error(f"[LLM Endpoint Availability Error] {str(e)}")
                llm_status_str = f"unreachable ({str(e)})"
                metrics_entry["status"] = "error"
                metrics_entry["error"] = metrics_entry.get("error", "") + f" LLM: {str(e)}"
        metrics_entry["steps"]["llm"] = {
            "duration_ms": (time.perf_counter() - llm_start) * 1000
        }
        
        # Thread Pool Stats
        thread_start = time.perf_counter()
        thread_info = {
            "active_threads": threading.active_count(),
            "thread_names": [t.name for t in threading.enumerate()],
            "is_blocked": threading.active_count() > 50  # Arbitrary threshold for blockage detection
        }
        metrics_entry["steps"]["threading"] = {"duration_ms": (time.perf_counter() - thread_start) * 1000}
        
        # Metrics Calculation
        with metrics_lock:
            total_queries = global_metrics["total_queries_processed"]
            error_rate = global_metrics["total_errors"] / total_queries if total_queries > 0 else 0
            avg_input_tokens = global_metrics["total_input_tokens"] / total_queries if total_queries > 0 else 0
            avg_output_tokens = global_metrics["total_output_tokens"] / total_queries if total_queries > 0 else 0
            uptime_hours = (time.time() - global_metrics["start_time"]) / 3600
            recent_runs = global_metrics["runs"][-10:]
            last_run = recent_runs[-1] if recent_runs else {"timestamp": None, "function": "N/A", "status": "N/A"}
        
        response = {
            "status": "healthy" if metrics_entry["status"] == "success" else "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_hours": round(uptime_hours, 2),
            "elasticsearch": {
                "status": es_status_str,
                "url": ES_URL,
                "index": INDEX
            },
            "llm": {
                "status": llm_status_str,
                "provider": llm_interface.provider,
                "model": llm_interface.model,
                "endpoint": llm_interface.endpoint or "default"
            },
            "threading": thread_info,
            "metrics": {
                "total_queries_processed": total_queries,
                "total_errors": global_metrics["total_errors"],
                "error_rate": round(error_rate, 4),
                "avg_input_tokens": round(avg_input_tokens, 2),
                "avg_output_tokens": round(avg_output_tokens, 2),
                "node_counts": dict(global_metrics["node_counts"]),
                "node_durations_ms": {k: round(v, 2) for k, v in global_metrics["node_durations"].items()},
                "node_errors": dict(global_metrics["node_errors"]),
                "last_run": last_run
            }
        }
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["health_check"] += 1
            global_metrics["node_durations"]["health_check"] += metrics_entry["total_duration_ms"]
            if metrics_entry.get("status") == "error":
                global_metrics["node_errors"]["health_check"] += 1
        save_metrics()
        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"[Health Endpoint Error] {str(e)}")
        metrics_entry["status"] = "error"
        metrics_entry["error"] = metrics_entry.get("error", "") + f" General: {str(e)}"
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["health_check"] += 1
            global_metrics["node_durations"]["health_check"] += metrics_entry["total_duration_ms"]
            global_metrics["node_errors"]["health_check"] += 1
        save_metrics()
        return JSONResponse(
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            },
            status_code=500
        )

# Chat Endpoint
@app.post("/chat")
async def chat(request: ChatRequest):
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "chat_endpoint",
        "status": "success",
        "steps": {}
    }
    session_id = get_or_generate_session_id(request.session_id)
    history = get_session_history(session_id)
    try:
        # Initialize state
        state = ObservibotState(
            query=request.query,
            session_id=session_id,
            query_type=None,
            filters=None,
            requested_fields=None,
            intermediate_output=None,
            final_output=None,
            token_usage={config["provider"]: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            chat_history=[
                {
                    "role": "human" if isinstance(msg, HumanMessage) else "assistant",
                    "content": msg.content
                }
                for msg in history.messages
            ],
            trace=[],
            agent_scratchpad=[],
            node_durations={}
        )
        
        # Execute workflow
        workflow_start = time.perf_counter()
        try:
            result = await compiled_workflow.ainvoke(state)
            metrics_entry["steps"]["workflow"] = {"duration_ms": (time.perf_counter() - workflow_start) * 1000}
        except Exception as e:
            logger.error(f"[Workflow Error] {str(e)}, traceback: {traceback.format_exc()}")
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
            result = {
                "final_output": f"# System Response\n\nError: Unable to process query due to workflow failure: {str(e)}.",
                "token_usage": state["token_usage"],
                "trace": state["trace"] + [{
                    "node": "workflow",
                    "input": state["query"],
                    "output": f"Error: {str(e)}",
                    "duration": time.perf_counter() - workflow_start
                }],
                "node_durations": state["node_durations"]
            }
        
        # Update history
        history.add_message(HumanMessage(content=request.query))
        final_output = result.get("final_output", "# System Response\n\nNo response generated.")
        history.add_message(AIMessage(content=final_output))
        
        # Format response
        response = {
            "response": format_final_response(final_output, is_health_query=result.get("query_type") == "health"),
            "session_id": session_id,
            "token_usage": result["token_usage"],
            "trace": result["trace"],
            "node_durations": {k: round(v * 1000, 2) for k, v in result["node_durations"].items()}
        }
        log_and_truncate_response(response)
        
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["total_queries_processed"] += 1
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["chat_endpoint"] += 1
            global_metrics["node_durations"]["chat_endpoint"] += metrics_entry["total_duration_ms"]
            if metrics_entry.get("status") == "error":
                global_metrics["node_errors"]["chat_endpoint"] += 1
                global_metrics["total_errors"] += 1
        save_metrics()
        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"[Chat Endpoint Error] {str(e)}, traceback: {traceback.format_exc()}")
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["total_queries_processed"] += 1
            global_metrics["total_errors"] += 1
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["chat_endpoint"] += 1
            global_metrics["node_durations"]["chat_endpoint"] += metrics_entry["total_duration_ms"]
            global_metrics["node_errors"]["chat_endpoint"] += 1
        save_metrics()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")
# Trace Endpoint
@app.post("/trace")
async def trace(request: TraceRequest):
    start_time = time.perf_counter()
    metrics_entry = {
        "run_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "function": "trace_endpoint",
        "status": "success",
        "steps": {}
    }
    session_id = get_or_generate_session_id(request.session_id)
    history = get_session_history(session_id)
    try:
        # Initialize state
        state = ObservibotState(
            query=request.query,
            session_id=session_id,
            query_type=None,
            filters=None,
            requested_fields=None,
            intermediate_output=None,
            final_output=None,
            token_usage={config["provider"]: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            chat_history=[{"role": msg.role, "content": msg.content} for msg in history.messages],
            trace=[],
            agent_scratchpad=[],
            node_durations={}
        )
        
        # Execute workflow
        workflow_start = time.perf_counter()
        try:
            result = await compiled_workflow.ainvoke(state)
            metrics_entry["steps"]["workflow"] = {"duration_ms": (time.perf_counter() - workflow_start) * 1000}
        except Exception as e:
            logger.error(f"[Workflow Error] {str(e)}, traceback: {traceback.format_exc()}")
            metrics_entry["status"] = "error"
            metrics_entry["error"] = str(e)
            result = {
                "trace": state["trace"] + [{
                    "node": "workflow",
                    "input": state["query"],
                    "output": f"Error: {str(e)}",
                    "duration": time.perf_counter() - workflow_start
                }],
                "token_usage": state["token_usage"],
                "node_durations": state["node_durations"]
            }
        
        # Format trace response
        response = {
            "trace": result["trace"],
            "session_id": session_id,
            "token_usage": result["token_usage"],
            "node_durations": {k: round(v * 1000, 2) for k, v in result["node_durations"].items()}
        }
        log_and_truncate_response(response)
        
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["total_queries_processed"] += 1
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["trace_endpoint"] += 1
            global_metrics["node_durations"]["trace_endpoint"] += metrics_entry["total_duration_ms"]
            if metrics_entry.get("status") == "error":
                global_metrics["node_errors"]["trace_endpoint"] += 1
                global_metrics["total_errors"] += 1
        save_metrics()
        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"[Trace Endpoint Error] {str(e)}, traceback: {traceback.format_exc()}")
        metrics_entry["status"] = "error"
        metrics_entry["error"] = str(e)
        metrics_entry["total_duration_ms"] = (time.perf_counter() - start_time) * 1000
        with metrics_lock:
            global_metrics["total_queries_processed"] += 1
            global_metrics["total_errors"] += 1
            global_metrics["runs"].append(metrics_entry)
            global_metrics["node_counts"]["trace_endpoint"] += 1
            global_metrics["node_durations"]["trace_endpoint"] += metrics_entry["total_duration_ms"]
            global_metrics["node_errors"]["trace_endpoint"] += 1
        save_metrics()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")



