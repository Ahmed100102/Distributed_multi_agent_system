import os
import json
import re
import logging
from uuid import uuid4
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from elasticsearch import Elasticsearch
from langchain.agents import create_react_agent, AgentExecutor
from langchain.tools import Tool
from langchain.prompts import PromptTemplate as LCPromptTemplate
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain.chains.summarize import load_summarize_chain
from langchain_core.documents import Document
import asyncio
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

# FastAPI Setup
app = FastAPI(
    title="Observibot",
    description="LLM agent system for Observix platform.",
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

# Field Groups
MINIMAL_FIELDS = [
    "log_id", "timestamp", "error_details.summary",
    "rca_details.summary", "rca_details.severity", "rca_details.category", "error_details.log_type"
]
REMEDIATION_FIELDS = ["remediation_plan.summary", "remediation_plan.steps.action"]
ROOT_CAUSE_FIELDS = ["rca_details.root_causes.cause"]
DETAILED_ANALYSIS_FIELDS = ["rca_details.detailed_analysis"]

# --- LLM ---
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
        "model": os.getenv("LLM_MODEL_REMEDIATION", "gemini-1.5-flash"),
        "endpoint": None,
        "api_key": "AIzaSyAPi3rnWIXNJj4alT4kyRYZxUu2C1OvcxA"


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
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            config["provider"], config["model"], config["endpoint"] or "default")
llm = llm_interface.llm

logger.info(f"LLM initialized: provider={config['provider']}, model={config['model']}")

# Utility Functions
def extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'({.*})', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise ValueError("Unable to extract JSON")

def build_filters(user_filters: dict) -> dict:
    mapped = {}
    for k, v in user_filters.items():
        key = k.lower()
        if key == "severity":
            mapped["rca_details.severity"] = v.upper()
        elif key == "date":
            mapped["timestamp"] = {"gte": "now-30d", "lte": "now"}
        elif key in {"platform", "infra", "source"}:
            mapped["error_details.log_type"] = v.upper()
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

def get_fields_by_request(user_input: str, explicit_fields: Optional[List[str]] = None) -> List[str]:
    final = set(explicit_fields or [])
    base = set(MINIMAL_FIELDS)
    text = user_input.lower()

    if "fix" in text or "remed" in text:
        final |= set(REMEDIATION_FIELDS)
    if "why" in text or "cause" in text:
        final |= set(ROOT_CAUSE_FIELDS)
    if "trace" in text or "details" in text:
        final |= set(DETAILED_ANALYSIS_FIELDS)
    if "everything" in text or "all" in text:
        final |= set(base | set(REMEDIATION_FIELDS + ROOT_CAUSE_FIELDS + DETAILED_ANALYSIS_FIELDS))
    if not final:
        final = base

    return list(final)

def format_issue(issue: dict, fields: List[str]) -> str:
    def show(f): return not fields or f in fields
    lines = []

    if show("log_id"):
        lines.append(f"Log ID: {issue.get('log_id', 'N/A')}")
    if show("timestamp"):
        lines.append(f"Timestamp: {issue.get('timestamp', 'N/A')}")
    if show("rca_details.severity"):
        lines.append(f"Severity: {issue.get('rca_details', {}).get('severity', 'N/A')}")
    if show("rca_details.category"):
        lines.append(f"Category: {issue.get('rca_details', {}).get('category', 'N/A')}")
    if show("error_details.log_type"):
        lines.append(f"Log Type: {issue.get('error_details', {}).get('log_type', 'N/A')}")

    summary = issue.get("error_details", {}).get("summary") or issue.get("rca_details", {}).get("summary")
    if summary and show("error_details.summary"):
        lines.append(f"Summary: {summary}")

    causes = issue.get("rca_details", {}).get("root_causes", [])
    if causes and show("rca_details.root_causes.cause"):
        lines.append("Root Cause(s):")
        for c in causes:
            lines.append(f"- {c.get('cause', 'N/A')}")

    if show("rca_details.detailed_analysis"):
        analysis = issue.get("rca_details", {}).get("detailed_analysis", "N/A")
        lines.append(f"Detailed Analysis: {analysis}")

    rem = issue.get("remediation_plan", {})
    if show("remediation_plan.summary") and rem.get("summary"):
        lines.append(f"Remediation Plan: {rem.get('summary', 'N/A')}")
    if show("remediation_plan.steps.action") and rem.get("steps"):
        lines.append("Remediation Steps:")
        lines.extend([f"- Step {idx+1}: {step.get('action', 'N/A')}" for idx, step in enumerate(rem["steps"])])

    return "\n".join(lines) or "No relevant information available"

def summarize_results(results: str) -> str:
    if not results or results == "NO_RESULTS_FOUND":
        return "No results to summarize."
    template = LCPromptTemplate(
        input_variables=["text"],
        template="You are an observability agent. Read the issues and provide a concise summary:\n\n\"\"\"\n{text}\n\"\"\"\n\nSummary:"
    )
    chain = load_summarize_chain(llm, chain_type="stuff", prompt=template)
    documents = [Document(page_content=results)]
    try:
        return chain.invoke({"input_documents": documents}).get("output_text", "Summary could not be generated.")
    except Exception as e:
        logger.error(f"[SummarizeResults Error] {str(e)}")
        return f"Summary failed: {str(e)}. Raw results: {results}"

