import requests
import re
import sys
import os
import csv
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .forms import LookupForm, ReportForm
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
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

headers = {
    'accept': 'application/json',
    'Authorization': f'Token {MESHDB_API_KEY}'
}

ALLOWED_NETWORK_NUMBERS = [1932, 1933, 1934, 1936]
ALLOWED_NETWORK_NUMBERS_3 = [1932, 1933, 1934]
DEVICE_PARENT_IDS = {
    1932: 'a57ddde1-6fff-463a-bbee-cbe90258daa6',
    #1933: '7e6d228f-2693-4ba2-9c0a-82811289f9f3',
    1933: '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6',
    1934: '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6',
    1936: '4a6f43ed-ac8b-41c6-867b-e893d9737c31'
}
install_to_building_map = {
    1932: 410,
    1933: 460,
    1934: 131,
    1936: 165
}
install_to_floor_map = {
    1932: range(2,27),
    1933: range(2,27),
    1934: range(2,27),
    1936: range(3,16)
}
install_to_unit_map = {
    1932: 8,
    1933: 8,
    1934: 8,
    1936: 18
}
alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

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

def get_device_data(parent_id):
    print(parent_id, file=sys.stderr)
    headers = {
        'accept': 'application/json',
        'x-auth-token': f'{UISP_API_KEY}'
    }
    url = f"{DEVICE_API_BASE_URL}{parent_id}"
    response = requests.get(url, headers=headers, verify=False)

    full_url = response.url
    print(f"UISP URL: {full_url}", file=sys.stderr)

    if response.status_code == 200:
        return response.json()
    else:
        return None

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
    for nn in DEVICE_PARENT_IDS:
        print(nn, file=sys.stderr)
        device_data = get_device_data(DEVICE_PARENT_IDS[nn])
        if device_data:
            for d in device_data:
                if d['identification']['name'].startswith(f"{install_to_building_map[nn]}-"):
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

    building_number = install_to_building_map.get(network_number)
    building_apt = str(building_number) + "-" + unit
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

def fetch_all_units(onus = False):
    building_map = {}
    uisp_message = ""
    for building in ALLOWED_NETWORK_NUMBERS:
        response = requests.get(f"{INSTALL_API_URL}/lookup/?network_number={building}&page_size=9999", headers=headers)
        if response.status_code == 200:
            data = response.json()
            units = {}
            for floor in install_to_floor_map[building]:
                for unit_letter in alphabet[:install_to_unit_map[building]]:
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
                                if onus:
                                    if len(onus) == 0:
                                        uisp_message = f"<br>UISP is down. No ONU statuses will be displayed."
                                    else:
                                        unit["onu"] = "none"
                                        building = 0
                                        if (unit["install"]['node']['network_number'] == 1932):
                                            building = 410
                                        elif (unit["install"]['node']['network_number'] == 1933):
                                            building = 460
                                        elif (unit["install"]['node']['network_number'] == 1934):
                                            building = 131
                                        elif (unit["install"]['node']['network_number'] == 1936):
                                            building = 1936
                                        for onu in onus:
                                            if onu['name'].startswith(str(building)):
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
            building_map[building] = floors
        else:
            building_map[building] = None
    if onus:
        return building_map, uisp_message
    else:
        return building_map

def fetch_all_installs(network_numbers):
    installs = []
    for building in network_numbers:
        response = requests.get(f"{INSTALL_API_URL}/lookup/?network_number={building}&page_size=9999", headers=headers)
        if response.status_code == 200:
            data = response.json()
            for install in data['results']:
                installs.append(install)
    return installs

