"""
Clients for the services the dashboard reads from: MeshDB, UISP, Stripe and Invoice Ninja.

Every request has a timeout, and a failed request returns None instead of raising,
so one service being down doesn't take the whole page with it.
"""
import csv
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from django.core.cache import cache
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TIMEOUT = 30

# Installs, ONUs and the support sheet are reused for this long, so clicking around the
# dashboard doesn't download them again every time. The Refresh button skips this.
CACHE_SECONDS = 60

MESHDB_URL = 'https://db.nycmesh.net/api/v1'
UISP_WEB_URL = 'https://10.70.76.21/nms'
UISP_URL = f'{UISP_WEB_URL}/api/v2.1'
STRIPE_URL = 'https://api.stripe.com/v1'
NINJA_URL = 'https://ninja.nycmesh.net/api/v1'

MESHDB_API_KEY = os.environ.get("MESHDB_API_KEY", "")
UISP_API_KEY = os.environ.get("UISP_API_KEY", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
NINJA_API_KEY = os.environ.get("NINJA_API_KEY", "")
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

DISPLAY_TIME = "%B %d, %Y %I:%M %p"

def in_parallel(calls):
    """Run {key: function} at the same time and return {key: result}. Pages wait on several slow services."""
    if not calls:
        return {}
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = {key: pool.submit(function) for key, function in calls.items()}
        return {key: future.result() for key, future in futures.items()}

def _cached(key, fetch, fresh):
    """fetch(), reusing a result from the last CACHE_SECONDS unless fresh is set. Failures aren't cached."""
    if not fresh:
        value = cache.get(key)
        if value is not None:
            return value
    value = fetch()
    if value is not None:
        cache.set(key, value, CACHE_SECONDS)
    return value

def _get(url, **kwargs):
    """GET a JSON resource, or return None if the request fails for any reason."""
    try:
        response = requests.get(url, timeout=TIMEOUT, **kwargs)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as e:
        # Log only the status, not the URL: query strings can contain member emails
        status = e.response.status_code if getattr(e, 'response', None) is not None else type(e).__name__
        log.warning("GET %s failed: %s", url, status)
        return None

# MeshDB

def _meshdb(path, params=None):
    return _get(f"{MESHDB_URL}/{path}", params=params, headers={
        'accept': 'application/json',
        'Authorization': f'Token {MESHDB_API_KEY}'
    })

def fetch_installs(network_number, fresh=False):
    """All installs under a network number, or None if MeshDB fails."""
    def fetch():
        data = _meshdb("installs/lookup/", {'network_number': network_number, 'page_size': 9999})
        return data['results'] if data else None
    return _cached(f"installs:{network_number}", fetch, fresh)

def fetch_member(member_id):
    return _meshdb(f"members/{member_id}/")

def search_members(query):
    """Members matching a name, email or 10-digit phone number, or None if MeshDB fails."""
    query = query.strip()
    if re.fullmatch(r"\d{10}", query):
        params = {'phone_number': f"+1 {query[:3]}-{query[3:6]}-{query[6:]}"}
    elif "@" in query:
        params = {'email_address': query}
    else:
        params = {'name': query}
    data = _meshdb("members/lookup/", {**params, 'page_size': 1000})
    return data['results'] if data else None

# UISP

def fetch_onus_by_parent(fresh=False):
    """{UISP parent (OLT) id: [ONUs under it]} for every ONU in UISP, or None if UISP can't be reached."""
    return _cached("onus", _fetch_onus_by_parent, fresh)

def _fetch_onus_by_parent():
    # One request for every ONU is much faster than one per OLT (UISP answers those one at a time).
    # UISP is on a mesh address with a self-signed certificate.
    data = _get(f"{UISP_URL}/devices", params={'type': 'onu'}, verify=False, headers={
        'accept': 'application/json',
        'x-auth-token': UISP_API_KEY
    })
    if data is None:
        return None

    onus = {}
    for d in data:
        last_seen = d['overview'].get('lastSeen')
        onus.setdefault((d.get('attributes') or {}).get('parentId'), []).append({
            'name': d['identification']['name'],
            # Opens the device's panel in the UISP web interface
            'url': f"{UISP_WEB_URL}/devices#id={d['identification']['id']}&panelType=device-panel",
            'status': d['overview']['status'],
            'signal': d['overview'].get('signal', 'N/A'),
            'lastSeen': datetime.strptime(last_seen, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                .astimezone(ZoneInfo("America/New_York")).strftime(DISPLAY_TIME) if last_seen else 'Never',
            'model': d['identification'].get('model', 'N/A'),
        })
    return onus

# Stripe

def _stripe(path, params=None):
    return _get(f"{STRIPE_URL}/{path}", params=params, headers={
        'Authorization': f'Bearer {STRIPE_API_KEY}',
        'Stripe-Version': '2024-06-20'
    })

def fetch_stripe_customer(email):
    """Return (customer id, delinquent) for the first customer with this email, or (None, None)."""
    data = _stripe("customers/search", {'query': f"email:'{email}'"})
    if data and data['data']:
        customer = data['data'][0]
        return customer['id'], customer['delinquent']
    return None, None

def fetch_stripe_subscription(customer_id):
    data = _stripe("subscriptions", {'customer': customer_id})
    if not data or not data['data']:
        return None

    subscription = data['data'][0]
    invoice = _stripe(f"invoices/{subscription['latest_invoice']}") if subscription.get('latest_invoice') else None
    paid_at = ((invoice or {}).get('status_transitions') or {}).get('paid_at')

    def display(timestamp):
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime(DISPLAY_TIME) if timestamp else None

    return {
        'nickname': subscription['items']['data'][0]['price']['nickname'],
        'status': subscription['status'],
        'current_period_start': display(subscription['current_period_start']),
        'current_period_end': display(subscription['current_period_end']),
        'last_paid': display(paid_at) or 'Not paid'
    }

# Invoice Ninja

def _ninja(path, params):
    return _get(f"{NINJA_URL}/{path}", params=params, headers={'X-API-TOKEN': NINJA_API_KEY})

def fetch_ninja_client(name):
    data = _ninja("clients", {'name': name})
    return data['data'][0] if data and data['data'] else None

def fetch_ninja_invoices(client_id):
    data = _ninja("invoices", {'client_id': client_id, 'client_status': 'unpaid,overdue'})
    if not data:
        return None
    return [{
        'amount': invoice['amount'],
        'date': invoice['date'],
        'reminder1_sent': invoice['reminder1_sent'],
        'reminder2_sent': invoice['reminder2_sent'],
        'reminder3_sent': invoice['reminder3_sent'],
        'next_send_date': invoice['next_send_date']
    } for invoice in data['data']]

# Google Sheets

def fetch_support_sheet(fresh=False):
    """Rows of the published GSG support log (header skipped), or None if it can't be downloaded."""
    return _cached("support-sheet", _fetch_support_sheet, fresh)

def _fetch_support_sheet():
    if not GOOGLE_SHEET_CSV_URL:
        log.warning("GOOGLE_SHEET_CSV_URL is not set")
        return None
    try:
        response = requests.get(GOOGLE_SHEET_CSV_URL, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as e:
        # The URL itself is a secret, so it isn't logged
        log.warning("Support sheet download failed: %s", type(e).__name__)
        return None
    return list(csv.reader(response.content.decode('utf-8').splitlines()))[1:]
