"""SMTP sending with bounded retries only when no recipient accepted the message."""
import html
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import make_msgid

log = logging.getLogger(__name__)
MAX_SEND_RETRIES = 3


class EmailNotSent(Exception):
    """SMTP definitively did not accept the message."""


def _temporary_setup_error(exc):
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (OSError, smtplib.SMTPServerDisconnected)) and not isinstance(exc, ssl.SSLCertVerificationError)


def _temporary_rejection(exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return bool(exc.recipients) and all(400 <= code < 500 for code, _ in exc.recipients.values())
    return isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError)) and 400 <= exc.smtp_code < 500


def send_message(recipient, subject, body, *, reply_to=None, html_body=None):
    msg=EmailMessage()
    msg['From']=os.environ['SMTP_FROM']; msg['To']=recipient; msg['Subject']=subject
    msg['Message-ID']=make_msgid()
    if reply_to: msg['Reply-To']=reply_to
    msg.set_content(body)
    msg.add_alternative(html_body if html_body is not None else '<html><body><pre style="white-space:pre-wrap;font-family:sans-serif">'+html.escape(body)+'</pre></body></html>',subtype='html')

    for attempt in range(MAX_SEND_RETRIES + 1):
        smtp = None
        try:
            try:
                smtp=smtplib.SMTP(os.environ['SMTP_HOST'],int(os.getenv('SMTP_PORT','587')),timeout=20)
                smtp.ehlo(); smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
                smtp.login(os.environ['SMTP_USERNAME'],os.environ['SMTP_PASSWORD'])
            except (OSError, smtplib.SMTPException) as exc:
                if not _temporary_setup_error(exc) or attempt == MAX_SEND_RETRIES:
                    raise EmailNotSent('SMTP connection or authentication failed before submission') from exc
            else:
                try:
                    refused=smtp.send_message(msg)
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                    if not _temporary_rejection(exc) or attempt == MAX_SEND_RETRIES:
                        raise EmailNotSent('SMTP rejected the message') from exc
                else:
                    if refused:
                        # Some recipients may have accepted the message; never retry it.
                        raise RuntimeError('SMTP partially refused recipients; delivery is uncertain')
                    return 'SMTP_ACCEPTED'
        finally:
            if smtp is not None:
                try: smtp.close()
                except OSError: pass
        log.warning('SMTP message was not accepted; retry %s of %s', attempt + 1, MAX_SEND_RETRIES)
        time.sleep(2 ** attempt)
