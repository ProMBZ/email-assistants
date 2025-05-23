import os
import base64
import streamlit as st
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from email.mime.text import MIMEText
from streamlit_autorefresh import st_autorefresh
from langchain_google_genai import ChatGoogleGenerativeAI

# Load environment variables
load_dotenv()

# Page config
st.set_page_config(page_title="AI Email Assistant", page_icon="📬")
st.title("📬 AI Email Assistant")
st.write("Summarize unread emails, draft smart replies, and send them instantly with Gemini AI.")

# Auto-refresh every 5 minutes (300,000 ms)
st_autorefresh(interval=300_000, key="email_checker")

# Initialize Gemini AI client
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    api_key=os.getenv("GEMINI_API_KEY")
)

# Gmail API scopes and config
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CLIENT_SECRETS_FILE = "credentials.json"
REDIRECT_URI = "https://mbz-email-assistant.streamlit.app"  # Must exactly match your OAuth client settings


def get_gmail_service():
    """Handles OAuth and returns authenticated Gmail API service."""

    creds = st.session_state.get('creds')

    # Refresh token if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            st.session_state.creds = creds  # update stored creds
        except Exception as e:
            st.warning(f"Token refresh failed, re-authentication required: {e}")
            creds = None

    if not creds:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )

        query_params = st.experimental_get_query_params()

        if "code" in query_params:
            code = query_params["code"][0]
            try:
                flow.fetch_token(code=code)
                creds = flow.credentials
                st.session_state.creds = creds
                # Clear URL params to avoid reusing the code
                st.experimental_set_query_params()
            except Exception as e:
                st.error(f"❌ Authentication error: {e}")
                st.stop()
        else:
            auth_url, _ = flow.authorization_url(
                prompt="consent",
                access_type="offline",
                include_granted_scopes="true"
            )
            st.markdown(f"🔐 [Click here to authorize Gmail access]({auth_url})")
            st.stop()

    return build('gmail', 'v1', credentials=st.session_state.creds)


def get_unread_emails(service, max_results=5):
    """Fetch unread emails from the inbox."""
    try:
        response = service.users().messages().list(
            userId='me', labelIds=['INBOX'], q='is:unread', maxResults=max_results
        ).execute()

        messages = response.get('messages', [])[::-1]  # oldest first
    except Exception as e:
        st.error(f"Error fetching emails: {e}")
        return []

    emails = []
    for msg in messages:
        try:
            data = service.users().messages().get(userId='me', id=msg['id']).execute()
            headers = data['payload']['headers']
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
            snippet = data.get('snippet', '')
            thread_id = data.get('threadId')

            # Mark as read right after fetching to avoid duplicate processing
            service.users().messages().modify(
                userId='me',
                id=msg['id'],
                body={'removeLabelIds': ['UNREAD']}
            ).execute()

            emails.append({
                'id': msg['id'],
                'thread_id': thread_id,
                'subject': subject,
                'sender': sender,
                'snippet': snippet
            })
        except Exception as e:
            st.warning(f"Failed to process message {msg['id']}: {e}")

    return emails


@st.cache_data(show_spinner=False)
def summarize_email(snippet):
    """Use Gemini AI to summarize email snippet in bullet points."""
    prompt = f"Summarize this email in bullet points:\n\n{snippet}"
    response = llm.invoke(prompt)
    return response.content if hasattr(response, 'content') else str(response)


@st.cache_data(show_spinner=False)
def generate_reply(snippet, user_instruction):
    """Generate a professional reply based on user instructions and email snippet."""
    prompt = f"""Write a professional reply to this email based on the user’s instructions.

Email:
{snippet}

Instructions:
{user_instruction}"""
    response = llm.invoke(prompt)
    return response.content if hasattr(response, 'content') else str(response)


def get_or_create_label(service, label_name="Replied"):
    """Get Gmail label ID or create it if not found."""
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
    """Send email reply and add 'Replied' label."""
    message = MIMEText(message_text)
    message['to'] = to
    message['subject'] = "Re: " + subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    body = {'raw': raw}
    if thread_id:
        body['threadId'] = thread_id

    sent_message = service.users().messages().send(userId='me', body=body).execute()

    # Tag message with "Replied" label
    replied_label_id = get_or_create_label(service, "Replied")
    service.users().messages().modify(userId='me', id=sent_message['id'], body={'addLabelIds': [replied_label_id]}).execute()
    return sent_message


# Initialize session variables if missing
if 'last_checked_count' not in st.session_state:
    st.session_state['last_checked_count'] = 0

try:
    service = get_gmail_service()
    if service:
        unread_emails = get_unread_emails(service)
        current_count = len(unread_emails)

        # Notify user on new emails
        if current_count > st.session_state['last_checked_count']:
            st.toast("📬 New email received!")

        st.session_state['emails'] = unread_emails
        st.session_state['last_checked_count'] = current_count

except Exception as e:
    st.error(f"❌ Authentication or Gmail error: {e}")

emails = st.session_state.get('emails', [])
if not emails:
    st.success("✅ No unread emails right now.")
else:
    for i, email in enumerate(emails):
        st.divider()
        st.subheader(f"📧 Email #{i+1}: {email['subject']}")
        st.write(f"**From:** {email['sender']}")
        st.write(f"**Snippet:** {email['snippet']}")

        # Summarize email
        if f"summary_{i}" not in st.session_state:
            st.session_state[f"summary_{i}"] = summarize_email(email['snippet'])
        st.success("📌 Summary:")
        st.markdown(st.session_state[f"summary_{i}"])

        # Input for user details (name, role, company)
        user_details = st.text_input(f"Your name/role/company (Email #{i+1})", key=f"details_{i}")

        # User instructions for reply generation
        user_instruction = st.text_area(
            f"Instructions for Gemini (Email #{i+1})",
            value="Write a polite and helpful reply.",
            key=f"instruction_{i}"
        )

        # Generate reply only once per email unless refreshed
        if f"reply_{i}" not in st.session_state:
            prompt = f"{user_instruction}\n\nUser details: {user_details}"
            st.session_state[f"reply_{i}"] = generate_reply(email['snippet'], prompt)

        # Editable reply box
        updated_reply = st.text_area(
            "📝 Edit the reply if needed before sending:",
            value=st.session_state[f"reply_{i}"],
            height=200,
            key=f"replybox_{i}"
        )

        col1, col2, col3 = st.columns([1, 1, 1])

        with col1:
            if st.button(f"✅ Send Reply (Email #{i+1})", key=f"send_{i}"):
                to_email = email['sender'].split('<')[-1].replace('>', '') if '<' in email['sender'] else email['sender']
                send_email(service, to_email, email['subject'], updated_reply, thread_id=email['thread_id'])
                st.success(f"✅ Reply sent to {to_email}")
                st.session_state[f"sent_{i}"] = True

        with col2:
            if st.button(f"⏭️ Skip Email #{i+1}", key=f"skip_{i}"):
                st.info(f"⏭️ Skipped Email #{i+1}")

        with col3:
            if st.button(f"🔄 Refresh Reply (Email #{i+1})", key=f"refresh_{i}"):
                st.session_state[f"summary_{i}"] = summarize_email(email['snippet'])
                prompt = f"{user_instruction}\n\nUser details: {user_details}"
                st.session_state[f"reply_{i}"] = generate_reply(email['snippet'], prompt)
                st.success("🔁 Reply regenerated.")
