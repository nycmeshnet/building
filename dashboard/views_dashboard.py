"""
The lookup and unit grid page, shared by every building group (/gsg/, /ph/).
"""
from collections import Counter, defaultdict

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import buildings, clients
from .forms import LookupForm

ONLINE = 'active'  # UISP's status for a connected ONU

# When a unit or member has several installs, show the one with the most relevant
# status, then the most recent
STATUS_PRIORITY = {"Active": 0, "Pending": 1, "Request Received": 2}

# Grid states in legend order, with their legend labels
LEGEND = [
    ("online", "Active, ONU online"),
    ("active", "Active, ONU status unknown"),
    ("offline", "Active, ONU offline"),
    ("no-onu", "Active, no ONU found"),
    ("unrecorded", "ONU online, no Active install"),
    ("pending", "Pending"),
    ("closed", "Closed"),
    ("empty", "No install"),
]
UISP_STATES = {"online", "offline", "no-onu", "unrecorded"}

def best_install(installs):
    return min(installs, key=lambda i: (STATUS_PRIORITY.get(i['status'], 3), -i['install_number']), default=None)

def member_install(member, installs_by_id):
    """The member's install in this group's buildings, or None if they have none here."""
    return best_install([installs_by_id[i['id']] for i in member.get('installs', []) if i['id'] in installs_by_id])

def onus_by_apartment(group_buildings, onus_by_parent):
    """Map "BUILDING-UNIT" to its ONU, preferring an online one when an apartment has several."""
    result = {}
    for building in group_buildings:
        for onu in onus_by_parent.get(building.uisp_parent_id) or []:
            parsed = buildings.onu_apartment(onu['name'])
            if not parsed or parsed[0] not in (building.number, building.network_number):
                continue
            apt = f"{building.number}-{parsed[1]}{parsed[2]}"
            if apt not in result or (onu['status'] == ONLINE and result[apt]['status'] != ONLINE):
                result[apt] = onu
    return result

def unit_state(install, onu, uisp_up):
    """The grid state for a unit: one of the states in LEGEND."""
    status = install['status'] if install else None
    online = bool(onu) and onu['status'] == ONLINE
    if status == "Active":
        if not uisp_up:
            return "active"
        return "online" if online else "offline" if onu else "no-onu"
    if online:
        return "unrecorded"
    if status in ("Pending", "Request Received"):
        return "pending"
    if status:
        return "closed"
    return "empty"

def unit_grid(building, installs, onus, uisp_up):
    by_apartment = defaultdict(list)
    for install in installs or []:
        apt = buildings.apartment_key(install)
        if apt:
            by_apartment[apt].append(install)

    floors = []
    for floor in reversed(building.floors):
        units = []
        for letter in building.unit_letters:
            name = f"{floor}{letter}"
            apt = f"{building.number}-{name}"
            install = best_install(by_apartment[apt])
            units.append({'name': name, 'install': install, 'state': unit_state(install, onus.get(apt), uisp_up)})
        floors.append(units)
    return {'building': building, 'floors': floors, 'loaded': installs is not None}

def legend(grid, uisp_up):
    """(state, label, number of units) for each state that can appear on the page."""
    counts = Counter(unit['state'] for building in grid for floor in building['floors'] for unit in floor)
    hidden = {"active"} if uisp_up else UISP_STATES
    return [(state, label, counts[state]) for state, label in LEGEND if state not in hidden]

def member_details(member, installs_by_id, onus):
    install = member_install(member, installs_by_id)
    apartment = buildings.apartment_key(install) if install else None

    invoices = None
    if install and install.get('unit'):
        building = buildings.BY_NETWORK_NUMBER[install['node']['network_number']]
        ninja_client = clients.fetch_ninja_client(apartment or f"{building.number}-{install['unit']}")
        if ninja_client:
            invoices = clients.fetch_ninja_invoices(ninja_client['id'])

    email = member.get('stripe_email_address') or member.get('primary_email_address')
    customer_id, delinquent = clients.fetch_stripe_customer(email) if email else (None, None)

    return {
        'member': member,
        'install': install,
        'device': onus.get(apartment),
        'subscription': clients.fetch_stripe_subscription(customer_id) if customer_id else None,
        'delinquent': delinquent,
        'invoices': invoices,
    }

@login_required
def index(request, group):
    group_buildings = buildings.in_group(group)

    installs = {b.network_number: clients.fetch_installs(b.network_number) for b in group_buildings}
    installs_by_id = {i['id']: i for found in installs.values() for i in found or []}
    onus_by_parent = {parent: clients.fetch_onus(parent) for parent in {b.uisp_parent_id for b in group_buildings}}
    uisp_up = all(found is not None for found in onus_by_parent.values())
    onus = onus_by_apartment(group_buildings, onus_by_parent)

    results = None
    selected = None
    error_message = None

    if request.method == 'POST':
        form = LookupForm(request.POST, results=request.session.get('results', []))
        if form.is_valid():
            selected_id = form.cleaned_data.get('selected_member')
            query = form.cleaned_data.get('query', '').strip()
            if selected_id:
                selected = next((m for m in request.session.get('results', []) if str(m['id']) == selected_id), None)
            elif not query:
                error_message = "Enter a name, email, or phone number."
            else:
                members = clients.search_members(query)
                if members is None:
                    error_message = "MeshDB lookup failed. Try again in a moment."
                else:
                    results = [m for m in members if member_install(m, installs_by_id)]
                    request.session['results'] = results
                    if len(results) == 1:
                        selected = results[0]
                    elif not results:
                        error_message = "No results found."

    member_id = request.GET.get("member")
    if member_id:
        selected = clients.fetch_member(member_id)
        if not selected:
            error_message = "Couldn't load that member from MeshDB."

    grid = [unit_grid(b, installs[b.network_number], onus, uisp_up) for b in group_buildings]

    return render(request, 'dashboard/dashboard.html', {
        'group': buildings.GROUPS[group],
        'results': results,
        'error_message': error_message,
        'uisp_up': uisp_up,
        'details': member_details(selected, installs_by_id, onus) if selected else None,
        'grid': grid,
        'legend': legend(grid, uisp_up),
    })
