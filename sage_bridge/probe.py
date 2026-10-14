"""Probe the Sage 50 COM API on this machine.

Run once before starting bridge.py. It opens the PeachwServer session,
creates a PTOWSalesOrder, and prints every property/method the object
actually exposes — compare the output against FIELD_MAP in
sage_client.py and adjust names that differ.

    python probe.py
"""
import os
from dotenv import load_dotenv
import win32com.client
import pythoncom

load_dotenv()
COMPANY_PATH = os.environ.get('SAGE_COMPANY_PATH', '')

pythoncom.CoInitialize()
server = win32com.client.Dispatch('PeachwServer')
server.Begin('RatechProbe')
company = server.OpenCompany(COMPANY_PATH)

so = company.CreateObject('PTOWSalesOrder')
print('=== PTOWSalesOrder members ===')
for name in sorted(set(dir(so)) - set(dir(object))):
    print(' ', name)

try:
    lines = so.LineItems
    print('=== Line item members ===')
    ln = lines.Add() if hasattr(lines, 'Add') else lines
    for name in sorted(set(dir(ln)) - set(dir(object))):
        print(' ', name)
except Exception as e:
    print('LineItems probe failed:', e)

server.End()
pythoncom.CoUninitialize()
