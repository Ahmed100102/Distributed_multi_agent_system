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

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from elasticsearch import Elasticsearch
from langchain.tools import Tool
from langchain.prompts import PromptTemplate as LCPromptTemplate
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.messages import HumanMessage, AIMessage
from langchain.chains.summarize import load_summarize_chain
from langchain_core.documents import Document
from langgraph.graph import StateGraph, END
import tiktoken

from llm_interface import LLMInterface

# Logger Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Observibot")

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
    version="1.0.0"
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
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://10.254.117.52:9200")
INDEX = os.getenv("ELASTICSEARCH_INDEX", "observix-results-*")
es = Elasticsearch(
    ES_URL,
    verify_certs=os.getenv("ELASTICSEARCH_VERIFY_CERTS", "true").lower() == "true",
    retry_on_timeout=True,
    max_retries=3,
    request_timeout=30
)

# LLM Setup
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["llama_cpp", "gemini", "groq", "ollama"]
runtime_configs = {
    "llama_cpp": {
        "provider": "llama_cpp",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "qwen3:4b"),
        "endpoint": os.getenv("LLM_ENDPOINT", "http://localhost:18000"),
        "api_key": None
    },
    "gemini": {
        "provider": "gemini",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "gemini-2.5-flash"),
        "endpoint": None,
        "api_key": os.getenv("GEMINI_API_KEY", "AIzaSyAECBFg1Zl5tAgH4U0S7XDz0eD_R3ijRI0")
    },
    "groq": {
        "provider": "groq",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "meta-llama/llama-4-scout-17b-16e-instruct"),
        "endpoint": None,
        "api_key": os.getenv("GROQ_API_KEY")
    },
    "ollama": {
        "provider": "ollama",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "llama3.2:3b"),
        "endpoint": os.getenv("LLM_ENDPOINT", "http://localhost:11434"),
        "api_key": None
    }
}
if MODEL_RUNTIME not in VALID_RUNTIMES:
    raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")
config = runtime_configs[MODEL_RUNTIME]
llm_interface = LLMInterface(
    provider=config["provider"],
    model=config["model"],
    endpoint=config["endpoint"],
    api_key=config["api_key"]
)
llm = llm_interface.llm
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            config["provider"], config["model"], config["endpoint"] or "default")

