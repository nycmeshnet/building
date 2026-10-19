"""
Clients for the services the dashboard reads from: MeshDB, UISP, Stripe and Invoice Ninja.

Every request has a timeout, and a failed request returns None instead of raising,
so one service being down doesn't take the whole page with it.
"""
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TIMEOUT = 30

MESHDB_URL = 'https://db.nycmesh.net/api/v1'
UISP_URL = 'https://10.70.76.21/nms/api/v2.1'
STRIPE_URL = 'https://api.stripe.com/v1'
NINJA_URL = 'https://ninja.nycmesh.net/api/v1'

MESHDB_API_KEY = os.environ.get("MESHDB_API_KEY", "")
UISP_API_KEY = os.environ.get("UISP_API_KEY", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
NINJA_API_KEY = os.environ.get("NINJA_API_KEY", "")

DISPLAY_TIME = "%B %d, %Y %I:%M %p"

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

def fetch_installs(network_number):
    """All installs under a network number, or None if MeshDB fails."""
    data = _meshdb("installs/lookup/", {'network_number': network_number, 'page_size': 9999})
    return data['results'] if data else None

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

def fetch_onus(parent_id):
    """ONUs under a UISP parent device, or None if UISP can't be reached."""
    # UISP is on a mesh address with a self-signed certificate
    data = _get(f"{UISP_URL}/devices/onus", params={'parentId': parent_id}, verify=False, headers={
        'accept': 'application/json',
        'x-auth-token': UISP_API_KEY
    })
    if data is None:
        return None

    onus = []
    for d in data:
        last_seen = d['overview'].get('lastSeen')
        onus.append({
            'name': d['identification']['name'],
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
