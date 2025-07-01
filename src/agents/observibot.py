from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import logging
import re
import os
from datetime import datetime, UTC, timedelta
from typing import Dict, List, Any, Optional
from elasticsearch import Elasticsearch
from langchain.agents import AgentExecutor, create_react_agent
from langchain.prompts import PromptTemplate
from langchain.tools import Tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain_ollama import OllamaLLM
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.exceptions import LangChainException
from pydantic import SecretStr
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="ObservabilityAnalyst Chatbot", description="API for analyzing Observix results")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic model for request body
class ChatRequest(BaseModel):
    query: str
    session_id: str = "observability_session"

# LLMInterface
class LLMInterface:
    def __init__(self, provider: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.provider = provider.lower()
        self.model = model
        self.endpoint = endpoint or "http://localhost:18000"
        self.api_key = api_key
        self.llm = self._initialize_llm()

    def _initialize_llm(self):
        if self.provider == "ollama":
            return OllamaLLM(model=self.model, base_url=self.endpoint)
        elif self.provider == "openai":
            return ChatOpenAI(model=self.model, api_key=SecretStr(self.api_key) if self.api_key else None)
        elif self.provider == "gemini":
            if not self.api_key:
                raise ValueError("API key required for Gemini")
            return ChatGoogleGenerativeAI(model=self.model, google_api_key=self.api_key)
        elif self.provider == "groq":
            return ChatGroq(model=self.model, api_key=SecretStr(self.api_key) if self.api_key else None, temperature=0.7)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def call(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> str:
        try:
            prompt = f"System: {system_prompt}\nUser: {user_prompt}"
            response = self.llm.invoke(prompt, timeout=timeout)
            result = str(response.content) if hasattr(response, 'content') else str(response)
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            return result.strip()
        except Exception as e:
            raise LangChainException(f"LLM call failed: {str(e)}")

# Environment configuration for LLM
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["ollama", "openai", "gemini", "groq"]
if MODEL_RUNTIME not in VALID_RUNTIMES:
    raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")

if MODEL_RUNTIME == "gemini":
    LLM_PROVIDER = "gemini"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "gemini-2.0-flash")
    LLM_ENDPOINT = None
    LLM_API_KEY = "AIzaSyB0uvjN8c94PsROQHCwIDxAv3vjQwScTp4"
elif MODEL_RUNTIME == "ollama":
    LLM_PROVIDER = "ollama"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "llama3")
    LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:11434")
    LLM_API_KEY = None
elif MODEL_RUNTIME == "openai":
    LLM_PROVIDER = "openai"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "gpt-4o")
    LLM_ENDPOINT = None
    LLM_API_KEY = os.getenv("OPENAI_API_KEY")
elif MODEL_RUNTIME == "groq":
    LLM_PROVIDER = "groq"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "mixtral-8x7b-32768")
    LLM_ENDPOINT = None
    LLM_API_KEY = os.getenv("GROQ_API_KEY")

logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s", LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT or "default")

# Initialize LLM
llm = LLMInterface(provider=LLM_PROVIDER, model=LLM_MODEL, endpoint=LLM_ENDPOINT, api_key=LLM_API_KEY)

# Elasticsearch client setup
es = Elasticsearch(
    "http://10.254.117.52:9200",
    verify_certs=False,
    ssl_show_warn=False,
    request_timeout=30,
    max_retries=3
)

query_cache = TTLCache(maxsize=200, ttl=300)

# Helper functions
def parse_time_range(time_range: str) -> dict:
    """Parse any time range (e.g., 'last 2 hours', 'last 3 days') into Elasticsearch range query."""
    try:
        match = re.match(r'last\s+(\d+)\s+(hour|day|week|month|year)s?', time_range.lower())
        if not match:
            logger.warning(f"Invalid time range '{time_range}', defaulting to last 24 hours")
            return {"range": {"remediation_metadata.remediation_timestamp": {"gte": "now-24h", "lte": "now"}}}

        value, unit = int(match.group(1)), match.group(2)
        unit_map = {"hour": "h", "day": "d", "week": "w", "month": "M", "year": "y"}
        es_unit = unit_map.get(unit, "h")
        return {"range": {"remediation_metadata.remediation_timestamp": {"gte": f"now-{value}{es_unit}", "lte": "now"}}}
    except Exception as e:
        logger.error(f"Error parsing time range '{time_range}': {str(e)}")
        return {"range": {"remediation_metadata.remediation_timestamp": {"gte": "now-24h", "lte": "now"}}}

