import re
from collections import Counter, defaultdict
from datetime import datetime

from dateutil import parser
from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from . import buildings, clients

REPORT_START = datetime(2022, 1, 1)

# A support visit counts as internet-related if its issue mentions any of these
INTERNET_KEYWORDS = ["internet", "wifi", "loco", "onu", "connection", "fiber"]

# Slack thread links pasted into an issue are shown as "Details in Slack" instead of the raw URL
SLACK_LINK = re.compile(r"https?://[\w.-]*slack\.com/\S+")

def fetch_installs(group_buildings):
    """All installs in the given buildings. Buildings MeshDB fails to return are left out."""
    return [install for b in group_buildings for install in clients.fetch_installs(b.network_number) or []]

def report_months():
    """Every month from REPORT_START up to, but not including, the current month."""
    months = []
    month = REPORT_START
    this_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month < this_month:
        months.append({'value': month.strftime("%Y%m"), 'formatted': month.strftime("%B %Y")})
        month = month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)
    return months

def parse_day(value):
    return datetime.strptime(value, "%Y-%m-%d") if value else None

def parse_request_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")

def report_category(install):
    """The install-data row an install counts toward and the date it counts on, or None."""
    status = install['status']
    if status == "Active" and install['install_date']:
        return "completed", parse_day(install['install_date'])
    if status == "Request Received":
        return "pending", parse_request_date(install['request_date'])
    if status == "Closed" and install['abandon_date']:
        return "uninstalled", parse_day(install['abandon_date'])
    if status == "Closed":
        return "no_reply", parse_request_date(install['request_date'])
    return None

def install_report(installs, year, month):
    """Install counts for the selected month and the 4 before it, the selected year and the 3 before it, and all time."""
    months = [(year, month)]
    for _ in range(4):
        y, m = months[-1]
        months.append((y - 1, 12) if m == 1 else (y, m - 1))
    years = [year - i for i in range(4)]

    monthly = defaultdict(Counter)
    yearly = defaultdict(Counter)
    all_time = Counter()
    waits = []
    for install in installs:
        found = report_category(install)
        if not found:
            continue
        category, date = found
        monthly[category][(date.year, date.month)] += 1
        yearly[category][date.year] += 1
        all_time[category] += 1
        if category == "completed" and (date.year, date.month) == (year, month):
            requested = parse_request_date(install['request_date']).replace(hour=0, minute=0, second=0, microsecond=0)
            waits.append((date - requested).days)

    late = sum(1 for wait in waits if wait > 7)
    report = {
        "written_months": [datetime(y, m, 1).strftime("%B") for y, m in months],
        "written_years": [str(y) for y in years],
        "response_meanwait": round(sum(waits) / len(waits), 2) if waits else 0,
        "response_weekwait": late,
        "response_shortwait": len(waits) - late,
        "response_percent": int((len(waits) - late) / len(waits) * 100) if waits else 0,
    }
    for category in ("completed", "pending", "no_reply", "uninstalled"):
        report[f"{category}_installs"] = [monthly[category][ym] for ym in months]
        report[f"{category}_installs_years"] = [yearly[category][y] for y in years]
        report[f"{category}_installs_all_time"] = all_time[category]
    return report

def support_visit(unit, issue, raw_reported, raw_resolved, active_apartments, year=None, month=None):
    """
    One row of the support table, or None if the row has no readable report date or,
    when year and month are given, was reported in a different month.
    """
    try:
        reported = parser.parse(raw_reported.strip())
    except (ValueError, OverflowError):
        return None
    if year and month and (reported.year, reported.month) != (year, month):
        return None

    try:
        resolved = parser.parse(raw_resolved.strip()) if raw_resolved.strip() else None
    except (ValueError, OverflowError):
        resolved = None

    return {
        "unit": unit.strip().replace(" ", ""),
        "mesh": buildings.apartment_from_name(unit, buildings.in_group("gsg")) in active_apartments,
        "issue": SLACK_LINK.sub("", issue).strip(" /"),
        "slack_links": SLACK_LINK.findall(issue),
        "date_reported": reported.strftime("%m/%d/%Y"),
        "date_resolved": resolved.strftime("%m/%d/%Y") if resolved else "Open",
        "wait": ((resolved or datetime.now()) - reported).days
    }

@login_required
def reports(request):
    """The status report for ?month=YYYYMM, defaulting to the last full month, with support visits from the Google Sheet."""
    months = report_months()
    value = request.GET.get("month")
    if value not in {m['value'] for m in months}:
        value = months[-1]['value']
    year, month = int(value[:4]), int(value[4:])

    installs = fetch_installs(buildings.in_group("gsg"))
    report = {
        "value": value,
        "formatted": next(m['formatted'] for m in months if m['value'] == value),
        **install_report(installs, year, month)
    }

    # Support log columns: ID, Apartment, Issue, Reported By, Date Reported, Date Resolved
    support = []
    stats = {}
    error_message = None
    sheet = clients.fetch_support_sheet()
    if sheet is None:
        error_message = "Couldn't download the support sheet from Google Sheets."
    else:
        active_apartments = {buildings.apartment_key(i) for i in installs if i['status'] == "Active"} - {None}
        for row in (r + [""] * 6 for r in sheet):
            if row[0]:
                visit = support_visit(row[1], row[2], row[4], row[5], active_apartments, year, month)
                if visit:
                    support.append(visit)

        if support:
            stats = {
                "visits": len(support),
                "percent_mesh": int(sum(v['mesh'] for v in support) / len(support) * 100),
                "percent_internet": int(sum(any(k in v['issue'].lower() for k in INTERNET_KEYWORDS) for v in support) / len(support) * 100),
                "avg_wait": round(sum(v['wait'] for v in support) / len(support), 2)
            }
        else:
            error_message = f"No support visits were reported in {report['formatted']}."

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
    installs = fetch_installs(billable)

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
