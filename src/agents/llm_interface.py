from langchain_ollama import OllamaLLM
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from typing import Optional
import os
import re
from pydantic import SecretStr

class LLMInterface:
    def __init__(self, provider: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.provider = provider.lower()
        self.model = model
        self.endpoint = endpoint or "http://localhost:11434"  # Default Ollama endpoint
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
            raise NotImplementedError("LLaMA.cpp support not implemented yet")
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def call(self, system_prompt: str, user_prompt: str) -> str:
        try:
            # Format prompt for Ollama, mimicking /set system behavior
            if self.provider == "ollama":
                prompt = f"{system_prompt}\n\n{user_prompt}"
            else:
                # For other providers, adjust prompt format if needed
                prompt = f"System: {system_prompt}\nUser: {user_prompt}"
            
            # Invoke the LLM
            response = self.llm.invoke(prompt)
            
            # Handle different response types
            if isinstance(response, str):
                result = response
            elif hasattr(response, 'content'):
                result = str(response.content)
            else:
                result = str(response)
            
            # Remove <think>...</think> tags and their content
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
            
            return result.strip()
        except Exception as e:
            return f"LLM call failed: {str(e)}"

# Example usage
if __name__ == "__main__":
    # Test Ollama
    llm_ollama = LLMInterface(provider="ollama", model="qwen3:1.7b")
    print("\nTesting Ollama:")
    response = llm_ollama.call("you are Einstein", "What is the universe")
    print(f"Ollama response: {response}")

    # Test Groq
    groq_api_key = os.getenv("GROQ_API_KEY")
    if groq_api_key:
        llm_groq = LLMInterface(
            provider="groq",
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            api_key=groq_api_key
        )
        print("\nTesting Groq:")
        response = llm_groq.call("you are Einstein", "What is the universe")
        print(f"Groq response: {response}")
    else:
        print("\nGroq API key not found in environment variables")