def get_stored_data_info() -> dict:
    """Fetch available data info from observix-results-* (components, severities, categories, time range)."""
    try:
        if not es.ping():
            return {"components": [], "severities": [], "categories": [], "time_range": {"earliest": "unknown", "latest": "unknown"}}

        # Fetch components, severities, and categories
        query = {
            "query": {"bool": {"filter": [{"exists": {"field": "rca_details.component"}}]}},
            "aggs": {
                "by_component": {"terms": {"field": "rca_details.component.keyword", "size": 10}},
                "by_severity": {"terms": {"field": "rca_details.severity.keyword", "size": 10}},
                "by_category": {"terms": {"field": "rca_details.category.keyword", "size": 10}},
                "by_timestamp": {
                    "stats": {"field": "remediation_metadata.remediation_timestamp"}
                }
            },
            "size": 0
        }
        result = es.search(index="observix-results-*", body=query)

        components = [bucket["key"] for bucket in result["aggregations"]["by_component"]["buckets"]]
        severities = [bucket["key"] for bucket in result["aggregations"]["by_severity"]["buckets"]]
        categories = [bucket["key"] for bucket in result["aggregations"]["by_category"]["buckets"]]
        time_stats = result["aggregations"]["by_timestamp"]
        earliest = time_stats.get("min_as_string", "unknown")
        latest = time_stats.get("max_as_string", "unknown")

        return {
            "components": components or ["FISCD", "VGM", "Netprobe", "Gateway", "Observix"],
            "severities": severities or ["HIGH", "MEDIUM", "LOW"],
            "categories": categories or ["OTHER"],
            "time_range": {"earliest": earliest, "latest": latest}
        }
    except Exception as e:
        logger.error(f"Failed to fetch stored data info: {str(e)}")
        return {
            "components": ["FISCD", "VGM", "Netprobe", "Gateway", "Observix"],
            "severities": ["HIGH", "MEDIUM", "LOW"],
            "categories": ["OTHER"],
            "time_range": {"earliest": "unknown", "latest": "unknown"}
        }

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def analyze_observix_results(params: dict) -> str:
    """Query observix-results-* for analysis results based on parameters."""
    try:
        index = "observix-results-*"
        time_range = params.get("time_range", "last 24 hours")
        components = params.get("components", [])
        severity = params.get("severity", None)
        category = params.get("category", None)
        analysis_type = params.get("analysis_type", "observix_analysis")

        if not es.ping():
            return json.dumps({"status": "error", "message": "Elasticsearch connection failed", "recommendation": "Verify connectivity to http://10.254.117.52:9200"})

        if not es.indices.exists(index=index):
            return json.dumps({"status": "error", "message": f"Index {index} does not exist", "recommendation": "Verify Logstash is ingesting Observix results"})

        query = {
            "query": {"bool": {"must": [parse_time_range(time_range)]}},
            "size": 500,
            "sort": [{"remediation_metadata.remediation_timestamp": {"order": "desc"}}],
            "aggs": {
                "by_severity": {"terms": {"field": "rca_details.severity.keyword", "size": 10}},
                "by_component": {"terms": {"field": "rca_details.component.keyword", "size": 10}},
                "by_category": {"terms": {"field": "rca_details.category.keyword", "size": 10}},
                "by_error_pattern": {"terms": {"field": "rca_details.system_state.error_patterns.keyword", "size": 10}}
            }
        }

        if components:
            query["query"]["bool"]["must"].append({"terms": {"rca_details.component.keyword": components}})
        if severity:
            query["query"]["bool"]["must"].append({"match": {"rca_details.severity": severity}})
        if category:
            query["query"]["bool"]["must"].append({"match": {"rca_details.category": category}})

        if analysis_type == "performance_analysis":
            query["aggs"].update({
                "performance_over_time": {
                    "date_histogram": {
                        "field": "remediation_metadata.remediation_timestamp",
                        "fixed_interval": "1h",
                        "min_doc_count": 0
                    },
                    "aggs": {
                        "avg_response_time": {"avg": {"field": "error_details.response_time", "missing": 0}},
                        "avg_cpu_usage": {"avg": {"field": "rca_details.system_state.cpu_usage", "missing": 0}},
                        "avg_memory_usage": {"avg": {"field": "rca_details.system_state.memory_usage", "missing": 0}}
                    }
                }
            })

        result = es.search(index=index, body=query)
        total_logs = result["hits"]["total"]["value"]

        if total_logs == 0:
            return json.dumps({
                "status": "success",
                "data": {
                    "total_logs": 0,
                    "severity_distribution": {},
                    "component_distribution": {},
                    "category_distribution": {},
                    "error_pattern_distribution": {},
                    "issues": [],
                    "warning": f"No Observix results found in {index} for {time_range}. Verify data ingestion."
                }
            })

        issues = [
            {
                "log_id": hit["_source"].get("log_id", "unknown"),
                "summary": hit["_source"].get("rca_details", {}).get("summary", "Unknown"),
                "root_causes": hit["_source"].get("rca_details", {}).get("root_causes", []),
                "remediation_plan": hit["_source"].get("remediation_plan", {}),
                "severity": hit["_source"].get("rca_details", {}).get("severity", "UNKNOWN"),
                "category": hit["_source"].get("rca_details", {}).get("category", "UNKNOWN"),
                "component": hit["_source"].get("rca_details", {}).get("component", "UNKNOWN"),
                "timestamp": hit["_source"].get("remediation_metadata", {}).get("remediation_timestamp", "unknown")
            }
            for hit in result["hits"]["hits"]
        ]

        analysis_result = {
            "total_logs": total_logs,
            "severity_distribution": {bucket["key"]: bucket["doc_count"] for bucket in result["aggregations"]["by_severity"]["buckets"]},
            "component_distribution": {bucket["key"]: bucket["doc_count"] for bucket in result["aggregations"]["by_component"]["buckets"]},
            "category_distribution": {bucket["key"]: bucket["doc_count"] for bucket in result["aggregations"]["by_category"]["buckets"]},
            "error_pattern_distribution": {bucket["key"]: bucket["doc_count"] for bucket in result["aggregations"]["by_error_pattern"]["buckets"]},
            "issues": issues[:10],  # Limit to top 10 issues for brevity
        }

        if analysis_type == "performance_analysis":
            analysis_result["performance_trends"] = result["aggregations"].get("performance_over_time", {}).get("buckets", [])

        return json.dumps({"status": "success", "data": analysis_result})

    except Exception as e:
        logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
        return json.dumps({"status": "error", "message": f"Elasticsearch search failed: {str(e)}", "recommendation": "Check Elasticsearch connectivity and index mappings"})

