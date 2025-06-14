from langchain_ollama import OllamaLLM
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_community.chat_models import ChatLlamaCpp
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import Optional
from pydantic import SecretStr
import os
import re
from langchain_core.exceptions import LangChainException

class LLMInterface:
    def __init__(self, provider: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.provider = provider.lower()
        self.model = model
        self.endpoint = endpoint or "http://localhost:18000"
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
                api_key=SecretStr(self.api_key) if self.api_key else None
            )
        elif self.provider == "gemini":
            if not self.api_key:
                raise ValueError("API key required for Gemini")
            return ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key="AIzaSyBS7ljmFDPyP5EaP1iuAW2-eW7hmWCqZp8"
            )
        elif self.provider == "groq":
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

    def call(self, system_prompt: str, user_prompt: str, timeout: int = 30) -> str:
        try:
            if self.provider in ["ollama", "llama_cpp"]:
                prompt = f"{system_prompt}\n\n{user_prompt}"
            else:
                prompt = f"System: {system_prompt}\nUser: {user_prompt}"

            response = self.llm.invoke(prompt, timeout=timeout)

            if isinstance(response, str):
                result = response
            elif hasattr(response, 'content'):
                result = str(response.content)
            else:
                result = str(response)

            # Remove internal <think> tags if present
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            return result.strip()

        except Exception as e:
            raise LangChainException(f"LLM call failed: {str(e)}")


# Example usage
if __name__ == "__main__":
    # Test Gemini
    print("\nTesting Gemini:")
    try:
        gemini_api_key = os.getenv("GEMINI_API_KEY", "AIzaSyBS7ljmFDPyP5EaP1iuAW2-eW7hmWCqZp8")  # You can also hardcode it here for testing
        if not gemini_api_key:
            raise ValueError("Set GEMINI_API_KEY environment variable or provide it explicitly.")

        llm_gemini = LLMInterface(
            provider="gemini",
            model="gemini-2.0-flash",  # Use a valid model name
            api_key=gemini_api_key
        )
        response = llm_gemini.call("You are Einstein", "What is the universe?")
        print(f"Gemini response: {response}")
    except Exception as e:
        print(f"Failed to test Gemini: {e}")

    # Test LLaMA.cpp (llama-serve)
    print("\nTesting LLaMA.cpp (llama-serve):")
    try:
        llm_llamacpp = LLMInterface(
            provider="llama_cpp",
            model="qwen3:1.7b",
            endpoint="http://localhost:18000"
        )
        response = llm_llamacpp.call("You are Einstein", "What is the universe?")
        print(f"LLaMA.cpp response: {response}")
    except Exception as e:
        print(f"Failed to test llama_cpp: {e}")
