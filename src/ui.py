import streamlit as st
import requests
import time
import pandas as pd
from datetime import datetime
import json

API_BASE_URL = "http://localhost:8200"

TEST_QUERIES = [
    "Give full details for log_id MZYbt5cB4rxvVeedMz-Y",
    "Summarize the error in log_id z5Ybt5cB4rxvVeedN1Eu",
    "List all high-severity APPLICATION category issues",
    "List all NETWORK issues with severity HIGH",
    "Find issues last 30 days",
    "Show remediation steps for log_id s2GkZJcB4rxvVeed6fc1",
    "Give root cause for log_id s2GkZJcB4rxvVeed6fc1",
    "Find issues mentioning 'Socket write error'",
    "What is the detailed analysis for log_id MZYbt5cB4rxvVeedMz-Y?",
    "system health last month"
]

st.set_page_config(page_title="Observibot Dashboard", layout="wide")
st.title("🔍 Observibot - LLM Observability UI")
st.caption(f"Date: {datetime.now().strftime('%A, %B %d, %Y, %I:%M %p')}")

with st.sidebar:
    st.header("Test & Metrics Controls")
    test_mode = st.radio("Test Mode", ["Single Query", "Batch Test"])
    if test_mode == "Batch Test":
        selected_tests = st.multiselect("Select Queries to Run", TEST_QUERIES, default=TEST_QUERIES)
        run_tests = st.button("Run Selected Tests")
    else:
        custom_query = st.text_area("Custom Query", value="", height=100)
        submit_custom = st.button("Submit")

    if st.button("Clear Results"):
        st.session_state.pop("test_results", None)
    st.markdown("---")
    st.subheader("Instructions")
    st.info(
        "Run batch or single query, monitor metrics in real time.\n"
        "For each query, see: Response, Latency, Token Usage, Fields Used, Node Durations, Trace, Errors.\n"
        "Use 'Copy Query' to rerun specific queries."
    )

# -----------------------
# Helper Functions
# -----------------------

def call_api(query, session_id=None):
    try:
        t0 = time.perf_counter()
        chat_payload = {"query": query}
        if session_id:
            chat_payload["session_id"] = session_id
        chat_resp = requests.post(f"{API_BASE_URL}/chat", json=chat_payload, timeout=1800)
        chat_resp.raise_for_status()  # Raises exception for 4xx/5xx status codes
        chat_data = chat_resp.json()
        latency = time.perf_counter() - t0
        requested_fields = chat_data.get("requested_fields", [])
        node_durations = chat_data.get("node_durations", {})
        tokens = chat_data.get("total_tokens", {})
        response = chat_data.get("response", "No response returned")
        session_id_ret = chat_data.get("session_id", session_id)
        error = None

    except requests.exceptions.HTTPError as e:
        latency = time.perf_counter() - t0
        error = f"HTTP Error: {str(e)} - {chat_resp.text[:200]}..."
        response = None
        session_id_ret = session_id
        tokens = {}
        node_durations = {}
        requested_fields = []
    except requests.exceptions.RequestException as e:
        latency = time.perf_counter() - t0
        error = f"API Error: {str(e)}"
        response = None
        session_id_ret = session_id
        tokens = {}
        node_durations = {}
        requested_fields = []
    except ValueError as e:
        latency = time.perf_counter() - t0
        error = f"JSON Parse Error: {str(e)}"
        response = None
        session_id_ret = session_id
        tokens = {}
        node_durations = {}
        requested_fields = []

    # Get trace if no error and session_id exists
    trace_steps = []
    trace_error = None
    if session_id_ret and not error:
        try:
            trace_resp = requests.post(
                f"{API_BASE_URL}/trace",
                json={"query": query, "session_id": session_id_ret},
                timeout=1800
            )
            trace_resp.raise_for_status()
            trace_json = trace_resp.json()
            trace_steps = trace_json.get("trace", [])
        except requests.exceptions.RequestException as e:
            trace_error = f"Trace Error: {str(e)}"

    return {
        "query": query,
        "response": response,
        "latency": latency,
        "tokens": tokens,
        "trace": trace_steps,
        "trace_error": trace_error,
        "requested_fields": requested_fields,
        "node_durations": node_durations,
        "error": error,
        "session_id": session_id_ret,
        "timestamp": datetime.now()
    }

