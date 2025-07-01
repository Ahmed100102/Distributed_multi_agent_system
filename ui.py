import dash
from dash import html, dcc, Input, Output, State
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import requests
import json
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Dash app
app = dash.Dash(__name__, title="ObservabilityAnalyst Dashboard")
app.config.suppress_callback_exceptions = True

# Tailwind CSS CDN for styling
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-100">
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

# Layout
app.layout = html.Div(className="min-h-screen flex flex-col", children=[
    # Header
    html.Header(className="bg-blue-600 text-white p-4 shadow-md", children=[
        html.H1("ObservabilityAnalyst Dashboard", className="text-2xl font-bold"),
        html.P("Interact with Observix results and view system insights", className="text-sm")
    ]),

    # Main content
    html.Div(className="flex flex-1", children=[
        # Sidebar
        html.Div(className="w-1/4 bg-white p-4 shadow-md", children=[
            html.H2("Query Input", className="text-lg font-semibold mb-4"),
            dcc.Input(
                id="query-input",
                type="text",
                placeholder="Enter query (e.g., 'health', 'performance last 2 days')",
                className="w-full p-2 border rounded mb-4"
            ),
            html.Button("Submit", id="submit-button", n_clicks=0, className="w-full bg-blue-500 text-white p-2 rounded hover:bg-blue-600"),
            html.H3("Predefined Prompts", className="text-md font-semibold mt-6 mb-2"),
            html.Ul(className="space-y-2", children=[
                html.Li(html.Button("System Health", id="health-btn", className="w-full text-left p-2 bg-gray-200 rounded hover:bg-gray-300")),
                html.Li(html.Button("Error Trends", id="trends-btn", className="w-full text-left p-2 bg-gray-200 rounded hover:bg-gray-300")),
                html.Li(html.Button("High-Severity Issues", id="high-severity-btn", className="w-full text-left p-2 bg-gray-200 rounded hover:bg-gray-300")),
                html.Li(html.Button("Performance Trends", id="performance-btn", className="w-full text-left p-2 bg-gray-200 rounded hover:bg-gray-300")),
                html.Li(html.Button("Help", id="help-btn", className="w-full text-left p-2 bg-gray-200 rounded hover:bg-gray-300"))
            ]),
            html.H3("Stored Data Info", className="text-md font-semibold mt-6 mb-2"),
            html.Div(id="stored-data-info", className="text-sm")
        ]),

        # Main content area
        html.Div(className="w-3/4 p-4", children=[
            # Tabs for different views
            dcc.Tabs(id="tabs", value="insights", children=[
                dcc.Tab(label="Insights", value="insights", className="text-blue-600"),
                dcc.Tab(label="Charts", value="charts", className="text-blue-600"),
                dcc.Tab(label="Raw Data", value="raw-data", className="text-blue-600")
            ]),
            html.Div(id="tabs-content", className="mt-4")
        ])
    ]),

    # Store for session history
    dcc.Store(id="session-history", data=[]),
    dcc.Store(id="session-id", data=f"session_{datetime.now().isoformat()}")
])

