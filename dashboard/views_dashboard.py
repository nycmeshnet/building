"""
The lookup and unit grid page, shared by every building group (/gsg/, /ph/).
"""
from collections import Counter, defaultdict
from functools import partial

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

# Payments view states in legend order, with their legend labels
PAYMENTS_LEGEND = [
    ("pay-stripe", "Stripe, paid up"),
    ("pay-stripe-behind", "Stripe, behind"),
    ("pay-ninja", "Ninja, paid up"),
    ("pay-ninja-owes", "Ninja, owes a balance"),
    ("pay-ninja-setup", "Ninja client, no recurring invoice"),
    ("pay-stripe-canceled", "Stripe canceled, nothing else"),
    ("pay-unbilled", "Active, no payment record"),
    ("pay-inactive-paying", "No Active install, still paying on Stripe"),
    ("pay-building", "Paid by the building"),
    ("pay-unknown", "Payment data unavailable"),
    ("pay-none", "No install, not paying"),
]
PAYMENT_LABELS = dict(PAYMENTS_LEGEND)
STRIPE_PAYING = {"active", "trialing"}
STRIPE_BEHIND = {"past_due", "unpaid", "incomplete"}
INTERNET_MONTHLY = 10  # dollars a resident pays per month

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
            apt = buildings.apartment_from_name(onu['name'], [building])
            if not apt:
                continue
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

class PaymentRecords:
    """Stripe subscriptions, Invoice Ninja clients and member emails, looked up by install."""

    def __init__(self, subscriptions, ninja_clients, member_emails, group_buildings):
        self.available = subscriptions is not None and ninja_clients is not None
        self.subscriptions = defaultdict(list)
        for subscription in subscriptions or []:
            if subscription['email']:
                self.subscriptions[subscription['email']].append(subscription)
        # Ninja clients are named after the apartment, e.g. "410-14B"
        self.ninja = {}
        for client in ninja_clients or []:
            apt = buildings.apartment_from_name(client['name'], group_buildings)
            if apt:
                self.ninja[apt] = client
        self.member_emails = member_emails

    def for_install(self, install):
        """(emails, Stripe subscriptions, Ninja client) for an install's member and apartment."""
        emails = self.member_emails.get((install.get('member') or {}).get('id'), [])
        subscriptions = [s for email in emails for s in self.subscriptions.get(email, [])]
        return emails, subscriptions, self.ninja.get(buildings.apartment_key(install))

def payment_state(building, install, records):
    """The grid state for a unit in the Payments view: one of the states in PAYMENTS_LEGEND."""
    if not install:
        return "pay-none"
    if not records.available:
        return "pay-unknown"
    emails, subscriptions, ninja = records.for_install(install)
    statuses = {s['status'] for s in subscriptions}
    if install['status'] != "Active":
        return "pay-inactive-paying" if statuses & (STRIPE_PAYING | STRIPE_BEHIND) else "pay-none"
    if not building.residents_pay:
        return "pay-building"
    if statuses & STRIPE_BEHIND:
        return "pay-stripe-behind"
    if statuses & STRIPE_PAYING:
        return "pay-stripe"
    if ninja:
        if not ninja['recurring']:
            return "pay-ninja-setup"
        return "pay-ninja-owes" if ninja['balance'] > 0 else "pay-ninja"
    if statuses:
        return "pay-stripe-canceled"
    return "pay-unbilled"

def payment_details(install, records):
    """What the Payments view knows about one install, for the member details."""
    building = buildings.BY_NETWORK_NUMBER[install['node']['network_number']]
    emails, subscriptions, ninja = records.for_install(install)
    return {
        'state': PAYMENT_LABELS[payment_state(building, install, records)],
        'emails': emails,
        'subscriptions': subscriptions,
        'ninja': ninja,
        'months_owed': round(ninja['balance'] / INTERNET_MONTHLY, 1) if ninja else None,
    }

def unit_grid(building, installs, onus, uisp_up, records=None):
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
            unit = {'name': name, 'install': install, 'state': unit_state(install, onus.get(apt), uisp_up)}
            if records:
                unit['payment'] = payment_state(building, install, records)
            units.append(unit)
        floors.append(units)
    return {'building': building, 'floors': floors, 'loaded': installs is not None}

def legend(grid, uisp_up, payments=None):
    """(state, label, number of units) for each state that can appear on the page."""
    if payments:
        counts = Counter(unit['payment'] for building in grid for floor in building['floors'] for unit in floor)
        hidden = set() if not payments.available else {"pay-unknown"}
        return [(state, label, counts[state]) for state, label in PAYMENTS_LEGEND if state not in hidden]
    counts = Counter(unit['state'] for building in grid for floor in building['floors'] for unit in floor)
    hidden = {"active"} if uisp_up else UISP_STATES
    return [(state, label, counts[state]) for state, label in LEGEND if state not in hidden]

