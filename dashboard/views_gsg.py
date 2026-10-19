import requests
import sys
import os
import csv
import json
from dateutil import parser
from datetime import datetime, timedelta
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .forms import ReportForm
from . import buildings
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

INSTALL_API_URL = 'https://db.nycmesh.net/api/v1/installs/'
MESHDB_API_KEY = os.environ.get("MESHDB_API_KEY","")
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

headers = {
    'accept': 'application/json',
    'Authorization': f'Token {MESHDB_API_KEY}'
}

# Derived from buildings.py until reports is moved onto it directly
GSG_BUILDINGS = buildings.in_group("gsg")
ALLOWED_NETWORK_NUMBERS = [b.network_number for b in GSG_BUILDINGS]
install_to_building_map = {b.network_number: b.number for b in GSG_BUILDINGS}

internet_keywords = [
    "internet",
    "wifi",
    "loco",
    "onu",
    "connection",
    "fiber"
]

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
    if not raw_date_reported:
        return None

    # Clean whitespace and unexpected spaces
    unit = unit.strip().replace(" ", "")
    issue = issue.strip()
    raw_date_reported = raw_date_reported.strip()
    raw_date_resolved = raw_date_resolved.strip() if raw_date_resolved else ""

    # Parse Date Reported
    try:
        date_reported = parser.parse(raw_date_reported)
    except Exception as e:
        print(e, file=sys.stderr)
        return None

    # Filter by month and year
    if filter_year and filter_month:
        if date_reported.year != filter_year or date_reported.month != filter_month:
            return None

    # Parse Date Resolved if present
    date_resolved = None
    if raw_date_resolved:
        try:
            date_resolved = parser.parse(raw_date_resolved)
        except Exception:
            date_resolved = None

    # Calculate wait time
    if date_resolved:
        wait = (date_resolved - date_reported).days
    else:
        wait = (datetime.now() - date_reported).days

    # Mesh detection
    mesh = False
    building = "0"
    apt = unit

    if "-" in unit:
        parts = unit.split("-")
        if len(parts) >= 2:
            building = parts[0]
            apt = parts[1]

    nn = 0
    for install in all_active_installs:
        for k, v in install_to_building_map.items():
            if v == int(building):
                nn = k
                break

        try:
            if int(install["node"]["network_number"]) == nn:
                if install["unit"].lower() == apt.lower():
                    mesh = True
                    break
        except Exception as e:
            print(e, file=sys.stderr)
            continue

    # Format output
    display_resolved = date_resolved.strftime("%m/%d/%Y") if date_resolved else "Open"

    return {
        "unit": unit,
        "mesh": mesh,
        "issue": issue,
        "date_reported": date_reported.strftime("%m/%d/%Y"),
        "date_resolved": display_resolved,
        "wait": wait
    }

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
    error_message = None # New variable to pass errors to template

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
            selected_year = current_year = int(value[:4])
            selected_month = current_month = int(value[4:])
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
                    # 0: Unit, 1: Issue, 2: Reported, 3: Resolved
                    # (Assuming User's provided CSV structure)
                    unit = row[0]
                    issue = row[1]
                    raw_date_reported = row[2]
                    raw_date_resolved = row[3]

                    visit = process_support_row(
                        unit, issue, raw_date_reported, raw_date_resolved, 
                        all_active_installs, install_to_building_map
                    )

                    if visit:
                        support.append(visit)
                        if visit['mesh']: mesh_count += 1
                        avg_wait += visit['wait']
                        issue_text = visit["issue"].lower()
                        if any(keyword in issue_text for keyword in internet_keywords):
                            internet_count += 1

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
                            selected_year, selected_month
                        )

                        if visit:
                            support.append(visit)
                            if visit['mesh']: mesh_count += 1
                            avg_wait += visit['wait']
                            issue_text = visit["issue"].lower()
                            if any(keyword in issue_text for keyword in internet_keywords):
                                internet_count += 1
                            
                except Exception as e:
                    error_message = f"Error connecting to Google Sheets: {e}"

            # Calculate Statistics if support data exists
            if len(support) > 0:
                stats = {
                    "visits": len(support),
                    "percent_mesh": int((mesh_count / len(support)) * 100),
                    "percent_internet": int((internet_count / len(support)) * 100),
                    "avg_wait": round(avg_wait / len(support), 2)
                }
            else:
                error_message = "No support information to be displayed!"
    
    # NOTE: We are passing 'error_message' to the template. 
    # Ensure your template can display {{ error_message }} if it's not None.
    return render(request, 'dashboard/gsg-reports.html', {
        'months': months,
        'report': report,
        'support': support,
        'stats': stats,
        'error_message': error_message 
    })

@login_required
def billing(request):
    first_month = datetime(2022, 1, 1)
    this_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    billable = buildings.billable()
    installs = fetch_all_installs([b.network_number for b in billable])

    # Everything on this page is counted by apartment (see buildings.apartment_key), so the
    # active counts and the monthly table agree on what a unit is.
    active_counts = defaultdict(int)  # apartment -> number of Active installs
    first_installed = {}              # apartment -> earliest install date in the billing window
    excluded = []                     # installs whose unit isn't a recognizable apartment

    for install in installs:
        apt = buildings.apartment_key(install)
        install_date = datetime.strptime(install['install_date'], "%Y-%m-%d") if install.get('install_date') else None
        is_active = install.get('status') == "Active"

        if not apt:
            if is_active or install_date:
                building = buildings.BY_NETWORK_NUMBER.get((install.get('node') or {}).get('network_number'))
                excluded.append(f"{building.number if building else '?'} \"{install.get('unit')}\" ({install.get('status')})")
            continue

        if is_active:
            active_counts[apt] += 1
        if install_date and first_month <= install_date < this_month:
            if apt not in first_installed or install_date < first_installed[apt]:
                first_installed[apt] = install_date

    building_order = [str(b.number) for b in billable]
    def apt_sort(apt):
        building, unit = apt.split("-")
        return (building_order.index(building), int(unit[:-1]), unit[-1])

    months = []
    month = first_month
    while month < this_month:
        apartments = sorted(
            (apt for apt, date in first_installed.items() if (date.year, date.month) == (month.year, month.month)),
            key=apt_sort
        )
        months.append({
            'value': month.strftime("%Y%m"),
            'formatted': month.strftime("%B %Y"),
            'count': len(apartments),
            'apartments': apartments
        })
        month = month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)

    active_by_building = []
    for building in building_order:
        apts = [apt for apt in active_counts if apt.startswith(f"{building}-")]
        active_by_building.append({
            'name': building,
            'active': len(apts),
            'duplicates': ", ".join(apt.split("-")[1] for apt in sorted(apts, key=apt_sort) if active_counts[apt] > 1)
        })

    active = set(active_counts)
    billed = set(first_installed)

    return render(request, 'dashboard/gsg-billing.html', {
        'months': months,
        'first_month': first_month,
        'total': len(billed),
        'buildings': active_by_building,
        'active_total': len(active),
        'no_longer_active': sorted(billed - active, key=apt_sort),
        'active_not_billed': sorted(active - billed, key=apt_sort),
        'excluded': excluded
    })