# Function to fetch data from FastAPI
def fetch_observix_data(query, session_id):
    try:
        response = requests.post(
            "http://localhost:8200/chat",
            json={"query": query, "session_id": session_id},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching data: {str(e)}")
        return {"status": "error", "response": f"Error: {str(e)}. Verify FastAPI server is running at http://localhost:8200."}

# Callback to update stored data info
@app.callback(
    Output("stored-data-info", "children"),
    Input("submit-button", "n_clicks")
)
def update_stored_data_info(n_clicks):
    data = fetch_observix_data("dashboard", f"session_{datetime.now().isoformat()}")
    if data.get("status") != "success":
        return html.P("Error fetching stored data info.", className="text-red-500")

    stored_data = json.loads(data["response"]).get("stored_data_info", {})
    components = stored_data.get("components", [])
    severities = stored_data.get("severities", [])
    categories = stored_data.get("categories", [])
    time_range = stored_data.get("time_range", {})

    return html.Div([
        html.P(f"Components: {', '.join(components) or 'None'}"),
        html.P(f"Severities: {', '.join(severities) or 'None'}"),
        html.P(f"Categories: {', '.join(categories) or 'None'}"),
        html.P(f"Time Range: {time_range.get('earliest', 'unknown')} to {time_range.get('latest', 'unknown')}")
    ])

# Callback to handle query submission and predefined prompts
@app.callback(
    [
        Output("tabs-content", "children"),
        Output("session-history", "data"),
        Output("query-input", "value")
    ],
    [
        Input("submit-button", "n_clicks"),
        Input("health-btn", "n_clicks"),
        Input("trends-btn", "n_clicks"),
        Input("high-severity-btn", "n_clicks"),
        Input("performance-btn", "n_clicks"),
        Input("help-btn", "n_clicks")
    ],
    [
        State("query-input", "value"),
        State("session-id", "data"),
        State("session-history", "data"),
        State("tabs", "value")
    ]
)
def update_output(submit_clicks, health_clicks, trends_clicks, high_severity_clicks, performance_clicks, help_clicks, query, session_id, history, tab):
    ctx = dash.callback_context
    if not ctx.triggered:
        return html.P("Enter a query or select a predefined prompt.", className="text-gray-500"), history, query

    button_id = ctx.triggered[0]["prop_id"].split(".")[0]
    query_map = {
        "health-btn": "health",
        "trends-btn": "trends",
        "high-severity-btn": "high_severity",
        "performance-btn": "performance",
        "help-btn": "help"
    }
    query_to_send = query_map.get(button_id, query or "health")
    new_query = "" if button_id != "submit-button" else query

    data = fetch_observix_data(query_to_send, session_id)
    history.append({"query": query_to_send, "response": data.get("response", "No response")})

    if data.get("status") != "success":
        return html.P(data.get("response", "Error fetching data."), className="text-red-500"), history, new_query

    response_data = data.get("response", "")
    try:
        parsed_data = json.loads(response_data) if "dashboard" in query_to_send.lower() else {"insights": response_data}
    except json.JSONDecodeError:
        parsed_data = {"insights": response_data}

    # Insights tab
    insights_content = html.Div([
        html.H3("Analysis Insights", className="text-lg font-semibold mb-4"),
        html.Pre(parsed_data.get("insights", response_data), className="bg-gray-100 p-4 rounded")
    ])

    # Charts tab
    charts_content = html.Div()
    if "dashboard" in query_to_send.lower():
        observix_results = parsed_data.get("observix_results", {})
        severity_df = pd.DataFrame(
            [(k, v) for k, v in observix_results.get("severity_distribution", {}).items()],
            columns=["Severity", "Count"]
        )
        component_df = pd.DataFrame(
            [(k, v) for k, v in observix_results.get("component_distribution", {}).items()],
            columns=["Component", "Count"]
        )
        performance_trends = parsed_data.get("performance_trends", [])
        perf_df = pd.DataFrame([
            {
                "Time": bucket["key_as_string"],
                "CPU Usage (%)": bucket.get("avg_cpu_usage", {}).get("value", 0),
                "Memory Usage (%)": bucket.get("avg_memory_usage", {}).get("value", 0),
                "Response Time (ms)": bucket.get("avg_response_time", {}).get("value", 0)
            }
            for bucket in performance_trends
        ])

        charts_content = html.Div([
            html.H3("Visualizations", className="text-lg font-semibold mb-4"),
            dcc.Graph(
                figure=px.bar(severity_df, x="Severity", y="Count", title="Severity Distribution").update_layout(
                    xaxis_title="Severity", yaxis_title="Issue Count", template="plotly_white"
                )
            ) if not severity_df.empty else html.P("No severity data available."),
            dcc.Graph(
                figure=px.bar(component_df, x="Component", y="Count", title="Component Distribution").update_layout(
                    xaxis_title="Component", yaxis_title="Issue Count", template="plotly_white"
                )
            ) if not component_df.empty else html.P("No component data available."),
            dcc.Graph(
                figure=go.Figure(
                    data=[
                        go.Scatter(x=perf_df["Time"], y=perf_df["CPU Usage (%)"], name="CPU Usage", mode="lines+markers"),
                        go.Scatter(x=perf_df["Time"], y=perf_df["Memory Usage (%)"], name="Memory Usage", mode="lines+markers"),
                        go.Scatter(x=perf_df["Time"], y=perf_df["Response Time (ms)"], name="Response Time", mode="lines+markers")
                    ],
                    layout=go.Layout(title="Performance Trends", xaxis_title="Time", yaxis_title="Value", template="plotly_white")
                )
            ) if not perf_df.empty else html.P("No performance data available.")
        ])

    # Raw Data tab
    raw_data_content = html.Div([
        html.H3("Raw Analysis Data", className="text-lg font-semibold mb-4"),
        html.Pre(json.dumps(parsed_data, indent=2), className="bg-gray-100 p-4 rounded overflow-auto max-h-[600px]")
    ])

    # History section
    history_content = html.Div([
        html.H3("Conversation History", className="text-lg font-semibold mb-4"),
        html.Ul(className="space-y-2", children=[
            html.Li(f"Q: {item['query']} | A: {item['response'][:100]}...", className="bg-gray-50 p-2 rounded")
            for item in history[-5:]  # Show last 5 interactions
        ])
    ])

    # Combine content based on selected tab
    tab_content = {
        "insights": insights_content,
        "charts": charts_content,
        "raw-data": raw_data_content
    }

    return (
        html.Div([tab_content.get(tab, insights_content), history_content], className="space-y-4"),
        history,
        new_query
    )

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)