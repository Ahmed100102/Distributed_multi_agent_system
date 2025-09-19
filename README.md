# Distributed Multi-Agent System

A small distributed system of agents for log retrieval, root-cause analysis (RCA), and automated remediation. Agents communicate via Kafka and store/consume data from Elasticsearch. The project contains FastAPI-based agent services and utilities.

## Quick start

1. Install dependencies:

```powershell
pip install -r requirements.txt
```

2. Configure environment variables in a `.env` file or export them into your shell. See `docs/setup.md` for details.

3. Start Kafka and Elasticsearch.

4. Create required Kafka topics:

```powershell
python src/create_kafka_topics.py
```

5. Run agents (each in its own terminal):

```powershell
python src/agents/retrieval_agent.py
uvicorn src.agents.rca_agent:app --host 0.0.0.0 --port 8001
uvicorn src.agents.remediation_agent:app --host 0.0.0.0 --port 8002
uvicorn src.agents.langgraph_observibot2:app --host 0.0.0.0 --port 8003
```

## Documentation and wiki

The repository includes a `docs/` folder with canonical documentation and a generated `wiki/` folder suitable for pushing to a Git-based wiki (Gogs/Gitea/GitHub). To publish the wiki to a Gogs wiki repository, see `scripts/push_wiki_to_gogs.ps1` or the `docs/` pages for manual steps.
