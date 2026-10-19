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

    @property
    def unit_letters(self):
        return ALPHABET[:self.units_per_floor]

BUILDINGS = [
    Building(1932, 410, "gsg", range(2, 27), 8, 'a57ddde1-6fff-463a-bbee-cbe90258daa6', billable=True),
    Building(1933, 460, "gsg", range(2, 27), 8, '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6', billable=True),
    Building(1934, 131, "gsg", range(2, 27), 8, '7ee45009-6bc2-4a26-9b83-a3b07c70a3f6', billable=True),
    Building(1936, 165, "gsg", range(3, 16), 18, '4a6f43ed-ac8b-41c6-867b-e893d9737c31'),
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