def ninja_invoices(name):
    ninja_client = clients.fetch_ninja_client(name)
    return clients.fetch_ninja_invoices(ninja_client['id']) if ninja_client else None

def stripe_info(email):
    """(subscription, delinquent) for the Stripe customer with this email."""
    customer_id, delinquent = clients.fetch_stripe_customer(email) if email else (None, None)
    return (clients.fetch_stripe_subscription(customer_id) if customer_id else None), delinquent

def member_details(member, installs_by_id, onus, payments=None):
    install = member_install(member, installs_by_id)
    apartment = buildings.apartment_key(install) if install else None

    # Stripe and Invoice Ninja are looked up at the same time
    calls = {'stripe': lambda: stripe_info(member.get('stripe_email_address') or member.get('primary_email_address'))}
    if install and install.get('unit'):
        building = buildings.BY_NETWORK_NUMBER[install['node']['network_number']]
        calls['invoices'] = lambda: ninja_invoices(apartment or f"{building.number}-{install['unit']}")
    found = clients.in_parallel(calls)
    subscription, delinquent = found['stripe']

    return {
        'member': member,
        'install': install,
        'device': onus.get(apartment),
        'subscription': subscription,
        'delinquent': delinquent,
        'invoices': found.get('invoices'),
        'payment': payment_details(install, payments) if payments and payments.available and install else None,
    }

@login_required
def index(request, group):
    group_buildings = buildings.in_group(group)
    parents = {b.uisp_parent_id for b in group_buildings}
    fresh = 'refresh' in request.GET
    view = 'payments' if request.GET.get('view') == 'payments' else 'operations'

    results = None
    selected = None
    error_message = None

    form = None
    if request.method == 'POST':
        form = LookupForm(request.POST, results=request.session.get('results', []))
        if not form.is_valid():
            form = None
    selected_id = form.cleaned_data.get('selected_member') if form else None
    query = form.cleaned_data.get('query', '').strip() if form and not selected_id else ''
    member_id = request.GET.get("member")

    # Everything the page needs from other services, fetched at the same time
    calls = {('installs', b.network_number): partial(clients.fetch_installs, b.network_number, fresh) for b in group_buildings}
    calls['onus'] = partial(clients.fetch_onus_by_parent, fresh)
    if query:
        calls['search'] = partial(clients.search_members, query)
    if member_id:
        calls['member'] = partial(clients.fetch_member, member_id)
    if view == 'payments':
        calls['stripe'] = partial(clients.fetch_stripe_subscriptions, fresh)
        calls['ninja'] = partial(clients.fetch_ninja_billing, fresh)
    found = clients.in_parallel(calls)

    installs = {b.network_number: found[('installs', b.network_number)] for b in group_buildings}
    installs_by_id = {i['id']: i for found_installs in installs.values() for i in found_installs or []}
    uisp_up = found['onus'] is not None
    onus_by_parent = {parent: (found['onus'] or {}).get(parent, []) for parent in parents}
    onus = onus_by_apartment(group_buildings, onus_by_parent)

    if form:
        if selected_id:
            selected = next((m for m in request.session.get('results', []) if str(m['id']) == selected_id), None)
        elif not query:
            error_message = "Enter a name, email, or phone number."
        elif found['search'] is None:
            error_message = "MeshDB lookup failed. Try again in a moment."
        else:
            results = [m for m in found['search'] if member_install(m, installs_by_id)]
            request.session['results'] = results
            if len(results) == 1:
                selected = results[0]
            elif not results:
                error_message = "No results found."

    if member_id:
        selected = found['member']
        if not selected:
            error_message = "Couldn't load that member from MeshDB."

    payments = None
    if view == 'payments':
        member_ids = sorted({i['member']['id'] for i in installs_by_id.values() if i.get('member')})
        payments = PaymentRecords(found['stripe'], found['ninja'], clients.fetch_member_emails(member_ids), group_buildings)
        if not payments.available:
            error_message = "Couldn't load payment data from Stripe or Invoice Ninja, so payment colors aren't shown."

    grid = [unit_grid(b, installs[b.network_number], onus, uisp_up, payments) for b in group_buildings]

    return render(request, 'dashboard/dashboard.html', {
        'group': buildings.GROUPS[group],
        'results': results,
        'error_message': error_message,
        'uisp_up': uisp_up,
        'view': view,
        'details': member_details(selected, installs_by_id, onus, payments) if selected else None,
        'grid': grid,
        'legend': legend(grid, uisp_up, payments),
    })
