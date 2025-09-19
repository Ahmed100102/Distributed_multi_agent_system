# LLM Interface

The `llm_interface.py` script provides a standardized interface for interacting with various Large Language Models (LLMs). It supports multiple providers and handles the complexities of initialization, input/output validation, and token counting.

## Overview

The `LLMInterface` class is the core component of this module. It allows you to instantiate a connection to an LLM from a supported provider and make calls to it with a consistent API.

### Key Features

- **Multiple Provider Support:** Works with "llama_cpp", "gemini", "groq", "ollama", and "openrouter".
- **Configuration:** Can be configured via environment variables or by passing parameters directly to the constructor.
- **Input/Output Validation:** Performs strict validation on prompts and cleans the LLM's output.
- **Token Counting:** Provides token counts for both input and output, with estimation for providers that don't supply this information.
- **Timeout Handling:** Implements a timeout mechanism for LLM calls.
- **Legacy Compatibility:** Includes a wrapper for older code that expects a simple string response.

## `LLMInterface` Class

### Initialization

The `LLMInterface` class can be initialized in two ways:

1.  **Using Environment Variables:** If no parameters are provided to the constructor, the class will use the `MODEL_RUNTIME` environment variable to determine which provider to use. The corresponding configuration will be loaded from the `runtime_configs` dictionary.

2.  **Passing Parameters:** You can explicitly specify the `provider`, `model`, `endpoint`, and `api_key` when creating an instance of the class.

### Methods

#### `__init__(self, provider: str = None, model: str = None, endpoint: Optional[str] = None, api_key: Optional[str] = None)`

-   **Description:** Initializes the `LLMInterface` instance.
-   **Parameters:**
    -   `provider` (str, optional): The LLM provider to use (e.g., "gemini", "ollama").
    -   `model` (str, optional): The specific model to use.
    -   `endpoint` (str, optional): The API endpoint for the LLM service.
    -   `api_key` (str, optional): The API key for authentication.

#### `call(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> Tuple[str, Dict[str, int]]`

-   **Description:** Makes a call to the LLM with the given prompts.
-   **Parameters:**
    -   `system_prompt` (str): The system prompt to guide the LLM's behavior.
    -   `user_prompt` (str): The user's prompt or question.
    -   `timeout` (int, optional): The timeout for the LLM call in seconds. Defaults to 30.
-   **Returns:** A tuple containing the LLM's response (as a string) and a dictionary with the input and output token counts.

#### `call_compatible(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> str`

-   **Description:** A compatibility wrapper for legacy code that returns only the LLM's response as a string.
-   **Parameters:**
    -   `system_prompt` (str): The system prompt.
    -   `user_prompt` (str): The user's prompt.
    -   `timeout` (int, optional): The timeout for the LLM call. Defaults to 30.
-   **Returns:** The LLM's response as a string.

## Supported Providers

The `runtime_configs` dictionary defines the configurations for the following providers:

-   **llama_cpp:** For running GGUF models with a local server.
-   **gemini:** For using Google's Gemini models.
-   **groq:** For using models on the Groq platform.
-   **ollama:** For running models with the Ollama server.
-   **openrouter:** For accessing various models through the OpenRouter API.

## Testing

The script includes a `if __name__ == "__main__"` block for testing the `LLMInterface` with different providers. You can run this script directly to test the functionality.

**Note:** Ensure you have the necessary environment variables set for the providers you want to test (e.g., `GEMINI_API_KEY`, `OR_API_KEY`).