import os
import re
import threading
from typing import Optional, Tuple, Dict
from pydantic import SecretStr
from langchain_ollama import OllamaLLM
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_community.chat_models import ChatLlamaCpp
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.exceptions import LangChainException

# Predefined model configurations
MODEL_RUNTIME = os.getenv("MODEL_RUNTIME", "gemini").lower()
VALID_RUNTIMES = ["llama_cpp", "gemini", "groq", "ollama", "openrouter"]
runtime_configs = {
    "llama_cpp": {
        "provider": "llama_cpp",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "qwen3:4b"),
        "endpoint": os.getenv("LLM_ENDPOINT", "http://localhost:18000"),
        "api_key": None
    },
    "gemini": {
        "provider": "gemini",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "gemini-2.0-flash"),
        "endpoint": None,
        "api_key": os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
    },
    "groq": {
        "provider": "groq",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "meta-llama/llama-4-scout-17b-16e-instruct"),
        "endpoint": None,
        "api_key": os.getenv("GROQ_API_KEY", "YOUR_GROQ_API_KEY")
    },
    "ollama": {
        "provider": "ollama",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "qwen3:4b"),
        "endpoint": os.getenv("LLM_ENDPOINT", "http://localhost:11434"),
        "api_key": None
    },
    "openrouter": {
        "provider": "openai",
        "model": os.getenv("LLM_MODEL_REMEDIATION", "qwen/qwen3-30b-a3b:free"),
        "endpoint": os.getenv("LLM_ENDPOINT", "https://openrouter.ai/api/v1"),
        "api_key": os.getenv("OR_API_KEY", "YOUR_OPENROUTER_API_KEY")
    }
}

class LLMInterface:
    def __init__(self, provider: str = None, model: str = None, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        # Use runtime config if no explicit parameters provided
        if provider is None and model is None and endpoint is None and api_key is None:
            if MODEL_RUNTIME not in VALID_RUNTIMES:
                raise ValueError(f"Invalid MODEL_RUNTIME: {MODEL_RUNTIME}. Must be one of {VALID_RUNTIMES}")
            config = runtime_configs[MODEL_RUNTIME]
            self.provider = config["provider"].lower()
            self.model = config["model"]
            self.endpoint = config["endpoint"]
            self.api_key = config["api_key"]
        else:
            self.provider = provider.lower() if provider else "gemini"
            self.model = model or "gemini-2.5-flash"
            self.endpoint = endpoint or ("http://localhost:18000" if self.provider in ["llama_cpp", "ollama"] else None)
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
                api_key=SecretStr(self.api_key) if self.api_key else None,
                base_url=self.endpoint
            )
        elif self.provider == "gemini":
            if not self.api_key:
                raise ValueError("API key required for Gemini")
            return ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=self.api_key
            )
        elif self.provider == "groq":
            if not self.api_key:
                raise ValueError("API key required for Groq")
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

    def _validate_input(self, prompt: str, max_length: int = 10000) -> None:
        """Strict classical validation for input prompts."""
        if not isinstance(prompt, str):
            raise ValueError(f"Prompt must be a string, got {type(prompt)}")
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        if len(prompt) > max_length:
            raise ValueError(f"Prompt exceeds maximum length of {max_length} characters")
        try:
            prompt.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError("Prompt contains invalid UTF-8 characters")

    def _validate_output(self, response: str, max_length: int = 100000) -> str:
        """Strict classical validation and cleaning for LLM output."""
        if not isinstance(response, str):
            response = str(response)
        if len(response) > max_length:
            response = response[:max_length]
        # Remove non-printable characters
        response = re.sub(r'[^\x20-\x7E\n\t]', '', response)
        return response.strip()

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for providers without native token counting."""
        # Simple word-based estimation: assume 1 token per 4 characters
        return len(text) // 4 + 1 if text else 0

    def call(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> Tuple[str, Dict[str, int]]:
        """Call the LLM and return response with token counts."""
        try:
            # Validate inputs
            self._validate_input(system_prompt)
            self._validate_input(user_prompt)

            # Format prompt based on provider
            if self.provider in ["ollama", "llama_cpp"]:
                prompt = f"{system_prompt}\n\n{user_prompt}"
            else:
                prompt = f"System: {system_prompt}\nUser: {user_prompt}"

            # Handle timeout for Ollama
            if self.provider == "ollama":
                result = [None]
                exception = [None]
                def llm_call_wrapper():
                    try:
                        result[0] = self.llm.invoke(prompt)  # No timeout parameter
                    except Exception as e:
                        exception[0] = e

                llm_thread = threading.Thread(target=llm_call_wrapper)
                llm_thread.start()
                llm_thread.join(timeout=timeout)

                if llm_thread.is_alive():
                    raise LangChainException("LLM call timed out")
                if exception[0]:
                    raise exception[0]
                response = result[0]
            else:
                # Other providers that support timeout
                response = self.llm.invoke(prompt, timeout=timeout)

            # Extract response content
            if isinstance(response, str):
                result = response
            elif hasattr(response, 'content'):
                result = str(response.content)
            else:
                result = str(response)

            # Clean and validate output
            result = self._validate_output(result)

            # Get token counts
            token_counts = {"input_tokens": 0, "output_tokens": 0}
            if self.provider in ["gemini", "groq"] and hasattr(response, 'usage'):
                # Use API-provided token counts
                token_counts["input_tokens"] = getattr(response.usage, 'prompt_token_count', 0) or self._estimate_tokens(prompt)
                token_counts["output_tokens"] = getattr(response.usage, 'completion_token_count', 0) or self._estimate_tokens(result)
            else:
                # Estimate tokens for ollama and llama_cpp
                token_counts["input_tokens"] = self._estimate_tokens(prompt)
                token_counts["output_tokens"] = self._estimate_tokens(result)

            # Clean up any internal tags
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            return result, token_counts

        except Exception as e:
            raise LangChainException(f"LLM call failed: {str(e)}")

    def call_compatible(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> str:
        """Compatibility wrapper for legacy code expecting string response."""
        response, _ = self.call(system_prompt, user_prompt, timeout)
        return response

# --------------------------- 
#         TESTING
# --------------------------- 

if __name__ == "__main__":
    print("\nTesting Gemini:")
    try:
        gemini_api_key = os.getenv("GEMINI_API_KEY", "AIzaSyAECBFg1Zl5tAgH4U0S7XDz0eD_R3ijRI0")
        if not gemini_api_key:
            raise ValueError("Set GEMINI_API_KEY environment variable or provide it explicitly.")

        llm_gemini = LLMInterface(
            provider="gemini",
            model="gemini-2.0-flash",
            api_key=gemini_api_key
        )

        response, token_counts = llm_gemini.call("You are Einstein", "What is the universe?")
        print(f"Gemini response: {response}")
        print(f"Token counts: {token_counts}")

    except Exception as e:
        print(f"Failed to test Gemini: {e}")

    print("\nTesting OpenRouter:")
    try:
        open_router_api_key = os.getenv("OR_API_KEY")
        if not open_router_api_key:
            raise ValueError("Set OR_API_KEY environment variable or provide it explicitly.")

        llm_openrouter = LLMInterface(
            provider="openai",
            model="qwen/qwen3-30b-a3b:free",
            endpoint="https://openrouter.ai/api/v1",
            api_key=open_router_api_key
        )

        response, token_counts = llm_openrouter.call("You are Einstein", "What is the universe?")
        print(f"OpenRouter response: {response}")
        print(f"Token counts: {token_counts}")

    except Exception as e:
        print(f"Failed to test OpenRouter: {e}")