def format_final_response(response: str) -> str:
    if response == "NO_RESULTS_FOUND":
        return "No matching records found."
    return response.strip() or "No relevant information available."

# Tool Functions
def tool_filter(params: str) -> str:
    try:
        data = extract_json(params)
        filters = build_filters(data.get("filters", {}))
        user_req = data.get("user_request", "")
        fields = get_fields_by_request(user_req)

        results = es.search(
            index=INDEX,
            body={"query": build_es_query(filters), "_source": fields, "size": data.get("size", 5)}
        )
        hits = [hit["_source"] for hit in results["hits"]["hits"]]
        formatted = [format_issue(doc, fields) for doc in hits]

        logger.info(f"Filter matched {len(formatted)} record(s).")
        return "\n\n".join(formatted) if formatted else "NO_RESULTS_FOUND"
    except Exception as e:
        logger.error(f"[Filter Tool Error] {str(e)}")
        return f"Error: {str(e)}"

def tool_get_by_id(params: str) -> str:
    try:
        data = extract_json(params)
        log_id = data.get("log_id")
        if not log_id:
            raise ValueError("log_id is required")
        fields = get_fields_by_request(data.get("user_request", ""))

        result = es.search(
            index=INDEX,
            body={"query": {"term": {"log_id": log_id}}, "_source": fields, "size": 1}
        )
        if not result["hits"]["hits"]:
            return f"No record found for log_id: {log_id}"
        return format_issue(result["hits"]["hits"][0]["_source"], fields)
    except Exception as e:
        logger.error(f"[GetErrorById Error] {str(e)}")
        return f"Error: {str(e)}"

# Register Tools
tools = [
    Tool(
        name="FilterObservix",
        func=tool_filter,
        description="Filters Observix issues by criteria such as severity, category, or source."
    ),
    Tool(
        name="GetErrorById",
        func=tool_get_by_id,
        description="Retrieves a specific Observix record by its log ID."
    ),
    Tool(
        name="SummarizeResults",
        func=summarize_results,
        description="Summarizes a list of Observix results into a concise summary.",
        return_direct=True
    ),
    Tool(
        name="DirectAnswer",
        func=lambda msg: msg,
        description="Provides a simple direct message reply for greetings or basic queries.",
        return_direct=True
    ),
    Tool(
        name="ReturnDirect",
        func=lambda msg: msg,
        description="Returns the final answer to the user.",
        return_direct=True
    ),
]

