"""
salesforce/writer.py

Thin wrapper around simple-salesforce's write methods.

Two operations:
  execute_update(object_name, record_id, payload) → dict
  execute_create(object_name, payload)            → dict

These are the only Salesforce write calls in the entire codebase.
All higher-level logic (ordering, locks, retries) lives in batchqueue/.

simple-salesforce return values:
  .update() → HTTP status code (int): 204 on success; raises on error
  .create() → {"id": "...", "errors": [], "success": True}; raises on error

Exceptions (SalesforceError and subclasses) are NOT caught here —
they propagate to batchqueue/executor.py which decides how to handle them.
"""

import logging

from salesforce.client import get_client

logger = logging.getLogger(__name__)


def execute_update(object_name: str, record_id: str, payload: dict) -> dict:
    """
    PATCH /sobjects/{object_name}/{record_id} with payload.

    Returns {"id": record_id, "status": http_status_code}.
    Raises SalesforceError on any API error.
    """
    sf = get_client()
    sobject = getattr(sf, object_name)
    http_status = sobject.update(record_id, payload)

    logger.info(
        "SF update | obj=%s | id=%s | fields=%s | status=%d",
        object_name, record_id, list(payload.keys()), http_status,
    )
    return {"id": record_id, "status": http_status}


def execute_create(object_name: str, payload: dict) -> dict:
    """
    POST /sobjects/{object_name} with payload.

    Returns {"id": "...", "errors": [], "success": True} on success.
    Raises SalesforceError on any API error.
    """
    sf = get_client()
    sobject = getattr(sf, object_name)
    result = sobject.create(payload)

    logger.info(
        "SF create | obj=%s | new_id=%s | fields=%s",
        object_name, result.get("id"), list(payload.keys()),
    )
    return result
