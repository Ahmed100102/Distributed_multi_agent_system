# LangGraph Observibot

The `langgraph_observibot.py` script implements a sophisticated, conversational AI agent named Observibot. This agent is designed to interact with users to query, analyze, and summarize data from the Observix platform, which is stored in Elasticsearch.

## Overview

Observibot acts as a natural language interface to the monitoring and observability data. Users can ask questions in plain English, and Observibot will translate them into Elasticsearch queries, retrieve the relevant information, and present it in a clear and understandable format.

### Key Features

-   **Conversational Interface:** Users can interact with Observibot in a chat-like manner.
-   **Query Classification:** It can understand the user's intent and classify queries into different types (e.g., asking for a specific log, filtering for a set of issues, or requesting a health overview).
-   **Dynamic Routing:** Based on the query type, it routes the request to the appropriate processing subgraph.
-   **Tool Usage:** It uses a set of tools to interact with Elasticsearch, such as filtering for issues or retrieving a specific log by its ID.
-   **Summarization:** It can summarize the results of a query to provide a concise overview.
-   **Tracing:** It provides a `/trace` endpoint to visualize the agent's reasoning process.

## Architecture

Observibot is built on a modern stack for AI-powered applications:

-   **FastAPI:** Provides a high-performance web server for the `/chat` and `/trace` API endpoints.
-   **LangGraph:** Orchestrates the entire workflow as a graph of interconnected nodes. This allows for complex, stateful interactions and dynamic routing.
-   **Elasticsearch:** The primary data source, containing the log and remediation data from the Observix platform.
-   **LLMInterface:** The standardized interface for communicating with the configured Large Language Model.

## State Management

The agent's state is managed by the `ObservibotState` TypedDict. This dictionary is passed between the nodes in the LangGraph and contains all the information related to the current query, including:

-   `query`: The user's query.
-   `session_id`: A unique identifier for the conversation.
-   `query_type`: The classified type of the query.
-   `filters`: The Elasticsearch filters extracted from the query.
-   `intermediate_output`: The raw results from the Elasticsearch query.
-   `final_output`: The final, user-facing response.
-   `trace`: A list of steps taken by the agent to generate the response.

## Workflow and Routing

The main workflow is a LangGraph graph that starts with the `classify_query` node. This node determines the user's intent and sets the `query_type` in the state. Based on this `query_type`, the workflow is routed to one of the following subgraphs:

-   `log_id_subgraph`: For queries that contain a specific log ID.
-   `filter_subgraph`: For queries that involve filtering for issues based on certain criteria.
-   `health_subgraph`: For queries about the overall health or status of the system.
-   `summarize_subgraph`: For queries that explicitly ask for a summary.
-   `greeting_subgraph`: For simple greetings.
-   `dynamic_subgraph`: For queries that don't fit into any of the other categories.

## Subgraphs

Each subgraph is a smaller LangGraph workflow designed to handle a specific type of query. For example, the `log_id_subgraph` will execute the `tool_get_by_id` tool to retrieve a specific log, while the `filter_subgraph` will use the `tool_filter` tool to query for a set of issues.

## Tools

Observibot has access to a set of tools to perform its tasks:

-   `tool_filter`: Queries Elasticsearch with a set of filters.
-   `tool_get_by_id`: Retrieves a single log from Elasticsearch by its ID.
-   `tool_summarize_results`: Uses the LLM to summarize a list of issues.

## API Endpoints

The agent is exposed through a FastAPI server with two main endpoints:

-   **`/chat`:** The primary endpoint for interacting with Observibot. It takes a user query and returns a user-friendly response.
-   **`/trace`:** This endpoint is used for debugging and visualization. It returns a detailed trace of the agent's execution path, showing the inputs and outputs of each node in the graph.

## Metrics

The Observibot agent (V1) does not generate a separate metrics file, but it does provide some metrics in the response to the `/chat` and `/trace` endpoints.

-   **`total_tokens`:** An object containing the total number of prompt, completion, and total tokens used for the query.
-   **`node_durations`:** A dictionary containing the duration of each node in the LangGraph workflow.

## Configuration

Observibot can be configured using environment variables:

-   `ELASTICSEARCH_URL`: The URL of the Elasticsearch cluster.
-   `ELASTICSEARCH_INDEX`: The Elasticsearch index to query.
-   `MODEL_RUNTIME`: The LLM provider to use.
-   API keys and other settings for the chosen LLM provider.