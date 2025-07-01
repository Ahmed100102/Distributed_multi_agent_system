import json
import logging
import re
import os
from datetime import datetime, UTC
import time
from collections import defaultdict, Counter
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
from langchain_community.chat_models import ChatLlamaCpp
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.exceptions import LangChainException
from pydantic import SecretStr

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
            return OllamaLLM(
                model=self.model,
                base_url=self.endpoint
            )
        elif self.provider == "openai":
            return ChatOpenAI(
                model=self.model,
                api_key=SecretStr(self.api_key) if self.api_key else None
            )
        elif self.provider == "gemini":
            if not self.api_key:
                raise ValueError("API key required for Gemini")
            return ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=self.api_key
            )
        elif self.provider == "groq":
            return ChatGroq(
                model=self.model,
                api_key=SecretStr(self.api_key) if self.api_key else None,
                temperature=0.7
            )
        elif self.provider == "llama_cpp":
            return ChatOpenAI(
                base_url=f"{self.endpoint}/v1",
                api_key="not-needed",
                model=self.model,
                temperature=0.7
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def call(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> str:
        try:
            if self.provider in ["ollama", "llama_cpp"]:
                prompt = f"{system_prompt}\n\n{user_prompt}"
            else:
                prompt = f"System: {system_prompt}\nUser: {user_prompt}"

            response = self.llm.invoke(prompt, timeout=timeout)

            if isinstance(response, str):
                result = response
            elif hasattr(response, 'content'):
                result = str(response.content)
            else:
                result = str(response)

            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            return result.strip()

        except Exception as e:
            raise LangChainException(f"LLM call failed: {str(e)}")

# Environment configuration for LLM
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["ollama", "openai", "gemini", "groq", "llama_cpp"]
if MODEL_RUNTIME not in VALID_RUNTIMES:
    raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")

if MODEL_RUNTIME == "gemini":
    LLM_PROVIDER = "gemini"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "gemini-2.0-flash")
    LLM_ENDPOINT = None
    LLM_API_KEY = "AIzaSyB0uvjN8c94PsROQHCwIDxAv3vjQwScTp4"  # Hardcoded as requested
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
    if not LLM_API_KEY:
        raise ValueError("OPENAI_API_KEY environment variable must be set for openai provider")
elif MODEL_RUNTIME == "groq":
    LLM_PROVIDER = "groq"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "mixtral-8x7b-32768")
    LLM_ENDPOINT = None
    LLM_API_KEY = os.getenv("GROQ_API_KEY")
    if not LLM_API_KEY:
        raise ValueError("GROQ_API_KEY environment variable must be set for groq provider")
elif MODEL_RUNTIME == "llama_cpp":
    LLM_PROVIDER = "llama_cpp"
    LLM_MODEL = os.getenv("LLM_MODEL_AI", "qwen3:1.7b")
    LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:18000")
    LLM_API_KEY = None

logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT or "default")

# Initialize LLM
llm = LLMInterface(
    provider=LLM_PROVIDER,
    model=LLM_MODEL,
    endpoint=LLM_ENDPOINT,
    api_key=LLM_API_KEY
)

# Elasticsearch client setup
es = Elasticsearch(
    "http://10.254.117.52:9200",  # Updated to provided IP
    verify_certs=False,
    ssl_show_warn=False,
    request_timeout=30,
    max_retries=3
)

query_cache = TTLCache(maxsize=200, ttl=300)

def parse_time_range(time_range: str, is_observix: bool = False) -> dict:
    TIME_RANGE_MAP = {
        "last hour": {"gte": "now-1h", "lt": "now"},
        "last 24 hours": {"gte": "now-24h", "lt": "now"},
        "last 7 days": {"gte": "now-7d", "lt": "now"},
        "last 30 days": {"gte": "now-30d", "lt": "now"},
        "last year": {"gte": "now-1y", "lt": "now"},
        "last 5 years": {"gte": "now-5y", "lt": "now"}
    }
    field = "remediation_metadata.remediation_timestamp" if is_observix else "@timestamp"
    return {"range": {field: TIME_RANGE_MAP[time_range]}} if time_range in TIME_RANGE_MAP else {}

def extract_error_patterns(log_messages: List[str]) -> Dict[str, int]:
    patterns = {
        'connection_errors': [r'connection.*(?:refused|timeout|failed)', r'unable to connect', r'connection lost'],
        'authentication_errors': [r'authentication.*failed', r'unauthorized', r'access denied', r'invalid.*credentials'],
        'database_errors': [r'database.*error', r'sql.*exception', r'connection.*pool', r'deadlock'],
        'network_errors': [r'network.*error', r'socket.*error', r'dns.*resolution', r'host.*unreachable'],
        'memory_errors': [r'out of memory', r'memory.*exceeded', r'heap.*space', r'oom'],
        'timeout_errors': [r'timeout', r'request.*timeout', r'operation.*timeout'],
        'validation_errors': [r'validation.*failed', r'invalid.*input', r'bad.*request', r'malformed'],
        'service_errors': [r'service.*unavailable', r'service.*down', r'endpoint.*not.*found'],
        'configuration_errors': [r'configuration.*error', r'missing.*property', r'invalid.*config'],
        'license_errors': [r'license.*not authorized', r'insufficient license', r'license.*failed']
    }
    
    pattern_counts = defaultdict(int)
    for message in log_messages:
        if not message:
            continue
        message_lower = message.lower()
        for category, pattern_list in patterns.items():
            for pattern in pattern_list:
                if re.search(pattern, message_lower):
                    pattern_counts[category] += 1
                    break
    
    return dict(pattern_counts)

