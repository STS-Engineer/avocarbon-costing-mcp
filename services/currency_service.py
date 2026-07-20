"""Currency normalization and fixed master-data FX resolution."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Dict, Optional


_ALIASES = {"RMB": "CNY"}
_KNOWN_CODES = set("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND
BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU
CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD
OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD
TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU
XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA YER ZAR ZMW ZWG
""".split())


def normalize_currency_code(value: Any) -> Optional[str]:
    """Return a supported ISO currency code without guessing from symbols."""
    if value is None:
        return None
    code = str(value).strip().upper()
    if not code:
        return None
    code = _ALIASES.get(code, code)
    if not re.fullmatch(r"[A-Z]{3}", code) or code not in _KNOWN_CODES:
        return None
    return code


def resolve_project_currency(project_currency: Any, plant_selling_currency: Any) -> Optional[str]:
    """Project currency may fall back to plant selling currency; offers may not."""
    return normalize_currency_code(project_currency) or normalize_currency_code(plant_selling_currency)


def _number(value: Any) -> Optional[float]:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _explicit_rate(source: str, destination: str, rates: Any) -> Optional[Dict[str, Any]]:
    if not rates:
        return None
    if isinstance(rates, dict):
        direct = _number(rates.get(f"{source}_to_{destination}"))
        if direct and direct > 0:
            return {"rate": direct, "rate_date": None, "rate_source": "provided_fx_rates"}
        reverse = _number(rates.get(f"{destination}_to_{source}"))
        if reverse and reverse > 0:
            return {"rate": 1 / reverse, "rate_date": None, "rate_source": "provided_fx_rates_inverse"}
        records = rates.get("rates") if isinstance(rates.get("rates"), list) else []
    elif isinstance(rates, list):
        records = rates
    else:
        records = []
    for row in records:
        if not isinstance(row, dict):
            continue
        row_source = normalize_currency_code(row.get("source_currency") or row.get("from_currency"))
        row_destination = normalize_currency_code(row.get("destination_currency") or row.get("to_currency"))
        rate = _number(row.get("rate") or row.get("exchange_rate") or row.get("monthly_rate"))
        if row_source == source and row_destination == destination and rate and rate > 0:
            return {
                "rate": rate,
                "rate_date": row.get("rate_date") or row.get("effective_date"),
                "rate_source": row.get("source") or "provided_fx_records",
            }
    return None


def _database_rate(source: str, destination: str) -> Optional[Dict[str, Any]]:
    """Read a direct pair from the configured fixed monthly-rate table.

    Column names are discovered from the table and limited to known semantic
    aliases. Ambiguous base-currency table shapes are intentionally rejected.
    """
    try:
        from sqlalchemy import MetaData, Table, create_engine, inspect, select
    except Exception:
        return None

    source_names = ("source_currency", "from_currency", "currency_from")
    destination_names = ("destination_currency", "to_currency", "currency_to")
    rate_names = ("exchange_rate", "monthly_rate", "rate")
    date_names = ("rate_date", "effective_date", "month", "created_at")
    for env_name in ("KPI_DB_FINAL_URL", "DATABASE_URL"):
        database_url = str(os.getenv(env_name) or "").strip()
        if not database_url:
            continue
        try:
            engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
            inspector = inspect(engine)
            if not inspector.has_table("material_exchange_monthly_rate", schema="public"):
                engine.dispose()
                continue
            table = Table("material_exchange_monthly_rate", MetaData(), schema="public", autoload_with=engine)
            columns = set(table.c.keys())
            source_col = next((name for name in source_names if name in columns), None)
            destination_col = next((name for name in destination_names if name in columns), None)
            rate_col = next((name for name in rate_names if name in columns), None)
            date_col = next((name for name in date_names if name in columns), None)
            if not source_col or not destination_col or not rate_col:
                engine.dispose()
                continue
            query = select(table).where(
                table.c[source_col] == source,
                table.c[destination_col] == destination,
            )
            if date_col:
                query = query.order_by(table.c[date_col].desc())
            with engine.connect() as connection:
                row = connection.execute(query.limit(1)).mappings().first()
            engine.dispose()
            if row:
                rate = _number(row.get(rate_col))
                if rate and rate > 0:
                    rate_date = row.get(date_col) if date_col else None
                    if isinstance(rate_date, (date, datetime)):
                        rate_date = rate_date.isoformat()
                    return {
                        "rate": rate,
                        "rate_date": rate_date,
                        "rate_source": f"{env_name}.material_exchange_monthly_rate",
                    }
        except Exception:
            continue
    return None


def resolve_exchange_rate(
    source_currency: Any,
    destination_currency: Any,
    rates: Any = None,
    allow_database: bool = True,
) -> Dict[str, Any]:
    source = normalize_currency_code(source_currency)
    destination = normalize_currency_code(destination_currency)
    base = {"source_currency": source, "destination_currency": destination}
    if not source or not destination:
        return {**base, "status": "missing", "reason": "currency_missing"}
    if source == destination:
        return {**base, "status": "found", "rate": 1.0, "rate_date": None, "rate_source": "same_currency"}
    result = _explicit_rate(source, destination, rates)
    if result is None and allow_database and rates is None:
        result = _database_rate(source, destination)
    if result is None:
        return {**base, "status": "missing", "reason": "exchange_rate_missing"}
    return {**base, "status": "found", **result}


def convert_currency(amount: Any, source_currency: Any, destination_currency: Any, rates: Any = None) -> Dict[str, Any]:
    numeric = _number(amount)
    fx = resolve_exchange_rate(source_currency, destination_currency, rates=rates)
    if numeric is None:
        return {**fx, "status": "missing", "reason": "amount_missing", "original_amount": None}
    if fx.get("status") != "found":
        return {**fx, "original_amount": numeric, "converted_amount": None}
    return {
        **fx,
        "original_amount": numeric,
        "converted_amount": numeric * fx["rate"],
    }
