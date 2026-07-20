"""🔐 vault_get — let the agent read non-sensitive vault VALUES.

Strict contract: this tool ONLY returns kind="value" entries. If the
handle resolves to a kind="secret" entry, the tool refuses and tells
the model to use the substitution syntax (``{{secret:name}}``) inside
a shell command or code block instead.

This is the opacity boundary: the model is never given a path to a
plaintext secret. Substitution happens inside the shell tool, after
the model's args are formed but before exec — that lands in M56.
"""

from langchain_core.tools import tool


@tool
def vault_get(handle: str) -> str:
    """Read a non-sensitive value from the vault by its handle.

    Use this for stored URLs, account IDs, environment names, or any
    other non-secret reference value the user has saved. The result is
    the value as a plain string.

    REFUSES to return SECRET handles — those must be used via the
    ``{{secret:<handle>}}`` substitution syntax inside a shell command
    or code block (the shell tool resolves the placeholder at execution
    time so the value never enters your context).

    Examples:
      vault_get("prod_dashboard_url")  -> "https://dash.example.com/prod"
      vault_get("my_github_org")       -> "acmecorp"
    """
    from talos.infra.vault import resolve

    resolved = resolve(handle)
    if resolved is None:
        return f"Error: no vault handle named {handle!r}"
    if resolved.entry.kind == "secret":
        return (
            f"Error: {handle!r} is a SECRET — vault_get refuses to expose "
            "it. Use the substitution syntax in a shell command instead, "
            f'e.g. shell("curl -H \\"Authorization: {{secret:{handle}}}\\" ...")'
        )
    return resolved.value