def generate_insights(data: str) -> str:
    """Generate human-readable insights from Observix analysis results."""
    try:
        data_dict = json.loads(data) if isinstance(data, str) else data
        insights = []

        if data_dict.get("status") == "error":
            insights.append(f"⚠️ Analysis Error: {data_dict.get('message', 'Unknown error')}")
            insights.append(f"Recommendation: {data_dict.get('recommendation', 'Verify Elasticsearch connectivity to http://10.254.117.52:9200 and index mappings.')}")
            return "\n".join(insights)

        analysis_data = data_dict.get("data", {})
        total_logs = analysis_data.get("total_logs", 0)

        if total_logs == 0:
            insights.append(f"⚠️ No Observix results found: {analysis_data.get('warning', 'No data available')}")
            insights.append("Recommendation: Verify data ingestion into observix-results-*.")
        else:
            insights.append(f"📊 Analyzed {total_logs} Observix results.")
            severity_dist = analysis_data.get("severity_distribution", {})
            if severity_dist:
                high_severity = severity_dist.get("HIGH", 0)
                if high_severity > 0:
                    insights.append(f"🚨 {high_severity} high-severity issues detected.")
                top_severity = max(severity_dist.items(), key=lambda x: x[1], default=("Unknown", 0))
                insights.append(f"🔍 Dominant severity: {top_severity[0]} ({top_severity[1]} occurrences)")

            component_dist = analysis_data.get("component_distribution", {})
            if component_dist:
                top_component = max(component_dist.items(), key=lambda x: x[1], default=("Unknown", 0))
                insights.append(f"🖥️ Most affected component: {top_component[0]} ({top_component[1]} issues)")

            error_patterns = analysis_data.get("error_pattern_distribution", {})
            if error_patterns:
                top_pattern = max(error_patterns.items(), key=lambda x: x[1], default=("Unknown", 0))
                insights.append(f"🔍 Dominant error pattern: {top_pattern[0]} ({top_pattern[1]} occurrences)")

            for issue in analysis_data.get("issues", [])[:3]:  # Limit to top 3 issues
                insights.append(f"  - {issue['summary']} (Severity: {issue['severity']}, Component: {issue['component']})")
                if issue.get("remediation_plan", {}).get("summary"):
                    insights.append(f"    Remediation: {issue['remediation_plan']['summary']}")
                if issue.get("root_causes"):
                    insights.append(f"    Root Causes: {', '.join(issue['root_causes'])}")

            if "performance_trends" in analysis_data and analysis_data["performance_trends"]:
                recent_trend = analysis_data["performance_trends"][-1]
                if recent_trend.get("doc_count", 0) > 0:
                    insights.append(f"📈 Performance (last hour): CPU {recent_trend.get('avg_cpu_usage', {}).get('value', 0):.1f}%, Memory {recent_trend.get('avg_memory_usage', {}).get('value', 0):.1f}%")

        if not insights:
            insights.append("✅ No critical issues detected.")
            insights.append("Recommendation: Try a different time range or verify data ingestion.")

        return "\n".join(insights)

    except Exception as e:
        logger.error(f"Error generating insights: {str(e)}")
        return f"⚠️ Error generating insights: {str(e)}\nRecommendation: Verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Observix results are ingested."

