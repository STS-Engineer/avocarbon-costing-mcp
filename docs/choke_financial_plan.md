# Choke Financial Plan V1

## Scope

The financial plan consumes the saved technical Choke result and commercial
assumptions. It does not trigger or recalculate agent outputs. V1 persists
versioned input and result JSON beside the workflow:

- `financial_plan_input.json`
- `financial_plan_result.json`
- `financial_price_solver_result.json`

The calculation is deterministic and records an input hash plus technical and
commercial source revisions.

## Technical handoff

The current plant basis is explicit:

```text
added_value_direct = DL + VOH + logistics
FOH = added_value_direct * FOH/DC rate
Fee = added_value_direct * Fee/DC rate
manufacturing_added_value = added_value_direct + FOH + Fee
total_before_commercial = base material + manufacturing_added_value
```

Logistics is therefore included once. Material is not part of the current
FOH/Fee basis.

## Annual formulas

The model uses Y-1 as discount period 0, Y0 as period 1, and Y6 as period 7.

```text
sales = quantity * selling price
GMDC = sales - material - transport - DL - VOH
EBITDA = GMDC - FOH - Fee
AR = sales / 365 * customer payment days
component AP = annual component purchase value / 365 * supplier payment days
TWC = AR + total inventory - AP
Delta TWC = current TWC - previous TWC
cash evaluation = EBITDA - Delta TWC - investment expenditure + collections
financial charge = max(0, -cash evaluation) * financing rate
operating result = EBITDA - depreciation - financial charge
taxes = max(0, operating result) * plant tax rate
annual cash flow = EBITDA - financial charge - investment expenditure
                   + collections - taxes - Delta TWC - business link
NPV = sum(annual cash flow * discount factor)
```

Straight-line generic CAPEX depreciation defaults to exactly five charges,
Y0 through Y4. Tooling and specific CAPEX follow their explicit treatment.

## Existing model audit

Existing concepts reused:

- `commercial_costing_parameter.sop_date`
- quantity, productivity scope and Y1-Y3 rates
- tooling payment/depreciation fields
- Incoterm, payment terms, delivery frequency and platform
- component offer payment days and Incoterm
- plant tax, FOH/DC and Fee/DC values
- MOST generic/specific CAPEX and tooling estimates

Durable schema gaps:

- annual quantity, price, indexation, P&L and cash-flow series
- normalized customer and supplier payment-day sources
- supplier zone relation and stock overrides
- configurable FOH/Fee basis
- CAPEX approval, ownership and collection schedules
- financial input/result revision tables
- product NPV/ROCE target semantics

V1 intentionally uses workflow JSON persistence rather than creating duplicate
database concepts. A reviewed migration should normalize these gaps after
Olivier confirms the commercial policies.

## Readiness

Firm mode is blocked by unresolved components. Preliminary mode may exclude an
unresolved component only when the exclusion is visible and is never marked
commercially usable. SOP, year quantities/rule, payment terms, Incoterms,
stock ownership inputs, tax, discount rate, profitability target, investment
treatment and required investment FX remain mandatory for an annual plan.
