from typing import Dict


def bom_conversation_key(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
) -> str:
    trigger_run_id = str(trigger_run_id or "").strip()
    if not trigger_run_id:
        raise ValueError("BOM invocation requires trigger_run_id.")
    return f"{project_code}:{product_id}:sequential:bom:{trigger_run_id}"


def bom_idempotency_key(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
) -> str:
    return bom_conversation_key(project_code, product_id, trigger_run_id)


def bom_invocation_identifiers(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
) -> Dict[str, str]:
    return {
        "conversation_key": bom_conversation_key(
            project_code,
            product_id,
            trigger_run_id,
        ),
        "idempotency_key": bom_idempotency_key(
            project_code,
            product_id,
            trigger_run_id,
        ),
    }