def analyze_log_severity_trends(logs: List[Dict], is_observix: bool = False) -> Dict:
    severity_by_hour = defaultdict(lambda: defaultdict(int))
    
    for log in logs:
        if is_observix:
            timestamp = log.get("remediation_metadata", {}).get("remediation_timestamp", "")
            severity = log.get("rca_details", {}).get("severity", "UNKNOWN").upper()
        else:
            timestamp = log.get('@timestamp', '')
            severity = log.get('log_level', 'UNKNOWN').upper()
        
        if not timestamp:
            continue
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            hour_key = dt.strftime('%Y-%m-%d %H:00')
            severity_by_hour[hour_key][severity] += 1
        except:
            continue
    
    return dict(severity_by_hour)

def detect_anomalies(time_series_data: Dict) -> List[Dict]:
    anomalies = []
    
    if not time_series_data:
        return anomalies
    
    all_values = []
    for hour_data in time_series_data.values():
        total = sum(hour_data.values())
        all_values.append(total)
    
    if len(all_values) < 3:
        return anomalies
    
    mean_val = sum(all_values) / len(all_values)
    std_dev = (sum((x - mean_val) ** 2 for x in all_values) / len(all_values)) ** 0.5
    threshold = mean_val + (2 * std_dev)
    
    for hour, data in time_series_data.items():
        total = sum(data.values())
        if total > threshold:
            anomalies.append({
                'timestamp': hour,
                'value': total,
                'baseline': mean_val,
                'deviation': total - mean_val,
                'severity_breakdown': dict(data)
            })
    
    return sorted(anomalies, key=lambda x: x['deviation'], reverse=True)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def analyze_logs(input_str: str) -> str:
    try:
        # Validate input JSON
        if not input_str or input_str.strip() == "":
            logger.error("Empty input string provided to analyze_logs")
            return json.dumps({"status": "error", "message": "Empty input string"})
        
        try:
            input_dict = json.loads(input_str)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON input: {str(e)}")
            return json.dumps({"status": "error", "message": f"Invalid JSON input: {str(e)}"})
        
        analysis_type = input_dict.get("analysis_type")
        params = input_dict.get("params", {})
        
        if not analysis_type:
            logger.error("No analysis_type specified")
            return json.dumps({"status": "error", "message": "No analysis_type specified"})
        
        if analysis_type == "error_trend_analysis":
            index = params.get("index", "logstash-*")
            time_range = params.get("time_range", "last 24 hours")
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            # Check if index exists
            try:
                if not es.indices.exists(index=index):
                    logger.error(f"Index {index} does not exist")
                    return json.dumps({"status": "error", "message": f"Index {index} does not exist", "recommendation": "Verify Logstash is ingesting logs"})
            except Exception as e:
                logger.error(f"Index check failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Index check failed: {str(e)}"})
            
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"terms": {"log_level": ["ERROR", "FATAL", "CRITICAL"]}},
                            parse_time_range(time_range)
                        ]
                    }
                },
                "size": 1000,
                "sort": [{"@timestamp": {"order": "desc"}}],
                "aggs": {
                    "errors_over_time": {
                        "date_histogram": {
                            "field": "@timestamp",
                            "fixed_interval": "1h",
                            "min_doc_count": 0
                        },
                        "aggs": {
                            "by_component": {
                                "terms": {"field": "type.keyword", "size": 10, "missing": "UNKNOWN"}
                            }
                        }
                    }
                }
            }
            
            try:
                result = es.search(index=index, body=query)
                if result["hits"]["total"]["value"] == 0:
                    logger.info(f"No data found in {index} for time range {time_range}")
                    return json.dumps({
                        "status": "success",
                        "data": {
                            "total_errors": 0,
                            "error_patterns": {},
                            "trends": {},
                            "anomalies": [],
                            "time_distribution": [],
                            "warning": f"No data found in {index} for {time_range}. Verify Logstash ingestion."
                        }
                    })
                
                error_messages = [hit["_source"].get("message", hit["_source"].get("log_message", "")) for hit in result["hits"]["hits"]]
                error_patterns = extract_error_patterns(error_messages)
                
                trends = analyze_log_severity_trends([hit["_source"] for hit in result["hits"]["hits"]])
                anomalies = detect_anomalies(trends)
                
                analysis_result = {
                    "total_errors": result["hits"]["total"]["value"],
                    "error_patterns": error_patterns,
                    "trends": trends,
                    "anomalies": anomalies,
                    "time_distribution": result["aggregations"]["errors_over_time"]["buckets"]
                }
                
                response = json.dumps({"status": "success", "data": analysis_result})
            except Exception as e:
                logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch search failed: {str(e)}", "recommendation": "Check Elasticsearch connectivity and index mappings"})
            
        elif analysis_type == "component_health_analysis":
            components = params.get("components", ["FISCD", "VGM", "Netprobe", "Gateway"])
            time_range = params.get("time_range", "last 24 hours")
            
            component_health = {}
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            for component in components:
                index = f"logstash-{component.lower()}-*" if component != "Observix" else "observix-results-*"
                
                # Check if index exists
                try:
                    if not es.indices.exists(index=index):
                        logger.error(f"Index {index} does not exist")
                        component_health[component] = {
                            "error": f"Index {index} does not exist",
                            "log_volume": 0,
                            "error_count": 0,
                            "error_rate": 0.0,
                            "warning": "Verify Logstash is ingesting logs"
                        }
                        continue
                except Exception as e:
                    logger.error(f"Index check failed for {index}: {str(e)}")
                    component_health[component] = {
                        "error": f"Index check failed: {str(e)}",
                        "log_volume": 0,
                        "error_count": 0,
                        "error_rate": 0.0
                    }
                    continue
                
                volume_query = {
                    "query": {
                        "bool": {"must": [parse_time_range(time_range, is_observix=(component == "Observix"))]}
                    }
                }
                try:
                    volume_result = es.count(index=index, body=volume_query)
                    if volume_result["count"] == 0:
                        logger.info(f"No data found in {index} for time range {time_range}")
                        component_health[component] = {
                            "log_volume": 0,
                            "error_count": 0,
                            "error_rate": 0.0,
                            "error_patterns": {},
                            "avg_response_time": None,
                            "log_level_distribution": {},
                            "warning": f"No data found in {index} for {time_range}. Verify Logstash ingestion."
                        }
                        continue
                except Exception as e:
                    logger.error(f"Elasticsearch count failed for {component}: {str(e)}")
                    component_health[component] = {
                        "error": f"Count query failed: {str(e)}",
                        "log_volume": 0,
                        "error_count": 0,
                        "error_rate": 0.0
                    }
                    continue
                
                error_query = {
                    "query": {
                        "bool": {
                            "must": [
                                {"terms": {"log_level": ["ERROR", "FATAL", "CRITICAL"]}} if component != "Observix" else {"match": {"rca_details.severity": "HIGH"}},
                                parse_time_range(time_range, is_observix=(component == "Observix"))
                            ]
                        }
                    },
                    "size": 100
                }
                
                try:
                    error_result = es.search(index=index, body=error_query)
                    error_messages = [hit["_source"].get("message", hit["_source"].get("log_message", "")) for hit in error_result["hits"]["hits"]]
                    error_patterns = extract_error_patterns(error_messages)
                    
                    perf_query = {
                        "query": {
                            "bool": {"must": [parse_time_range(time_range, is_observix=(component == "Observix"))]}
                        },
                        "aggs": {
                            "avg_response_time": {
                                "avg": {"field": "response_time", "missing": 0}
                            },
                            "log_levels": {
                                "terms": {"field": "log_level.keyword", "size": 10, "missing": "UNKNOWN"}
                            }
                        }
                    }
                    try:
                        perf_result = es.search(index=index, body=perf_query)
                        component_health[component] = {
                            "log_volume": volume_result["count"],
                            "error_count": error_result["hits"]["total"]["value"],
                            "error_rate": (error_result["hits"]["total"]["value"] / max(volume_result["count"], 1)) * 100,
                            "error_patterns": error_patterns,
                            "avg_response_time": perf_result["aggregations"].get("avg_response_time", {}).get("value", None),
                            "log_level_distribution": {
                                bucket["key"]: bucket["doc_count"] 
                                for bucket in perf_result["aggregations"].get("log_levels", {}).get("buckets", [])
                            }
                        }
                    except Exception as e:
                        logger.error(f"Elasticsearch performance query failed for {component}: {str(e)}")
                        component_health[component] = {
                            "log_volume": volume_result["count"],
                            "error_count": error_result["hits"]["total"]["value"],
                            "error_rate": (error_result["hits"]["total"]["value"] / max(volume_result["count"], 1)) * 100,
                            "error_patterns": error_patterns,
                            "avg_response_time": None,
                            "log_level_distribution": {},
                            "error": f"Performance query failed: {str(e)}"
                        }
                except Exception as e:
                    logger.error(f"Elasticsearch error query failed for {component}: {str(e)}")
                    component_health[component] = {
                        "log_volume": volume_result.get("count", 0),
                        "error_count": 0,
                        "error_rate": 0.0,
                        "error_patterns": {},
                        "avg_response_time": None,
                        "log_level_distribution": {},
                        "error": f"Error query failed: {str(e)}"
                    }
            
            response = json.dumps({"status": "success", "data": component_health})
            
        elif analysis_type == "observix_analysis":
            index = "observix-results-*"
            time_range = params.get("time_range", "last 24 hours")
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            # Check if index exists
            try:
                if not es.indices.exists(index=index):
                    logger.error(f"Index {index} does not exist")
                    return json.dumps({"status": "error", "message": f"Index {index} does not exist", "recommendation": "Verify Logstash is ingesting logs"})
            except Exception as e:
                logger.error(f"Index check failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Index check failed: {str(e)}"})
            
            query = {
                "query": {
                    "bool": {"must": [parse_time_range(time_range, is_observix=True)]}
                },
                "size": 500,
                "sort": [{"remediation_metadata.remediation_timestamp": {"order": "desc"}}],
                "aggs": {
                    "by_severity": {
                        "terms": {"field": "rca_details.severity.keyword", "size": 10}
                    },
                    "by_component": {
                        "terms": {"field": "rca_details.component.keyword", "size": 10}
                    },
                    "by_error_pattern": {
                        "terms": {"field": "rca_details.system_state.error_patterns.keyword", "size": 10}
                    }
                }
            }
            
            try:
                result = es.search(index=index, body=query)
                if result["hits"]["total"]["value"] == 0:
                    logger.info(f"No data found in {index} for time range {time_range}")
                    return json.dumps({
                        "status": "success",
                        "data": {
                            "total_logs": 0,
                            "severity_distribution": {},
                            "component_distribution": {},
                            "error_pattern_distribution": {},
                            "high_severity_analysis": {"count": 0, "common_issues": []},
                            "warning": f"No data found in {index} for {time_range}. Verify Logstash ingestion."
                        }
                    })
                
                test_data = [hit["_source"] for hit in result["hits"]["hits"]]
                high_severity_issues = [test for test in test_data if test.get("rca_details", {}).get("severity") == "HIGH"]
                
                analysis_result = {
                    "total_logs": result["hits"]["total"]["value"],
                    "severity_distribution": {
                        bucket["key"]: bucket["doc_count"] 
                        for bucket in result["aggregations"]["by_severity"]["buckets"]
                    },
                    "component_distribution": {
                        bucket["key"]: bucket["doc_count"] 
                        for bucket in result["aggregations"]["by_component"]["buckets"]
                    },
                    "error_pattern_distribution": {
                        bucket["key"]: bucket["doc_count"] 
                        for bucket in result["aggregations"]["by_error_pattern"]["buckets"]
                    },
                    "high_severity_analysis": {
                        "count": len(high_severity_issues),
                        "common_issues": Counter([test.get("rca_details", {}).get("summary", "Unknown") for test in high_severity_issues]).most_common(5)
                    }
                }
                
                response = json.dumps({"status": "success", "data": analysis_result})
            except Exception as e:
                logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch search failed: {str(e)}", "recommendation": "Check Elasticsearch connectivity and index mappings"})
            
        elif analysis_type == "system_performance_trends":
            indices = params.get("indices", ["logstash-*"])
            time_range = params.get("time_range", "last 24 hours")
            
            performance_data = {}
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            for index in indices:
                # Check if index exists
                try:
                    if not es.indices.exists(index=index):
                        logger.error(f"Index {index} does not exist")
                        performance_data[index] = {
                            "error": f"Index {index} does not exist",
                            "warning": "Verify Logstash is ingesting logs"
                        }
                        continue
                except Exception as e:
                    logger.error(f"Index check failed for {index}: {str(e)}")
                    performance_data[index] = {"error": f"Index check failed: {str(e)}"}
                    continue
                
                query = {
                    "query": {
                        "bool": {"must": [parse_time_range(time_range)]}
                    },
                    "size": 0,
                    "aggs": {
                        "performance_over_time": {
                            "date_histogram": {
                                "field": "@timestamp",
                                "fixed_interval": "1h",
                                "min_doc_count": 0
                            },
                            "aggs": {
                                "avg_response_time": {"avg": {"field": "response_time", "missing": 0}},
                                "avg_cpu_usage": {"avg": {"field": "cpu_usage", "missing": 0}},
                                "avg_memory_usage": {"avg": {"field": "memory_usage", "missing": 0}},
                                "error_rate": {
                                    "filter": {"terms": {"log_level": ["ERROR", "FATAL"]}},
                                    "aggs": {
                                        "count": {"value_count": {"field": "@timestamp"}}
                                    }
                                }
                            }
                        }
                    }
                }
                
                try:
                    result = es.search(index=index, body=query)
                    if result["hits"]["total"]["value"] == 0:
                        logger.info(f"No data found in {index} for time range {time_range}")
                        performance_data[index] = {
                            "warning": f"No data found for {time_range}. Verify Logstash ingestion.",
                            "buckets": []
                        }
                    else:
                        performance_data[index] = result["aggregations"]["performance_over_time"]["buckets"]
                except Exception as e:
                    logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
                    performance_data[index] = {"error": f"Elasticsearch search failed: {str(e)}"}
            
            response = json.dumps({"status": "success", "data": performance_data})
            
        elif analysis_type == "security_analysis":
            index = params.get("index", "logstash-*")
            time_range = params.get("time_range", "last 24 hours")
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            # Check if index exists
            try:
                if not es.indices.exists(index=index):
                    logger.error(f"Index {index} does not exist")
                    return json.dumps({"status": "error", "message": f"Index {index} does not exist", "recommendation": "Verify Logstash is ingesting logs"})
            except Exception as e:
                logger.error(f"Index check failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Index check failed: {str(e)}"})
            
            security_query = {
                "query": {
                    "bool": {
                        "must": [parse_time_range(time_range)],
                        "should": [
                            {"match": {"message": "unauthorized"}},
                            {"match": {"message": "authentication failed"}},
                            {"match": {"message": "access denied"}},
                            {"match": {"message": "login failed"}},
                            {"match": {"message": "suspicious"}},
                            {"match": {"message": "security"}},
                            {"match": {"message": "intrusion"}},
                            {"range": {"status_code": {"gte": 400, "lt": 500}}}
                        ],
                        "minimum_should_match": 1
                    }
                },
                "size": 200,
                "aggs": {
                    "security_events_by_type": {
                        "terms": {"field": "type.keyword", "size": 10, "missing": "UNKNOWN"}
                    },
                    "source_ips": {
                        "terms": {"field": "source_ip.keyword", "size": 20}
                    },
                    "user_agents": {
                        "terms": {"field": "user_agent.keyword", "size": 10}
                    }
                }
            }
            
            try:
                result = es.search(index=index, body=security_query)
                if result["hits"]["total"]["value"] == 0:
                    logger.info(f"No security events found in {index} for time range {time_range}")
                    return json.dumps({
                        "status": "success",
                        "data": {
                            "total_security_events": 0,
                            "event_types": {},
                            "top_source_ips": [],
                            "suspicious_patterns": {},
                            "warning": f"No security events found in {index} for {time_range}. Verify Logstash ingestion."
                        }
                    })
                
                security_events = [hit["_source"] for hit in result["hits"]["hits"]]
                
                analysis_result = {
                    "total_security_events": result["hits"]["total"]["value"],
                    "event_types": {
                        bucket["key"]: bucket["doc_count"] 
                        for bucket in result["aggregations"]["security_events_by_type"]["buckets"]
                    },
                    "top_source_ips": [
                        {"ip": bucket["key"], "count": bucket["doc_count"]} 
                        for bucket in result["aggregations"]["source_ips"]["buckets"]
                    ],
                    "suspicious_patterns": extract_error_patterns([event.get("message", "") for event in security_events])
                }
                
                response = json.dumps({"status": "success", "data": analysis_result})
            except Exception as e:
                logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch search failed: {str(e)}", "recommendation": "Check Elasticsearch connectivity and index mappings"})
            
        elif analysis_type == "business_impact_analysis":
            components = params.get("components", ["FISCD", "VGM", "Gateway"])
            time_range = params.get("time_range", "last 24 hours")
            
            impact_analysis = {}
            
            # Check Elasticsearch connectivity
            try:
                es.ping()
            except Exception as e:
                logger.error(f"Elasticsearch connection failed: {str(e)}")
                return json.dumps({"status": "error", "message": f"Elasticsearch connection failed: {str(e)}"})
            
            for component in components:
                index = f"logstash-{component.lower()}-*"
                
                # Check if index exists
                try:
                    if not es.indices.exists(index=index):
                        logger.error(f"Index {index} does not exist")
                        impact_analysis[component] = {
                            "error": f"Index {index} does not exist",
                            "warning": "Verify Logstash is ingesting logs"
                        }
                        continue
                except Exception as e:
                    logger.error(f"Index check failed for {index}: {str(e)}")
                    impact_analysis[component] = {"error": f"Index check failed: {str(e)}"}
                    continue
                
                critical_query = {
                    "query": {
                        "bool": {
                            "must": [
                                {"terms": {"log_level": ["FATAL", "CRITICAL"]}},
                                parse_time_range(time_range)
                            ]
                        }
                    },
                    "size": 50
                }
                
                try:
                    critical_result = es.search(index=index, body=critical_query)
                    if critical_result["hits"]["total"]["value"] == 0:
                        logger.info(f"No critical events found in {index} for time range {time_range}")
                        impact_analysis[component] = {
                            "critical_errors": 0,
                            "transaction_failures": 0,
                            "service_downtime_events": 0,
                            "business_impact_score": 0,
                            "warning": f"No critical events found for {time_range}. Verify Logstash ingestion."
                        }
                        continue
                    
                    transaction_failures = 0
                    service_downtime = 0
                    
                    for hit in critical_result["hits"]["hits"]:
                        message = hit["_source"].get("message", hit["_source"].get("log_message", "")).lower()
                        if any(keyword in message for keyword in ["transaction", "payment", "transfer"]):
                            transaction_failures += 1
                        if any(keyword in message for keyword in ["service down", "unavailable", "timeout"]):
                            service_downtime += 1
                    
                    impact_analysis[component] = {
                        "critical_errors": critical_result["hits"]["total"]["value"],
                        "transaction_failures": transaction_failures,
                        "service_downtime_events": service_downtime,
                        "business_impact_score": (
                            (transaction_failures * 10) + 
                            (service_downtime * 5) + 
                            (critical_result["hits"]["total"]["value"] * 2)
                        )
                    }
                except Exception as e:
                    logger.error(f"Elasticsearch search failed for {index}: {str(e)}")
                    impact_analysis[component] = {
                        "error": f"Elasticsearch search failed: {str(e)}",
                        "warning": "Check Elasticsearch connectivity and index mappings"
                    }
            
            response = json.dumps({"status": "success", "data": impact_analysis})
            
        else:
            logger.error(f"Invalid analysis_type: {analysis_type}")
            return json.dumps({"status": "error", "message": f"Invalid analysis_type: {analysis_type}"})
        
        cache_key = hash(input_str)
        query_cache[cache_key] = response
        return response
        
    except Exception as e:
        logger.error("Error in log analysis: %s", str(e))
        return json.dumps({"status": "error", "message": str(e), "recommendation": "Check Elasticsearch connectivity and index mappings"})

