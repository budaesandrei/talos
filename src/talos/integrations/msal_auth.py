"""🔐 MSAL (Microsoft Entra ID) auth — enterprise BYOK without static keys.

Many corporate LLM gateways sit behind Azure AD: instead of a long-lived
API key you get an app registration (client_id + client_secret +
tenant_id) and exchange it for a short-lived bearer token via the OAuth2
client-credentials flow. Talos does that exchange with MSAL when the
three ``TALOS_MSAL_*`` settings are present.

Refresh is free: ``ConfidentialClientApplication`` keeps an in-memory
token cache and ``acquire_token_for_client`` returns the cached token
until shortly before expiry, then silently fetches a new one — so calling
``get_token()`` per request is cheap and always valid.

Install the optional dependency with:  pip install -e ".[msal]"
"""

import threading

from talos.config import settings

_app = None
_lock = threading.Lock()


def msal_enabled() -> bool:
    """True when all three client-credential settings are configured."""
    return bool(
        settings.msal_client_id
        and settings.msal_client_secret
        and settings.msal_tenant_id
    )


def _application():
    """The MSAL app, built once — it owns the token cache."""
    global _app
    if _app is None:
        try:
            import msal
        except ImportError as exc:
            raise RuntimeError(
                "MSAL auth is configured (TALOS_MSAL_*) but the msal "
                "package is not installed — run: pip install -e '.[msal]'"
            ) from exc
        with _lock:
            if _app is None:
                _app = msal.ConfidentialClientApplication(
                    settings.msal_client_id,
                    authority=(
                        "https://login.microsoftonline.com/"
                        f"{settings.msal_tenant_id}"
                    ),
                    client_credential=settings.msal_client_secret,
                    # honor TALOS_VERIFY_SSL for corporate re-signing
                    # proxies — msal uses requests, not our httpx clients
                    verify=settings.verify_ssl,
                )
    return _app


def get_token() -> str:
    """A currently-valid access token (cached by MSAL, renewed as needed)."""
    scope = settings.msal_scope or f"api://{settings.msal_client_id}/.default"
    result = _application().acquire_token_for_client(scopes=[scope])
    if "access_token" not in result:
        raise RuntimeError(
            "MSAL token acquisition failed: "
            f"{result.get('error', '?')} — "
            f"{str(result.get('error_description', ''))[:200]}"
        )
    return result["access_token"]
