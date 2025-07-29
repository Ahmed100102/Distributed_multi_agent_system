import streamlit as st
import requests
from datetime import datetime
import uuid

# --- CONFIGURATION ---
API_URL = "http://localhost:8200/chat"

st.set_page_config(
    page_title="Observix Chatbot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- SESSION STATE ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# --- CUSTOM CSS FOR MODERN DESIGN ---
st.markdown("""
    <style>
        body {
            background: #f7f8fa;
        }
        .stChatMessage {
            margin-bottom: 1.2rem;
        }
        .chat-bubble {
            padding: 1rem 1.3rem;
            border-radius: 1.5rem;
            max-width: 70%;
            font-size: 1.09rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            margin-bottom: 0.2rem;
        }
        .chat-bubble.user {
            background: linear-gradient(90deg, #e0f7fa 0%, #b2ebf2 100%);
            color: #222;
            align-self: flex-end;
        }
        .chat-bubble.assistant {
            background: linear-gradient(90deg, #f8f9fa 0%, #e3e7ea 100%);
            color: #222;
            align-self: flex-start;
        }
        .avatar {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            margin-right: 0.7rem;
            background: #fff;
            box-shadow: 0 0 0 2px #e0e0e0;
            object-fit: cover;
        }
        .chat-row {
            display: flex;
            align-items: flex-end;
            margin-bottom: 1.2rem;
        }
        .chat-row.user {
            flex-direction: row-reverse;
        }
        .timestamp {
            font-size: 0.79rem;
            color: #888;
            margin: 2px 8px;
        }
        @media (max-width: 600px) {
            .chat-bubble { max-width: 95%; }
        }
    </style>
""", unsafe_allow_html=True)

# --- SIDEBAR ---
with st.sidebar:
    st.image("https://img.icons8.com/color/96/000000/artificial-intelligence.png", width=72)
    st.markdown("<h2 style='margin-bottom:0;'>Observix Chatbot</h2>", unsafe_allow_html=True)
    st.caption("Your AI-powered Observability Assistant")
    st.markdown("""
    <div style='margin-bottom:1.1rem;'>
        <b>Ask me about:</b>
        <ul>
            <li>System health</li>
            <li>High-severity issues</li>
            <li>Error trends</li>
            <li>Performance metrics</li>
            <li>Or just say hello!</li>
        </ul>
        <b>Examples:</b>
        <ul>
            <li>"Show health for Payments last 3 days"</li>
            <li>"List high-severity issues in VGM last 2 hours"</li>
            <li>"Analyze performance for Gateway last 1 week"</li>
        </ul>
    </div>
    """, unsafe_allow_html=True)
    st.divider()
    st.caption("⚡ Powered by Observix + LLM")

# --- CHAT UI ---
st.title("🦾 Observix AI Assistant")

def render_message(msg, is_user, timestamp=None):
    avatar_url = (
        "https://img.icons8.com/color/48/000000/user-male-circle--v2.png"
        if is_user else
        "https://img.icons8.com/color/48/000000/robot-2--v2.png"
    )
    bubble_class = "user" if is_user else "assistant"
    align = "chat-row user" if is_user else "chat-row"
    role = "You" if is_user else "Observix"
    time_str = f"<span class='timestamp'>{timestamp or datetime.now().strftime('%H:%M')}</span>"

    st.markdown(
        f"""
        <div class="{align}">
            <img src="{avatar_url}" class="avatar" alt="{role}"/>
            <div>
                <div class="chat-bubble {bubble_class}">
                    <b>{role}:</b><br>{msg}
                </div>
                {time_str}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

chat_container = st.container()
with chat_container:
    for msg in st.session_state.messages:
        render_message(
            msg["content"],
            msg["role"] == "user",
            msg.get("timestamp")
        )

# --- API CALL ---
def call_api(query, session_id):
    try:
        resp = requests.post(API_URL, json={"query": query, "session_id": session_id}, timeout=6000)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("response", "No response from assistant.")
        else:
            return f"❌ Error: {resp.status_code} - {resp.text}"
    except Exception as e:
        return f"❌ Connection error: {str(e)}"

# --- USER INPUT ---
user_input = st.chat_input("Type your message and press Enter...")

if user_input:
    now = datetime.now().strftime("%H:%M")
    # Add user message
    st.session_state.messages.append({
        "role": "user",
        "content": user_input,
        "timestamp": now
    })
    with chat_container:
        render_message(user_input, is_user=True, timestamp=now)
        with st.spinner("Observix is thinking..."):
            assistant_response = call_api(user_input, st.session_state.session_id)
            render_message(assistant_response, is_user=False, timestamp=datetime.now().strftime("%H:%M"))
            st.session_state.messages.append({
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now().strftime("%H:%M")
            })

# --- FOOTER / THEME ---
st.markdown("""
    <style>
    footer {visibility: hidden;}
    .stApp {background: #f7f8fa;}
    </style>
""", unsafe_allow_html=True)
