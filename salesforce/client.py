"""
salesforce/client.py

Responsibilities:
  - Authenticate to Salesforce once at startup using simple-salesforce
  - Expose a singleton Salesforce connection to every module that needs it
  - Provide the instance URL (used to construct record links in responses)

Why a singleton?
  Every Salesforce API call needs an authenticated session. Authentication
  involves an HTTP round-trip to Salesforce's login endpoint and returns a
  session token. Doing this per-call would be wasteful and slow. One shared
  connection reuses the same session token for its lifetime.

Authentication method used: username + password + security token
  Salesforce requires a "security token" appended to the password for logins
  from unrecognized IP addresses. For sandbox orgs, the domain is "test"
  (login.salesforce.com → test.salesforce.com). For production it is "login".
  simple-salesforce handles this via the `domain` parameter.

Input:  cfg.sf_* values from config.py
Output: a simple_salesforce.Salesforce instance via get_client()

Usage:
    from salesforce.client import init, get_client

    init()                    # call once at startup
    sf = get_client()         # call anywhere, returns the same connection
    result = sf.Account.get('001ABC')
"""

import logging
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed
from config import cfg

logger = logging.getLogger(__name__)

# Module-level singleton — one authenticated session for the process lifetime
_client: Salesforce | None = None


def init() -> None:
    """
    Authenticate to Salesforce and store the connection as a module singleton.

    simple-salesforce's Salesforce() constructor makes the login HTTP call
    immediately — if credentials are wrong, it raises SalesforceAuthenticationFailed
    here at startup rather than on the first real API call.

    Raises:
        SalesforceAuthenticationFailed: wrong username/password/token/domain
        Exception: network issues, Salesforce outage, etc.
    """
    global _client

    logger.info(
        "Connecting to Salesforce | user=%s | domain=%s",
        cfg.sf_username, cfg.sf_domain
    )

    _client = Salesforce(
        username=cfg.sf_username,
        password=cfg.sf_password,
        security_token=cfg.sf_security_token,
        domain=cfg.sf_domain,
        # version is not pinned — simple-salesforce defaults to the highest
        # API version available on the org, which is what we want
    )

    logger.info(
        "Salesforce connected | instance=%s | session_id=...%s",
        _client.sf_instance,
        _client.session_id[-6:],   # log only last 6 chars for security
    )


def get_client() -> Salesforce:
    """
    Return the shared authenticated Salesforce connection.

    Raises:
        RuntimeError: if init() was never called (startup ordering bug)
    """
    if _client is None:
        raise RuntimeError(
            "Salesforce client not initialized. "
            "Call salesforce.client.init() before using get_client()."
        )
    return _client


def get_instance_url() -> str:
    """
    Return the base URL of the Salesforce org.

    Used by the approval formatter and executor to construct clickable
    record links, e.g.:
        https://myorg.my.salesforce.com/006XXXXXXXXXXXXXXX

    Raises:
        RuntimeError: if init() was never called
    """
    sf = get_client()
    # sf_instance is e.g. "myorg.my.salesforce.com" (no trailing slash, no scheme)
    return f"https://{sf.sf_instance}"


def record_url(record_id: str) -> str:
    """
    Construct a direct link to a Salesforce record.

    Args:
        record_id: the 15 or 18 character Salesforce record ID

    Returns:
        Full URL string, e.g.:
        "https://myorg.my.salesforce.com/006XXXXXXXXXXXXXXX"
    """
    return f"{get_instance_url()}/{record_id}"