def generate_insights(data: str) -> str:
    try:
        if not data or data.strip() == "":
            logger.error("Empty data provided to generate_insights")
            return "⚠️ No data provided for analysis. Please verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Logstash is ingesting logs. Run: `curl -X GET http://10.254.117.52:9200/_cat/indices/logstash-*?v` to check available indices."
        
        try:
            data_dict = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON data: {str(e)}")
            return f"⚠️ Invalid analysis data: {str(e)}. Please verify Elasticsearch response format. Run: `curl -X GET http://10.254.117.52:9200/_cat/indices/logstash-*?v` to check indices."
        
        insights = []
        
        # Handle error case
        if data_dict.get("status") == "error":
            insights.append(f"⚠️ Analysis Error: {data_dict.get('message', 'Unknown error')}")
            insights.append(f"Recommendation: {data_dict.get('recommendation', 'Verify Elasticsearch connectivity to http://10.254.117.52:9200 and index mappings for fields like @timestamp and log_level. Run: `curl -X GET http://10.254.117.52:9200/_cat/indices/logstash-*?v` to check available indices.')}")
            return "\n".join(insights)
        
        # Handle greeting case
        if data_dict.get("message") and "How can I help" in data_dict.get("message"):
            insights.append(data_dict["message"])
            insights.append("Please specify a query (e.g., 'health', 'trends', or 'Are there any anomalies?').")
            return "\n".join(insights)
        
        # Handle analysis results
        analysis_data = data_dict.get("data", data_dict.get("analysis_results", data_dict))
        
        # Observix analysis insights
        if "total_logs" in analysis_data:
            total_logs = analysis_data.get("total_logs", 0)
            if total_logs == 0:
                insights.append(f"⚠️ No Observix logs found in the specified timeframe: {analysis_data.get('warning', 'No data available')}")
                insights.append("Recommendation: Verify Logstash pipeline and index 'observix-results-*'. Ensure logs are being ingested.")
            else:
                insights.append(f"📊 Analyzed {total_logs} Observix logs.")
                high_severity_count = analysis_data.get("high_severity_analysis", {}).get("count", 0)
                if high_severity_count > 0:
                    insights.append(f"🚨 {high_severity_count} high-severity issues detected.")
                    common_issues = analysis_data.get("high_severity_analysis", {}).get("common_issues", [])
                    for issue, count in common_issues:
                        insights.append(f"  - {issue} ({count} occurrences)")
                error_patterns = analysis_data.get("error_pattern_distribution", {})
                if error_patterns:
                    top_pattern = max(error_patterns.items(), key=lambda x: x[1], default=("Unknown", 0))
                    insights.append(f"🔍 Dominant error pattern: {top_pattern[0]} ({top_pattern[1]} occurrences)")

        # Error trend insights
        if "error_patterns" in analysis_data and analysis_data["error_patterns"]:
            patterns = analysis_data["error_patterns"]
            top_pattern = max(patterns.items(), key=lambda x: x[1], default=("Unknown", 0))
            insights.append(f"🔍 Primary Issue: {top_pattern[0].replace('_', ' ').title()} ({top_pattern[1]} occurrences)")
        elif "total_errors" in analysis_data and analysis_data["total_errors"] == 0:
            insights.append(f"✅ No error events detected: {analysis_data.get('warning', 'No data available')}")
        
        # Anomaly insights
        if "anomalies" in analysis_data and analysis_data["anomalies"]:
            anomaly = analysis_data["anomalies"][0]
            insights.append(f"⚠️ Anomaly Detected: {anomaly['timestamp']} showed {anomaly['value']} events ({anomaly['deviation']:.1f} above baseline)")
        
        # Component health insights
        if isinstance(analysis_data, dict) and any(isinstance(v, dict) for v in analysis_data.values()):
            unhealthy_components = [(k, v["error_rate"]) for k, v in analysis_data.items() if isinstance(v, dict) and v.get("error_rate", 0) > 5]
            if unhealthy_components:
                worst_component = max(unhealthy_components, key=lambda x: x[1])
                insights.append(f"🚨 Component Alert: {worst_component[0]} has {worst_component[1]:.1f}% error rate")
            error_components = [(k, v["error"]) for k, v in analysis_data.items() if isinstance(v, dict) and v.get("error")]
            for component, error in error_components:
                insights.append(f"⚠️ Component {component} failed analysis: {error}")
                insights.append(f"Recommendation: Check Elasticsearch index logstash-{component.lower()}-* for data availability or correct field mappings (e.g., '@timestamp', 'log_level').")
            empty_components = [(k, v["warning"]) for k, v in analysis_data.items() if isinstance(v, dict) and v.get("warning")]
            for component, warning in empty_components:
                insights.append(f"⚠️ Component {component}: {warning}")
                insights.append(f"Recommendation: Ensure Logstash is ingesting logs for logstash-{component.lower()}-*. Try a different time range (e.g., 'last 7 days').")
            if not unhealthy_components and not error_components and not empty_components:
                insights.append("✅ All components have low error rates (<5%) or no data in the analyzed timeframe.")
        
        # Business impact insights
        if isinstance(analysis_data, dict) and any(isinstance(v, dict) and "business_impact_score" in v for v in analysis_data.values()):
            high_impact = [(k, v["business_impact_score"]) for k, v in analysis_data.items() if isinstance(v, dict) and v.get("business_impact_score", 0) > 20]
            if high_impact:
                critical_component = max(high_impact, key=lambda x: x[1])
                insights.append(f"💼 Business Impact: {critical_component[0]} has high impact score ({critical_component[1]})")
            empty_impact = [(k, v["warning"]) for k, v in analysis_data.items() if isinstance(v, dict) and v.get("warning")]
            for component, warning in empty_impact:
                insights.append(f"⚠️ Component {component}: {warning}")
                insights.append(f"Recommendation: Ensure Logstash is ingesting logs for logstash-{component.lower()}-*. Try a different time range.")
        
        # Add guidance if no critical issues or errors detected
        if not insights:
            insights.append(f"✅ No critical issues detected: {analysis_data.get('warning', 'No data available in the analyzed timeframe')}")
            insights.append("Please verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Logstash is ingesting logs. Run: `curl -X GET http://10.254.117.52:9200/_cat/indices/logstash-*?v` to check available indices. Try a different time range (e.g., 'last 7 days').")
        
        return "\n".join(insights)
        
    except Exception as e:
        logger.error(f"Error generating insights: {str(e)}")
        return f"⚠️ Error generating insights: {str(e)}\nPlease verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Logstash is ingesting logs. Run: `curl -X GET http://10.254.117.52:9200/_cat/indices/logstash-*?v` to check indices."

