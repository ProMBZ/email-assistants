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

# Streamlit page setup
st.set_page_config(page_title="📬 AI Email Assistant", page_icon="📬")
st.title("📬 AI Email Assistant")
st.write("Summarize unread emails, draft smart replies, and send them instantly with Gemini AI.")

# Auto-refresh every 5 minutes
st_autorefresh(interval=300000, key="email_checker")

# Initialize Gemini LLM
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    api_key=os.getenv("GEMINI_API_KEY")
)

# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# --- AUTH FLOW OUTSIDE CACHE ---

if 'credentials' not in st.session_state:
    st.session_state['credentials'] = None

if st.session_state['credentials'] is None:
    # Step 1: Generate authorization URL
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    auth_url, _ = flow.authorization_url(prompt='consent')

    st.markdown(f"🔐 [Click here to authorize Gmail access]({auth_url})")

    # Step 2: User inputs the authorization code
    auth_code = st.text_input("Paste the authorization code here:")

    if auth_code:
        try:
            flow.fetch_token(code=auth_code)
            creds = flow.credentials
            st.session_state['credentials'] = creds
            st.experimental_rerun()  # Reload app with credentials saved
        except Exception as e:
            st.error(f"❌ Error during token exchange: {e}")

# Use cached resource to build the Gmail service client with valid credentials
@st.cache_resource(show_spinner=False)
def get_gmail_service(credentials):
    return build('gmail', 'v1', credentials=credentials)

if st.session_state['credentials']:
    service = get_gmail_service(st.session_state['credentials'])

    # --- Gmail helper functions ---

    def get_unread_emails(service):
        result = service.users().messages().list(userId='me', labelIds=['INBOX'], q='is:unread', maxResults=5).execute()
        messages = result.get('messages', [])
        emails = []
        for msg in messages[::-1]:
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

    # Fetch emails and interface
    emails = get_unread_emails(service)

    if not emails:
        st.success("✅ No unread emails right now.")
    else:
        for i, email in enumerate(emails):
            st.divider()
            st.subheader(f"📧 Email #{i+1}: {email['subject']}")
            st.write(f"**From:** {email['sender']}")
            st.write(f"**Snippet:** {email['snippet']}")

            if f"summary_{i}" not in st.session_state:
                st.session_state[f"summary_{i}"] = summarize_email(email['snippet'])

            st.success("📌 Summary:")
            st.markdown(st.session_state[f"summary_{i}"])

            user_details = st.text_input(f"Your name/role/company (for Email #{i+1})", key=f"details_{i}")
            user_instruction = st.text_area(
                f"Instructions for Gemini (Email #{i+1})",
                value="Write a polite and helpful reply.",
                key=f"instruction_{i}"
            )

            if f"reply_{i}" not in st.session_state:
                prompt = f"{user_instruction}\n\nUser details: {user_details}"
                st.session_state[f"reply_{i}"] = generate_reply(email['snippet'], prompt)

            updated_reply = st.text_area(
                "📝 Edit the reply if needed before sending:",
                value=st.session_state[f"reply_{i}"],
                height=200,
                key=f"replybox_{i}"
            )

            col1, col2, col3 = st.columns([1, 1, 1])
            with col1:
                if st.button(f"✅ Send This Reply (Email #{i+1})", key=f"send_{i}"):
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
else:
    st.info("Please authenticate with Gmail to continue.")
