def get_demo_bom_output_3165001(customer_input):
    return {
        "project_code": "24003-CHO-00",
        "product_name": "Fuse choke",
        "part_number": "316-5001",
        "bom_status": "partial_with_assumptions",
        "status": "agent_sample_output",
        "technical_data": {
            "wire_diameter_mm": 1.25,
            "total_turns": 14,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5.2,
            "ferrite_length_mm": 20.5,
            "glue_requirement": "ambiguous",
            "left_direction_changes": 0,
            "right_direction_changes": 0,
        },
        "components": [
            {
                "component_id": "316-5001-ferrite",
                "component_type": "ferrite",
                "quantity_per_choke": 1,
                "quantity_per_product": 1,
                "diameter_mm": 5.2,
                "length_mm": 20.5,
                "material_family": "to_confirm",
                "costing_assumption": "use cheapest standard ferrite option until Fe/Ni or Fe/Mg confirmed",
                "inductance_uH": 30,
                "turns_effective": 13,
                "turns_total": 14,
                "estimated_mu_r": 136.36,
                "costing_route": "external_component_costing_agent",
                "missing_data": [
                    "confirmed Fe/Ni or Fe/Mg",
                    "grade",
                    "exact ferrite length",
                    "density",
                ],
            },
            {
                "component_id": "316-5001-wire",
                "component_type": "enameled_wire",
                "quantity_per_choke": 1,
                "quantity_per_product": 1,
                "wire_diameter_mm": 1.25,
                "total_turns": 14,
                "effective_turns": 13,
                "total_engaged_length_mm": 298.43,
                "copper_weight_g": 3.281,
                "finished_wire_weight_assumption_g": 3.33,
                "costing_route": "external_component_costing_agent",
                "scope_note": "raw enameled copper wire only; exclude winding, forming, tooling, fixture and added value",
                "missing_data": [
                    "temperature class",
                    "insulation grade",
                    "full formed geometry",
                    "enamel allowance",
                ],
            },
            {
                "component_id": "316-5001-tin",
                "component_type": "tin",
                "quantity_per_choke": 2,
                "quantity_per_product": 2,
                "stripped_tinned_length_each_side_mm": 3,
                "tin_thickness_micron": 20,
                "tin_weight_g": 0.0035,
                "costing_route": "material_price_lookup",
                "missing_data": [
                    "tin alloy",
                    "confirmed tin thickness",
                ],
            },
            {
                "component_id": "316-5001-glue",
                "component_type": "glue",
                "quantity_per_choke": None,
                "quantity_per_product": None,
                "glue_requirement": "ambiguous",
                "ferrite_diameter_mm": 5.2,
                "drawing_mentions_glue": False,
                "push_out_force_specified": False,
                "costing_route": "rule_based",
                "missing_data": [
                    "glue required or locked confirmation",
                ],
            },
        ],
        "assumptions": [
            "Preliminary demo BOM, not released for commercial quote.",
            "Ferrite material family is not confirmed; cheapest standard ferrite option is used only for demo costing.",
            "Glue requirement is ambiguous; process route uses glued by doubt rule.",
        ],
        "points_to_confirm": [
            "Fe/Ni or Fe/Mg ferrite family",
            "ferrite grade and density",
            "wire temperature class and insulation grade",
            "tin alloy and confirmed tin thickness",
            "locked versus glued fixation confirmation",
        ],
        "customer_input_reference": {
            "project_code": (customer_input or {}).get("project_code"),
            "product_id": (customer_input or {}).get("product_id"),
        },
    }


def get_demo_component_cost_outputs_3165001():
    return [
        {
            "component_id": "316-5001-ferrite",
            "component_type": "ferrite",
            "status": "demo_preliminary",
            "origin_classification": "External",
            "recommended_offer": {
                "origin_classification": "External",
                "origin": "China",
                "purchasing_currency": "CNY",
                "reporting_currency": "CNY",
                "selling_price_per_unit": 0.120,
                "selling_price_converted_per_unit": 0.120,
                "incoterm": "FCA",
                "commercially_usable": False,
                "confirmation_status": "unconfirmed",
                "confirmation_gaps": [
                    "Fe/Ni or Fe/Mg not confirmed",
                    "ferrite grade not confirmed",
                    "supplier quote not confirmed",
                ],
                "supply_chain": {
                    "transportation_cost": 0.005,
                    "custom_duty_cost": 0.0,
                    "forwarder_cost": 0.001,
                    "capital_cost_12pct": 0.003,
                    "delivered_cost": 0.129,
                    "assumptions": ["China local supply to Kunshan demo assumption"],
                },
            },
            "normalized_cost": {
                "currency": "CNY",
                "material_cost_per_piece": 0.120,
                "delivered_cost_per_piece": 0.129,
                "commercially_usable": False,
            },
        },
        {
            "component_id": "316-5001-wire",
            "component_type": "enameled_wire",
            "status": "demo_preliminary_raw_material_only",
            "origin_classification": "External",
            "scope_note": "raw enameled copper wire only; winding/forming/tooling excluded",
            "recommended_offer": {
                "origin_classification": "External",
                "origin": "China",
                "purchasing_currency": "CNY",
                "reporting_currency": "CNY",
                "selling_price_per_unit": 0.328,
                "selling_price_converted_per_unit": 0.328,
                "incoterm": "FCA",
                "commercially_usable": False,
                "confirmation_status": "unconfirmed",
                "confirmation_gaps": [
                    "wire temperature class not confirmed",
                    "enamel grade not confirmed",
                    "copper index/source not confirmed",
                ],
                "supply_chain": {
                    "transportation_cost": 0.003,
                    "custom_duty_cost": 0.0,
                    "forwarder_cost": 0.001,
                    "capital_cost_12pct": 0.001,
                    "delivered_cost": 0.333,
                    "assumptions": ["Raw wire material only, no winding/forming conversion"],
                },
            },
            "normalized_cost": {
                "currency": "CNY",
                "material_cost_per_piece": 0.328,
                "delivered_cost_per_piece": 0.333,
                "commercially_usable": False,
            },
        },
    ]


def get_demo_tin_cost():
    tin_weight_g = 0.0035
    demo_tin_price_cny_per_kg = 200
    cost = tin_weight_g / 1000 * demo_tin_price_cny_per_kg
    return {
        "component_id": "316-5001-tin",
        "component_type": "tin",
        "status": "demo_material_lookup_preliminary",
        "tin_weight_g": tin_weight_g,
        "demo_tin_price_cny_per_kg": demo_tin_price_cny_per_kg,
        "commercially_usable": False,
        "normalized_cost": {
            "currency": "CNY",
            "material_cost_per_piece": cost,
            "delivered_cost_per_piece": cost,
            "commercially_usable": False,
            "missing_inputs": ["tin alloy confirmation", "tin price confirmation"],
        },
    }


def get_demo_glue_cost():
    return {
        "component_id": "316-5001-glue",
        "component_type": "glue",
        "status": "pending_confirmation",
        "reason": "No glue material cost added because requirement is ambiguous and ferrite diameter is below 6mm.",
        "commercially_usable": False,
        "normalized_cost": {
            "currency": "CNY",
            "material_cost_per_piece": 0,
            "delivered_cost_per_piece": 0,
            "commercially_usable": False,
            "missing_inputs": ["glue required or locked confirmation"],
        },
    }
