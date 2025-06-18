import os
import json
import re
import logging
from datetime import datetime
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
from langchain.agents import AgentExecutor, create_react_agent
from langchain.prompts import ChatPromptTemplate
from langchain.tools import Tool
from llm_interface import LLMInterface

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Choose model runtime (llama_cpp or gemini)
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["llama_cpp", "gemini","llama_cpp","groq"]
if MODEL_RUNTIME not in VALID_RUNTIMES:
    raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")
# Configure LLM based on runtime
if MODEL_RUNTIME == "llama_cpp":
    LLM_PROVIDER = "llama_cpp"
    LLM_MODEL = os.getenv("LLM_MODEL_REMEDIATION", "qwen3:1.7b")
    LLM_ENDPOINT = os.getenv("LLM_MODEL_REMEDIATION", "http://localhost:18000")
    LLM_API_KEY = None
elif MODEL_RUNTIME == "gemini":
    LLM_PROVIDER = "gemini"
    LLM_MODEL = "gemini-2.0-flash"
    LLM_ENDPOINT = None
    LLM_API_KEY = "AIzaSyBS7ljmFDPyP5EaP1iuAW2-eW7hmWCqZp8"
    if not LLM_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable must be set for gemini provider")
# Configure LLM based on runtime
if MODEL_RUNTIME == "groq":
    LLM_PROVIDER = "groq"
    LLM_MODEL = os.getenv("LLM_MODEL_REMEDIATION", "meta-llama/llama-4-scout-17b-16e-instruct")
    LLM_ENDPOINT = None
    LLM_API_KEY = os.getenv("GROQ_API_KEY")
    if not LLM_API_KEY:
        raise ValueError("GROQ_API_KEY environment variable must be set for groq provider")
elif MODEL_RUNTIME == "ollama":
    LLM_PROVIDER = "ollama"
    LLM_MODEL = os.getenv("LLM_MODEL_REMEDIATION", "llama3.2:3b")
    LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:11434")
    LLM_API_KEY = None

logger.info("Selected model runtime: %s (provider=%s, model=%s, endpoint=%s)",
            MODEL_RUNTIME, LLM_PROVIDER, LLM_MODEL, LLM_ENDPOINT or "default")

# Kafka setup with manual offset control
kafka_config = {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
logger.info("Kafka configuration: %s", kafka_config)

consumer = Consumer({
    **kafka_config,
    "group.id": "remediation_group",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,  # Manual offset control
    "max.poll.interval.ms": "1800000",  # 30 minutes for LLM processing
    "session.timeout.ms": "300000",     # 5 minutes
    "heartbeat.interval.ms": "10000",   # 10 seconds
    "fetch.min.bytes": 1,
    "fetch.wait.max.ms": 500
})
consumer.subscribe(["logs.rca.output"])
logger.info("Kafka consumer subscribed to: logs.rca.output with manual offset control")

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

def perform_remediation(rca_data: dict) -> dict:
    """Generate detailed remediation plan from RCA using direct LLM call"""
    log_id = rca_data.get("log_id", "unknown")
    rca = rca_data.get("rca", {})
    metadata = rca_data.get("metadata", {})
    recommended_actions = rca_data.get("recommended_actions", [])
    severity = rca_data.get("severity", "MEDIUM")
    confidence = rca_data.get("confidence", "MEDIUM")
    category = rca_data.get("category", "OTHER")

    # Deduplication
    if not hasattr(perform_remediation, "processed_ids"):
        perform_remediation.processed_ids = set()
    if log_id in perform_remediation.processed_ids:
        logger.info(f"Log_id {log_id} already processed, skipping remediation.")
        return None
    perform_remediation.processed_ids.add(log_id)

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

    try:
        logger.info("Starting remediation planning for log_id: %s", log_id)
        plan = llm_interface.call(system_prompt, user_prompt, timeout=30)

        # Clean and extract JSON
        plan = clean_llm_response(plan)
        parsed = json.loads(plan)

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
                "remediation_timestamp": datetime.utcnow().isoformat() + "Z"
            }
        }

        # Validate priority
        if result["priority"] not in ["HIGH", "MEDIUM", "LOW"]:
            result["priority"] = "MEDIUM"

        logger.info("Remediation planning completed successfully for log_id: %s", log_id)
        return result

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response for log_id %s: %s", log_id, str(e))
        return create_error_result(log_id, rca, metadata, recommended_actions, severity, confidence, category, str(e))
    except Exception as e:
        logger.error("Error generating remediation for log_id %s: %s", log_id, str(e))
        return create_error_result(log_id, rca, metadata, recommended_actions, severity, confidence, category, str(e))

