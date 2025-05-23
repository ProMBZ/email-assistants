import os
import base64
import streamlit as st
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from streamlit_autorefresh import st_autorefresh
from langchain_google_genai import ChatGoogleGenerativeAI

# Load environment variables
load_dotenv()

# Streamlit setup
st.set_page_config(page_title="📬 AI Email Assistant", page_icon="📬")
st.title("📬 AI Email Assistant")
st.write("Summarize unread emails, draft smart replies, and send them instantly with Gemini AI.")

# Auto-refresh every 5 mins
st_autorefresh(interval=300000, key="email_checker")

# Gemini LLM
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    api_key=os.getenv("GEMINI_API_KEY")
)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CLIENT_SECRET_FILE = "credentials.json"

# Authentication Step 1: Display auth URL
def get_auth_url():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    auth_url, _ = flow.authorization_url(prompt='consent')
    return auth_url, flow

# Authentication Step 2: Use code to get Gmail service
def build_gmail_service(code, flow):
    flow.fetch_token(code=code)
    creds = flow.credentials
    return build('gmail', 'v1', credentials=creds)

# Gmail functions
def get_unread_emails(service):
    result = service.users().messages().list(userId='me', labelIds=['INBOX'], q='is:unread', maxResults=5).execute()
    messages = result.get('messages', [])[::-1]
    emails = []

    for msg in messages:
        data = service.users().messages().get(userId='me', id=msg['id']).execute()
        headers = data['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
        snippet = data.get('snippet', '')
        thread_id = data.get('threadId')

        # Mark as read
        service.users().messages().modify(userId='me', id=msg['id'], body={'removeLabelIds': ['UNREAD']}).execute()
        emails.append({
            'id': msg['id'],
            'thread_id': thread_id,
            'subject': subject,
            'sender': sender,
            'snippet': snippet
        })

    return emails

@st.cache_data(show_spinner=False)
def summarize_email(snippet):
    prompt = f"Summarize this email in bullet points:\n\n{snippet}"
    response = llm.invoke(prompt)
    return response.content if hasattr(response, 'content') else str(response)

@st.cache_data(show_spinner=False)
def generate_reply(snippet, user_instruction):
    prompt = f"""Write a professional reply to this email based on the user’s instructions.

Email:
{snippet}

Instructions:
{user_instruction}"""
    response = llm.invoke(prompt)
    return response.content if hasattr(response, 'content') else str(response)

def get_or_create_label(service, label_name="Replied"):
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for label in labels:
        if label['name'].lower() == label_name.lower():
            return label['id']
    new_label = service.users().labels().create(userId='me', body={
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show"
    }).execute()
    return new_label['id']

def send_email(service, to, subject, message_text, thread_id=None):
    message = MIMEText(message_text)
    message['to'] = to
    message['subject'] = "Re: " + subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {'raw': raw, 'threadId': thread_id} if thread_id else {'raw': raw}
    sent_message = service.users().messages().send(userId='me', body=body).execute()

    replied_label_id = get_or_create_label(service, "Replied")
    service.users().messages().modify(userId='me', id=sent_message['id'], body={'addLabelIds': [replied_label_id]}).execute()
    return sent_message

# Streamlit logic
if 'flow' not in st.session_state:
    auth_url, flow = get_auth_url()
    st.session_state.flow = flow
    st.markdown(f"🔐 [Click here to authorize Gmail access]({auth_url})")

code = st.text_input("Paste the authorization code from the Gmail redirect:")
service = None

if code:
    try:
        service = build_gmail_service(code, st.session_state.flow)
        st.success("✅ Gmail authorized successfully.")
    except Exception as e:
        st.error(f"Authentication failed: {e}")

if service:
    emails = get_unread_emails(service)
    if not emails:
        st.success("✅ No unread emails right now.")
    else:
        for i, email in enumerate(emails):
            st.divider()
            st.subheader(f"📧 Email #{i+1}: {email['subject']}")
            st.write(f"**From:** {email['sender']}")
            st.write(f"**Snippet:** {email['snippet']}")

            summary = summarize_email(email['snippet'])
            st.success("📌 Summary:")
            st.markdown(summary)

            user_details = st.text_input(f"Your name/role/company (Email #{i+1})", key=f"details_{i}")
            user_instruction = st.text_area(f"Instructions (Email #{i+1})", value="Write a polite and helpful reply.", key=f"instruction_{i}")
            reply = generate_reply(email['snippet'], f"{user_instruction}\nUser details: {user_details}")
            updated_reply = st.text_area("✏️ Edit the reply before sending:", value=reply, key=f"edit_reply_{i}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button(f"✅ Send Reply (Email #{i+1})", key=f"send_{i}"):
                    to_email = email['sender'].split('<')[-1].replace('>', '') if '<' in email['sender'] else email['sender']
                    send_email(service, to_email, email['subject'], updated_reply, thread_id=email['thread_id'])
                    st.success(f"Reply sent to {to_email}")
            with col2:
                if st.button(f"🔁 Refresh Reply (Email #{i+1})", key=f"refresh_{i}"):
                    st.experimental_rerun()
