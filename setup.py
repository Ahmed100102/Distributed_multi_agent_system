from setuptools import setup, find_packages

setup(
    name="observix",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "confluent-kafka==2.10.0",
        "elasticsearch==8.15.0",
        "langchain==0.3.25",
        "langchain-community==0.3.24",
        "langchain-openai==0.3.19",
        "langchain-google-genai==2.1.5",
        "pydantic==2.11.5",
        "langchain-core==0.3.63",
        "langchain-text-splitters==0.3.8",
        "langchain-groq==0.3.2",
        "groq==0.26.0",
    ],
    author="Your Name",
    description="Multi-Agent System for Log Analysis",
    python_requires=">=3.8",
)
