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
financial charge = selected financing balance * financing rate
operating result = EBITDA - depreciation - financial charge
taxes = max(0, operating result) * plant tax rate
annual cash flow = EBITDA - financial charge - investment expenditure
                   + collections - taxes - Delta TWC - business link
NPV = sum(annual cash flow * discount factor)

The scenario selling-price solver fixes the discount rate at 12% and targets
NPV = 0. It is always labelled `scenario_solver` and is never commercially
usable. The existing product master field is
`products.roce_target_percent`; it is reported with provenance, but the
approved product-price solver remains blocked until the business maps ROCE to
an NPV residual.

## Confirmed Olivier Spicker rules

- Accounts payable is calculated component by component. Each component output
  supplies its own payment days and `ap_value_basis`
  (`base_purchase_value` or `delivered_purchase_value`). Preliminary mode may
  exclude an incomplete AP line with a warning; firm mode blocks.
- Choke WIP uses
  `annual quantity / 365 * WIP days * (Material + (DL + VOH + FOH) / 2)`.
  `Material` must be explicitly selected as `base_material` or
  `delivered_material`. Delivered material already contains logistics, so
  transport is not added again. Preliminary mode visibly defaults to base
  material; firm mode blocks without an approved selection.
- Financing is accumulated. Positive cash first repays opening debt; the 8%
  charge supports `closing_balance`, `opening_balance`, or `average_balance`.
  The current closing-balance policy is provisional when no approved setting
  is supplied, and the closing balance carries to the following year.
- Straight-line depreciation starts in Y1 and has exactly five charges,
  ending in Y5.
- Provisional glue consumption uses 80% of confirmed ferrite length, a 1 mm
  cylindrical strip, and density 1.5 g/cm3. It is preliminary only until
  explicitly approved.
```

Straight-line generic CAPEX depreciation defaults to exactly five charges,
Y1 through Y5. Tooling and specific CAPEX follow their explicit treatment.

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
