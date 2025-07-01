import os
import json
import re
import logging
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
from langchain.agents import AgentExecutor, create_react_agent
from langchain.prompts import ChatPromptTemplate
from langchain_core.exceptions import LangChainException
from langchain.tools import Tool 
from llm_interface import LLMInterface

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Choose model runtime (llama_cpp or gemini)
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["llama_cpp", "gemini"]
if MODEL_RUNTIME not in VALID_RUNTIMES:
    raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")

# Configure LLM based on runtime
if MODEL_RUNTIME == "llama_cpp":
    LLM_PROVIDER = "llama_cpp"
    LLM_MODEL = os.getenv("LLM_MODEL_RCA", "qwen3:1.7b")
    LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:18000")
    LLM_API_KEY = None
elif MODEL_RUNTIME == "gemini":
    LLM_PROVIDER = "gemini"
    LLM_MODEL = "gemini-2.0-flash"
    LLM_ENDPOINT = None
    LLM_API_KEY = "AIzaSyCjesXGbLVTL--_jJCQmaGxWf4N-eWUvAQ"
    if not LLM_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable must be set for gemini provider")

logger.info("Selected model runtime: %s (provider=%s, model=%s, endpoint=%s)",
            MODEL_RUNTIME, LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT or "default")

# Kafka setup with manual offset control
kafka_config = {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
logger.info("Kafka configuration: %s", kafka_config)

consumer = Consumer({
    **kafka_config,
    "group.id": "rca_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,  # Manual offset control
    "max.poll.interval.ms": "1800000",  # 30 minutes for LLM processing
    "session.timeout.ms": "300000",     # 5 minutes
    "heartbeat.interval.ms": "10000",   # 10 seconds
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
})
consumer.subscribe(["logs.anomalies"])
logger.info("Kafka consumer subscribed to: logs.anomalies with manual offset control")

producer = Producer({
    **kafka_config,
    "acks": "all",  # Wait for all replicas to acknowledge
    "retries": 3,
    "delivery.timeout.ms": 30000
})
logger.info("Kafka producer initialized with reliable delivery settings")

# LLM setup
llm_interface = LLMInterface(
    provider=LLM_PROVIDER,
    model=LLM_MODEL,
    endpoint=LLM_ENDPOINT,
    api_key=LLM_API_KEY
)
logger.info("LLM initialized: provider=%s, model=%s, endpoint=%s",
            LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT or "default")

llm = llm_interface.llm
logger.info("LLM object created for provider: %s", LLM_PROVIDER)

# Enhanced tools with better error handling
def publish_to_kafka_rca(data: str) -> str:
    """Publish RCA results to Kafka with delivery confirmation"""
    if not data or not data.strip():
        logger.error("No RCA data provided for publishing.")
        return "ERROR: No RCA data provided"
    
    logger.debug("Publishing RCA to logs.rca.output: %s", data[:200] + "..." if len(data) > 200 else data)
    
    try:
        # Validate JSON and required fields
        parsed = json.loads(data)
        required_fields = ["log_id", "rca", "recommended_actions", "severity"]
        missing_fields = [field for field in required_fields if field not in parsed]
        if missing_fields:
            logger.error("Missing required fields in RCA JSON: %s", missing_fields)
            return f"ERROR: Missing required fields - {', '.join(missing_fields)}"
        
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
        producer.flush(timeout=10.0)  # Wait up to 10 seconds for delivery
        logger.info("Successfully published RCA to logs.rca.output")
        return "SUCCESS: Published RCA to Kafka"
        
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in RCA data: %s", str(e))
        return f"ERROR: Invalid JSON format - {str(e)}"
    except KafkaError as e:
        logger.error("Kafka error publishing RCA: %s", str(e))
        return f"ERROR: Kafka publishing failed - {str(e)}"
    except Exception as e:
        logger.error("Unexpected error publishing RCA: %s", str(e))
        return f"ERROR: Unexpected error - {str(e)}"

tools = [
    Tool(
        name="PublishRCAResults",
        func=publish_to_kafka_rca,
        description="Publish completed RCA analysis as JSON to the logs.rca.output Kafka topic. Input must be valid JSON containing log_id, rca, recommended_actions, and severity fields."
    )
]
logger.info("Enhanced tools initialized: %s", [tool.name for tool in tools])

# Simplified prompt template to avoid variable issues
system_prompt = """You are an RCA Publishing Agent. Your only job is to publish RCA analysis results to Kafka using the provided tool.

INSTRUCTIONS:
- Use ONLY the PublishRCAResults tool to publish the RCA JSON.
- Validate that the input is valid JSON with required fields (log_id, rca, recommended_actions, severity).
- If publishing fails, report the error clearly and do NOT retry.
- If publishing succeeds, confirm success clearly.

Available Tool:
- PublishRCAResults: Publishes RCA JSON to logs.rca.output topic.

RESPONSE FORMAT:
Thought: [Your reasoning about the RCA data and publishing plan]
Action: PublishRCAResults
Action Input: {input}
Observation: [Tool response]
Thought: [Interpret the tool response]
Final Answer: SUCCESS: Published RCA to Kafka

EXAMPLE:
Thought: Ready to publish RCA JSON.
Action: PublishRCAResults
Action Input: {{"log_id": "123", "rca": {{"summary": "Example"}}, "recommended_actions": ["Action"], "severity": "LOW"}}
Observation: SUCCESS: Published RCA to Kafka
Thought: Publishing succeeded.
Final Answer: SUCCESS: Published RCA to Kafka

TOOLS: {tools}
TOOL_NAMES: {tool_names}
"""

human_prompt = """Publish this RCA analysis to Kafka:

{input}

Ensure the data is published successfully to the logs.rca.output topic.
{agent_scratchpad}"""

prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    ("human", human_prompt)
])
logger.info("Simplified prompt template initialized")

