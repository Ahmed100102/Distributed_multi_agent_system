# LLM Interface Documentation

## Overview
The LLM Interface module provides a unified interface for interacting with different Large Language Model (LLM) providers. It abstracts away provider-specific implementations, making it easy to switch between different LLM services.

## Features
- Support for multiple LLM providers:
  - Ollama (local deployment)
  - OpenAI
  - Google Gemini
  - LLaMA.cpp (planned)
- Unified prompt formatting
- Consistent response handling
- Error management
- Automatic provider-specific configuration

## Class: LLMInterface

### Constructor
```python
def __init__(self, provider: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None)
```

#### Parameters:
- `provider` (str): The LLM provider name ("ollama", "openai", "gemini")
- `model` (str): The specific model to use (e.g., "qwen3:1.7b", "gpt-3.5-turbo")
- `endpoint` (Optional[str]): API endpoint URL (default: "http://localhost:11434")
- `api_key` (Optional[str]): API key for authentication with commercial providers

### Methods

#### call(system_prompt: str, user_prompt: str) -> str
Makes a call to the LLM with system and user prompts.

##### Parameters:
- `system_prompt` (str): Instructions or context for the LLM
- `system_prompt` (str): The actual query or request

##### Returns:
- str: The processed response from the LLM

##### Example:
```python
llm = LLMInterface(
    provider="ollama",
    model="qwen3:1.7b"
)
response = llm.call(
    system_prompt="You are a helpful assistant",
    user_prompt="What is the capital of France?"
)
```

### Environment Variables
The module respects the following environment variables:
- `LLM_PROVIDER`: Default LLM provider
- `LLM_ENDPOINT`: Default API endpoint URL
- `LLM_API_KEY`: API key for commercial providers

### Provider-Specific Details

#### Ollama
- Default endpoint: http://localhost:11434
- No API key required
- Prompt format: `{system_prompt}\n\n{user_prompt}`

#### OpenAI
- Requires API key
- Uses ChatOpenAI from langchain_openai
- Prompt format: `System: {system_prompt}\nUser: {user_prompt}`

#### Google Gemini
- Requires API key
- Uses ChatGoogleGenerativeAI from langchain_google_genai
- Prompt format: `System: {system_prompt}\nUser: {user_prompt}`

### Error Handling
- Returns error message if LLM call fails
- Handles different response types (string, object with content)
- Removes thinking process tags (`<think>...</think>`)

### Dependencies
- langchain_ollama
- langchain_openai
- langchain_google_genai (imported dynamically)
- pydantic.SecretStr for secure API key handling

### Best Practices
1. Always handle the returned response which might be an error message
2. Use environment variables for configuration when possible
3. Consider the specific model capabilities when crafting prompts
4. Initialize once and reuse the instance for multiple calls
