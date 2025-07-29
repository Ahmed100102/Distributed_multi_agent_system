import os
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from elasticsearch import Elasticsearch
from langchain.agents import AgentExecutor, create_react_agent
from langchain.prompts import PromptTemplate
from langchain.tools import Tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain.chains.summarize import load_summarize_chain
from langchain.prompts import PromptTemplate as LC_PromptTemplate
from langchain_core.documents import Document

from llm_interface import LLMInterface

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Observibot")

def log_and_truncate_response(response, max_length=800):
    if isinstance(response, (dict, list)):
        response_str = json.dumps(response, indent=2)
    else:
        response_str = str(response)
    if len(response_str) > max_length:
        logger.info("Response (truncated): %s... [TRUNCATED, %d chars]", response_str[:max_length], len(response_str))
    else:
        logger.info("Response: %s", response_str)

# --- FastAPI App ---
app = FastAPI(title="Observibot", description="API for Observix analytics with tools")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None

class TraceRequest(BaseModel):
    query: str
    session_id: Optional[str] = None

# --- Elasticsearch Client ---
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://10.254.117.52:9200")
INDEX = os.getenv("ELASTICSEARCH_INDEX", "observix-results-*")
es = Elasticsearch(ES_URL, verify_certs=False, retry_on_timeout=True, max_retries=3, request_timeout=30)

# --- Field Policy ---
MINIMAL_FIELDS = [
    "log_id",
    "timestamp",
    "error_details.summary",
    "rca_details.summary",
    "rca_details.severity",
    "rca_details.category",
    "error_details.log_type"
]
REMEDIATION_FIELDS = [
    "remediation_plan.summary",
    "remediation_plan.steps.action"
]
ROOT_CAUSE_FIELDS = [
    "rca_details.root_causes.cause"
]
DETAILED_ANALYSIS_FIELDS = [
    "rca_details.detailed_analysis"
]

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
        "model": os.getenv("LLM_MODEL_REMEDIATION", "gemini-2.0-flash"),
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

