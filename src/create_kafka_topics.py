from confluent_kafka.admin import AdminClient, NewTopic, ConfigResource
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify_topic_config(admin_client, topic_name):
    """Verify topic exists and return its configuration"""
    resource = ConfigResource('topic', topic_name)
    try:
        result = admin_client.describe_configs([resource])
        configs = result[resource].result()
        logger.info(f"✓ Topic '{topic_name}' exists with config:")
        for config in configs.values():
            logger.info(f"  {config.name}: {config.value}")
        return True
    except Exception as e:
        logger.error(f"Failed to verify topic '{topic_name}': {e}")
        return False

def create_kafka_topics():
    admin_client = AdminClient({"bootstrap.servers": "localhost:9092"})
    
    # Define topics with configurations
    topics = [
        NewTopic(
            "logs.anomalies",
            num_partitions=1,
            replication_factor=1,
            config={
                "cleanup.policy": "delete",
                "retention.ms": "604800000",  # 7 days
                "min.insync.replicas": "1"
            }
        ),
        NewTopic(
            "logs.rca.output",
            num_partitions=1,
            replication_factor=1,
            config={
                "cleanup.policy": "delete",
                "retention.ms": "604800000",
                "min.insync.replicas": "1"
            }
        ),
        NewTopic(
            "logs.remediation",
            num_partitions=1,
            replication_factor=1,
            config={
                "cleanup.policy": "delete",
                "retention.ms": "604800000",
                "min.insync.replicas": "1"
            }
        ),
    ]

    # Try to create topics
    fs = admin_client.create_topics(topics)
    for topic, f in fs.items():
        try:
            f.result()
            logger.info(f"✓ Topic '{topic}' created successfully")
        except Exception as e:
            if "TOPIC_ALREADY_EXISTS" in str(e):
                logger.info(f"Topic '{topic}' already exists, verifying configuration...")
                verify_topic_config(admin_client, str(topic))
            else:
                logger.error(f"Failed to create topic '{topic}': {e}")

    # List all topics for verification
    try:
        cluster_metadata = admin_client.list_topics(timeout=10)
        logger.info("\nVerified Kafka Topics:")
        for topic in ["logs.anomalies", "logs.rca.output", "logs.remediation"]:
            if topic in cluster_metadata.topics:
                logger.info(f"✓ {topic}")
            else:
                logger.error(f"✗ {topic} not found!")
    except Exception as e:
        logger.error(f"Failed to list topics: {e}")

if __name__ == "__main__":
    create_kafka_topics()
