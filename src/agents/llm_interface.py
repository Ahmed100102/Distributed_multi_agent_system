from langchain_ollama import OllamaLLM
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_community.chat_models import ChatLlamaCpp  # Add this
from typing import Optional
import os
import re
from pydantic import SecretStr

class LLMInterface:
    def __init__(self, provider: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.provider = provider.lower()
        self.model = model
        self.endpoint = endpoint or "http://localhost:18000"  # Default for llama-serve
        self.api_key = api_key
        self.llm = self._initialize_llm()

    def _initialize_llm(self):
        if self.provider == "ollama":
            return OllamaLLM(
                model=self.model,
                base_url=self.endpoint
            )
        elif self.provider == "openai":
            return ChatOpenAI(model=self.model, api_key=SecretStr(self.api_key) if self.api_key else None)
        elif self.provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=self.model, google_api_key=self.api_key)
        elif self.provider == "groq":
            return ChatGroq(
                model=self.model,
                api_key=SecretStr(self.api_key) if self.api_key else None,
                temperature=0.7
            )
        elif self.provider == "llama_cpp":
            return ChatOpenAI(
                base_url=f"{self.endpoint}/v1",
                api_key="not-needed",  # llama-serve ignores auth
                model=self.model,
                temperature=0.7
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def call(self, system_prompt: str, user_prompt: str) -> str:
        try:
            if self.provider in ["ollama", "llama_cpp"]:
                prompt = f"{system_prompt}\n\n{user_prompt}"
            else:
                prompt = f"System: {system_prompt}\nUser: {user_prompt}"
            
            response = self.llm.invoke(prompt)
            
            if isinstance(response, str):
                result = response
            elif hasattr(response, 'content'):
                result = str(response.content)
            else:
                result = str(response)

            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            return result.strip()

        except Exception as e:
            return f"LLM call failed: {str(e)}"


# Example usage
if __name__ == "__main__":
    # Test LLaMA.cpp
    print("\nTesting LLaMA.cpp (llama-serve):")
    try:
        llm_llamacpp = LLMInterface(
            provider="llama_cpp",
            model="qwen3:1.7b",  # Match the model name used by llama-serve
            endpoint="http://localhost:18000"  # Ensure this matches your llama-serve config
        )
        response = llm_llamacpp.call("you are Einstein", "What is the universe")
        print(f"LLaMA.cpp response: {response}")
    except Exception as e:
        print(f"Failed to test llama_cpp: {e}")