# Predefined prompts for common queries
PREDEFINED_PROMPTS = {
    "health": {
        "description": "Analyze overall system health",
        "analysis_type": "observix_analysis",
        "params": {"components": None, "time_range": "last 24 hours"}
    },
    "trends": {
        "description": "Identify error patterns and trends",
        "analysis_type": "observix_analysis",
        "params": {"time_range": "last 24 hours"}
    },
    "high_severity": {
        "description": "List high-severity issues",
        "analysis_type": "observix_analysis",
        "params": {"severity": "HIGH", "time_range": "last 24 hours"}
    },
    "performance": {
        "description": "Analyze performance trends",
        "analysis_type": "performance_analysis",
        "params": {"time_range": "last 24 hours"}
    }
}

tools = [
    Tool(
        name="AnalyzeObservixResults",
        func=analyze_observix_results,
        description="Query observix-results-* index for analysis results. Input is JSON with 'analysis_type' ('observix_analysis' or 'performance_analysis'), 'time_range' (e.g., 'last 2 hours'), 'components', 'severity', and 'category'."
    ),
    Tool(
        name="GenerateInsights",
        func=generate_insights,
        description="Generate actionable insights from Observix analysis results."
    )
]

# Session history storage
session_histories = {}

# Fetch stored data info for agent context
stored_data_info = get_stored_data_info()

react_prompt = PromptTemplate.from_template("""You are ObservabilityAnalyst, an AI specialized in analyzing pre-computed Observix results from the observix-results-* index.

Your role is to query the observix-results-* index and provide insights on:
- System health and error patterns
- Root cause analysis (RCA) and remediation plans
- Component-specific issues
- Performance trends (CPU, memory, response time)

Available tools: {tools}
Tool names: {tool_names}

Stored Data Info:
- Components: {components}
- Severities: {severities}
- Categories: {categories}
- Available Time Range: {earliest} to {latest}

Predefined Prompts:
- health: Analyze overall system health
- trends: Identify error patterns and trends
- high_severity: List high-severity issues
- performance: Analyze performance trends

ANALYSIS APPROACH:
1. Map user query to a predefined prompt (health, trends, high_severity, performance) if applicable, or parse for custom parameters (time range, components, severity, category).
2. Use AnalyzeObservixResults with 'observix_analysis' for health/errors or 'performance_analysis' for performance metrics.
3. Support any time range (e.g., 'last 2 hours', 'last 3 days') by passing to AnalyzeObservixResults; default to 'last 24 hours' if unspecified.
4. Use components, severities, and categories from stored data info ({components}, {severities}, {categories}) unless user specifies others.
5. If AnalyzeObservixResults fails, use GenerateInsights to report errors and suggest diagnostics (e.g., check Elasticsearch connectivity to http://10.254.117.52:9200).
6. Summarize RCA details, remediation plans, and performance metrics, focusing on high-severity issues.

RESPONSE FORMAT:
Thought: [Determine analysis type and parameters based on query and stored data]
Action: AnalyzeObservixResults
Action Input: [JSON with analysis_type, time_range, components, severity, category]
Observation: [Tool output]
Thought: [Interpret results or handle errors]
Action: GenerateInsights
Action Input: [Analysis results]
Observation: [Insights output]
Final Answer: [Summarized insights with recommendations]

Current time: {current_time}
Stored Data Info: {stored_data_info}

Question: {input}
{agent_scratchpad}
""")

