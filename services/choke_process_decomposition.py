import json


def _number(value):
    if value in [None, ""]:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).replace(",", ".")
    digits = []
    for char in text:
        if char.isdigit() or char in ".-":
            digits.append(char)
        elif digits:
            break
    try:
        return float("".join(digits)) if digits else None
    except ValueError:
        return None


def _parse_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _text(value):
    return json.dumps(value or {}, ensure_ascii=False, default=str).lower()


def _find_value(data, names):
    if not isinstance(data, dict):
        return None
    for name in names:
        if data.get(name) not in [None, ""]:
            return data.get(name)
    for value in data.values():
        if isinstance(value, dict):
            nested = _find_value(value, names)
            if nested not in [None, ""]:
                return nested
        elif isinstance(value, list):
            for item in value:
                nested = _find_value(item, names)
                if nested not in [None, ""]:
                    return nested
    return None


def extract_bom_technical_data(bom_json, customer_input):
    bom = _parse_json(bom_json)
    customer_input = customer_input or {}
    return {
        "wire_diameter": _number(_find_value(bom, [
            "wire_diameter",
            "wire_diameter_mm",
            "diameter_mm",
        ])),
        "turns": _number(_find_value(bom, [
            "turns",
            "total_turns",
            "number_of_turns",
        ])),
        "tin_thickness": _number(_find_value(bom, [
            "tin_thickness",
            "tin_thickness_micron",
            "tinning_thickness_micron",
        ])),
        "ferrite_diameter": _number(_find_value(bom, [
            "ferrite_diameter",
            "ferrite_diameter_mm",
            "core_diameter_mm",
        ])),
        "annual_quantity": _number(customer_input.get("annual_quantity") or _find_value(bom, ["annual_quantity"])),
        "left_direction_changes": _number(_find_value(bom, [
            "left_direction_changes",
            "left_leg_direction_changes",
        ])),
        "right_direction_changes": _number(_find_value(bom, [
            "right_direction_changes",
            "right_leg_direction_changes",
        ])),
        "raw_bom": bom,
    }


def determine_core_option(bom_json):
    bom = _parse_json(bom_json)
    text = _text(bom)
    glued_indicators = [
        "glue",
        "glued",
        "adhesive",
        "push-out",
        "push out",
        "without wire across face",
        "face without wire",
    ]
    locked_indicators = [
        "no glue",
        "locked",
        "wire passes on both flat ferrite faces",
        "wire across both faces",
    ]
    if any(indicator in text for indicator in glued_indicators):
        return "glued", ["Glued indicator found in BOM/drawing analysis."]
    if all(indicator in text for indicator in locked_indicators[:1]) and any(
        indicator in text for indicator in locked_indicators[1:]
    ):
        return "locked", ["Locked indicators found in BOM/drawing analysis."]
    return "glued", ["Core fixation uncertain; Olivier rule says if doubt, use glued."]


def _interpolated_winding_time_per_turn(wire_diameter):
    if wire_diameter is None:
        return None
    low_diameter = 0.3
    high_diameter = 2.5
    low_time = 0.19
    high_time = 0.37
    clamped = min(max(wire_diameter, low_diameter), high_diameter)
    ratio = (clamped - low_diameter) / (high_diameter - low_diameter)
    return low_time + ratio * (high_time - low_time)


def _work_package(
    work_package_id,
    component_id,
    component_type,
    operation_id,
    operation_name,
    operation_type,
    mode,
    previous_operations,
    p_h,
    oee,
    operator_percent,
    generic_capex_eur,
    specific_capex_eur,
    tooling_cost_eur,
    tooling_life_pieces=None,
    tooling_type=None,
    tooling_adder_per_piece_eur=None,
    components_used=None,
    quality_controls=None,
    source_rule=None,
    parts_per_cycle=1,
):
    return {
        "work_package_id": work_package_id,
        "component_id": component_id,
        "component_type": component_type,
        "operation_id": operation_id,
        "operation_name": operation_name,
        "operation_type": operation_type,
        "mode": mode,
        "previous_operations": previous_operations or [],
        "parts_per_cycle": parts_per_cycle,
        "p_h": p_h,
        "oee": oee,
        "operator_percent": operator_percent,
        "generic_capex_eur": generic_capex_eur,
        "specific_capex_eur": specific_capex_eur,
        "tooling_cost_eur": tooling_cost_eur,
        "tooling_life_pieces": tooling_life_pieces,
        "tooling_type": tooling_type,
        "tooling_adder_per_piece_eur": tooling_adder_per_piece_eur,
        "components_used": components_used or [],
        "quality_controls": quality_controls or [],
        "source_rule": source_rule,
        "save_address": None,
    }


def _core_operation(core_option, annual_quantity, previous_operations):
    high_volume = annual_quantity is not None and annual_quantity >= 800000
    if core_option == "locked" and not high_volume:
        return _work_package(
            "wp_20_locking",
            "core_fixation",
            "core_fixation",
            20,
            "locking",
            "core_fixation",
            "manual",
            previous_operations,
            800,
            0.8,
            100,
            2000,
            1000,
            1000,
            800000,
            "lifetime warranty",
            None,
            ["wire", "ferrite"],
            ["visual core lock"],
            "locked <800k Olivier rule",
        )
    if core_option == "locked":
        return _work_package(
            "wp_20_locking",
            "core_fixation",
            "core_fixation",
            20,
            "locking",
            "core_fixation",
            "automatic",
            previous_operations,
            1600,
            0.8,
            25,
            10000,
            5000,
            1000,
            800000,
            "lifetime warranty",
            None,
            ["wire", "ferrite"],
            ["visual core lock"],
            "locked >800k Olivier rule",
        )
    if not high_volume:
        return _work_package(
            "wp_20_gluing_baking",
            "ferrite",
            "core_fixation",
            20,
            "gluing_baking",
            "core_fixation",
            "manual",
            previous_operations,
            800,
            0.8,
            100,
            11000,
            1000,
            1000,
            800000,
            "lifetime warranty",
            None,
            ["wire", "ferrite", "glue"],
            ["glue presence", "baking"],
            "glued <800k Olivier rule",
        )
    return _work_package(
        "wp_20_gluing_baking",
        "ferrite",
        "core_fixation",
        20,
        "gluing_baking",
        "core_fixation",
        "automatic",
        previous_operations,
        1300,
        0.8,
        25,
        19000,
        1000,
        1000,
        800000,
        "lifetime warranty",
        None,
        ["wire", "ferrite", "glue"],
        ["glue presence", "baking"],
        "glued >800k Olivier rule",
    )


