# Testing Documentation

## Overview
This document describes the testing strategy and implementation for the Observix log analysis system. It covers unit tests, integration tests, and testing utilities.

## Test Structure

### Unit Tests
Located in `tests/test_agents.py`, these tests cover individual components:

1. **LLM Interface Tests**
   - Provider initialization
   - API communication
   - Response processing
   - Error handling

2. **RCA Agent Tests**
   - Message processing
   - Analysis accuracy
   - Kafka integration
   - Tool functionality

3. **Remediation Agent Tests**
   - Plan generation
   - Message handling
   - Priority assignment
   - Tool usage

4. **Retrieval Agent Tests**
   - Elasticsearch operations
   - Query functionality
   - Data storage
   - Index management

## Testing Environment

### Setup
```python
# Test configuration example
test_config = {
    "kafka": {
        "bootstrap.servers": "localhost:9092",
        "test.topic.prefix": "test_observix_"
    },
    "elasticsearch": {
        "hosts": ["localhost:9200"],
        "index_prefix": "test_observix_"
    },
    "llm": {
        "provider": "ollama",
        "model": "qwen3:1.7b",
        "endpoint": "http://localhost:11434"
    }
}
```

### Test Data
- Sample log entries
- Known anomalies
- Expected RCA results
- Remediation templates

## Running Tests

### Command Line
```bash
# Run all tests
python -m pytest tests/

# Run specific test file
python -m pytest tests/test_agents.py

# Run with coverage
python -m pytest --cov=src tests/
```

### Test Categories

#### 1. Unit Tests
```python
def test_llm_interface():
    # Test LLM interface initialization
    # Test prompt formatting
    # Test response processing
    pass

def test_rca_processing():
    # Test RCA message handling
    # Test analysis generation
    # Test output formatting
    pass
```

#### 2. Integration Tests
```python
def test_end_to_end_flow():
    # Test complete processing pipeline
    # Validate results at each stage
    # Check data consistency
    pass
```

#### 3. Performance Tests
```python
def test_processing_performance():
    # Test processing latency
    # Test throughput
    # Test resource usage
    pass
```

## Mocking

### External Services
- Mock Kafka producers/consumers
- Mock Elasticsearch client
- Mock LLM responses

### Example Mock
```python
@pytest.fixture
def mock_llm():
    return MockLLM(
        responses={
            "test_prompt": "test_response"
        }
    )
```

## Test Coverage

### Areas Covered
1. **Code Coverage**
   - Function coverage
   - Branch coverage
   - Line coverage

2. **Functionality Coverage**
   - API endpoints
   - Message processing
   - Error handling
   - Edge cases

3. **Integration Coverage**
   - Component interaction
   - Data flow
   - System behavior

## Continuous Integration

### GitHub Actions
```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: python -m pytest tests/
```

## Best Practices

### 1. Test Organization
- Group related tests
- Use descriptive names
- Include setup/teardown

### 2. Test Data
- Use realistic samples
- Cover edge cases
- Maintain test fixtures

### 3. Assertions
- Be specific
- Check all aspects
- Include error messages

## Troubleshooting Tests

### Common Issues
1. **Failed Tests**
   - Check test environment
   - Verify dependencies
   - Review test data

2. **Slow Tests**
   - Optimize setup/teardown
   - Use appropriate fixtures
   - Parallel test execution

3. **Flaky Tests**
   - Identify race conditions
   - Check resource cleanup
   - Review async operations

## Adding New Tests

### Process
1. Identify test requirements
2. Create test fixtures
3. Write test cases
4. Verify coverage
5. Review and refine

### Example
```python
def test_new_feature():
    # Setup
    test_data = setup_test_data()
    
    # Execute
    result = process_test_data(test_data)
    
    # Assert
    assert result.status == "success"
    assert result.data == expected_data
```

## Testing Tools
- pytest
- pytest-cov
- pytest-asyncio
- pytest-kafka
- pytest-elasticsearch
