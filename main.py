import os
import pickle
import base64
from email.mime.text import MIMEText
import time
import random

import streamlit as st
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Constants ---
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_PICKLE = 'token.pickle'
CREDENTIALS_FILE = 'credentials.json'

# --- Streamlit UI setup ---
st.set_page_config(page_title="📬 AI Email Assistant", page_icon="📬")
st.title("📬 AI Email Assistant")
st.write("Summarize unread emails, draft smart replies, and send them instantly with Gemini AI.")

# --- Session state initialization ---
for key in ['creds', 'service', 'auth_url', 'flow', 'auth_started']:
    if key not in st.session_state:
        st.session_state[key] = None if key != 'auth_started' else False

# --- Gemini LLM setup ---
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    api_key=os.getenv("GEMINI_API_KEY")
)

# --- Retry decorator with exponential backoff ---
def retry_with_exponential_backoff(max_retries=5, base_delay=15, max_delay=60):
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise  # No retries left, re-raise the error
                    wait = delay + random.uniform(0, delay * 0.1)  # Add jitter
                    wait = min(wait, max_delay)
                    print(f"[Retry {attempt + 1}] Error: {e}. Retrying in {wait:.1f} seconds...")
                    time.sleep(wait)
                    delay *= 2  # Exponential backoff
        return wrapper
    return decorator

# --- Helper Functions with retry ---

@retry_with_exponential_backoff(max_retries=5, base_delay=15)
def get_unread_emails(service):
    results = service.users().messages().list(userId='me', labelIds=['UNREAD'], maxResults=10).execute()
    messages = results.get('messages', [])
    emails = []
    for message in messages:
        msg = service.users().messages().get(userId='me', id=message['id'], format='full').execute()
        headers = msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "(No Subject)")
        sender = next((h['value'] for h in headers if h['name'] == 'From'), "(Unknown Sender)")
        snippet = msg.get('snippet', '')
        thread_id = msg.get('threadId')
        emails.append({'id': message['id'], 'thread_id': thread_id, 'subject': subject, 'sender': sender, 'snippet': snippet})
    return emails

@retry_with_exponential_backoff(max_retries=3, base_delay=10)
def summarize_email(snippet):
    prompt = f"Summarize this email snippet in 2 sentences:\n\n{snippet}"
    response = llm.invoke(prompt)
    return response.text.strip()

@retry_with_exponential_backoff(max_retries=3, base_delay=10)
def generate_reply(snippet, instruction):
    prompt = f"{instruction}\n\nEmail snippet:\n{snippet}\n\nWrite a professional reply:"
    response = llm.invoke(prompt)
    return response.text.strip()

@retry_with_exponential_backoff(max_retries=5, base_delay=15)
def get_or_create_label(service, label_name):
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for label in labels:
        if label['name'] == label_name:
            return label['id']
    new_label = {
        'name': label_name,
        'labelListVisibility': 'labelShow',
        'messageListVisibility': 'show'
    }
    return service.users().labels().create(userId='me', body=new_label).execute()['id']

@retry_with_exponential_backoff(max_retries=5, base_delay=15)
def send_email(service, to, subject, body, thread_id=None):
    message = MIMEText(body)
    message['to'] = to
    message['subject'] = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    msg_body = {'raw': raw}
    if thread_id:
        msg_body['threadId'] = thread_id

    service.users().messages().send(userId='me', body=msg_body).execute()

    if thread_id:
        # Mark thread as read and add "Replied" label
        service.users().threads().modify(userId='me', id=thread_id, body={'removeLabelIds': ['UNREAD']}).execute()
        label_id = get_or_create_label(service, "Replied")
        service.users().threads().modify(userId='me', id=thread_id, body={'addLabelIds': [label_id]}).execute()

# --- Credentials handling ---

def save_creds(creds):
    with open(TOKEN_PICKLE, 'wb') as token_file:
        pickle.dump(creds, token_file)

