import os
from dotenv import load_dotenv
load_dotenv()
def flag(name, default=False):
    value=os.getenv(name, 'true' if default else 'false').strip().lower()
    if value not in ('true','false'): raise ValueError(f'{name} must be true or false')
    return value == 'true'
HITL_EMAIL_ENABLED=flag('HITL_EMAIL_ENABLED')
LIVE_EMAIL_INGESTION_ENABLED=flag('LIVE_EMAIL_INGESTION_ENABLED')
AI_ENABLED=flag('AI_ENABLED')
SECOND_DRAFT_APPROVAL_ENABLED=flag('SECOND_DRAFT_APPROVAL_ENABLED',True)
if AI_ENABLED and not os.getenv('OPENAI_API_KEY','').strip():
    raise RuntimeError('AI_ENABLED=true requires OPENAI_API_KEY')
if HITL_EMAIL_ENABLED:
    needed=['SMTP_HOST','SMTP_USERNAME','SMTP_PASSWORD','SMTP_FROM','IMAP_HOST','IMAP_USERNAME','IMAP_PASSWORD','APPROVER_EMAILS','APPROVAL_REPLY_TO']
    missing=[k for k in needed if not os.getenv(k)]
    if missing: raise RuntimeError('Missing live email configuration: '+', '.join(missing))
DATABASE_URL = os.getenv('DATABASE_URL','sqlite:///./data/deals.db')
APPROVAL_TIMEOUT_MINUTES = int(os.getenv('APPROVAL_TIMEOUT_MINUTES','20'))
if not 1 <= APPROVAL_TIMEOUT_MINUTES <= 1440: raise ValueError('APPROVAL_TIMEOUT_MINUTES must be between 1 and 1440')
if LIVE_EMAIL_INGESTION_ENABLED:
    missing=[k for k in ('IMAP_HOST','IMAP_USERNAME','IMAP_PASSWORD') if not os.getenv(k)]
    if missing: raise RuntimeError('Missing live ingestion configuration: '+', '.join(missing))
if not DATABASE_URL.startswith('sqlite:///'): raise RuntimeError('Only SQLite database URLs are supported')
DEFAULT_CRITERIA = {'max_price': 200000, 'min_arv': 150000, 'max_repairs': 60000, 'max_ratio': 0.70, 'min_spread': 30000, 'property_type': 'single-family residential', 'strategy': 'fix and flip'}