agent = create_react_agent(
    llm=llm.llm,
    tools=tools,
    prompt=react_prompt
)

executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    handle_parsing_errors="Check output and use exact tool names: AnalyzeObservixResults, GenerateInsights.",
    max_iterations=10
)

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

def generate_dashboard_data(time_range: str = "last 24 hours") -> dict:
    """Generate dashboard data from Observix results."""
    try:
        observix_input = {
            "analysis_type": "observix_analysis",
            "time_range": time_range,
            "components": stored_data_info["components"]
        }
        observix_data = json.loads(analyze_observix_results(observix_input))

        performance_input = {
            "analysis_type": "performance_analysis",
            "time_range": time_range,
            "components": stored_data_info["components"]
        }
        performance_data = json.loads(analyze_observix_results(performance_input))

        return {
            "observix_results": observix_data.get("data", {}),
            "performance_trends": performance_data.get("data", {}).get("performance_trends", []),
            "stored_data_info": stored_data_info,
            "timestamp": datetime.now(UTC).isoformat()
        }
    except Exception as e:
        logger.error(f"Error generating dashboard data: {str(e)}")
        return {
            "error": f"Failed to generate dashboard data: {str(e)}",
            "recommendation": "Verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Observix results are ingested."
        }

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")

        if request.query.lower() == "dashboard":
            dashboard_data = generate_dashboard_data()
            return {"status": "success", "response": json.dumps(dashboard_data, indent=2)}
        elif request.query.lower() == "help":
            return {
                "status": "success",
                "response": f"""
📊 ObservabilityAnalyst Commands:

PREDEFINED QUERIES:
- health: System health overview
- trends: Error patterns and trends
- high_severity: High-severity issues
- performance: Performance trends

CUSTOM QUERIES:
- "Show health for FISCD last 3 days"
- "List high-severity issues in VGM last 2 hours"
- "Analyze performance for Gateway last 1 week"
- "Show trends for category DATABASE last 30 days"

STORED DATA:
- Components: {', '.join(stored_data_info['components'])}
- Severities: {', '.join(stored_data_info['severities'])}
- Categories: {', '.join(stored_data_info['categories'])}
- Available Time Range: {stored_data_info['time_range']['earliest']} to {stored_data_info['time_range']['latest']}

TIME RANGES: Any duration (e.g., 'last 2 hours', 'last 3 days', 'last 1 week')
                """
            }

        # Map predefined prompts
        query_lower = request.query.lower()
        predefined = next((p for k, p in PREDEFINED_PROMPTS.items() if k in query_lower), None)
        if predefined:
            input_query = json.dumps({
                "analysis_type": predefined["analysis_type"],
                "params": predefined["params"]
            })
        else:
            # Parse custom query for time range, components, severity, or category
            time_range = "last 24 hours"
            components = stored_data_info["components"]
            severity = None
            category = None
            analysis_type = "observix_analysis"

            if "performance" in query_lower:
                analysis_type = "performance_analysis"
            if "high-severity" in query_lower or "high severity" in query_lower:
                severity = "HIGH"
            for comp in stored_data_info["components"]:
                if comp.lower() in query_lower:
                    components = [comp]
                    break
            for cat in stored_data_info["categories"]:
                if cat.lower() in query_lower:
                    category = cat
                    break
            time_match = re.search(r'last\s+\d+\s+(hour|day|week|month|year)s?', query_lower)
            if time_match:
                time_range = time_match.group(0)

            input_query = json.dumps({
                "analysis_type": analysis_type,
                "params": {
                    "time_range": time_range,
                    "components": components,
                    "severity": severity,
                    "category": category
                }
            })

        response_dict = await agent_with_history.ainvoke(
            {
                "input": input_query,
                "current_time": datetime.now(UTC).isoformat(),
                "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                "tool_names": ", ".join([t.name for t in tools]),
                "components": ", ".join(stored_data_info["components"]),
                "severities": ", ".join(stored_data_info["severities"]),
                "categories": ", ".join(stored_data_info["categories"]),
                "earliest": stored_data_info["time_range"]["earliest"],
                "latest": stored_data_info["time_range"]["latest"],
                "stored_data_info": json.dumps(stored_data_info)
            },
            config={"configurable": {"session_id": request.session_id}}
        )

        return {"status": "success", "response": response_dict.get("output", "No analysis available.")}

    except Exception as e:
        logger.error(f"Error processing query: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}. Please verify Elasticsearch connectivity to http://10.254.117.52:9200 or try again.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200)