# Create LangChain agent
agent = create_react_agent(llm, tools, prompt)
executor = AgentExecutor(
    agent=agent, 
    tools=tools, 
    verbose=True, 
    handle_parsing_errors=True,
    max_iterations=1,  # Avoid retrying invalid JSON
    early_stopping_method="generate"
)
logger.info("LangChain agent and executor initialized with enhanced error handling")

def perform_rca(log_data: dict) -> dict:
    """Perform comprehensive RCA using direct LLM call with enhanced prompting"""
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

    # Check if this log_id was already processed
    if log_id in perform_rca.processed_ids:
        logger.info(f"Log_id {log_id} already processed, skipping analysis.")
        return None
    perform_rca.processed_ids.add(log_id)
    
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
    
    try:
        logger.info("Starting RCA analysis for log_id: %s (type: %s)", log_id, log_type)
        analysis = llm_interface.call(system_prompt, user_prompt, timeout=30)
        
        # Clean up and parse response
        analysis = clean_llm_response(analysis)
        parsed = json.loads(analysis)
        
        # Ensure RCA is a proper JSON object, not a string
        if isinstance(parsed.get("rca"), str):
            try:
                parsed["rca"] = json.loads(parsed["rca"].replace("'", '"'))
            except json.JSONDecodeError:
                logger.warning("Could not parse RCA string as JSON, leaving as is")
        
        # Add timestamp and log_type if missing
        if "analysis_timestamp" not in parsed.get("metadata", {}):
            parsed["metadata"] = parsed.get("metadata", {})
            parsed["metadata"]["analysis_timestamp"] = timestamp
        if "log_type" not in parsed.get("metadata", {}):
            parsed["metadata"]["log_type"] = log_type
            
        # Ensure recommended_actions is present
        if "recommended_actions" not in parsed:
            logger.warning("LLM did not provide recommended_actions, adding default")
            parsed["recommended_actions"] = ["Review and update instrument database", "Validate client request parameters"]
            
        return parsed
        
    except LangChainException as e:
        logger.error("LLM call timed out or failed for log_id %s: %s", log_id, str(e))
        return {
            "log_id": str(log_id),
            "rca": {
                "summary": f"RCA analysis failed: {str(e)}",
                "detailed_analysis": "Analysis timed out or failed",
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
            "recommended_actions": ["Investigate LLM server timeout"],
            "severity": "LOW",
            "confidence": "LOW",
            "category": "OTHER",
            "metadata": {
                "analysis_timestamp": timestamp,
                "log_type": log_type,
                "log_level": level
            }
        }
    except Exception as e:
        logger.error("RCA analysis failed for log_id %s: %s", log_id, str(e))
        return {
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
            "recommended_actions": ["Review system logs for errors"],
            "severity": "LOW",
            "confidence": "LOW",
            "category": "OTHER",
            "metadata": {
                "analysis_timestamp": timestamp,
                "log_type": log_type,
                "log_level": level
            }
        }

# Add static set to track processed IDs
perform_rca.processed_ids = set()

def clean_llm_response(response: str) -> str:
    """Clean and standardize LLM response"""
    # Remove code block markers
    response = re.sub(r'```json\s*', '', response)
    response = re.sub(r'```\s*$', '', response)
    
    # Remove any HTML-like tags
    response = re.sub(r'<[^>]+>.*?</[^>]+>', '', response, flags=re.DOTALL)
    
    # Extract JSON object if embedded in text
    json_match = re.search(r'\{.*\}', response, re.DOTALL)
    if json_match:
        response = json_match.group(0)
    
    return response.strip()

def commit_message_offset(msg):
    """Commit offset for a specific message immediately"""
    try:
        # Commit the specific message offset
        partitions = [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)]
        consumer.commit(offsets=partitions, asynchronous=False)
        logger.info("Committed offset %d for partition %d of topic %s", 
                   msg.offset() + 1, msg.partition(), msg.topic())
        return True
    except Exception as e:
        logger.error("Failed to commit offset for message: %s", str(e))
        return False

