import imaplib
import smtplib
import email
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import List, Dict


def _decode_header_value(value):
    try:
        return str(make_header(decode_header(value or '')))
    except Exception:
        return value or ''


def _connect(host, port, user, password):
    """Open a read-only IMAP connection.

    Selecting INBOX with readonly=True uses EXAMINE mode - the server will
    not change any message flags, so mail cannot be marked read, moved or
    deleted by this app.
    """
    mail = imaplib.IMAP4_SSL(host, int(port))
    mail.login(user, password)
    mail.select('INBOX', readonly=True)
    return mail


def _connect_rw(host, port, user, password):
    """Open a read-write IMAP connection (SELECT) for flag updates only."""
    mail = imaplib.IMAP4_SSL(host, int(port))
    mail.login(user, password)
    mail.select('INBOX')
    return mail


def mark_as_read(host, port, user, password, uid) -> None:
    """Set the \\Seen flag on a single message, by UID.

    Called only after the app has processed (or consciously ignored) the
    message - reads themselves never touch flags.
    """
    mail = _connect_rw(host, port, user, password)
    try:
        mail.uid('store', str(uid), '+FLAGS', '(\\Seen)')
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def list_unseen_orders(host, port, user, password, limit=25, since=None) -> List[Dict]:
    """Return unread inbox messages, newest first.

    Read-only: BODY.PEEK fetches headers without setting the \\Seen flag.
    'since' (e.g. '12-Oct-2026') filters out mail received before that date.
    """
    mail = _connect(host, port, user, password)
    try:
        criteria = ['UNSEEN']
        if since:
            criteria += ['SINCE', since]
        typ, data = mail.uid('search', None, *criteria)
        if typ != 'OK' or not data or not data[0]:
            return []

        uids = data[0].split()[-limit:]
        items = []
        for uid in reversed(uids):
            typ, msg_data = mail.uid(
                'fetch', uid,
                '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])')
            if typ != 'OK' or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            headers = email.message_from_bytes(msg_data[0][1])
            items.append({
                'uid': uid.decode() if isinstance(uid, bytes) else str(uid),
                'subject': _decode_header_value(headers.get('Subject')) or '(no subject)',
                'sender': _decode_header_value(headers.get('From')),
                'date': headers.get('Date', ''),
            })
        return items
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def fetch_pdf_from_message(host, port, user, password, uid) -> List[Dict]:
    """Download PDF attachments from a single message.

    BODY.PEEK[] returns the full message body without marking it read.
    Returns a list of {'filename': str, 'bytes': bytes}.
    """
    mail = _connect(host, port, user, password)
    try:
        typ, data = mail.uid('fetch', str(uid), '(BODY.PEEK[])')
        if typ != 'OK' or not data or not isinstance(data[0], tuple):
            return []

        msg = email.message_from_bytes(data[0][1])
        pdfs = []
        for part in msg.walk():
            fname = part.get_filename()
            ctype = part.get_content_type()
            if ctype == 'application/pdf' or (fname and fname.lower().endswith('.pdf')):
                payload = part.get_payload(decode=True)
                if payload:
                    pdfs.append({
                        'filename': _decode_header_value(fname) or 'attachment.pdf',
                        'bytes': payload
                    })
        return pdfs
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def fetch_body_from_message(host, port, user, password, uid) -> str:
    """Return the plain-text body of a message (read-only BODY.PEEK).

    Prefers text/plain parts; falls back to stripped text/html.
    """
    mail = _connect(host, port, user, password)
    try:
        typ, data = mail.uid('fetch', str(uid), '(BODY.PEEK[])')
        if typ != 'OK' or not data or not isinstance(data[0], tuple):
            return ''

        msg = email.message_from_bytes(data[0][1])
        text_parts = []
        html_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition') or '')
            if 'attachment' in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or 'utf-8'
            if ctype == 'text/plain':
                text_parts.append(payload.decode(charset, errors='replace'))
            elif ctype == 'text/html':
                html_parts.append(payload.decode(charset, errors='replace'))

        if text_parts:
            return '\n'.join(text_parts)
        if html_parts:
            import re as _re
            html = '\n'.join(html_parts)
            html = _re.sub(r'(?i)<br\s*/?>', '\n', html)
            html = _re.sub(r'(?i)</(?:p|div|tr|li)>', '\n', html)
            html = _re.sub(r'<[^>]+>', '', html)
            from html import unescape
            return unescape(html)
        return ''
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def extract_email_address(sender_header: str) -> str:
    """Pull a bare email address out of a 'Name <addr>' From header."""
    return parseaddr(sender_header or '')[1]


def send_email(smtp_host, smtp_port, user, password, to_addr, subject, body):
    """Send a plain-text email via SMTP SSL (e.g. Yahoo: smtp.mail.yahoo.com:465)."""
    msg = EmailMessage()
    msg['From'] = user
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(smtp_host, int(smtp_port)) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
