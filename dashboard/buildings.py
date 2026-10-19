"""
Every building the dashboard knows about. Add or change a building here, not in the views.
"""
import re
from dataclasses import dataclass

ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

@dataclass(frozen=True)
class Building:
    network_number: int     # MeshDB node the building's installs are under
    number: int             # Building number used in apartment names ("410-14B") and Ninja client names
    group: str              # Dashboard the building appears on: "gsg" or "ph"
    floors: range           # Floors with apartments
    units_per_floor: int    # Apartment letters per floor, starting at A
    uisp_parent_id: str     # UISP device whose ONUs serve the building
    billable: bool = False  # Included on the GSG billing page
    residents_pay: bool = True  # Residents pay for internet themselves (Stripe or Invoice Ninja)

    @property
    def unit_letters(self):
        return ALPHABET[:self.units_per_floor]

BUILDINGS = [
    Building(1932, 410, "gsg", range(2, 27), 8, 'a57ddde1-6fff-463a-bbee-cbe90258daa6', billable=True),
    Building(1933, 460, "gsg", range(2, 27), 8, '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6', billable=True),
    Building(1934, 131, "gsg", range(2, 27), 8, '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6', billable=True),
    Building(1936, 165, "gsg", range(3, 16), 18, '4a6f43ed-ac8b-41c6-867b-e893d9737c31', residents_pay=False),  # paid by the building association
    Building(3461, 3461, "ph", range(1, 16), 12, 'cd815a56-612a-46a9-ae87-b80aab35730b'),
]

BY_NETWORK_NUMBER = {b.network_number: b for b in BUILDINGS}

def in_group(group):
    return [b for b in BUILDINGS if b.group == group]

def billable():
    return [b for b in BUILDINGS if b.billable]

def apartment_key(install):
    """
    Return the apartment for an install as "BUILDING-UNIT" (e.g. "410-14B"), or None if
    it isn't in a known building or its unit isn't a real apartment (APs, roof, typos).
    Units are normalized, so "APT 14B", "06D" and "18 H" become "14B", "6D" and "18H".
    """
    building = BY_NETWORK_NUMBER.get((install.get('node') or {}).get('network_number'))
    if not building:
        return None

    unit = re.sub(r'APARTMENT|APT\.?|#|\s', '', (install.get('unit') or '').upper())
    match = re.fullmatch(r'0*(\d+)([A-Z])', unit)
    if not match:
        return None

    floor, letter = int(match[1]), match[2]
    if floor not in building.floors or letter not in building.unit_letters:
        return None

    return f"{building.number}-{floor}{letter}"

# Settings for each dashboard page that differ between building groups
GROUPS = {
    "gsg": {
        'title': "GSG Dashboard",
        'url': "/gsg/",
        'grid_columns': "repeat(24, 1fr)",
        'grid_gap': "2px",
        'links': [("Switch to Reports", "/gsg/reports/"), ("Switch to Billing", "/gsg/billing/")],
    },
    "ph": {
        'title': "PH Dashboard",
        'url': "/ph/",
        'grid_columns': "repeat(5, 1fr) 55px repeat(7, 1fr)",
        'grid_gap': "0",
        'links': [],
    },
}

def parse_apartment(name):
    """
    Return (prefix, floor, letter) from an ONU name or support sheet unit, or None if it
    doesn't name an apartment. Accepts "410-14B", "1936-03A", "410 - 14B" and names with
    trailing words like "165-9D New ONU" or "410-14B (2 visits)". The prefix is the
    building number or network number, depending on how the name was written.
    """
    match = re.match(r'\s*(\d+)\s*-\s*0*(\d+)\s*([A-Za-z])\b', name or '')
    if not match:
        return None
    return int(match[1]), int(match[2]), match[3].upper()

def apartment_from_name(name, group_buildings):
    """Return "BUILDING-UNIT" for a name (see parse_apartment) in one of the given buildings, or None."""
    parsed = parse_apartment(name)
    if not parsed:
        return None
    building = next((b for b in group_buildings if parsed[0] in (b.number, b.network_number)), None)
    return f"{building.number}-{parsed[1]}{parsed[2]}" if building else None