# Enhanced main loop with reliable message processing
def main():
    """Main loop for RCA Agent: consumes logs, performs RCA, and publishes results reliably."""
    logger.info("Starting enhanced RCA Agent with reliable message processing")
    processing_msg = None
    last_processed_offset = {}
    processed_log_ids = set()  # Track processed log IDs across the main loop

    while True:
        try:
            # Only poll for new messages if not currently processing one
            if processing_msg is None:
                msg = consumer.poll(timeout=5.0)

                if msg is None:
                    logger.debug("No new messages in logs.anomalies (topic: %s, partitions: %s)",
                                 "logs.anomalies", consumer.assignment())
                    continue

                if msg.error():
                    logger.error("Kafka consumer error: %s", msg.error())
                    continue

                # Deduplication: skip if this offset was already processed
                topic = msg.topic()
                partition = msg.partition()
                offset = msg.offset()
                if last_processed_offset.get((topic, partition)) == offset:
                    logger.debug("Skipping duplicate message at offset %d partition %d", offset, partition)
                    continue

                # Parse input log to check log_id before processing
                try:
                    log = msg.value().decode("utf-8")
                    log_data = json.loads(log)
                    log_id = log_data.get("_id", "unknown")
                    if log_id in processed_log_ids:
                        logger.info(f"Log_id {log_id} already processed in this session, committing offset and skipping.")
                        commit_message_offset(msg)
                        continue
                except Exception as e:
                    logger.error(f"Error parsing message for log_id deduplication: {e}")
                    commit_message_offset(msg)
                    continue

                # Start processing this message
                processing_msg = msg
                logger.info("Started processing message at offset %d from partition %d", offset, partition)

            # Pause consumer to prevent new message fetching during processing
            consumer.pause([TopicPartition(processing_msg.topic(), processing_msg.partition())])

            try:
                # Parse input log
                log = processing_msg.value().decode("utf-8")
                log_data = json.loads(log)
                logger.info("Processing log from logs.anomalies: %s",
                            log[:200] + "..." if len(log) > 200 else log)

                # Perform RCA analysis
                logger.info("Starting RCA analysis...")
                rca_result = perform_rca(log_data)
                if rca_result is None:
                    logger.info("Skipping already processed log_id, committing offset.")
                    commit_message_offset(processing_msg)
                    processing_msg = None
                    continue

                # Track processed log_id in the main loop as well
                processed_log_ids.add(log_data.get("_id", "unknown"))

                rca_json = json.dumps(rca_result, indent=2)
                logger.debug("RCA analysis completed: %s", rca_json[:300] + "..." if len(rca_json) > 300 else rca_json)

                # Use agent to publish results
                logger.info("Publishing RCA results via agent...")
                logger.debug("Agent input: %s", {"input": rca_json[:200] + "..." if len(rca_json) > 200 else rca_json})
                result = executor.invoke({"input": rca_json})

                # Check if publishing was successful
                agent_output = result.get("output", "")
                logger.info("Agent execution result: %s", agent_output)

                if "SUCCESS" in agent_output and "Published RCA to Kafka" in agent_output:
                    # Commit offset immediately after successful processing
                    if commit_message_offset(processing_msg):
                        logger.info("✅ Message processed successfully and offset committed")
                        # Mark this offset as processed
                        last_processed_offset[(processing_msg.topic(), processing_msg.partition())] = processing_msg.offset()
                        processing_msg = None  # Ready for next message
                    else:
                        logger.error("❌ Processing succeeded but offset commit failed - will retry")
                        # Keep processing_msg to retry commit
                else:
                    logger.error("❌ Publishing failed: %s", agent_output)
                    logger.info("Will retry processing this message...")
                    # Reset processing_msg to force retry
                    processing_msg = None

            except json.JSONDecodeError as e:
                logger.error("Invalid JSON in consumed message: %s", str(e))
                # Skip this message and commit offset to avoid infinite loop
                commit_message_offset(processing_msg)
                processing_msg = None

            except Exception as e:
                logger.error("Error processing message: %s", str(e))
                logger.info("Will retry processing this message...")
                processing_msg = None

            finally:
                # Always resume consumer
                try:
                    consumer.resume([TopicPartition(processing_msg.topic() if processing_msg else "logs.anomalies",
                                                   processing_msg.partition() if processing_msg else 0)])
                except Exception as e:
                    logger.error("Error resuming consumer: %s", str(e))

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            break

        except Exception as e:
            logger.error("Unexpected error in main loop: %s", str(e))
            # Reset processing state on unexpected errors
            processing_msg = None

    # Cleanup
    logger.info("Shutting down RCA Agent...")
    try:
        consumer.close()
        producer.flush()
    except Exception as e:
        logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()