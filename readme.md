"AIzaSyAECBFg1Zl5tAgH4U0S7XDz0eD_R3ijRI0"
"AIzaSyDbn70BPNSnG5fDtxGBkKQJMUg_KiQRbN8"
"AIzaSyC7Gs6_lTnVRtFFBqPy7HoTJWLAzxF0Cvw"


curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key="AIzaSyC7Gs6_lTnVRtFFBqPy7HoTJWLAzxF0Cvw"" \
  -H 'Content-Type: application/json' \
  -X POST \
  -d '{
    "contents": [
      {
        "parts": [
          {
            "text": "Explain how AI works in a few words"
          }
        ]
      }
    ]
  }'


  correct the full code 
adn as i mentioned the source infra or platform is the source where this issue happened and make the agent have available fields already present in the prompt
> Entering new AgentExecutor chain...
2025-07-15 13:21:08,207 - ERROR - Error processing query: 'Input to PromptTemplate is missing variables {\'"log_id"\', \'"filters"\'}.  Expected: [\'"filters"\', \'"log_id"\', \'agent_scratchpad\', \'current_time\', \'input\'] Received: [\'input\', \'current_time\', \'tools\', \'tool_names\', \'chat_history\', \'intermediate_steps\', \'agent_scratchpad\']\nNote: if you intended {"log_id"} to be part of the string and not a variable, please escape it with double curly braces like: \'{{"log_id"}}\'.\nFor troubleshooting, visit: https://python.langchain.com/docs/troubleshooting/errors/INVALID_PROMPT_INPUT '
INFO:     127.0.0.1:33576 - "POST /chat HTTP/1.1" 500 Internal Server Error

https://grok.com/share/c2hhcmQtMw%3D%3D_59890490-0185-4bd2-8c0c-8f4b32c23fd4



/home/llmteam/.venv/bin/streamlit run /home/llmteam/Distributed_multi_agent_system/src/utils.test_0.py