def process_support_row(unit, issue, raw_date_reported, raw_date_resolved, all_active_installs, install_to_building_map, filter_year=None, filter_month=None):
    """
    Helper function to process a single row of support data.
    Returns a 'visit' dict or None if dates are invalid or match doesn't occur.
    """
    # Supported formats: US (MM/DD/YYYY), ISO (YYYY-MM-DD), ISO with time (YYYY-MM-DD HH:MM:SS)
    formats = ["%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]
    
    date_reported = None
    date_resolved = None

    # Parse Date Reported
    for fmt in formats:
        try:
            date_reported = datetime.strptime(raw_date_reported, fmt)
            break
        except ValueError:
            pass
    
    # Parse Date Resolved (might be empty)
    if raw_date_resolved:
        for fmt in formats:
            try:
                date_resolved = datetime.strptime(raw_date_resolved, fmt)
                break
            except ValueError:
                pass

    if not date_reported or not date_resolved:
        return None # Skip invalid date rows

    # --- FILTERING LOGIC ---
    if filter_year and filter_month:
        if date_reported.year != filter_year or date_reported.month != filter_month:
            return None
    # -----------------------

    wait = (date_resolved - date_reported).days

    # Determine Mesh Status
    mesh = False
    building = "0"
    apt = unit
    
    if '-' in unit:
        building = unit.split('-')[0]
        apt = unit.split('-')[1]
    
    nn = 0
    for install in all_active_installs:
        # Map building number to network number
        for k, v in install_to_building_map.items():
            if v == int(building):
                nn = k
                break
        
        # Check if this install matches the unit
        try:
            if int(install["node"]["network_number"]) == nn:
                if install["unit"] == apt:
                    mesh = True
                    break
        except (KeyError, ValueError):
            continue

    return {
        "unit": unit,
        "mesh": mesh,
        "issue": issue,
        "date_reported": date_reported.strftime("%m/%d/%Y"),
        "date_resolved": date_resolved.strftime("%m/%d/%Y"),
        "wait": wait
    }

@login_required
def index(request):
    results = None
    error_message = None
    selected_member_info = None
    device_info = None
    subscription_info = None
    uisp = fetch_uisp_info()
    all_units, uisp_message = fetch_all_units(uisp)

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
                    for onu in uisp:
                        # raise Exception(onu['name'], selected_member_info['unit'])
                        if onu['name'] == selected_member_info['unit']:
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
                print(f"MeshDB URL: {full_url}", file=sys.stderr)

                if response.status_code == 200:
                    members = response.json().get('results', [])
                    valid_members = []
                    for member in members:
                        valid = False
                        for install in member['installs']:
                            unit, network_number = get_install_unit_and_network_number(install['id'], headers)
                            if network_number in ALLOWED_NETWORK_NUMBERS:
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
            for onu in uisp:
                if onu['name'].endswith("-" + selected_member_info['unit']) or onu['name'].endswith("-0" + selected_member_info['unit']):
                    device_info = onu

    if error_message:
        if uisp_message:
            error_message += uisp_message
    else:
        if uisp_message:
            error_message = uisp_message
    
    return render(request, 'dashboard/gsg-index.html', {
        'form': form,
        'results': results,
        'error_message': error_message,
        'selected_member_info': selected_member_info,
        'device_info': device_info,
        'subscription_info': subscription_info,
        'all_units': all_units
    })

@login_required
def reports(request):
    months = []
    start_date = datetime(2022, 1, 1)  # Start from January 2022
    current_date = datetime.now()  # Current date
    current_year_month = (current_date.year, current_date.month)

    # Iterate through months from start_date to the month before now
    while (start_date.year, start_date.month) < current_year_month:
        months.append({
            'value': start_date.strftime("%Y%m"),  # Format as "YYYYMM"
            'formatted': start_date.strftime("%B %Y")  # Format as "Month Year"
        })
        # Increment the date by one month
        next_month = start_date.month % 12 + 1
        next_year = start_date.year + (1 if start_date.month == 12 else 0)
        start_date = start_date.replace(year=next_year, month=next_month)

    report = {}
    support = []
    stats = {}
    if request.method == 'POST':
        form = ReportForm(request.POST, request.FILES)
        if form.is_valid():
            value = form.cleaned_data['report']
            action = request.POST.get('action')

            formatted = None
            for month in months:
                if month['value'] == value:
                    formatted = month['formatted']

            installs = fetch_all_installs(ALLOWED_NETWORK_NUMBERS)
            current_installs = []
            all_active_installs = []

            # Parse the year and month from the value
            current_year = int(value[:4])
            current_month = int(value[4:])
            current_date = datetime(current_year, current_month, 1)

            # Generate the date ranges and formatted month names
            date_ranges = [(current_date.year, current_date.month)]
            written_months = [current_date.strftime("%B")]  # Start with the current month name

            for _ in range(4):  # Add the past 4 months
                current_date = (current_date.replace(day=1) - timedelta(days=1))
                date_ranges.append((current_date.year, current_date.month))
                written_months.append(current_date.strftime("%B"))

            # Generate years and written_years for current and past 3 years
            written_years = [str(current_year)]  # Start with the current year
            for _ in range(3):  # Add the past 3 years
                current_year -= 1
                written_years.append(str(current_year))

            # Initialize variables to store the counts
            completed_installs = defaultdict(int)
            pending_installs = defaultdict(int)
            no_reply_installs = defaultdict(int)
            uninstalled_installs = defaultdict(int)

            # Totals for years and all-time
            yearly_totals = defaultdict(lambda: defaultdict(int))
            all_time_totals = defaultdict(int)

            # Loop through the installs and categorize them
            for install in installs:
                install_date = datetime.strptime(install['install_date'], "%Y-%m-%d") if install['install_date'] else None
                try:
                    request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%S.%fZ")
                except ValueError:
                    request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%SZ")
                abandon_date = datetime.strptime(install['abandon_date'], "%Y-%m-%d") if install['abandon_date'] else None

                # Yearly and all-time calculations
                def update_totals(category, date):
                    if date:
                        yearly_totals[category][date.year] += 1
                        all_time_totals[category] += 1

                # Completed installs
                if install_date and install['status'] == "Active":
                    update_totals("completed", install_date)

                # Pending installs
                if request_date and install['status'] == "Request Received":
                    update_totals("pending", request_date)

                # No reply installs
                if request_date and install['status'] == "Closed" and not abandon_date:
                    update_totals("no_reply", request_date)

                # Uninstalled installs
                if abandon_date and install['status'] == "Closed":
                    update_totals("uninstalled", abandon_date)

                # Monthly counts
                for year, month in date_ranges:
                    if install_date and install['status'] == "Active" and install_date.year == year and install_date.month == month:
                        completed_installs[(year, month)] += 1
                        if month is date_ranges[0][1]:
                            current_installs.append(install)
                    elif install_date and install['status'] == "Active":
                        all_active_installs.append(install)
                    elif request_date and install['status'] == "Request Received" and request_date.year == year and request_date.month == month:
                        pending_installs[(year, month)] += 1
                    elif request_date and install['status'] == "Closed" and not abandon_date and request_date.year == year and request_date.month == month:
                        no_reply_installs[(year, month)] += 1
                    elif abandon_date and install['status'] == "Closed" and abandon_date.year == year and abandon_date.month == month:
                        uninstalled_installs[(year, month)] += 1

                # Build the yearly totals for each category
                completed_installs_years = [
                    yearly_totals["completed"][year] for year in range(current_date.year - 3, current_date.year + 1)
                ]
                pending_installs_years = [
                    yearly_totals["pending"][year] for year in range(current_date.year - 3, current_date.year + 1)
                ]
                no_reply_installs_years = [
                    yearly_totals["no_reply"][year] for year in range(current_date.year - 3, current_date.year + 1)
                ]
                uninstalled_installs_years = [
                    yearly_totals["uninstalled"][year] for year in range(current_date.year - 3, current_date.year + 1)
                ]

                # All-time totals for each category
                completed_installs_all_time = all_time_totals["completed"]
                pending_installs_all_time = all_time_totals["pending"]
                no_reply_installs_all_time = all_time_totals["no_reply"]
                uninstalled_installs_all_time = all_time_totals["uninstalled"]

            response_meanwait = 0
            response_weekwait = 0
            for install in current_installs:
                install_date = datetime.strptime(install['install_date'], "%Y-%m-%d") if install['install_date'] else None
                try:
                    request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%S.%fZ")
                except ValueError:
                    request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%SZ")
                wait = (install_date - request_date.replace(hour=0, minute=0, second=0, microsecond=0)).days
                response_meanwait += wait
                if wait > 7:
                    response_weekwait += 1
            if len(current_installs) > 0:
                response_meanwait = round(response_meanwait / len(current_installs), 2)
                response_shortwait = len(current_installs) - response_weekwait
                response_percent = int((response_shortwait / len(current_installs)) * 100)
            else:
                response_shortwait = 0
                response_percent = 0

            # Build the report dictionary
            report = {
                "value": value,
                "formatted": formatted,

                "written_months": written_months,
                "completed_installs": [completed_installs[date] for date in date_ranges],
                "pending_installs": [pending_installs[date] for date in date_ranges],
                "no_reply_installs": [no_reply_installs[date] for date in date_ranges],
                "uninstalled_installs": [uninstalled_installs[date] for date in date_ranges],

                "written_years": written_years,
                "completed_installs_years": completed_installs_years,
                "pending_installs_years": pending_installs_years,
                "no_reply_installs_years": no_reply_installs_years,
                "uninstalled_installs_years": uninstalled_installs_years,
                "completed_installs_all_time": completed_installs_all_time,
                "pending_installs_all_time": pending_installs_all_time,
                "no_reply_installs_all_time": no_reply_installs_all_time,
                "uninstalled_installs_all_time": uninstalled_installs_all_time,

                "response_meanwait": response_meanwait,
                "response_weekwait": response_weekwait,
                "response_shortwait": response_shortwait,
                "response_percent": response_percent
            }

            # ---------------------------------------------------------
            #  SUPPORT DATA HANDLING (File Upload OR Google Sheets)
            # ---------------------------------------------------------
            
            mesh_count = 0
            internet_count = 0
            avg_wait = 0

            # OPTION A: File Upload (CSV)
            # ---------------------------
            if request.FILES:
                support_file = request.FILES['file'].read().decode("utf-8-sig")
                lines = support_file.splitlines()
                reader = csv.reader(lines, delimiter=',')
                next(reader) # Skip header

                for row in reader:
                    if not row: continue
                    
                    # CSV format expected:
                    # 0: ID, 1: Apt, 2: Issue, 3: Reported By, 4: Reported, 5: Resolved
                    # (Assuming User's provided CSV structure)
                    unit = row[1]
                    issue = row[2]
                    raw_date_reported = row[4]
                    raw_date_resolved = row[5]

                    visit = process_support_row(
                        unit, issue, raw_date_reported, raw_date_resolved, 
                        all_active_installs, install_to_building_map,
                        current_year, current_month # PASSING FILTER DATES
                    )

                    if visit:
                        support.append(visit)
                        if visit['mesh']: mesh_count += 1
                        if visit['issue'] == "Internet": internet_count += 1
                        avg_wait += visit['wait']

            # OPTION B: Google Sheets (SIMPLE PUBLISHED CSV)
            # -----------------------
            elif action == 'google_sheets':
                try:
                    if not GOOGLE_SHEET_CSV_URL:
                        raise Exception("Missing GOOGLE_SHEET_CSV_URL environment variable")
                    
                    # Download the published CSV
                    response = requests.get(GOOGLE_SHEET_CSV_URL)
                    response.raise_for_status() # Raise error if download failed
                    
                    # Decode the bytes to string and split into lines
                    lines = response.content.decode('utf-8').splitlines()
                    reader = csv.reader(lines)
                    
                    # Skip Header
                    next(reader) 

                    for row in reader:
                        if not row or not row[0]: continue
                        
                        # Google Sheet format expected:
                        # 0: ID, 1: Apt, 2: Issue, 3: Reported By, 4: Reported, 5: Resolved
                        unit = row[1]
                        issue = row[2]
                        raw_date_reported = row[4]
                        raw_date_resolved = row[5]

                        visit = process_support_row(
                            unit, issue, raw_date_reported, raw_date_resolved, 
                            all_active_installs, install_to_building_map,
                            current_year, current_month # PASSING FILTER DATES
                        )

                        if visit:
                            support.append(visit)
                            if visit['mesh']: mesh_count += 1
                            if visit['issue'] == "Internet": internet_count += 1
                            avg_wait += visit['wait']
                            
                except Exception as e:
                    print(f"Error connecting to Google Sheets: {e}")
                    # Optional: Add an error message to 'report' or 'stats' to display in UI

            # Calculate Statistics if support data exists
            if len(support) > 0:
                stats = {
                    "visits": len(support),
                    "percent_mesh": int((mesh_count / len(support)) * 100),
                    "percent_internet": int((internet_count / len(support)) * 100),
                    "avg_wait": round(avg_wait / len(support), 2)
                }
    
    return render(request, 'dashboard/gsg-reports.html', {
        'months': months,
        'report': report,
        'support': support,
        'stats': stats
    })

@login_required
def billing(request):
    months = []
    start_date = datetime(2022, 1, 1)  # Start from January 2022
    current_date = datetime.now()  # Current date
    current_year_month = (current_date.year, current_date.month)

    installs = fetch_all_installs(ALLOWED_NETWORK_NUMBERS_3)
    units = fetch_all_units()

    installed = []

    while (start_date.year, start_date.month) < current_year_month:
        month_installs = []

        for install in installs:
            install_date = datetime.strptime(install['install_date'], "%Y-%m-%d") if install['install_date'] else None
            try:
                request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%S.%fZ")
            except ValueError:
                request_date = datetime.strptime(install['request_date'], "%Y-%m-%dT%H:%M:%SZ")
            abandon_date = datetime.strptime(install['abandon_date'], "%Y-%m-%d") if install['abandon_date'] else None
 
            if install_date and install_date.year == start_date.year and install_date.month == start_date.month:
                if install['unit'][0] == "0":
                    install['unit'] = install['unit'][1:]
                new_install = ""

                if (install['node']['network_number'] == 1932):
                    new_install = "410-" + install['unit'].upper()
                if (install['node']['network_number'] == 1933):
                    new_install = "460-" + install['unit'].upper()
                if (install['node']['network_number'] == 1934):
                    new_install = "131-" + install['unit'].upper()
                
                if new_install not in installed:
                    installed.append(new_install)
                    install['apt'] = new_install
                    month_installs.append(install)
       
        months.append({
            'value': start_date.strftime("%Y%m"),
            'formatted': start_date.strftime("%B %Y"),
            'count': len(month_installs),
            'installs': month_installs
        })

        next_month = start_date.month % 12 + 1
        next_year = start_date.year + (1 if start_date.month == 12 else 0)
        start_date = start_date.replace(year=next_year, month=next_month)

    return render(request, 'dashboard/gsg-billing.html', {
        'months': months,
        'total': len(installed)
    })
