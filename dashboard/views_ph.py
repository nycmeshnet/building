import requests
import re
import sys
import os
import csv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .forms import LookupForm, ReportForm
from . import buildings
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

API_URL = 'https://db.nycmesh.net/api/v1/members/lookup/'
INSTALL_API_URL = 'https://db.nycmesh.net/api/v1/installs/'
MEMBER_API_URL = 'https://db.nycmesh.net/api/v1/members/'
DEVICE_API_BASE_URL = 'https://10.70.76.21/nms/api/v2.1/devices/onus?parentId='
STRIPE_CUSTOMER_API_URL = 'https://api.stripe.com/v1/customers/search'
STRIPE_SUBSCRIPTION_API_URL = 'https://api.stripe.com/v1/subscriptions'
NINJA_API_URL = 'https://ninja.nycmesh.net/api/v1/clients'
NINJA_INVOICE_API_URL = 'https://ninja.nycmesh.net/api/v1/invoices'
MESHDB_API_KEY = os.environ.get("MESHDB_API_KEY","")
UISP_API_KEY = os.environ.get("UISP_API_KEY", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
NINJA_API_KEY = os.environ.get("NINJA_API_KEY", "")

headers = {
    'accept': 'application/json',
    'Authorization': f'Token {MESHDB_API_KEY}'
}

# Derived from buildings.py until this view is merged with the GSG one
PH_BUILDING = buildings.in_group("ph")[0]
ALLOWED_NETWORK_NUMBER = PH_BUILDING.network_number
DEVICE_PARENT_ID = PH_BUILDING.uisp_parent_id
FLOORS = PH_BUILDING.floors
APARTMENTS = PH_BUILDING.unit_letters

def normalize_phone_number(phone):
    phone = phone.strip()
    if len(phone) == 10:
        return f"+1 {phone[:3]}-{phone[3:6]}-{phone[6:]}"
    return phone

def get_install_unit_and_network_number(install_id, headers):
    response = requests.get(f"{INSTALL_API_URL}{install_id}/", headers=headers)
    if response.status_code == 200:
        data = response.json()
        return data.get('unit'), (data.get('node') or {}).get('network_number')
    else:
        return None, None

def fetch_stripe_customer(email):
    response = requests.get(
        STRIPE_CUSTOMER_API_URL,
        headers={
            'Authorization': f'Bearer {STRIPE_API_KEY}',
            'Stripe-Version': '2024-06-20'
        },
        params={'query': f"email:'{email}'"}
    )
    if response.status_code == 200:
        data = response.json()
        if data['data']:
            customer = data['data'][0]
            return customer['id'], customer['delinquent']
    return None, None

def fetch_stripe_subscription(customer_id):
    response = requests.get(
        STRIPE_SUBSCRIPTION_API_URL,
        headers={
            'Authorization': f'Bearer {STRIPE_API_KEY}',
            'Stripe-Version': '2024-06-20'
        },
        params={'customer': customer_id}
    )
    if response.status_code == 200:
        data = response.json()
        if data['data']:
            subscription = data['data'][0]
            nickname = subscription['items']['data'][0]['price']['nickname']
            status = subscription['status']
            latest_invoice_id = subscription['latest_invoice']
            invoice_response = requests.get(f'https://api.stripe.com/v1/invoices/{latest_invoice_id}', headers={'Authorization': f'Bearer {STRIPE_API_KEY}'})
            invoice_data = invoice_response.json()
            current_period_start = datetime.utcfromtimestamp(subscription['current_period_start']).strftime("%B %d, %Y %I:%M %p")
            current_period_end = datetime.utcfromtimestamp(subscription['current_period_end']).strftime("%B %d, %Y %I:%M %p")
            last_paid = datetime.utcfromtimestamp(invoice_data['status_transitions']['paid_at']).strftime("%B %d, %Y %I:%M %p")
            return {
                'nickname': nickname,
                'status': status,
                'current_period_start': current_period_start,
                'current_period_end': current_period_end,
                'last_paid': last_paid
            }
    return None

def fetch_ninja_client(building_apt):
    response = requests.get(
        NINJA_API_URL,
        headers={'X-API-TOKEN': NINJA_API_KEY},
        params={'name': building_apt}
    )

    full_url = response.url
    print(f"Ninja Client URL: {full_url}", file=sys.stderr)

    if response.status_code == 200:
        data = response.json()
        if data['data']:
            return data['data'][0]
    return None

def fetch_ninja_invoices(client_id):
    response = requests.get(
        NINJA_INVOICE_API_URL,
        headers={'X-API-TOKEN': NINJA_API_KEY},
        params={'client_id': client_id, 'client_status': 'unpaid,overdue'}
    )

    full_url = response.url
    print(f"Ninja Invoice URL: {full_url}", file=sys.stderr)

    if response.status_code == 200:
        data = response.json()
        if data['data']:
            invoices = []
            for invoice in data['data']:
                invoices.append({
                    'amount': invoice['amount'],
                    'date': invoice['date'],
                    'reminder1_sent': invoice['reminder1_sent'],
                    'reminder2_sent': invoice['reminder2_sent'],
                    'reminder3_sent': invoice['reminder3_sent'],
                    'next_send_date': invoice['next_send_date']
                })
            return invoices
    return None

def fetch_uisp_info():
    devices = []

    headers = {
        'accept': 'application/json',
        'x-auth-token': f'{UISP_API_KEY}'
    }
    url = f"{DEVICE_API_BASE_URL}{DEVICE_PARENT_ID}"
    response = requests.get(url, headers=headers, verify=False)

    full_url = response.url
    print(f"UISP URL: {full_url}", file=sys.stderr)

    if response.status_code == 200:
        device_data = response.json()
    
        if device_data:
            for d in device_data:
                ssid1 = 'N/A'
                ssid2 = 'N/A'
                password1 = 'N/A'
                password2 = 'N/A'
    
                for iface in d['interfaces']:
                    if iface['identification']['name'] == 'wlan0':
                        ssid1 = iface['wireless']['ssid']
                        password1 = iface['wireless']['key']
                    elif iface['identification']['name'] == 'wlan1':
                        ssid2 = iface['wireless']['ssid']
                        password2 = iface['wireless']['key']
    
                device_info = {
                    'name': d['identification']['name'],
                    'status': d['overview']['status'],
                    'signal': d['overview'].get('signal', 'N/A'),
                    'lastSeen': datetime.strptime(
                        d['overview']['lastSeen'], "%Y-%m-%dT%H:%M:%S.%fZ"
                    ).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y %I:%M %p"),
                    'model': d['identification'].get('model', 'N/A'),
                }
                devices.append(device_info)
    return devices

def fetch_subscription_info(selected_member_info):
    try:
        install = selected_member_info['installs'][0]
    except:
        install = selected_member_info
        #raise Exception(install['member']['id'])
        response = requests.get(f"{MEMBER_API_URL}/{install['member']['id']}", headers=headers)
        if response.status_code == 200:
            selected_member_info = response.json()
    unit, network_number = get_install_unit_and_network_number(install['id'], headers)

    # Use stripe_email_address if it exists; otherwise, fallback to primary_email_address
    stripe_email = selected_member_info.get('stripe_email_address')
    email = stripe_email if stripe_email else selected_member_info['primary_email_address']

    building_apt = "3461-" + unit
    ninja_client = fetch_ninja_client(building_apt) if unit else None
    if ninja_client:
        ninja_invoices = fetch_ninja_invoices(ninja_client['id'])
        selected_member_info['ninja_invoices'] = ninja_invoices

    customer_id, delinquent = fetch_stripe_customer(email)
    selected_member_info['delinquent'] = delinquent

    subscription_info = None
    if customer_id:
        subscription_info = fetch_stripe_subscription(customer_id)
    
    return subscription_info

def fetch_all_units(onus):
    uisp_message = ""
    response = requests.get(f"{INSTALL_API_URL}/lookup/?network_number={ALLOWED_NETWORK_NUMBER}&page_size=9999", headers=headers)
    if response.status_code == 200:
        data = response.json()
        units = {}
        for floor in FLOORS:
            for unit_letter in APARTMENTS:
                unit = str(floor)+unit_letter
                units[unit] = {"name": unit, "floor": floor, "install": None}
        options = {}
        for install in data['results']:
            unit = units.get(install["unit"])
            if unit:
                if unit["install"]:
                    if unit["install"]["status"] != "Request Received":
                        if unit["install"]["status"] != "Active":
                            unit["install"] = install
                        else:
                            if len(onus) == 0:
                                    uisp_message = f"<br>UISP is down. No ONU statuses will be displayed."
                            else:
                                unit["onu"] = "none"
                                for onu in onus:
                                    if onu['name'].endswith("-" + install['unit']) or onu['name'].endswith("-0" + install['unit']):
                                        unit["onu"] = onu['status']
                else:
                    unit["install"] = install
        floors = {}
        for unit in units.values():
            floor = unit["floor"]
            if not floor in floors:
                floors[floor] = []
            floors[floor].append(unit)
        return floors, uisp_message
    else:
        return None

@login_required
def index(request):
    results = None
    error_message = None
    selected_member_info = None
    device_info = None
    subscription_info = None
    onus = fetch_uisp_info()
    all_units, uisp_message = fetch_all_units(onus)

    if request.method == 'POST':
        form = LookupForm(request.POST, results=request.session.get('results', []))
        if form.is_valid():
            selected_member_id = form.cleaned_data.get('selected_member')
            if selected_member_id:
                selected_member_id = str(selected_member_id)
                results = request.session.get('results', [])
                selected_member_info = next((m for m in results if str(m['id']) == selected_member_id), None)

                if selected_member_info:
                    subscription_info = fetch_subscription_info(selected_member_info)
                    for onu in onus:
                        if onu['name'].endswith("-" + selected_member_info['unit']) or onu['name'].endswith("-0" + selected_member_info['unit']):
                            device_info = onu
                            break

            else:
                query = form.cleaned_data['query']
                params = {}
                params['page_size'] = 1000
                if re.match(r"^\d{10}$", query):
                    normalized_phone = normalize_phone_number(query)
                    params['phone_number'] = normalized_phone
                elif "@" in query:
                    params['email_address'] = query
                else:
                    params['name'] = query

                response = requests.get(API_URL, headers=headers, params=params)
                full_url = response.url

                if response.status_code == 200:
                    members = response.json().get('results', [])
                    valid_members = []
                    for member in members:
                        valid = False
                        for install in member['installs']:
                            unit, network_number = get_install_unit_and_network_number(install['id'], headers)
                            if str(network_number) is str(3461):
                                valid = True
                                member['unit'] = unit
                                break
                        if valid:
                            valid_members.append(member)
                    results = valid_members
                    request.session['results'] = results
                    if len(results) == 1:
                        selected_member_info = results[0]
                        subscription_info = fetch_subscription_info(selected_member_info)
                    elif not results:
                        error_message = "No results found."
                else:
                    error_message = f"Error {response.status_code}: {response.text}"
    else:
        form = LookupForm()

    if results and len(results) > 1:
        form = LookupForm(results=results)

    member_id = request.GET.get("member")
    if member_id:
        response = requests.get(f"{MEMBER_API_URL}/{member_id}", headers=headers)
        if response.status_code == 200:
            selected_member_info = response.json()
            selected_member_info['unit'], nn = get_install_unit_and_network_number(selected_member_info['installs'][0]['id'], headers)
            subscription_info = fetch_subscription_info(selected_member_info)
            for onu in onus:
                if onu['name'].endswith("-" + selected_member_info['unit']) or onu['name'].endswith("-0" + selected_member_info['unit']):
                    device_info = onu

    if error_message:
        if uisp_message:
            error_message += uisp_message
    else:
        if uisp_message:
            error_message = uisp_message

    return render(request, 'dashboard/ph-index.html', {
        'form': form,
        'results': results,
        'error_message': error_message,
        'selected_member_info': selected_member_info,
        'device_info': device_info,
        'subscription_info': subscription_info,
        'all_units': all_units
    })
