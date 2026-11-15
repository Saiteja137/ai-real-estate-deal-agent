import html, os, smtplib, ssl
from email.message import EmailMessage
from email.utils import make_msgid
def send_message(recipient, subject, body, *, reply_to=None, html_body=None):
    msg=EmailMessage()
    msg['From']=os.environ['SMTP_FROM']; msg['To']=recipient; msg['Subject']=subject
    msg['Message-ID']=make_msgid()
    if reply_to: msg['Reply-To']=reply_to
    msg.set_content(body)
    msg.add_alternative(html_body if html_body is not None else '<html><body><pre style="white-space:pre-wrap;font-family:sans-serif">'+html.escape(body)+'</pre></body></html>',subtype='html')
    with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.getenv('SMTP_PORT','587')),timeout=20) as smtp:
        smtp.ehlo(); smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
        smtp.login(os.environ['SMTP_USERNAME'],os.environ['SMTP_PASSWORD'])
        refused=smtp.send_message(msg)
        if refused: raise RuntimeError('SMTP refused recipient')
    return 'SMTP_ACCEPTED'
