from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic
import logging
import sys
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KafkaManager:
    def __init__(self, bootstrap_servers="localhost:9092"):
        self.admin_client = AdminClient({
            "bootstrap.servers": bootstrap_servers
        })

    def list_consumer_groups(self):
        """List all consumer groups"""
        try:
            groups = self.admin_client.list_consumer_groups().result()
            logger.info("\nExisting consumer groups:")
            for group in groups.valid:
                logger.info(f"  - {group.group_id}")
            return [group.group_id for group in groups.valid]
        except Exception as e:
            logger.error(f"Failed to list consumer groups: {e}")
            return []

    def check_consumer_group(self, group_id):
        """Check if a consumer group exists and its status"""
        try:
            groups = self.admin_client.list_consumer_groups().result()
            for group in groups.valid:
                if group.group_id == group_id:
                    # Get detailed group info
                    group_info = self.admin_client.describe_consumer_groups([group_id])
                    for id, future in group_info.items():
                        try:
                            info = future.result()
                            logger.info(f"Group {id} state: {info[0].state}")
                            return True
                        except Exception as e:
                            logger.error(f"Error getting group info for {id}: {e}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Error checking consumer group {group_id}: {e}")
            return False

    def delete_consumer_group(self, group_id):
        """Delete a specific consumer group with better error handling"""
        try:
            # First check if group exists
            if not self.check_consumer_group(group_id):
                logger.info(f"Consumer group {group_id} does not exist, skipping deletion")
                return

            # Try to delete the group
            result = self.admin_client.delete_consumer_groups([group_id])
            for group, f in result.items():
                try:
                    f.result(timeout=10.0)  # Add timeout
                    logger.info(f"Successfully deleted consumer group: {group}")
                except Exception as e:
                    if "GROUP_ID_NOT_FOUND" in str(e):
                        logger.info(f"Group {group} was already deleted or doesn't exist")
                    elif "COORDINATOR_NOT_AVAILABLE" in str(e):
                        logger.warning(f"Coordinator not available for group {group}, retry later")
                    else:
                        logger.error(f"Failed to delete consumer group {group}: {e}")
        except Exception as e:
            logger.error(f"Error deleting consumer group: {e}")

    def reset_consumer_groups(self):
        """Reset all consumer groups with retry logic"""
        problem_groups = [
            "console-consumer-57923",
            "console-consumer-95781",
            "retrieval_group",
            "rca_group",
            "remediation_group"
        ]
        
        # First list existing groups
        existing_groups = self.list_consumer_groups()
        logger.info(f"Found existing groups: {existing_groups}")
        
        for group in problem_groups:
            max_retries = 3
            retry_count = 0
            while retry_count < max_retries:
                try:
                    logger.info(f"Attempting to delete group {group} (attempt {retry_count + 1}/{max_retries})")
                    self.delete_consumer_group(group)
                    break
                except Exception as e:
                    logger.error(f"Error deleting group {group} (attempt {retry_count + 1}): {e}")
                    retry_count += 1
                    if retry_count < max_retries:
                        time.sleep(2 ** retry_count)  # Exponential backoff
        
        # Verify deletions
        remaining_groups = self.list_consumer_groups()
        logger.info(f"Remaining consumer groups after cleanup: {remaining_groups}")

    def verify_topic(self, topic_name):
        """Verify topic existence and configuration"""
        try:
            topics = self.admin_client.list_topics().topics
            if topic_name not in topics:
                logger.error(f"Topic {topic_name} does not exist!")
                return False

            resource = ConfigResource('topic', topic_name)
            result = self.admin_client.describe_configs([resource])
            configs = result[resource].result()
            
            logger.info(f"\nConfiguration for topic {topic_name}:")
            for config in configs.values():
                logger.info(f"  {config.name}: {config.value}")
            
            return True
        except Exception as e:
            logger.error(f"Error verifying topic {topic_name}: {e}")
            return False

    def repair_topics(self):
        """Repair essential topics with correct configuration"""
        topics = [
            NewTopic(
                "logs.anomalies",
                num_partitions=3,  # Increased partitions for better parallelism
                replication_factor=1,
                config={
                    "cleanup.policy": "delete",
                    "retention.ms": "604800000",  # 7 days
                    "min.insync.replicas": "1"
                }
            ),
            NewTopic(
                "logs.rca.output",
                num_partitions=3,
                replication_factor=1,
                config={
                    "cleanup.policy": "delete",
                    "retention.ms": "604800000",
                    "min.insync.replicas": "1"
                }
            ),
            NewTopic(
                "logs.remediation",
                num_partitions=3,
                replication_factor=1,
                config={
                    "cleanup.policy": "delete",
                    "retention.ms": "604800000",
                    "min.insync.replicas": "1"
                }
            )
        ]

        for topic in topics:
            try:
                self.admin_client.create_topics([topic])
                logger.info(f"Created/Updated topic: {topic.topic}")
            except Exception as e:
                if "TopicExistsException" in str(e):
                    logger.info(f"Topic {topic.topic} already exists")
                else:
                    logger.error(f"Error creating topic {topic.topic}: {e}")

def main():
    manager = KafkaManager()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--delete-groups":
            logger.info("Starting consumer group cleanup...")
            manager.reset_consumer_groups()
        elif sys.argv[1] == "--verify":
            # List current state
            logger.info("Current Kafka state:")
            manager.list_consumer_groups()
            topics = ["logs.anomalies", "logs.rca.output", "logs.remediation"]
            for topic in topics:
                manager.verify_topic(topic)
        elif sys.argv[1] == "--repair":
            # Full repair mode
            logger.info("Starting Kafka repair...")
            manager.reset_consumer_groups()
            manager.repair_topics()
    else:
        # Default behavior: show status
        logger.info("Kafka Manager Usage:")
        logger.info("  --delete-groups  Delete problematic consumer groups")
        logger.info("  --verify        Check current Kafka state")
        logger.info("  --repair        Perform full repair (groups and topics)")

if __name__ == "__main__":
    main()