# Token Calculation
def count_tokens(text: str, provider: str, model: str, max_length: int = 500) -> int:
    try:
        if provider == "gemini":
            encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
            tokens = encoding.encode(text)[:max_length]
            return len(tokens)
        elif provider in ["llama_cpp", "groq"]:
            encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
            tokens = encoding.encode(text)[:max_length]
            return len(tokens)
        elif provider == "ollama":
            return min(len(text) // 4, max_length)
        else:
            return min(len(text) // 4, max_length)
    except Exception as e:
        logger.warning(f"Token counting failed for {provider}: {str(e)}")
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
    final = set(explicit_fields or MINIMAL_FIELDS)
    query_lower = user_input.lower()
    
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', query_lower)
    is_full_details = "full details" in query_lower and log_id_match
    is_root_cause = any(k in query_lower for k in ["root cause", "cause of", "why did"])
    is_remediation = any(k in query_lower for k in ["how to fix", "remediation", "solution", "fix it"])
    is_health_query = any(k in query_lower for k in ["health", "status", "overview"])
    
    if is_full_details:
        return ALL_FIELDS
    elif is_root_cause:
        return list(set(MINIMAL_FIELDS + ROOT_CAUSE_FIELDS))
    elif is_remediation:
        return list(set(MINIMAL_FIELDS + REMEDIATION_FIELDS))
    elif is_health_query:
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
    try:
        response = await llm.ainvoke(prompt)
        response_text = clean_llm_response(response.content)
        fields = extract_json(response_text)
        if not isinstance(fields, list):
            logger.warning(f"LLM returned non-list fields: {response_text}")
            fields = list(final)
        fields = list(set(fields) | set(MINIMAL_FIELDS))
        return fields
    except Exception as e:
        logger.error(f"[get_fields_by_request Error] {str(e)}. Raw response: {response_text[:200]}...")
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
def tool_filter(params: str) -> str:
    try:
        data = extract_json(params)
        filters = build_filters(data.get("filters", {}))
        fields = data.get("fields") or MINIMAL_FIELDS
        is_health_query = data.get("is_health_query", False)
        body = {"query": build_es_query(filters), "_source": fields, "size": data.get("size", 100 if is_health_query else 5)}
        results = es.search(index=INDEX, body=body)
        hits = [hit["_source"] for hit in results["hits"]["hits"]]
        formatted = [format_issue(doc, fields, is_health_query) for doc in hits]
        logger.info(f"Filter matched {len(formatted)} record(s).")
        result = json.dumps(hits) if hits else "NO_RESULTS_FOUND"
        return result
    except Exception as e:
        logger.error(f"[Filter Tool Error] {str(e)}")
        return json.dumps({"error": str(e)})

def tool_get_by_id(params: str) -> str:
    try:
        data = extract_json(params)
        log_id = data.get("log_id")
        if not log_id:
            raise ValueError("log_id is required")
        fields = data.get("fields") or MINIMAL_FIELDS
        result = es.search(
            index=INDEX,
            body={"query": {"term": {"log_id": log_id}}, "_source": fields, "size": 1}
        )
        if not result["hits"]["hits"]:
            return json.dumps({"error": f"No record found for log_id: {log_id}"})
        return json.dumps(result["hits"]["hits"][0]["_source"])
    except Exception as e:
        logger.error(f"[GetErrorById Error] {str(e)}")
        return json.dumps({"error": str(e)})

def tool_summarize_results(results: str) -> str:
    if not results or results == "NO_RESULTS_FOUND":
        return "# System Response\n\nNo results to summarize."
    template = LCPromptTemplate(
        input_variables=["text"],
        template="System: Return a concise Markdown summary (150–200 words) without explanations or tags like <think>.\n\nRead the issues and provide a summary in Markdown format:\n\n{text}\n\n# Summary\n"
    )
    chain = load_summarize_chain(llm, chain_type="stuff", prompt=template)
    documents = [Document(page_content=results)]
    try:
        response = chain.invoke({"input_documents": documents})
        logger.info(f"Summarize response type: {type(response)}, content: {str(response)[:200]}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
        if isinstance(response, dict):
            summary = clean_llm_response(response.get("output_text", response.get("content", response.get("text", "Summary could not be generated."))))
        elif isinstance(response, str):
            summary = clean_llm_response(response)
        elif hasattr(response, "content"):
            summary = clean_llm_response(response.content)
        else:
            logger.error(f"Unexpected response format in summarize_results: {type(response)}, content: {str(response)[:200]}")
            summary = "Summary could not be generated due to unexpected response format."
        # Ensure the summary is in Markdown
        if not summary.startswith("#"):
            summary = f"# Summary\n\n{summary}"
        return summary
    except Exception as e:
        logger.error(f"[SummarizeResults Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
        return f"# System Response\n\nSummary failed: {str(e)}."

# LangGraph Nodes
async def classify_query(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    query = state["query"].lower()
    filter_patterns = r'^(list|show|find|give\s+me|all)\b.*(issues|errors|logs)'
    health_patterns = r'\b(health|status|overview|system\s*(health|status))\b'
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', query)
    fields = await get_fields_by_request(query)
    state["requested_fields"] = fields
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
        try:
            response = await llm.ainvoke(prompt)
            query_type = clean_llm_response(response.content.strip())
            query_type_match = re.search(r'(log_id|filter|health|summarize|greeting|unknown)', query_type, re.IGNORECASE)
            query_type = query_type_match.group(0).lower() if query_type_match else "unknown"
            prompt_tokens = count_tokens(prompt, config["provider"], config["model"])
            completion_tokens = count_tokens(response.content, config["provider"], config["model"])
            state["token_usage"][config["provider"]] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens
            }
        except Exception as e:
            logger.error(f"[classify_query Error] Failed to parse LLM response: {str(e)}. Raw response: {response.content}")
            query_type = "unknown"
            state["token_usage"][config["provider"]] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
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
    return state

async def parse_filters(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    query = state["query"].lower()
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
    return state

async def execute_filter(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
    results = tool_filter(params)
    state["intermediate_output"] = results
    state["trace"].append({
        "node": "execute_filter",
        "input": params,
        "fields_used": state.get("requested_fields", []),
        "output": results,
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_filter"] = time.perf_counter() - start_time
    return state

async def execute_get_by_id(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    log_id_match = re.search(r'[a-zA-Z0-9_-]{20,24}', state["query"])
    log_id = log_id_match.group(0) if log_id_match else None
    if not log_id:
        state["intermediate_output"] = json.dumps({"error": "No valid log_id found in query."})
        state["trace"].append({
            "node": "execute_get_by_id",
            "input": state["query"],
            "fields_used": state.get("requested_fields", []),
            "output": state["intermediate_output"],
            "duration": time.perf_counter() - start_time
        })
        state["node_durations"]["execute_get_by_id"] = time.perf_counter() - start_time
        return state
    params = json.dumps({
        "log_id": log_id,
        "user_request": state["query"],
        "fields": fields
    })
    result = tool_get_by_id(params)
    state["intermediate_output"] = result
    state["trace"].append({
        "node": "execute_get_by_id",
        "input": params,
        "fields_used": state.get("requested_fields", []),
        "output": result,
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_get_by_id"] = time.perf_counter() - start_time
    return state

async def execute_health_check(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
    results = tool_filter(params)
    state["intermediate_output"] = results
    state["trace"].append({
        "node": "execute_health_check",
        "input": params,
        "fields_used": state.get("requested_fields", []),
        "output": results,
        "duration": time.perf_counter() - start_time
    })
    state["node_durations"]["execute_health_check"] = time.perf_counter() - start_time
    return state

async def transform_output(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
    if not state.get("requested_fields"):
        fields = await get_fields_by_request(state["query"])
        state["requested_fields"] = fields
    else:
        fields = state.get("requested_fields", MINIMAL_FIELDS)
    
    # Handle empty or invalid intermediate_output
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
        return state
    
    # Try to parse intermediate_output as JSON
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
        return state
    
    query_type = state["query_type"].lower()
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
            logger.info(f"Transform response type: {type(response)}, content: {str(response)[:200]}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
            if isinstance(response, dict):
                summary = clean_llm_response(response.get("output_text", response.get("content", response.get("text", "Summary could not be generated."))))
            elif isinstance(response, str):
                summary = clean_llm_response(response)
            elif hasattr(response, "content"):
                summary = clean_llm_response(response.content)
            else:
                logger.error(f"Unexpected response format in transform_output: {type(response)}, content: {str(response)[:200]}")
                summary = "Summary could not be generated due to unexpected response format."
            if not summary.startswith("#"):
                summary = f"# System Health Summary\n\n{summary}"
            prompt_tokens = count_tokens(prompt_template.template.format(text=limited_input), config["provider"], config["model"])
            completion_tokens = count_tokens(summary, config["provider"], config["model"])
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nSummary failed: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
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
            logger.info(f"Transform response type: {type(response)}, content: {str(response)[:200]}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
            if isinstance(response, dict):
                summary = clean_llm_response(response.get("output_text", response.get("content", response.get("text", "Summary could not be generated."))))
            elif isinstance(response, str):
                summary = clean_llm_response(response)
            elif hasattr(response, "content"):
                summary = clean_llm_response(response.content)
            else:
                logger.error(f"Unexpected response format in transform_output: {type(response)}, content: {str(response)[:200]}")
                summary = "Summary could not be generated due to unexpected response format."
            if not summary.startswith("#"):
                summary = f"# Summary for Query: {state['query']}\n\n{summary}"
            prompt_tokens = count_tokens(prompt_template.template.format(text=limited_input, query=state["query"]), config["provider"], config["model"])
            completion_tokens = count_tokens(summary, config["provider"], config["model"])
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nSummary failed: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
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
            response = await llm.ainvoke(prompt_template.format(text=limited_input, query=state["query"]))
            logger.info(f"Transform response type: {type(response)}, content: {str(response)[:200]}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")
            if isinstance(response, dict):
                summary = clean_llm_response(response.get("content", response.get("text", "Response could not be generated.")))
            elif isinstance(response, str):
                summary = clean_llm_response(response)
            elif hasattr(response, "content"):
                summary = clean_llm_response(response.content)
            else:
                logger.error(f"Unexpected response format in transform_output: {type(response)}, content: {str(response)[:200]}")
                summary = "Response could not be generated due to unexpected response format."
            if not summary.startswith("#"):
                summary = f"# Response to Query: {state['query']}\n\n{summary}"
            prompt_tokens = count_tokens(prompt_template.template.format(text=limited_input, query=state["query"]), config["provider"], config["model"])
            completion_tokens = count_tokens(summary, config["provider"], config["model"])
        except Exception as e:
            logger.error(f"[transform_output Error] {str(e)}, response: {str(response)[:200] if 'response' in locals() else 'N/A'}, traceback: {traceback.format_exc()}")
            summary = f"# System Response\n\nError: Unable to process query: {str(e)}."
            prompt_tokens = count_tokens(limited_input, config["provider"], config["model"])
            completion_tokens = 0
    state["token_usage"][config["provider"]] = {
        "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + prompt_tokens,
        "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + completion_tokens,
        "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + prompt_tokens + completion_tokens
    }
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
    return state

async def summarize_results(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
        return state
    results_text = state["intermediate_output"]
    summary = tool_summarize_results(results_text)
    prompt_tokens = count_tokens(results_text, config["provider"], config["model"])
    completion_tokens = count_tokens(summary, config["provider"], config["model"])
    state["token_usage"][config["provider"]] = {
        "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + prompt_tokens,
        "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + completion_tokens,
        "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + prompt_tokens + completion_tokens
    }
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
    return state

async def direct_answer(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
    return state

async def handle_error(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
    return state

async def dynamic_reasoning(state: ObservibotState) -> ObservibotState:
    start_time = time.perf_counter()
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
    try:
        response = await llm.ainvoke(prompt)
        decision = extract_json(clean_llm_response(response.content))
        prompt_tokens = count_tokens(prompt, config["provider"], config["model"])
        completion_tokens = count_tokens(response.content, config["provider"], config["model"])
        state["token_usage"][config["provider"]] = {
            "prompt_tokens": state["token_usage"].get(config["provider"], {}).get("prompt_tokens", 0) + prompt_tokens,
            "completion_tokens": state["token_usage"].get(config["provider"], {}).get("completion_tokens", 0) + completion_tokens,
            "total_tokens": state["token_usage"].get(config["provider"], {}).get("total_tokens", 0) + prompt_tokens + completion_tokens
        }
    except Exception as e:
        logger.error(f"[dynamic_reasoning Error] Failed to parse LLM response: {str(e)}. Raw response: {response.content}")
        decision = {"action": "none", "input": json.dumps({"error": "Unable to process query due to LLM response parsing failure."}), "reasoning": str(e)}
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
        # Ensure direct answers are in Markdown
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
        result = tool_map[action](action_input)
        state["intermediate_output"] = result
        state["agent_scratchpad"].append({"observation": result})
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
    if sid not in session_histories:
        session_histories[sid] = ChatMessageHistory()
    return session_histories[sid]

def get_or_generate_session_id(session_id: Optional[str]) -> str:
    return session_id or str(uuid4())

# API Endpoints
@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"[User Query] {request.query} | SID: {session_id}")
        start_time = time.perf_counter()
        history = get_session_history(session_id)
        state = ObservibotState(
            query=request.query,
            session_id=session_id,
            query_type=None,
            filters=None,
            requested_fields=None,
            intermediate_output=None,
            final_output=None,
            token_usage={config["provider"]: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            chat_history=history.messages,
            trace=[],
            agent_scratchpad=[],
            node_durations={}
        )
        result = await compiled_workflow.ainvoke(state)
        logger.info(f"Workflow result: type={type(result)}, keys={list(result.keys())}, final_output={result.get('final_output', 'N/A')[:200]}")
        history.add_user_message(request.query)
        history.add_ai_message(result["final_output"])
        elapsed_seconds = time.perf_counter() - start_time
        logger.info(f"[Latency] {elapsed_seconds} s")
        log_and_truncate_response(result["final_output"])
        total_tokens = {
            "prompt_tokens": sum(
                usage.get("prompt_tokens", 0) for usage in result["token_usage"].values()
            ),
            "completion_tokens": sum(
                usage.get("completion_tokens", 0) for usage in result["token_usage"].values()
            ),
            "total_tokens": sum(
                usage.get("total_tokens", 0) for usage in result["token_usage"].values()
            )
        }
        return {
            "status": "success",
            "session_id": session_id,
            "response": result["final_output"],
            "total_tokens": total_tokens,
            "requested_fields": result.get("requested_fields", []),
            "node_durations": result["node_durations"]
        }
    except Exception as e:
        logger.error(f"[Chat Endpoint Error] {str(e)}, traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"# System Response\n\nInternal Server Error: {str(e)}")

@app.post("/trace")
async def trace(request: TraceRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"[Trace Query] {request.query} | SID: {session_id}")
        start_time = time.perf_counter()
        history = get_session_history(session_id)
        state = ObservibotState(
            query=request.query,
            session_id=session_id,
            query_type=None,
            filters=None,
            requested_fields=None,
            intermediate_output=None,
            final_output=None,
            token_usage={config["provider"]: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            chat_history=history.messages,
            trace=[],
            agent_scratchpad=[],
            node_durations={}
        )
        result = await compiled_workflow.ainvoke(state)
        logger.info(f"Workflow result: type={type(result)}, keys={list(result.keys())}, final_output={result.get('final_output', 'N/A')[:200]}")
        history.add_user_message(request.query)
        history.add_ai_message(result["final_output"])
        elapsed_seconds = time.perf_counter() - start_time
        logger.info(f"[Latency] {elapsed_seconds} s")
        total_tokens = {
            "prompt_tokens": sum(
                usage.get("prompt_tokens", 0) for usage in result["token_usage"].values()
            ),
            "completion_tokens": sum(
                usage.get("completion_tokens", 0) for usage in result["token_usage"].values()
            ),
            "total_tokens": sum(
                usage.get("total_tokens", 0) for usage in result["token_usage"].values()
            )
        }
        result_dict = {
            "status": "success",
            "trace": result["trace"],
            "final_output": result["final_output"],
            "session_id": session_id,
            "total_tokens": total_tokens,
            "requested_fields": result.get("requested_fields", []),
            "node_durations": result["node_durations"]
        }
        log_and_truncate_response(result_dict)
        return JSONResponse(content=result_dict)
    except Exception as e:
        logger.error(f"[Trace Endpoint Error] {str(e)}, traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"# System Response\n\nError: {str(e)}. Please verify Elasticsearch or LLM connectivity and try again.")

# Main Entry
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="info")