# --- Utility Functions ---
def extract_first_json(s):
    try:
        return json.loads(s)
    except Exception:
        match = re.search(r'(\{.*\})', s, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception as e:
                raise ValueError(f"Could not parse JSON from extracted block: {e}")
        raise ValueError("No valid JSON object found in string")

def map_user_filters(filters: dict) -> dict:
    VALID_LOG_TYPES = {"gateway", "vgmlog", "fiscd", "netprobe", "infralog"}
    mapped = {}
    for k, v in filters.items():
        kl = k.lower()
        if kl == "severity":
            mapped["rca_details.severity"] = v.upper()
        elif kl == "date":
            mapped["timestamp"] = {"gte": "now-30d", "lte": "now"}
        elif kl in ("platform", "infra", "source"):
            log_type = str(v).lower()
            if log_type not in VALID_LOG_TYPES:
                logger.warning(f"Invalid log_type '{log_type}', must be one of {VALID_LOG_TYPES}")
                raise ValueError(f"Invalid source '{log_type}'. Must be one of: {', '.join(VALID_LOG_TYPES)}")
            mapped["error_details.log_type"] = log_type.upper()
        else:
            mapped[k] = v
    return mapped

def build_es_query(filters: dict) -> dict:
    must = []
    for field, value in filters.items():
        if isinstance(value, dict) and any(k in value for k in ("gte", "lte", "gt", "lt")):
            must.append({"range": {field: value}})
        else:
            must.append({"term": {field: value}})
    return {"bool": {"must": must}} if must else {"match_all": {}}

def get_requested_fields(user_request: str, explicit_fields: Optional[List[str]] = None) -> List[str]:
    user_request_lower = user_request.lower() if user_request else ""
    fields = set(explicit_fields) if explicit_fields else set(MINIMAL_FIELDS)
    if any(word in user_request_lower for word in [
        "remediation", "solution", "fix", "how to solve", "how do i solve", "how do i fix", "how to fix",
        "repair", "resolve", "resolution", "workaround", "steps to fix", "how can i fix", "how can this be fixed",
        "how to address", "mitigation", "how to remediate", "remediate", "timeline"
    ]):
        fields.update(REMEDIATION_FIELDS)
    if any(word in user_request_lower for word in [
        "root cause", "why", "reason", "cause", "origin", "what caused", "underlying issue", "failure reason"
    ]):
        fields.update(ROOT_CAUSE_FIELDS)
    if any(word in user_request_lower for word in [
        "details", "trace", "stack", "context", "full info", "detailed analysis", "explanation", "deep dive", "full analysis"
    ]):
        fields.update(DETAILED_ANALYSIS_FIELDS)
    if any(word in user_request_lower for word in [
        "everything", "all info", "all information", "full report", "complete details", "entire record", "all fields"
    ]):
        fields.update(MINIMAL_FIELDS)
        fields.update(REMEDIATION_FIELDS)
        fields.update(ROOT_CAUSE_FIELDS)
        fields.update(DETAILED_ANALYSIS_FIELDS)
    if not explicit_fields and not any(word in user_request_lower for word in [
        "summary", "list", "show", "display", "find", "fetch", "get", "give me"
    ]):
        if "?" in user_request_lower or any(word in user_request_lower for word in [
            "how", "what", "why", "can you", "could you", "would you", "explain", "help", "assist"
        ]):
            fields.update(REMEDIATION_FIELDS)
            fields.update(ROOT_CAUSE_FIELDS)
    logger.info(f"get_requested_fields: user_request='{user_request}' | explicit_fields={explicit_fields} | final_fields={fields}")
    return list(fields)

def filter_observix(filters: dict, fields_to_read: Optional[List[str]] = None, size: int = 5) -> list:
    query = build_es_query(filters)
    body = {
        "query": query,
        "size": size,
        "_source": fields_to_read if fields_to_read else MINIMAL_FIELDS
    }
    resp = es.search(index=INDEX, body=body)
    return [hit["_source"] for hit in resp["hits"]["hits"]]

def format_single_issue(issue: dict, fields_to_show: Optional[List[str]] = None) -> str:
    lines = []
    def show(field): return (not fields_to_show) or (field in fields_to_show)
    log_id = issue.get("log_id", "")
    timestamp = issue.get("timestamp", "")
    summary = issue.get("error_details", {}).get("summary", "") or issue.get("rca_details", {}).get("summary", "")
    severity = issue.get("rca_details", {}).get("severity", "")
    category = issue.get("rca_details", {}).get("category", "")
    log_type = issue.get("error_details", {}).get("log_type", "")

    if show("log_id"): lines.append(f"Log ID: {log_id}")
    if show("timestamp") and timestamp: lines.append(f"Timestamp: {timestamp}")
    if show("rca_details.severity") and severity: lines.append(f"Severity: {severity}")
    if show("rca_details.category") and category: lines.append(f"Category: {category}")
    if show("error_details.log_type") and log_type: lines.append(f"Log Type: {log_type}")
    if (show("error_details.summary") or show("rca_details.summary")) and summary: lines.append(f"Summary: {summary}")

    root_causes = []
    rca = issue.get("rca_details", {})
    if show("rca_details.root_causes.cause") and "root_causes" in rca:
        causes = rca["root_causes"]
        if isinstance(causes, list):
            for c in causes:
                cause = c.get("cause")
                if cause:
                    root_causes.append(cause)
        elif isinstance(causes, dict):
            cause = causes.get("cause")
            if cause:
                root_causes.append(cause)
    if root_causes:
        lines.append("Root Cause(s):")
        for idx, cause in enumerate(root_causes, 1):
            lines.append(f"  {idx}. {cause}")
    elif show("rca_details.summary") and "summary" in rca and rca["summary"]:
        lines.append(f"Root Cause: {rca['summary']}")
    else:
        lines.append("Root Cause: Not available for this error.")

    if show("rca_details.detailed_analysis"):
        details = rca.get("detailed_analysis", "")
        if details:
            lines.append(f"Detailed Analysis: {details}")

    remediation = issue.get("remediation_plan", {})
    remediation_summary = remediation.get("summary", "")
    remediation_steps = remediation.get("steps", [])
    if show("remediation_plan.summary") and remediation_summary:
        lines.append(f"Remediation Plan: {remediation_summary}")
    if show("remediation_plan.steps.action") and remediation_steps:
        lines.append("Remediation Steps:")
        for idx, step in enumerate(remediation_steps, 1):
            action = step.get("action", "")
            if action:
                lines.append(f"  Step {idx}: {action}")

    return "\n".join(lines)

def format_es_results_for_llm(data: list, fields_to_show: Optional[List[str]] = None) -> str:
    logger.info(f"Input to format_es_results_for_llm: {json.dumps(data, indent=2)}")
    if not data or len(data) == 0:
        logger.info("Returning NO_RESULTS_FOUND due to empty data")
        return "NO_RESULTS_FOUND"
    if len(data) == 1:
        result = format_single_issue(data[0], fields_to_show)
        logger.info(f"Formatted LLM input for single issue: {result}")
        return result
    lines = [f"THERE ARE {len(data)} RESULTS."]
    for d in data:
        log_id = d.get("log_id", "")
        summary = d.get("rca_details", {}).get("summary", d.get("error_details", {}).get("summary", ""))
        sev = d.get("rca_details", {}).get("severity", "")
        cat = d.get("rca_details", {}).get("category", "")
        ts = d.get("timestamp", "")
        log_type = d.get("error_details", {}).get("log_type", "")
        line = f"- Log ID: {log_id} | Severity: {sev} | Category: {cat} | Time: {ts} | Log Type: {log_type}\n  Summary: {summary}"
        lines.append(line)
    lines.append(f"THERE ARE {len(data)} RESULTS.")
    result = "\n".join(lines)
    logger.info(f"Formatted LLM input: {result}")
    return result

# --- Tools ---
def tool_filter_observix(params: Dict[str, Any]) -> str:
    try:
        filters = map_user_filters(params.get("filters", params))
        size = params.get("size", 5)
        user_request = params.get("user_request", "")
        explicit_fields = params.get("fields")
        fields = get_requested_fields(user_request, explicit_fields)
        results = filter_observix(filters, fields_to_read=fields, size=size)
        logger.info("Number of ES hits for query: %d", len(results))
        log_and_truncate_response(results)
        formatted_results = format_es_results_for_llm(results, fields)
        if len(results) > 7:
            return tool_summarize_results(formatted_results)
        return formatted_results
    except Exception as e:
        logger.error(f"Error in tool_filter_observix: {e}")
        return f"Error: {str(e)}"

def get_error_by_id(log_id: str) -> dict:
    query = {"term": {"log_id": log_id}}
    body = {"query": query, "size": 1}
    resp = es.search(index=INDEX, body=body)
    hits = resp["hits"]["hits"]
    if hits:
        logger.info(f"Full ES doc for log_id={log_id}: {json.dumps(hits[0]['_source'], indent=2)}")
    return hits[0]["_source"] if hits else {}

def tool_get_error_by_id(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        log_id = params.get("log_id")
        if not log_id:
            return {"return_direct": True, "result": "Error: 'log_id' is required."}
        user_request = params.get("user_request", "")
        explicit_fields = params.get("fields")
        fields = set(get_requested_fields(user_request, explicit_fields))
        fields.update(REMEDIATION_FIELDS)
        fields = list(fields)
        logger.info(f"tool_get_error_by_id: user_request='{user_request}' | fields returned: {fields}")
        error = get_error_by_id(log_id)
        log_and_truncate_response(error)
        if not error:
            return {"return_direct": True, "result": f"No error found with log_id: {log_id}"}
        formatted_error = format_es_results_for_llm([error], fields)
        if "summarize" in user_request.lower():
            summarized = tool_summarize_results(formatted_error)
            return {"return_direct": False, "result": summarized}
        return {"return_direct": True, "result": formatted_error}
    except Exception as e:
        logger.error(f"Error in tool_get_error_by_id: {e}")
        return {"return_direct": True, "result": f"Error: {str(e)}"}

def tool_summarize_results(formatted_results: str) -> str:
    try:
        logger.info(f"SummarizeResults input: {formatted_results[:100]}...")
        if formatted_results == "NO_RESULTS_FOUND":
            return "No issues were found."
        if not formatted_results.startswith("THERE ARE "):
            return formatted_results
        lines = formatted_results.split("\n")
        results = [line for line in lines if line.startswith("- Log ID:")]
        if len(results) > 10:
            return f"Summary of {len(results)} issues: Multiple issues detected. Common patterns include [e.g., client session errors, network queue issues]. Please refine the query for detailed analysis."
        return "\n".join([line for line in lines if line.startswith("- Log ID:") or line.startswith("  Summary:")])
    except Exception as e:
        logger.error(f"Error in tool_summarize_results: {e}")
        return f"Error: {str(e)}"

def tool_direct_answer(text: str) -> str:
    logger.info(f"DirectAnswer: {text}")
    return text

def tool_return_direct(text: str) -> str:
    logger.info(f"ReturnDirect: {text}")
    return text

# --- Custom Agent Executor ---
class CustomAgentExecutor(AgentExecutor):
    def _parse_llm_output(self, output: str) -> tuple[str, Any]:
        """Parse LLM output to extract action and input."""
        try:
            action_match = re.search(r"Action:\s*(\w+)\s*\nAction Input:\s*(.+)", output, re.DOTALL)
            if action_match:
                action, action_input = action_match.groups()
                valid_tools = ["FilterObservix", "GetErrorById", "SummarizeResults", "DirectAnswer", "ReturnDirect"]
                if action not in valid_tools:
                    raise ValueError(f"Invalid tool name: {action}")
                if action in ["FilterObservix", "GetErrorById"]:
                    action_input = json.loads(action_input.strip())
                return action, action_input
            else:
                raise ValueError("Could not parse LLM output format")
        except Exception as e:
            logger.error(f"Parsing error: {e}\nOutput: {output}")
            if "ReturnDirect" in output:
                input_match = re.search(r"Action Input:\s*(.+)", output, re.DOTALL)
                if input_match:
                    logger.info("Recovered ReturnDirect output")
                    return "ReturnDirect", input_match.group(1).strip()
            retry_prompt = f"""
            Previous output failed to parse: {output}
            Please provide the response in the correct format:
            ```
            Action: [ToolName]
            Action Input: [Valid JSON or string]
            ```
            Use exact tool names: FilterObservix, GetErrorById, SummarizeResults, DirectAnswer, ReturnDirect.
            """
            try:
                retry_output = self.llm.invoke(retry_prompt)
                action, action_input = self._parse_llm_output(retry_output)
                return action, action_input
            except Exception as retry_error:
                logger.error(f"Retry failed: {retry_error}")
                return "DirectAnswer", f"Error processing query due to output parsing failure: {str(e)}. Please try rephrasing the query."

    def _call(self, inputs: Dict[str, Any], run_manager=None) -> Dict[str, Any]:
        """Execute the agent with input query."""
        query = inputs.get("input", "")
        steps = []
        max_iterations = 4
        current_step = 0

        while current_step < max_iterations:
            current_step += 1
            llm_output = self.llm.invoke(self._prepare_prompt(query, steps))
            action, action_input = self._parse_llm_output(llm_output)

            if action == "ReturnDirect":
                return {"output": action_input}

            if action == "FilterObservix":
                result = tool_filter_observix(action_input)
            elif action == "GetErrorById":
                result = tool_get_error_by_id(action_input)
                if result.get("return_direct", False):
                    return {"output": result["result"]}
                result = result["result"]
            elif action == "SummarizeResults":
                result = tool_summarize_results(action_input)
            elif action == "DirectAnswer":
                result = tool_direct_answer(action_input)
            else:
                result = tool_return_direct(action_input)

            steps.append(({"tool": action, "tool_input": action_input, "log": llm_output}, result))

            if result == "NO_RESULTS_FOUND":
                return {"output": "NO_RESULTS_FOUND"}

        logger.warning("Agent stopped due to iteration limit")
        return {"output": "Agent stopped due to iteration limit. Please try rephrasing the query."}

    def _prepare_prompt(self, query: str, steps: list) -> str:
        """Prepare the prompt with query and history."""
        history = "\n".join([f"Step {i+1}: Action: {step[0]['tool']}, Input: {step[0]['tool_input']}, Observation: {step[1]}" for i, step in enumerate(steps)])
        return react_prompt.format(
            input=query,
            current_time=datetime.now(timezone.utc).isoformat(),
            tools="\n".join([f"{t.name}: {t.description}" for t in tools]),
            tool_names=", ".join([t.name for t in tools]),
            agent_scratchpad=history
        )

# --- Tools Definition ---
tools = [
    Tool(
        name="FilterObservix",
        func=tool_filter_observix,
        description=(
            "Filters Observix issues based on criteria. Input: JSON with 'filters' (e.g., {'rca_details.severity': 'HIGH', 'error_details.log_type': 'GATEWAY', 'timestamp': {'gte': 'now-30d', 'lte': 'now'}}), 'size' (default 5), and 'user_request'. "
            "Maps user-friendly keys (e.g., 'source' to 'error_details.log_type'). Returns formatted results or 'NO_RESULTS_FOUND'."
        ),
        return_direct=False
    ),
    Tool(
        name="GetErrorById",
        func=tool_get_error_by_id,
        description=(
            "Fetches a single error by log_id. Input: JSON with 'log_id', 'user_request', and optional 'fields'. "
            "Returns detailed error report unless 'summarize' is in user_request, then chains to SummarizeResults."
        ),
        return_direct=False
    ),
    Tool(
        name="SummarizeResults",
        func=tool_summarize_results,
        description=(
            "Summarizes formatted output from FilterObservix or GetErrorById. Input: Formatted string. "
            "Returns concise summary for lists (≤10 issues) or single issues."
        ),
        return_direct=False
    ),
    Tool(
        name="DirectAnswer",
        func=tool_direct_answer,
        description="Answers greetings or general questions. Input: The answer to return. Chains to ReturnDirect.",
        return_direct=False
    ),
    Tool(
        name="ReturnDirect",
        func=tool_return_direct,
        description="Returns the final response. Input: Formatted string. ALWAYS use this for the final response.",
        return_direct=True
    )
]

# --- Prompt ---
react_prompt = PromptTemplate.from_template("""
You are Observibot, an expert Observix analyst designed to provide deterministic, accurate, and actionable responses for system issue analysis.

**TOOLS AVAILABLE**:
{tools}
**Tool Names**: {tool_names}

**AVAILABLE FIELDS**:
- **log_id**: Unique identifier (e.g., "z5Ybt5cB4rxvVeedN1Eu").
- **timestamp**: ISO 8601 format (e.g., "2025-07-01T15:15:16.186218+00:00"). Supports range queries (e.g., {{"gte": "now-30d", "lte": "now"}}).
- **rca_details.severity**: Severity level (e.g., HIGH, MEDIUM, LOW).
- **rca_details.category**: Issue category (e.g., APPLICATION, NETWORK, DATABASE).
- **error_details.log_type**: Log type (e.g., gateway, vgmlog, FISCD, netprobe, infraLog).
- **error_details.summary**: Brief error description.
- **rca_details.summary**: Root cause analysis summary.
- **rca_details.root_causes.cause**: Root cause description.
- **remediation_plan**: List of remediation steps.
- **rca_details.detailed_analysis**: Detailed technical analysis or stack trace.

**INSTRUCTIONS**:
1. **Query Analysis**:
   - Identify if the query is for a single issue (mentions `log_id`), a list/filter (mentions filters like severity, category, timestamp, or source), or a direct question/greeting.
   - Map query terms to fields (e.g., "high-severity" → `rca_details.severity: "HIGH"`, "gateway errors" → `error_details.log_type: "GATEWAY"`).

2. **Single Issue Queries (e.g., "how do I fix log_id X", "summarize error for log_id X")**:
   - Use `GetErrorById` with `log_id` and `user_request`.
   - If "summarize" is in the query, chain to `SummarizeResults` for a concise summary (log_id, summary, severity, category, log_type, root cause, brief remediation).
   - Use `ReturnDirect` to deliver the final output.

3. **List/Filter Queries (e.g., "List all high-severity APPLICATION category issues")**:
   - Use `FilterObservix` with appropriate `filters` and `user_request`.
   - If no results, return "NO_RESULTS_FOUND" via `ReturnDirect`.
   - If ≤7 results, use `ReturnDirect` with formatted results.
   - If >7 results, use `SummarizeResults`, then `ReturnDirect`.

4. **Timestamp Queries (e.g., "Find issues last 30 days")**:
   - Use `FilterObservix` with `timestamp` filter (e.g., {{"timestamp": {{"gte": "now-30d", "lte": "now"}}}}).
   - Follow list query rules for results handling.

5. **Source Queries (e.g., "gateway errors", "infraLog issues")**:
   - Map 'platform', 'infra', 'source', or source-specific terms to `error_details.log_type` (valid: gateway, vgmlog, FISCD, netprobe, infraLog).
   - Use `FilterObservix` with the mapped filter.

6. **General/Greeting Queries (e.g., "hi", "hello")**:
   - Use `DirectAnswer` for a friendly response, then chain to `ReturnDirect`.

7. **Output Formatting**:
   - Always format actions as:
     ```
     Action: [ToolName]
     Action Input: [Valid JSON or string]
     ```
   - Ensure `Action Input` is valid JSON for `FilterObservix` and `GetErrorById`, or a formatted string for `SummarizeResults`, `DirectAnswer`, and `ReturnDirect`.
   - For `ReturnDirect`, format as Markdown with clear structure (e.g., bullet points for lists, sections for single issues).

8. **Error Handling**:
   - If a tool returns an error, use `DirectAnswer` to explain and suggest next steps.
   - If parsing fails, retry with exact tool names: `FilterObservix`, `GetErrorById`, `SummarizeResults`, `DirectAnswer`, `ReturnDirect`.

**EXAMPLES**:
1. **Single Issue Query**:
   Query: "summarize error for log_id z5Ybt5cB4rxvVeedN1Eu"
   ```
   <think>The query requests a summary for a specific log_id. Use GetErrorById, then SummarizeResults.</think>
   Action: GetErrorById
   Action Input: {{"log_id": "z5Ybt5cB4rxvVeedN1Eu", "user_request": "summarize error"}}
   Observation: [Detailed error details]
   Action: SummarizeResults
   Action Input: [Detailed error details]
   Action: ReturnDirect
   Action Input: #### Error Summary for Log ID z5Ybt5cB4rxvVeedN1Eu\n- **Timestamp**: 2025-07-01T15:15:16.186218+00:00\n- **Severity**: HIGH\n- **Category**: APPLICATION\n- **Log Type**: vgmlog\n- **Summary**: ClientSessionManager failed to flush clients.\n- **Root Cause**: Likely database connection failure.\n- **Remediation**: Check database connectivity, restart server.
   ```

2. **List Query**:
   Query: "List all high-severity APPLICATION category issues"
   ```
   <think>Use FilterObservix with filters for severity and category.</think>
   Action: FilterObservix
   Action Input: {{"filters": {{"rca_details.severity": "HIGH", "rca_details.category": "APPLICATION"}}, "user_request": "high-severity APPLICATION category issues"}}
   Observation: THERE ARE 5 RESULTS.\n[...]
   Action: ReturnDirect
   Action Input: #### High-Severity APPLICATION Category Issues\n1. **Log ID**: s2GkZJcB4rxvVeed6fc1\n   - **Severity**: HIGH\n   - **Category**: APPLICATION\n   - **Time**: 2025-06-14T13:59:05.686125+00:00\n   - **Log Type**: vgmlog\n   - **Summary**: Failed to locate OTP for instrument MLP/5209.\n[...]
   ```

3. **Timestamp Query**:
   Query: "Find issues last 30 days"
   ```
   <think>Use FilterObservix with a timestamp filter.</think>
   Action: FilterObservix
   Action Input: {{"filters": {{"timestamp": {{"gte": "now-30d", "lte": "now"}}}}, "user_request": "issues last 30 days"}}
   Observation: THERE ARE 5 RESULTS.\n[...]
   Action: ReturnDirect
   Action Input: #### Issues from Last 30 Days\n1. **Log ID**: MZYbt5cB4rxvVeedMz-Y\n   - **Severity**: HIGH\n   - **Category**: APPLICATION\n   - **Time**: 2025-07-01T15:17:49.275870+00:00\n   - **Log Type**: vgmlog\n   - **Summary**: Socket write error due to full queue.\n[...]
   ```

4. **Source Query**:
   Query: "List all FISCD errors"
   ```
   <think>Use FilterObservix with error_details.log_type=FISCD.</think>
   Action: FilterObservix
   Action Input: {{"filters": {{"error_details.log_type": "FISCD"}}, "user_request": "FISCD errors"}}
   Observation: THERE ARE 3 RESULTS.\n[...]
   Action: ReturnDirect
   Action Input: #### FISCD Errors\n1. **Log ID**: abc123\n   - **Severity**: MEDIUM\n   - **Category**: DATABASE\n   - **Time**: 2025-07-01T10:00:00+00:00\n   - **Log Type**: FISCD\n   - **Summary**: Database query timeout.\n[...]
   ```

5. **Greeting Query**:
   Query: "hi"
   ```
   <think>The query is a greeting. Use DirectAnswer, then ReturnDirect.</think>
   Action: DirectAnswer
   Action Input: Hello! How can I assist you with Observix analytics today?
   Action: ReturnDirect
   Action Input: Hello! How can I assist you with Observix analytics today?
   ```

**Current Time**: {current_time}
**Question**: {input}
{agent_scratchpad}
""")

# --- Agent ---
agent = create_react_agent(
    llm=llm,
    tools=tools,
    prompt=react_prompt
)
executor = CustomAgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    max_iterations=4,
    return_intermediate_steps=True
)

session_histories = {}
def get_session_history(session_id: str):
    if session_id not in session_histories:
        session_histories[session_id] = ChatMessageHistory()
    return session_histories[session_id]

agent_with_history = RunnableWithMessageHistory(
    runnable=executor,
    get_session_history=get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history"
)

def get_or_generate_session_id(session_id: Optional[str]) -> str:
    if session_id:
        return session_id
    return str(uuid4())

def format_final_response(text: str) -> str:
    if not text or text.strip() == "" or text.strip().lower().startswith("error"):
        return "❗ **Sorry, I could not process your request. Please try again.**"
    if text.startswith("NO_RESULTS_FOUND") or "No issues were found" in text or "No matching issues found" in text:
        return "### No Results Found\nNo matching issues were found for your query."
    section_map = {
        "Log ID:": "#### Log ID",
        "Timestamp:": "#### Timestamp",
        "Severity:": "#### Severity",
        "Category:": "#### Category",
        "Log Type:": "#### Log Type",
        "Summary:": "#### Summary",
        "Root Cause:": "#### Root Cause",
        "Root Cause(s):": "#### Root Cause(s)",
        "Detailed Analysis:": "#### Detailed Analysis",
        "Remediation Plan:": "#### Remediation Plan",
        "Remediation Steps:": "#### Remediation Steps",
    }
    lines = text.splitlines()
    formatted = []
    for line in lines:
        for k, v in section_map.items():
            if line.startswith(k):
                line = line.replace(k, v)
                break
        if re.match(r"\s*Step \d+:", line):
            line = f"- {line.strip()}"
        formatted.append(line)
    return "\n".join(formatted)

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"Received query: '{request.query}' (session_id={session_id})")
        response_dict = await agent_with_history.ainvoke(
            {
                "input": request.query,
                "current_time": datetime.now(timezone.utc).isoformat(),
                "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                "tool_names": ", ".join([t.name for t in tools])
            },
            config={"configurable": {"session_id": session_id}}
        )
        placeholder_patterns = [
            r"\[.*list of.*issues.*\]",
            r"\[.*full error details.*\]",
            r"\[.*observation.*\]",
            r"\[.*summary.*\]",
            r"^\s*\[.*\]\s*$"
        ]
        final_output = response_dict.get('output', '').strip()
        is_placeholder = (
            not final_output
            or any(re.match(p, final_output, re.IGNORECASE) for p in placeholder_patterns)
            or "Check output and use exact tool names" in final_output
        )
        if is_placeholder:
            steps = response_dict.get("intermediate_steps", [])
            if steps:
                last_obs = steps[-1][1] if isinstance(steps[-1], (list, tuple)) and len(steps[-1]) > 1 else None
                if last_obs:
                    final_output = last_obs.strip()
            if not final_output:
                final_output = "Sorry, I could not process your request. Please try again."
        formatted = format_final_response(final_output)
        log_and_truncate_response(formatted)
        logger.info("Agent intermediate steps:")
        for idx, step in enumerate(response_dict.get("intermediate_steps", [])):
            logger.info(f"Step {idx+1}: {step}")
        return {
            "status": "success",
            "response": formatted,
            "session_id": session_id
        }
    except Exception as e:
        logger.error(f"Error processing query: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}. Please verify Elasticsearch or LLM connectivity and try again.")

@app.post("/trace")
async def trace(request: TraceRequest):
    try:
        session_id = get_or_generate_session_id(request.session_id)
        logger.info(f"Received trace query: '{request.query}' (session_id={session_id})")
        response_dict = await agent_with_history.ainvoke(
            {
                "input": request.query,
                "current_time": datetime.now(timezone.utc).isoformat(),
                "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                "tool_names": ", ".join([t.name for t in tools])
            },
            config={"configurable": {"session_id": session_id}}
        )
        steps = response_dict.get("intermediate_steps", [])
        trace = []
        for idx, step in enumerate(steps):
            action, observation = step
            trace.append({
                "step": idx + 1,
                "thought": action.get("log", ""),
                "tool": action.get("tool", ""),
                "tool_input": action.get("tool_input", ""),
                "tool_output": observation
            })
        final_output = response_dict.get('output', '')
        result = {
            "status": "success",
            "trace": trace,
            "final_output": format_final_response(final_output),
            "session_id": session_id
        }
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error processing trace: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}. Please verify Elasticsearch or LLM connectivity and try again.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8200)