tools = [
    Tool(
        name="AnalyzeLogs",
        func=analyze_logs,
        description="""Perform advanced log analytics. Input must be JSON with 'analysis_type' and 'params'.
        Analysis types:
        - 'error_trend_analysis': Analyze error patterns and trends over time
        - 'component_health_analysis': Analyze health of system components
        - 'observix_analysis': Analyze observix test results
        - 'system_performance_trends': Analyze system performance metrics
        - 'security_analysis': Analyze security-related events
        - 'business_impact_analysis': Analyze business impact of system issues"""
    ),
    Tool(
        name="GenerateInsights",
        func=generate_insights,
        description="Generate actionable insights and recommendations from analysis data."
    )
]

history = ChatMessageHistory()

react_prompt = PromptTemplate.from_template("""You are ObservabilityAnalyst, an advanced AI assistant specialized in log analytics and system observability.

Your role is to analyze log data from various system components and provide actionable insights about:
- System health and performance trends
- Error patterns and root cause analysis  
- Security incidents and anomalies
- Business impact of system issues
- Predictive maintenance recommendations

Available tools: {tools}
Tool names: {tool_names}

System Components and Indices:
- FISCD (Financial Information System): logstash-fiscd-*
- VGM (VGM Management): logstash-vgm-*
- Netprobe (Network Monitoring): logstash-netprobe-*
- Gateway (API Gateway): logstash-gateway-*
- Observix Results: observix-results-*

Time Ranges: 'last hour', 'last 24 hours', 'last 7 days', 'last 30 days', 'last year'

ANALYSIS APPROACH:
1. Select the most relevant analysis type based on the user's question.
2. Use AnalyzeLogs with appropriate parameters, using component names ('FISCD', 'VGM', 'Netprobe', 'Gateway', 'Observix') without 'logstash-' prefix.
3. For 'health' or vague queries, perform 'component_health_analysis' for all components over 'last hour' by default, or the specified time range (e.g., 'last year' for 'health last year').
4. For anomaly detection, use 'error_trend_analysis' and 'security_analysis' with 'last 24 hours' by default.
5. If AnalyzeLogs returns an error (e.g., JSON parsing, empty data, or connection issues), use GenerateInsights to report the error and suggest diagnostics (e.g., check Elasticsearch connectivity to http://10.254.117.52:9200, index mappings, or Logstash ingestion).
6. Do not repeat AnalyzeLogs for the same analysis_type and parameters if it fails; try a different time range (e.g., 'last 7 days') or proceed to GenerateInsights.
7. Handle empty data scenarios (e.g., no recent logs ingested) by returning zeroed results and providing guidance in GenerateInsights about verifying Logstash ingestion.
8. Generate actionable insights, focusing on summarized data (e.g., error counts, patterns) rather than raw logs.
9. Optimize for large datasets by limiting query sizes and using aggregations.

RESPONSE FORMAT:
Thought: [analyze what type of analysis is needed]
Action: [tool name: AnalyzeLogs or GenerateInsights]
Action Input: [JSON for AnalyzeLogs or analysis results for GenerateInsights]
Observation: [tool output]
Thought: [interpret results or handle errors]
[Repeat Action/Observation/Thought as needed, but avoid redundant AnalyzeLogs calls]
Final Answer: [comprehensive analysis with recommendations, including error guidance if applicable]

Current time: {current_time}

Question: {input}
{agent_scratchpad}

**Important**: Strictly adhere to the RESPONSE FORMAT. Use components without 'logstash-' prefix (e.g., 'FISCD'). If AnalyzeLogs fails, use GenerateInsights to report issues and suggest checking Elasticsearch connectivity or Logstash ingestion. Do not repeat failed AnalyzeLogs actions more than once for the same parameters. Always use exact tool names: AnalyzeLogs, GenerateInsights.
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
    handle_parsing_errors="Check your output and make sure it conforms to the RESPONSE FORMAT. Use the exact tool names: AnalyzeLogs, GenerateInsights.",
    max_iterations=15,
    early_stopping_method="force"
)

agent_with_history = RunnableWithMessageHistory(
    runnable=executor,
    get_session_history=lambda session_id: history,
    input_messages_key="input",
    history_messages_key="chat_history"
)

def generate_dashboard_data(analysis_type: str, time_range: str = "last 24 hours") -> dict:
    try:
        if analysis_type == "system_overview":
            health_input = json.dumps({
                "analysis_type": "component_health_analysis",
                "params": {"components": ["FISCD", "VGM", "Netprobe", "Gateway", "Observix"], "time_range": time_range}
            })
            health_data = json.loads(analyze_logs(health_input))
            
            error_input = json.dumps({
                "analysis_type": "error_trend_analysis", 
                "params": {"time_range": time_range}
            })
            error_data = json.loads(analyze_logs(error_input))
            
            observix_input = json.dumps({
                "analysis_type": "observix_analysis",
                "params": {"time_range": time_range}
            })
            observix_data = json.loads(analyze_logs(observix_input))
            
            return {
                "component_health": health_data.get("data", {}),
                "error_trends": error_data.get("data", {}),
                "observix_results": observix_data.get("data", {}),
                "timestamp": datetime.now(UTC).isoformat()
            }
        
        elif analysis_type == "security_dashboard":
            security_input = json.dumps({
                "analysis_type": "security_analysis",
                "params": {"time_range": time_range}
            })
            return json.loads(analyze_logs(security_input))
        
        elif analysis_type == "business_impact":
            impact_input = json.dumps({
                "analysis_type": "business_impact_analysis",
                "params": {"time_range": time_range}
            })
            return json.loads(analyze_logs(impact_input))
        
        return {"error": "Unknown analysis type"}
    
    except Exception as e:
        logger.error(f"Error generating dashboard data: {str(e)}")
        return {
            "error": f"Failed to generate dashboard data: {str(e)}",
            "recommendation": "Verify Elasticsearch connectivity to http://10.254.117.52:9200 and ensure Logstash is ingesting logs."
        }

def main():
    print("🔍 Welcome to ObservabilityAnalyst - Advanced Log Analytics Assistant!")
    print("I analyze your system logs to provide insights about trends, issues, and recommendations.")
    print("\nAvailable commands:")
    print("- 'dashboard': Generate dashboard overview data")
    print("- 'health': Analyze component health")
    print("- 'trends': Analyze error trends and patterns") 
    print("- 'security': Security event analysis")
    print("- 'impact': Business impact analysis")
    print("- 'observix': Observix test results analysis")
    print("- 'help': Show detailed help")
    print("- 'exit': Quit")
    
    session_id = "observability_session"
    
    while True:
        try:
            user_input = input("\n🔍 Analysis Query: ").strip()
            
            if user_input.lower() == "exit":
                break
            elif user_input.lower() == "help":
                print("""
📊 ObservabilityAnalyst Commands:

QUICK ANALYSIS:
- health: Component health analysis
- trends: Error pattern and trend analysis
- security: Security event analysis
- impact: Business impact assessment
- observix: Test results analysis
- dashboard: Generate dashboard data

DETAILED QUERIES:
- "What are the main error patterns in FISCD last 24 hours?"
- "Show me performance trends for all components"
- "Analyze security events in the last week"
- "What's the business impact of recent errors?"
- "Are there any anomalies in the system?"

TIME RANGES: last hour, last 24 hours, last 7 days, last 30 days, last year
COMPONENTS: FISCD, VGM, Netprobe, Gateway, Observix
                """)
                continue
            elif user_input.lower() == "dashboard":
                print("\n📊 Generating dashboard data...")
                dashboard_data = generate_dashboard_data("system_overview")
                print(json.dumps(dashboard_data, indent=2))
                continue
            elif not user_input:
                continue

            response_dict = agent_with_history.invoke(
                {
                    "input": user_input,
                    "current_time": datetime.now(UTC).isoformat(),
                    "tools": "\n".join([f"{t.name}: {t.description}" for t in tools]),
                    "tool_names": ", ".join([t.name for t in tools])
                },
                config={"configurable": {"session_id": session_id}}
            )
            
            output = response_dict.get("output", "No analysis available.")
            print(f"\n📈 {output}")

        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
            break
        except Exception as e:
            logger.error("Error: %s", str(e))
            print(f"❌ Error: {str(e)}. Please verify Elasticsearch connectivity to http://10.254.117.52:9200 or try again.")

    logger.info("Shutting down ObservabilityAnalyst...")
    query_cache.clear()
    print("👋 Goodbye!")

if __name__ == "__main__":
    main()