def create_error_result(log_id: str, rca: dict, metadata: dict, recommended_actions: list, severity: str, confidence: str, category: str, error: str) -> dict:
    """Create error remediation result with RCA and error details"""
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
    return {
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
            "remediation_timestamp": datetime.utcnow().isoformat() + "Z",
            "llm_provider": LLM_PROVIDER,
            "llm_model": LLM_MODEL
        }
    }

def clean_llm_response(response: str) -> str:
    """Clean and standardize LLM response"""
    response = re.sub(r'```json\s*', '', response)
    response = re.sub(r'```\s*$', '', response)
    response = re.sub(r'<[^>]+>.*?</[^>]+>', '', response, flags=re.DOTALL)
    json_match = re.search(r'\{.*\}', response, re.DOTALL)
    if json_match:
        response = json_match.group(0)
    return response.strip()

def publish_to_kafka_remediation(data: str) -> str:
    """Publish remediation results to Kafka with delivery confirmation"""
    if not data or not data.strip():
        logger.error("No remediation data provided for publishing.")
        return "ERROR: No remediation data provided"
    logger.debug("Publishing remediation to logs.remediation: %s", data[:200] + "..." if len(data) > 200 else data)

    try:
        # Validate JSON and required fields
        parsed = json.loads(data)
        required_fields = ["log_id", "error_details", "rca_details", "remediation_plan", "priority"]
        missing_fields = [field for field in required_fields if field not in parsed]
        if missing_fields:
            logger.error("Missing required fields in remediation JSON: %s", missing_fields)
            return f"ERROR: Missing required fields - {', '.join(missing_fields)}"

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
        logger.info("Successfully published remediation to logs.remediation")
        return "SUCCESS: Published remediation to Kafka"
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in remediation data: %s", str(e))
        return f"ERROR: Invalid JSON format - {str(e)}"
    except KafkaError as e:
        logger.error("Kafka error publishing remediation: %s", str(e))
        return f"ERROR: Kafka publishing failed - {str(e)}"
    except Exception as e:
        logger.error("Unexpected error publishing remediation: %s", str(e))
        return f"ERROR: Unexpected error - {str(e)}"

tools = [
    Tool(
        name="PublishKafkaRemediation",
        func=publish_to_kafka_remediation,
        description="Publish remediation plan as JSON to the logs.remediation Kafka topic. Input must be valid JSON containing log_id, error_details, rca_details, remediation_plan, and priority."
    )
]
logger.info("Enhanced tools initialized: %s", [tool.name for tool in tools])

# Corrected prompt template with agent_scratchpad
system_prompt = """You are a Remediation Publishing Agent. Your only job is to publish remediation plans to Kafka using the provided tool.

INSTRUCTIONS:
- Use ONLY the PublishKafkaRemediation tool to publish the remediation JSON.
- Validate that the input is valid JSON with required fields (log_id, error_details, rca_details, remediation_plan, priority).
- If publishing fails, report the error clearly and do NOT retry.
- If publishing succeeds, confirm success clearly.

Available Tool:
- PublishKafkaRemediation: Publishes remediation JSON to logs.remediation topic.

RESPONSE FORMAT:
Thought: [Your reasoning about the remediation data and publishing plan]
Action: PublishKafkaRemediation
Action Input: {input}
Observation: [Tool response]
Thought: [Interpret the tool response]
Final Answer: SUCCESS: Published remediation to Kafka

EXAMPLE:
Thought: Ready to publish remediation JSON.
Action: PublishKafkaRemediation
Action Input: {{"log_id": "123", "error_details": {{"log_message": "Error"}}, "rca_details": {{"summary": "RCA"}}, "remediation_plan": {{"summary": "Plan"}}, "priority": "HIGH"}}
Observation: SUCCESS: Published remediation to Kafka
Thought: Publishing succeeded.
Final Answer: SUCCESS: Published remediation to Kafka

TOOLS: {tools}
TOOL_NAMES: {tool_names}
"""

human_prompt = """Publish this remediation plan to Kafka:

{input}

Ensure the data is published successfully to the logs.remediation topic.

{agent_scratchpad}"""

prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    ("human", human_prompt)
])
logger.info("Corrected prompt template initialized with agent_scratchpad")

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