def collect_metrics(results):
    metrics = {
        "Total Tests": len(results),
        "Success Rate": f"{sum(1 for r in results if not r['error'])}/{len(results)}",
        "Average Latency (s)": round(sum(r["latency"] for r in results) / len(results), 2) if results else 0.0,
        "Max Latency (s)": round(max((r["latency"] for r in results), default=0), 2),
        "Total Tokens": sum(r["tokens"].get("total_tokens", 0) for r in results),
        "Average Tokens": round(
            sum(r["tokens"].get("total_tokens", 0) for r in results) / len(results), 2) if results else 0.0,
        "Fields Used (Avg)": round(
            sum(len(r["requested_fields"]) for r in results) / len(results), 2) if results else 0.0,
        "Tests with Errors": sum(1 for r in results if r["error"]),
    }
    return metrics

# -----------------------
# Main UI Logic
# -----------------------

if "test_results" not in st.session_state:
    st.session_state.test_results = []

output = st.container()

if test_mode == "Single Query":
    if submit_custom and custom_query.strip():
        with st.spinner("Processing query..."):
            result = call_api(custom_query)
            st.session_state.test_results.insert(0, result)
            if not result["error"]:
                st.success(
                    f"Query complete in {result['latency']:.2f}s, Tokens: {result['tokens'].get('total_tokens', 0)}"
                )
            else:
                st.error(f"Query failed: {result['error']}")

elif test_mode == "Batch Test" and run_tests:
    for idx, query in enumerate(selected_tests):
        with st.spinner(f"Processing {idx+1}/{len(selected_tests)}..."):
            if idx!=0 : time.sleep(10)
            result = call_api(query)
            st.session_state.test_results.append(result)
            status_icon = "✅" if not result["error"] else "⚠️"
            st.toast(
                f"Finished: {query[:60]}... "
                f"({result['latency']:.2f}s, {result['tokens'].get('total_tokens', 0)} tokens)",
                icon=status_icon
            )
            if result["error"]:
                st.toast(f"Error: {result['error'][:100]}...", icon="⚠️")
        # Show metrics after each test
        metrics = collect_metrics(st.session_state.test_results)
        with output:
            st.markdown("### 📊 Aggregate Metrics (so far)")
            cols = st.columns(len(metrics))
            for col, (label, val) in zip(cols, metrics.items()):
                col.metric(label, val)
            st.divider()

if st.session_state.test_results:
    st.markdown("## 📝 Results History")
    df = pd.DataFrame([{
        "Query": r["query"],
        "Latency (s)": round(r["latency"], 2),
        "Tokens": r["tokens"].get("total_tokens", 0),
        "Status": "✅" if not r["error"] else "❌",
        "Time": r["timestamp"].strftime("%H:%M:%S")
    } for r in st.session_state.test_results])
    st.dataframe(df, use_container_width=True)

    for idx, res in enumerate(st.session_state.test_results):
        with st.expander(f'Query {idx+1} - {res["query"][:60]}{"..." if len(res["query"]) > 60 else ""}'):
            if st.button("Copy Query", key=f"copy_{idx}"):
                st.code(res["query"], language="text")
                st.success("Query copied to clipboard!")
            st.markdown(f"**Session ID:** {res['session_id'] or 'N/A'}")
            st.markdown(f"**Response:**\n\n{res['response'] or 'No response available'}")
            st.markdown("**Token Usage:**")
            st.code(json.dumps(res["tokens"], indent=2), language="json")
            st.markdown(f"**Latency:** {res['latency']:.2f} s")
            if res["requested_fields"]:
                st.markdown("**Fields Used:**")
                st.code(json.dumps(res["requested_fields"], indent=2), language="json")
            if res["node_durations"]:
                st.markdown("**Node Execution Durations (s):**")
                st.code(json.dumps({k: round(v, 3) for k, v in res["node_durations"].items()}, indent=2), language="json")
            if res["trace"]:
                with st.expander("Trace (per node):"):
                    st.json(res["trace"])
            if res["trace_error"]:
                st.error(f"Trace Error: {res['trace_error']}")
            if res["error"]:
                st.error(f"Error: {res['error']}")