def _bending_count(left_changes, right_changes):
    left = left_changes or 0
    right = right_changes or 0
    if left > 2 and right > 2:
        return 2
    if left > 2 or right > 2:
        return 1
    return 0


def decompose_choke_process(bom_json, customer_input):
    technical = extract_bom_technical_data(bom_json, customer_input)
    missing_rules = []
    assumptions = []
    work_packages = []

    wire_diameter = technical["wire_diameter"]
    turns = technical["turns"]
    tin_thickness = technical["tin_thickness"]
    annual_quantity = technical["annual_quantity"]

    for field_name, value in [
        ("wire diameter", wire_diameter),
        ("total turns", turns),
        ("tin thickness", tin_thickness),
        ("annual_quantity", annual_quantity),
    ]:
        if value is None:
            missing_rules.append(field_name)

    core_option, core_assumptions = determine_core_option(bom_json)
    assumptions.extend(core_assumptions)

    if wire_diameter is not None and turns is not None and tin_thickness is not None:
        time_per_turn = _interpolated_winding_time_per_turn(wire_diameter)
        fixed_time = 1.5 if tin_thickness < 10 else 0.5
        cycle_time = fixed_time + turns * time_per_turn
        specific_capex = 14000 if wire_diameter < 1.6 else 18000
        specific_capex += 1000
        if tin_thickness < 10:
            specific_capex += 4000
        work_packages.append(_work_package(
            "wp_10_winding",
            "wire",
            "wire",
            10,
            "winding",
            "winding",
            "automatic",
            [],
            3600 / cycle_time,
            0.75,
            25,
            0,
            specific_capex,
            2500,
            250000,
            "first tooling prepaid + lifetime warranty renewal adder",
            0.002,
            ["wire", "ferrite"],
            ["winding count", "tin thickness"],
            "winding interpolation Olivier rule",
        ))

    if annual_quantity is not None:
        previous = [work_packages[-1]["work_package_id"]] if work_packages else []
        work_packages.append(_core_operation(core_option, annual_quantity, previous))

        bend_count = _bending_count(
            technical["left_direction_changes"],
            technical["right_direction_changes"],
        )
        for index in range(bend_count):
            operation_id = 25 + index
            work_packages.append(_work_package(
                f"wp_{operation_id}_manual_bend",
                "wire_forming",
                "wire",
                operation_id,
                f"manual_bend_{index + 1}",
                "extra_bending",
                "manual",
                [work_packages[-1]["work_package_id"]],
                800,
                0.8,
                100,
                2000,
                1000,
                1000,
                800000,
                "lifetime warranty",
                None,
                ["wire"],
                ["bend angle"],
                "extra bending Olivier rule",
            ))

        high_volume = annual_quantity >= 800000
        if core_option == "glued":
            if high_volume:
                work_packages.append(_work_package(
                    "wp_30_testing",
                    "finished_choke",
                    "finished_choke",
                    30,
                    "testing",
                    "quality_test",
                    "automatic",
                    [work_packages[-1]["work_package_id"]],
                    1400,
                    0.8,
                    20,
                    4000,
                    2000,
                    1000,
                    800000,
                    "lifetime warranty",
                    None,
                    ["finished_choke"],
                    ["push test"],
                    "glued >800k push test Olivier rule",
                ))
                missing_rules.append("final inspection packaging rule for glued >=800k")
            else:
                work_packages.append(_work_package(
                    "wp_30_testing",
                    "finished_choke",
                    "finished_choke",
                    30,
                    "testing",
                    "quality_test",
                    "manual",
                    [work_packages[-1]["work_package_id"]],
                    1200,
                    0.8,
                    100,
                    3000,
                    1000,
                    1000,
                    800000,
                    "lifetime warranty",
                    None,
                    ["finished_choke"],
                    ["push test"],
                    "glued <800k push test Olivier rule",
                ))
                work_packages.append(_work_package(
                    "wp_40_inspection_packaging",
                    "finished_choke",
                    "finished_choke",
                    40,
                    "inspection_packaging",
                    "inspection_packaging",
                    "manual",
                    [work_packages[-1]["work_package_id"]],
                    2500,
                    0.8,
                    100,
                    4000,
                    1000,
                    0,
                    None,
                    None,
                    None,
                    ["finished_choke"],
                    ["final inspection", "packaging"],
                    "glued <800k final inspection packaging Olivier rule",
                ))
        else:
            missing_rules.append(
                "final inspection packaging rule for locked <800k or locked >=800k"
            )

    return {
        "status": "blocked" if missing_rules and not work_packages else "created",
        "core_option": core_option,
        "work_packages": work_packages,
        "missing_rules": list(dict.fromkeys(missing_rules)),
        "assumptions": list(dict.fromkeys(assumptions)),
    }
