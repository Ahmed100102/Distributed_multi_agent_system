from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import logging
from datetime import datetime, UTC
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="ObservabilityAnalyst Mock Chatbot", description="Mock API for UI testing")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow requests from any origin
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)


# Pydantic model for request body (identical to real API)
class ChatRequest(BaseModel):
    query: str
    session_id: str = "observability_session"

# Mock session history
mock_session_histories = {}

# Mock response function
def mock_analyze_response(query: str) -> dict:
    if query.lower() == "dashboard":
        return {
            "status": "success",
            "response": json.dumps({
                "component_health": {
                    "FISCD": {"log_volume": 100, "error_count": 2, "error_rate": 2.0},
                    "VGM": {"log_volume": 150, "error_count": 3, "error_rate": 2.0},
                    "Netprobe": {"log_volume": 80, "error_count": 1, "error_rate": 1.25},
                    "Gateway": {"log_volume": 200, "error_count": 5, "error_rate": 2.5},
                    "Observix": {"log_volume": 50, "error_count": 0, "error_rate": 0.0}
                },
                "error_trends": {
                    "total_errors": 11,
                    "error_patterns": {"connection_errors": 5, "authentication_errors": 3},
                    "anomalies": []
                },
                "observix_results": {
                    "total_logs": 50,
                    "high_severity_analysis": {"count": 0, "common_issues": []}
                },
                "timestamp": datetime.now(UTC).isoformat()
            }, indent=2)
        }
    elif query.lower() == "help":
        return {
            "status": "success",
            "response": """
📊 ObservabilityAnalyst Commands:

QUICK ANALYSIS:
- health: Component health analysis
- trends: Error pattern and trend analysis
- security: Security event analysis
- impact: Business impact assessment
- observix: Test results analysis
- dashboard: Generate dashboard data

DETAILED QUERIES:
- "What are the main error patterns in FISCD last 24 hours?"
- "Show me performance trends for all components"
- "Analyze security events in the last week"
- "What's the business impact of recent errors?"
- "Are there any anomalies in the system?"

TIME RANGES: last hour, last 24 hours, last 7 days, last 30 days, last year
COMPONENTS: FISCD, VGM, Netprobe, Gateway, Observix
            """
        }
    elif "health" in query.lower():
        return {
            "status": "success",
            "response": "✅ All components have low error rates (<5%) in the last 24 hours.\nComponents: FISCD, VGM, Netprobe, Gateway, Observix"
        }
    elif "trends" in query.lower():
        return {
            "status": "success",
            "response": "📊 Error Trends: 10 connection errors detected in logstash-* over last 24 hours.\nNo anomalies found."
        }
    elif "security" in query.lower():
        return {
            "status": "success",
            "response": "🔒 Security Analysis: No suspicious activities detected in the last 24 hours."
        }
    elif "impact" in query.lower():
        return {
            "status": "success",
            "response": "💼 Business Impact: No critical errors affecting business operations in the last 24 hours."
        }
    elif "observix" in query.lower():
        return {
            "status": "success",
            "response": "📊 Observix Analysis: 100 logs analyzed, 2 high-severity issues detected."
        }
    else:
        return {
            "status": "success",
            "response": "🔍 Mock Response: Analysis completed for query. Please specify 'health', 'trends', 'security', 'impact', 'observix', or 'dashboard' for detailed results."
        }

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        if not request.query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # Simulate session history
        if request.session_id not in mock_session_histories:
            mock_session_histories[request.session_id] = []
        mock_session_histories[request.session_id].append({
            "query": request.query,
            "timestamp": datetime.now(UTC).isoformat()
        })
        
        # Return mock response
        response_dict = mock_analyze_response(request.query)
        return {
            "status": response_dict.get("status", "success"),
            "response": response_dict.get("response", "No analysis available.")
        }
    
    except Exception as e:
        logger.error(f"Error processing mock query: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200)