def commit_message_offset(msg):
    """Commit offset for a specific message immediately"""
    try:
        partitions = [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)]
        consumer.commit(offsets=partitions, asynchronous=False)
        logger.info("Committed offset %d for partition %d of topic %s",
                    msg.offset() + 1, msg.partition(), msg.topic())
        return True
    except Exception as e:
        logger.error("Failed to commit offset for message: %s", str(e))
        return False

def main():
    """Main loop for Remediation Agent: consumes RCA, performs remediation, and publishes results reliably."""
    logger.info("Starting Remediation Agent main loop with reliable message processing")
    processing_msg = None
    last_processed_offset = {}
    processed_log_ids = set()

    while True:
        try:
            if processing_msg is None:
                msg = consumer.poll(timeout=5.0)
                if msg is None:
                    logger.debug("No new messages in logs.rca.output (topic: %s, partitions: %s)",
                                 "logs.rca.output", consumer.assignment())
                    continue
                if msg.error():
                    logger.error("Kafka consumer error: %s", msg.error())
                    continue

                try:
                    rca_data = json.loads(msg.value().decode("utf-8"))
                    log_id = rca_data.get("log_id", "unknown")
                    if log_id in processed_log_ids:
                        logger.info(f"Log_id {log_id} already processed in this session, committing offset and skipping.")
                        commit_message_offset(msg)
                        continue

                    topic = msg.topic()
                    partition = msg.partition()
                    offset = msg.offset()
                    if last_processed_offset.get((topic, partition)) == offset:
                        logger.debug("Skipping duplicate message at offset %d partition %d", offset, partition)
                        continue

                except json.JSONDecodeError:
                    logger.error("Invalid JSON in message, skipping")
                    commit_message_offset(msg)
                    continue
                except Exception as e:
                    logger.error(f"Error parsing message: {e}")
                    commit_message_offset(msg)
                    continue

                processing_msg = msg
                logger.info("Started processing message at offset %d from partition %d", offset, partition)

            consumer.pause([TopicPartition(processing_msg.topic(), processing_msg.partition())])

            try:
                rca = processing_msg.value().decode("utf-8")
                rca_data = json.loads(rca)
                logger.info("Processing RCA from logs.rca.output: %s",
                            rca[:200] + "..." if len(rca) > 200 else rca)

                remediation_result = perform_remediation(rca_data)
                if remediation_result is None:
                    logger.info("Skipping already processed log_id, committing offset.")
                    commit_message_offset(processing_msg)
                    processing_msg = None
                    continue

                processed_log_ids.add(rca_data.get("log_id", "unknown"))

                remediation_json = json.dumps(remediation_result, indent=2)
                logger.debug("Remediation plan generated: %s",
                             remediation_json[:300] + "..." if len(remediation_json) > 300 else remediation_json)

                logger.info("Publishing remediation results via agent...")
                logger.debug("Agent input: %s", {"input": remediation_json[:200] + "..." if len(remediation_json) > 200 else remediation_json})
                result = executor.invoke({"input": remediation_json})
                agent_output = result.get("output", "")
                logger.info("Agent execution result: %s", agent_output)

                if "SUCCESS" in agent_output and "Published remediation to Kafka" in agent_output:
                    if commit_message_offset(processing_msg):
                        logger.info("✅ Message processed successfully and offset committed")
                        last_processed_offset[(processing_msg.topic(), processing_msg.partition())] = processing_msg.offset()
                        processing_msg = None
                    else:
                        logger.error("❌ Processing succeeded but offset commit failed - will retry")
                else:
                    logger.error("❌ Publishing failed: %s", agent_output)
                    logger.info("Will retry processing this message...")
                    processing_msg = None

            except json.JSONDecodeError as e:
                logger.error("Invalid JSON in consumed message: %s", str(e))
                commit_message_offset(processing_msg)
                processing_msg = None

            except Exception as e:
                logger.error("Error processing message: %s", str(e))
                logger.info("Will retry processing this message...")
                processing_msg = None

            finally:
                try:
                    consumer.resume([TopicPartition(processing_msg.topic() if processing_msg else "logs.rca.output",
                                                   processing_msg.partition() if processing_msg else 0)])
                except Exception as e:
                    logger.error("Error resuming consumer: %s", str(e))

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            break

        except Exception as e:
            logger.error("Unexpected error in main loop: %s", str(e))
            processing_msg = None

    logger.info("Shutting down Remediation Agent...")
    try:
        consumer.close()
        producer.flush()
    except Exception as e:
        logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()