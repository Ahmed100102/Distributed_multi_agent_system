from confluent_kafka.admin import AdminClient, NewTopic, ConfigResource
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KafkaRepair:
    def __init__(self, bootstrap_servers="localhost:9092"):
        self.admin_client = AdminClient({
            "bootstrap.servers": bootstrap_servers
        })

    def reset_consumer_group(self, group_id):
        """Reset a consumer group by deleting it"""
        try:
            result = self.admin_client.delete_consumer_groups([group_id])
            for group, f in result.items():
                try:
                    f.result()
                    logger.info(f"Successfully deleted consumer group: {group}")
                except Exception as e:
                    logger.error(f"Failed to delete consumer group {group}: {str(e)}")
        except Exception as e:
            logger.error(f"Error resetting consumer group: {str(e)}")

    def list_consumer_groups(self):
        """List all consumer groups"""
        try:
            groups = self.admin_client.list_consumer_groups().result()
            valid_groups = groups.valid
            logger.info("Found consumer groups:")
            for group in valid_groups:
                logger.info(f"  - {group.group_id}")
            return [group.group_id for group in valid_groups]
        except Exception as e:
            logger.error(f"Error listing consumer groups: {str(e)}")
            return []

    def check_topic_config(self, topic):
        """Check and display topic configuration"""
        try:
            resource = ConfigResource('topic', topic)
            result = self.admin_client.describe_configs([resource])
            configs = result[resource].result()
            logger.info(f"\nConfiguration for topic {topic}:")
            for config in configs.values():
                logger.info(f"  {config.name}: {config.value}")
        except Exception as e:
            logger.error(f"Error checking topic config: {str(e)}")

if __name__ == "__main__":
    kafka_repair = KafkaRepair()
    
    # List all consumer groups
    logger.info("\nListing all consumer groups...")
    groups = kafka_repair.list_consumer_groups()
    
    # Reset problematic consumer groups
    problem_groups = [
        'console-consumer-95781',  # The problematic group from the error
        'retrieval_group',
        'rca_group',
        'remediation_group'
    ]
    
    for group in problem_groups:
        if group in groups:
            logger.info(f"\nResetting consumer group: {group}")
            kafka_repair.reset_consumer_group(group)
    
    # Check configurations for our topics
    topics = ['logs.anomalies', 'logs.rca.output', 'logs.remediation']
    for topic in topics:
        kafka_repair.check_topic_config(topic)
