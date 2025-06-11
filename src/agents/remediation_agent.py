import os
import json
import re
import logging
from confluent_kafka import Consumer, Producer, KafkaError, TopicPartition
from langchain.agents import AgentExecutor, create_react_agent
from langchain.prompts import ChatPromptTemplate
from langchain.tools import Tool
from llm_interface import LLMInterface

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Enhanced Kafka setup with manual offset control
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
# Comment the next line to skip provider choice
choice = input("\nChoose LLM provider (ollama/groq): ").lower().strip()

if choice == 'groq':
    llm_interface = LLMInterface(
        provider="groq",
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        api_key=os.getenv("GROQ_API_KEY")
    )
    logger.info("Using Groq LLM with model: meta-llama/llama-4-scout-17b-16e-instruct")
else:  # Default to Ollama
    llm_interface = LLMInterface(
        provider=os.getenv("LLM_PROVIDER", "ollama"),
        model=os.getenv("LLM_MODEL_REMEDIATION", "llama3.2:3b"),
        endpoint=os.getenv("LLM_ENDPOINT", "http://localhost:11434")
    )
    logger.info("Using default Ollama LLM with model: %s", os.getenv("LLM_MODEL_REMEDIATION", "llama3.2:3b"))

llm = llm_interface.llm

# Add static set to track processed IDs
def perform_remediation(rca_data: dict) -> dict:
    """Generate detailed remediation plan from RCA using direct LLM call"""
    log_id = rca_data.get("log_id", "unknown")
    # Eliminate duplicate processing by tracking processed log_ids
    if not hasattr(perform_remediation, "processed_ids"):
        perform_remediation.processed_ids = set()
    if log_id in perform_remediation.processed_ids:
        logger.info(f"Log_id {log_id} already processed, skipping remediation.")
        return None
    perform_remediation.processed_ids.add(log_id)
    
    system_prompt = f"""You are a remediation specialist focusing on system issues.

CONTEXT:
- Log ID: {log_id}
- RCA Summary: {rca_data.get('summary', '')}
- Detailed Analysis: {rca_data.get('detailed_analysis', '')}
- Root Causes: {json.dumps(rca_data.get('root_causes', []))}
- System State: {json.dumps(rca_data.get('system_state', {}))}

Generate a comprehensive remediation plan considering the RCA details.
Return ONLY a valid JSON object with exactly these fields:
{{
    "log_id": "{log_id}",
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

    user_prompt = "Generate a detailed remediation plan based on the RCA results and recommendations provided."
    
    try:
        logger.info("Starting remediation planning for log_id: %s", log_id)
        plan = llm_interface.call(system_prompt, user_prompt)
        
        # Clean and extract JSON
        plan = plan.strip()
        plan = re.sub(r'```json\s*', '', plan)
        plan = re.sub(r'```\s*$', '', plan)
        json_match = re.search(r'\{.*\}', plan, re.DOTALL)
        if json_match:
            plan = json_match.group(0)
            
        parsed = json.loads(plan)
        result = {
            "log_id": str(log_id),
            "remediation": parsed.get("remediation_plan", "No remediation generated"),
            "priority": str(parsed.get("priority", "MEDIUM")).upper(),
            "estimated_time": str(parsed.get("estimated_time", "Unknown")),
            "required_resources": list(parsed.get("required_resources", ["Operations Team"]))
        }
        
        if result["priority"] not in ["HIGH", "MEDIUM", "LOW"]:
            result["priority"] = "MEDIUM"
            
        logger.info("Remediation planning completed successfully for log_id: %s", log_id)
        return result
        
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response for log_id %s: %s", log_id, str(e))
        return {
            "log_id": str(log_id),
            "remediation": f"Failed to generate remediation plan: {str(e)}",
            "priority": "LOW",
            "estimated_time": "Unknown",
            "required_resources": ["Manual intervention required"]
        }
    except Exception as e:
        logger.error("Error generating remediation for log_id %s: %s", log_id, str(e))
        return {
            "log_id": str(log_id),
            "remediation": f"Remediation generation failed: {str(e)}",
            "priority": "LOW",
            "estimated_time": "Unknown",
            "required_resources": ["Error investigation required"]
        }

def publish_to_kafka_remediation(data: str) -> str:
    """Publish remediation results to Kafka with delivery confirmation"""
    if not data or not data.strip():
        logger.error("No remediation data provided for publishing.")
        return "ERROR: No remediation data provided"
    logger.debug("Publishing remediation to logs.remediation: %s", data[:200] + "..." if len(data) > 200 else data)
    
    try:
        # Validate JSON before publishing
        json.loads(data)
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
        description="Publish remediation plan as JSON to the logs.remediation Kafka topic. Input must be valid JSON containing log_id, remediation, priority, and other required fields."
    )
]
logger.info("Enhanced tools initialized: %s", [tool.name for tool in tools])

# Improved prompt with explicit format and example, and required variables for LangChain
system_prompt = """You are a Remediation Publishing Agent. Your only job is to publish remediation plans to Kafka using the provided tool.