# Prompt Template
prompt_template = LCPromptTemplate.from_template(
    """You are Observibot, an expert Observix analyst designed to provide deterministic, accurate, and actionable responses for system issue analysis.

**TOOLS AVAILABLE**:
{tools}
**Tool Names**: {tool_names}

**AVAILABLE FIELDS**:
- **log_id**: Unique identifier for the issue.
- **timestamp**: Time the issue occurred.
- **error_details.summary**: Brief description of the issue.
- **rca_details.summary**: Summary from root cause analysis.
- **rca_details.severity**: Severity level (e.g., HIGH, MEDIUM, LOW).
- **rca_details.category**: Issue category (e.g., APPLICATION, NETWORK).
- **error_details.log_type**: Source of the issue (e.g., PLATFORM, INFRA).
- **remediation_plan.summary**: Overview of the remediation plan.
- **remediation_plan.steps.action**: Specific actions to resolve the issue.
- **rca_details.root_causes.cause**: Root cause(s) of the issue.
- **rca_details.detailed_analysis**: Detailed technical analysis or stack trace.

**CONTEXT**:
You analyze Observix system issues stored in Elasticsearch. The 'error_details.log_type' field indicates the source where the issue occurred (e.g., PLATFORM for application platform errors, INFRA for infrastructure errors). Your goal is to handle all query types (single issue, list/filter, remediation, root cause, details, greetings) deterministically, always selecting the correct tool and ensuring a final response is returned via ReturnDirect.

**OBJECTIVES**
1. Deterministically select the appropriate tool(s) based on the query.
2. Minimize unnecessary tool or LLM calls; never repeat the same tool call with identical inputs.
3. Always return a clear, actionable response to the user via ReturnDirect.
4. Support filtering by source (error_details.log_type) for platform or infra queries.

**INSTRUCTIONS**
- If the query has a log_id → use **GetErrorById**
- If it's a filter request (severity, category, platform, time) → use **FilterObservix**
- If the query explicitly requests a summary (e.g., contains "summarize") or if FilterObservix returns >7 results → use **SummarizeResults** with the actual FilterObservix output
- Always return final output via **ReturnDirect**
- If user greets you ("Hi", "Hello") → use **DirectAnswer** + **ReturnDirect**
- Avoid speculative responses or unnecessary reasoning
- For filter requests with ≤7 results, return the raw FilterObservix output directly via ReturnDirect unless summarization is explicitly requested
- Ensure SummarizeResults receives the full FilterObservix output as input, not a placeholder

**EXAMPLES**

Q: What is the issue with log_id s2GkZJcB4rxvVeed6fc1?
Thought: The query specifies a log_id. Use GetErrorById.
Action: GetErrorById
Action Input: {{"log_id": "s2GkZJcB4rxvVeed6fc1", "user_request": "issue with log_id"}}
Observation: [Formatted error details]
Action: ReturnDirect
Action Input: [Formatted error details]

Q: List all high severity platform errors in the last 30 days.
Thought: Use FilterObservix with severity=HIGH, source=PLATFORM, time range=last 30 days. Return raw results since summarization not requested.
Action: FilterObservix
Action Input: {{"filters": {{"rca_details.severity": "HIGH", "error_details.log_type": "PLATFORM", "timestamp": {{"gte": "now-30d", "lte": "now"}}}}, "user_request": "high severity platform errors"}}
Observation: [Multiple issue summaries]
Action: ReturnDirect
Action Input: [Multiple issue summaries]

Q: Summarize all high severity NETWORK issues.
Thought: Use FilterObservix with severity=HIGH, category=NETWORK, then summarize since explicitly requested.
Action: FilterObservix
Action Input: {{"filters": {{"rca_details.severity": "HIGH", "rca_details.category": "NETWORK"}}, "user_request": "summarize high severity NETWORK issues"}}
Observation: [Multiple issue summaries]
Action: SummarizeResults
Action Input: [Multiple issue summaries]
Action: ReturnDirect
Action Input: [Summarized output]

Q: List all NETWORK issues with severity HIGH.
Thought: Use FilterObservix with severity=HIGH, category=NETWORK. Return raw results since ≤7 expected and no summarization requested.
Action: FilterObservix
Action Input: {{"filters": {{"rca_details.severity": "HIGH", "rca_details.category": "NETWORK"}}, "user_request": "List all NETWORK issues with severity HIGH"}}
Observation: [Multiple issue summaries]
Action: ReturnDirect
Action Input: [Multiple issue summaries]

Q: Hi!
Thought: Greet user.
Action: DirectAnswer
Action Input: Hello! How can I assist you with Observix analytics today?
Observation: Hello! How can I assist you with Observix analytics today?
Action: ReturnDirect
Action Input: Hello! How can I assist you with Observix analytics today?

**Current Time**: {current_time}
**Question**: {input}
{agent_scratchpad}"""
)
# Agent Setup
agent = create_react_agent(llm=llm, tools=tools, prompt=prompt_template)
executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    return_intermediate_steps=True,
    max_iterations=5,
    max_execution_time=60.0
)

session_histories = {}
def get_session_history(sid: str) -> ChatMessageHistory:
    if sid not in session_histories:
        session_histories[sid] = ChatMessageHistory()
    return session_histories[sid]

agent_with_history = RunnableWithMessageHistory(
    runnable=executor,
    get_session_history=get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history"
)

def get_or_generate_session_id(session_id: Optional[str]) -> str:
    return session_id or str(uuid4())

# API Endpoints
@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"[User Query] {request.query} | SID: {session_id}")
        start_time = time.monotonic()
        response = await agent_with_history.ainvoke(
            {
                "input": request.query,
                "current_time": datetime.now(timezone.utc).isoformat(),
                "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                "tool_names": ", ".join([t.name for t in tools])
            },
            config={"configurable": {"session_id": session_id}}
        )
        
        end_time = time.monotonic()
        elapsed_seconds = end_time - start_time
        logger.info(f"[Latency] {elapsed_seconds} s")
        output = response.get("output", "").strip()
        await asyncio.sleep(elapsed_seconds*60)
        log_and_truncate_response(output)
        return {
            "status": "success",
            "session_id": session_id,
            "response": format_final_response(output)
        }
    except Exception as e:
        logger.error(f"[Chat Endpoint Error] {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

@app.post("/trace")
async def trace(request: TraceRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"[Trace Query] {request.query} | SID: {session_id}")
        start_time = time.monotonic()
        response_dict = await agent_with_history.ainvoke(
            {
                "input": request.query,
                "current_time": datetime.now(timezone.utc).isoformat(),
                "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                "tool_names": ", ".join([t.name for t in tools])
            },
            config={"configurable": {"session_id": session_id}}
        )
        end_time = time.monotonic()
        elapsed_seconds = end_time - start_time
        logger.info(f"[Latency] {elapsed_seconds} s")
        steps = response_dict.get("intermediate_steps", [])
        trace = []
        for idx, step in enumerate(steps):
            action, observation = step
            trace.append({
                "step": idx + 1,
                "thought": getattr(action, "log", None) or getattr(action, "thought", "N/A"),
                "tool": getattr(action, "tool", "N/A"),
                "tool_input": getattr(action, "tool_input", "N/A"),
                "tool_output": observation or "N/A"
            })
        final_output = response_dict.get('output', '')
        result = {
            "status": "success",
            "trace": trace,
            "final_output": format_final_response(final_output),
            "session_id": session_id
        }
        log_and_truncate_response(result)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"[Trace Endpoint Error] {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}. Please verify Elasticsearch or LLM connectivity and try again.")

# Main Entry
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="info")