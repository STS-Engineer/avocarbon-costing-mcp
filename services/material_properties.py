"""Central material property master data used for dimensional cost derivation.

Keep material constants here rather than as unexplained literals scattered
across costing code, so a future material addition/correction is a one-line
change in one place.
"""

from typing import Optional


# Density in grams per cubic millimetre (g/mm3). 1 g/cm3 = 0.001 g/mm3.
_DENSITY_G_PER_MM3 = {
    "copper": 0.00896,
    "cu": 0.00896,
    "magnet_wire": 0.00896,
    "enameled_wire": 0.00896,
    "tin": 0.00731,
    "sn": 0.00731,
    "solder": 0.0074,
    "sn_ag_cu": 0.0074,
    "snag3.5": 0.0074,
    "snag": 0.0074,
    "ferrite": 0.0048,
    "nizn_ferrite": 0.0048,
    "mnzn_ferrite": 0.0049,
}


def _normalize_key(material_key) -> Optional[str]:
    if not material_key:
        return None
    return str(material_key).strip().lower().replace(" ", "_").replace("-", "_")


def get_density_g_per_mm3(material_key) -> Optional[float]:
    """Return density in g/mm3 for a known material key, or None if unknown."""
    key = _normalize_key(material_key)
    if key is None:
        return None
    if key in _DENSITY_G_PER_MM3:
        return _DENSITY_G_PER_MM3[key]
    for known_key, density in _DENSITY_G_PER_MM3.items():
        if known_key in key or key in known_key:
            return density
    return None


def get_density_g_per_cm3(material_key) -> Optional[float]:
    density_mm3 = get_density_g_per_mm3(material_key)
    return density_mm3 * 1000 if density_mm3 is not None else None


def derive_mass_g_from_cylindrical_wire(diameter_mm, developed_length_mm, material_key="copper"):
    """cross_section_mm2 = pi * d^2 / 4 ; volume_mm3 = cross_section * length ; mass_g = volume * density."""
    import math

    if diameter_mm in (None, "") or developed_length_mm in (None, ""):
        return None
    density = get_density_g_per_mm3(material_key)
    if density is None:
        return None
    try:
        diameter_mm = float(diameter_mm)
        developed_length_mm = float(developed_length_mm)
    except (TypeError, ValueError):
        return None
    if diameter_mm <= 0 or developed_length_mm <= 0:
        return None
    cross_section_mm2 = math.pi * diameter_mm**2 / 4
    volume_mm3 = cross_section_mm2 * developed_length_mm
    return volume_mm3 * density