INSTRUCTIONS:
- Use ONLY the PublishKafkaRemediation tool to publish the remediation JSON.
- Validate that the input is valid JSON before publishing.
- If publishing fails, report the error clearly and do NOT retry.
- If publishing succeeds, confirm success clearly.

Available Tool:
- PublishKafkaRemediation: Publishes remediation JSON to logs.remediation topic.

RESPONSE FORMAT:
Thought: [Your reasoning about the remediation data and publishing plan]
Action: PublishKafkaRemediation
Action Input: [The exact remediation JSON provided]
Observation: [Tool response]
Thought: [Interpret the tool response]
Final Answer: SUCCESS: Published remediation to Kafka

EXAMPLE:
Thought: Ready to publish remediation JSON.
Action: PublishKafkaRemediation
Action Input: {{"log_id": "123", "remediation": "Step 1: Restart service\nStep 2: Verify logs", "priority": "HIGH"}}
Observation: SUCCESS: Published remediation to Kafka
Thought: Publishing succeeded.
Final Answer: SUCCESS: Published remediation to Kafka

TOOLS: {tools}
TOOL_NAMES: {tool_names}
"""

human_prompt = """Publish this remediation plan to Kafka:

{input}

Ensure the data is published successfully to the logs.remediation topic.
"""

prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    ("human", human_prompt + "\n\n{agent_scratchpad}")
])
logger.info("Enhanced prompt template initialized")

agent = create_react_agent(llm, tools, prompt)
executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    handle_parsing_errors=True,
    max_iterations=3
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
    processed_log_ids = set()  # Track processed log IDs

    while True:
        try:
            # Only poll for new messages if not currently processing one
            if processing_msg is None:
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    logger.debug("No new messages in logs.rca.output")
                    continue
                if msg.error():
                    logger.error("Kafka consumer error: %s", msg.error())
                    continue

                # Parse message to check log_id before processing
                try:
                    rca_data = json.loads(msg.value().decode("utf-8"))
                    log_id = rca_data.get("log_id")
                    
                    # Skip if we've already processed this log_id
                    if log_id in processed_log_ids:
                        logger.info(f"Log_id {log_id} already processed in this session, committing offset and skipping.")
                        commit_message_offset(msg)
                        continue

                    # Check offset deduplication
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
                    logger.error(f"Error parsing message: {str(e)}")
                    commit_message_offset(msg)
                    continue

                processing_msg = msg
                logger.info("Started processing message at offset %d from partition %d", offset, partition)

            # Pause consumer while processing
            consumer.pause([TopicPartition(processing_msg.topic(), processing_msg.partition())])

            try:
                rca = processing_msg.value().decode("utf-8")
                rca_data = json.loads(rca)
                logger.info("Processing RCA from logs.rca.output: %s",
                           rca[:200] + "..." if len(rca) > 200 else rca)

                # Perform remediation
                remediation_result = perform_remediation(rca_data)
                if remediation_result is None:
                    logger.info("Skipping already processed log_id, committing offset.")
                    commit_message_offset(processing_msg)
                    processing_msg = None
                    continue

                # Track processed log_id in the main loop as well
                processed_log_ids.add(rca_data.get("log_id", "unknown"))

                remediation_json = json.dumps(remediation_result, indent=2)
                logger.debug("Remediation plan generated: %s", 
                           remediation_json[:300] + "..." if len(remediation_json) > 300 else remediation_json)

                # Publish remediation
                result = executor.invoke({
                    "input": remediation_json
                })
                agent_output = result.get("output", "")
                logger.info("Agent execution result: %s", agent_output)

                if "SUCCESS" in agent_output and "Published remediation to Kafka" in agent_output:
                    if commit_message_offset(processing_msg):
                        logger.info("✅ Message processed successfully and offset committed")
                        last_processed_offset[(processing_msg.topic(), processing_msg.partition())] = processing_msg.offset()
                        processing_msg = None
                    else:
                        logger.error("❌ Processing succeeded but offset commit failed - will retry")

            except json.JSONDecodeError as e:
                logger.error("Invalid JSON in consumed message: %s", str(e))
                commit_message_offset(processing_msg)
                processing_msg = None

            except Exception as e:
                logger.error("Error processing message: %s", str(e))
                logger.info("Will retry processing this message...")

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

    # Cleanup
    logger.info("Shutting down Remediation Agent...")
    try:
        consumer.close()
        producer.flush()
    except Exception as e:
        logger.error("Error during shutdown: %s", str(e))

if __name__ == "__main__":
    main()