def load_creds():
    if os.path.exists(TOKEN_PICKLE):
        with open(TOKEN_PICKLE, 'rb') as token_file:
            return pickle.load(token_file)
    return None

def build_service(creds):
    return build('gmail', 'v1', credentials=creds)

def creds_valid(creds):
    return creds and creds.valid

def authenticate_manual():
    try:
        if st.session_state.flow is None:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES,
                redirect_uri='urn:ietf:wg:oauth:2.0:oob'  # for Streamlit local testing
            )
            auth_url, _ = flow.authorization_url(prompt='consent')
            st.session_state.flow = flow
            st.session_state.auth_url = auth_url

        st.info("1️⃣ Click the link below to authorize Gmail access:")
        st.markdown(f"[Authorize Gmail Access]({st.session_state.auth_url})", unsafe_allow_html=True)
        code = st.text_input("Paste authorization code here:")

        if code:
            st.session_state.flow.fetch_token(code=code)
            creds = st.session_state.flow.credentials
            save_creds(creds)
            st.session_state.creds = creds
            st.session_state.service = build_service(creds)
            st.success("✅ Gmail connected successfully!")
            st.experimental_rerun()
    except Exception as e:
        st.error(f"Authentication failed: {e}")

# --- Load or Refresh Credentials ---

creds = load_creds()
if creds_valid(creds):
    st.session_state.creds = creds
    st.session_state.service = build_service(creds)
elif creds and creds.expired and creds.refresh_token:
    creds.refresh(Request())
    save_creds(creds)
    st.session_state.creds = creds
    st.session_state.service = build_service(creds)

# --- Gmail Connection UI ---
if st.session_state.creds is None:
    if st.button("🔗 Connect Gmail"):
        st.session_state.auth_started = True
        st.experimental_rerun()

if st.session_state.auth_started:
    authenticate_manual()

# --- Main App UI ---

if st.session_state.creds:
    service = st.session_state.service
    emails = get_unread_emails(service)
    if emails:
        st.success(f"You have {len(emails)} unread email(s).")
        for i, email in enumerate(emails):
            st.divider()
            st.subheader(f"📧 Email #{i+1}: {email['subject']}")
            st.write(f"**From:** {email['sender']}")
            st.write(f"**Snippet:** {email['snippet']}")

            summary_key = f"summary_{email['id']}"
            if summary_key not in st.session_state:
                st.session_state[summary_key] = summarize_email(email['snippet'])
            st.markdown(f"**Summary:** {st.session_state[summary_key]}")

            user_details = st.text_input(f"Your name or instruction for Email #{i+1}", key=f"detail_{email['id']}")
            instruction = st.text_area(f"Reply instructions for Email #{i+1}", value="Write a polite and relevant reply.", key=f"instruction_{email['id']}")
            reply_key = f"reply_{email['id']}"

            if reply_key not in st.session_state or st.session_state[reply_key] == "Error generating reply.":
                prompt_text = f"{instruction}\n\nDetails: {user_details}"
                st.session_state[reply_key] = generate_reply(email['snippet'], prompt_text)

            reply_box = st.text_area("Generated reply:", value=st.session_state[reply_key], key=f"replybox_{email['id']}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"Regenerate Reply #{i+1}", key=f"regen_{email['id']}"):
                    prompt_text = f"{instruction}\n\nDetails: {user_details}"
                    st.session_state[reply_key] = generate_reply(email['snippet'], prompt_text)
                    st.experimental_rerun()
            with col2:
                if st.button(f"Send Reply #{i+1}", key=f"send_{email['id']}"):
                    try:
                        send_email(service, email['sender'], email['subject'], reply_box, thread_id=email['thread_id'])
                        st.success(f"✅ Reply sent to {email['sender']}")
                        # Remove the email from the list by marking as read handled in send_email
                        # Refresh page to update
                        st.experimental_rerun()
                    except Exception as e:
                        st.error(f"Failed to send email: {e}")
    else:
        st.info("No unread emails found!")

else:
    st.info("Please connect your Gmail account to continue.")

