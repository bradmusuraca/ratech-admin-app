import imaplib
import email
from email.header import decode_header, make_header
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


def list_unseen_orders(host, port, user, password, limit=25) -> List[Dict]:
    """Return unread inbox messages, newest first.

    Read-only: BODY.PEEK fetches headers without setting the \\Seen flag.
    """
    mail = _connect(host, port, user, password)
    try:
        typ, data = mail.uid('search', None, 'UNSEEN')
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
