"""Voice agent system prompt — role prompt loading."""

import os

from loguru import logger


def get_role_prompt_text(role: str) -> str:
    """Read the prompt for a specific role from its dedicated file."""
    path = os.path.join(os.path.dirname(__file__), f"{role}_prompt.txt")
    if not os.path.exists(path):
        logger.error("Role prompt file missing for role={!r}: {}", role, os.path.abspath(path))
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_role_prompt_text(role: str, prompt: str) -> None:
    """Save the prompt for a specific role to its dedicated file."""
    path = os.path.join(os.path.dirname(__file__), f"{role}_prompt.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompt.strip())


def get_role_rag_source_text(role: str) -> str:
    """Read the RAG source text for a role from data/{role}/rag_source.txt."""
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", role)
    path = os.path.join(base, "rag_source.txt")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_role_rag_source_text(role: str, text: str) -> None:
    """Save the RAG source text for a role to data/{role}/rag_source.txt."""
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", role)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "rag_source.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip())


from typing import Optional


def _resolved_prompt_and_rag(
    role: str, role_config: Optional[dict] = None
) -> tuple[str, str]:
    """Non-empty SQLite (role_config) wins over files so restarts/deploys don't drop saves."""
    file_p = get_role_prompt_text(role)
    file_r = get_role_rag_source_text(role)
    db_p = ""
    db_r = ""
    if isinstance(role_config, dict):
        db_p = (role_config.get("prompt") or "").strip()
        db_r = (role_config.get("rag") or "").strip()
    from core.role_sandbox import coerce_role_prompt, coerce_role_rag

    return coerce_role_prompt(role, db_p, file_p), coerce_role_rag(role, db_r, file_r)


def _resolve_lead_name(lead: Optional[dict]) -> str:
    """Extract a clean first name from lead data, returning empty string if unavailable."""
    if not lead:
        return ""
    raw = str(lead.get("name") or "").strip()
    if not raw or raw.lower() in (
        "nan", "none", "null", "n/a", "na", "unknown", "-", "test", "demo", "testing",
        "{{name}}", "{name}", "[name]", "[customer name]",
    ):
        return ""
    return raw.split()[0]


def _replace_template_variables(prompt: str, lead: Optional[dict] = None) -> str:
    """Replace {{name}}, {name}, [Name], [Customer Name] placeholders with actual lead name.

    If name is missing/empty, removes the placeholder and surrounding punctuation
    to produce natural fallback text (e.g., 'Thank you.' instead of 'Thank you, {{name}}.').
    """
    import re
    from loguru import logger

    lead_name = _resolve_lead_name(lead)

    raw_lead_name = str((lead or {}).get("name") or "").strip()
    logger.debug(
        "Template substitution: raw_name={!r}, resolved_name={!r}",
        raw_lead_name, lead_name,
    )

    if lead_name:
        prompt = prompt.replace("{{name}}", lead_name)
        prompt = prompt.replace("{name}", lead_name)
        prompt = re.sub(r"\[name\]", lead_name, prompt, flags=re.IGNORECASE)
        prompt = re.sub(r"\[customer\s+name\]", lead_name, prompt, flags=re.IGNORECASE)
    else:
        prompt = prompt.replace("{{name}}", "[NAME NOT PROVIDED]")
        prompt = prompt.replace("{name}", "[NAME NOT PROVIDED]")
        prompt = re.sub(r"\[name\]", "[NAME NOT PROVIDED]", prompt, flags=re.IGNORECASE)
        prompt = re.sub(r"\[customer\s+name\]", "[NAME NOT PROVIDED]", prompt, flags=re.IGNORECASE)

    return prompt


def _validate_no_unresolved_placeholders(prompt: str) -> str:
    """Safety net: detect any remaining {{...}} template variables and strip them.
    
    Only removes {{double-brace}} patterns (actual template vars).
    Leaves {single-brace} patterns alone as they may be instructional text
    (e.g., 'That line is NOT "am I speaking with {name}?"').
    
    Returns the cleaned prompt. Logs a warning if placeholders were found.
    """
    import re
    from loguru import logger

    # Only match {{anything}} patterns (double-brace template variables)
    unresolved = re.findall(r"\{\{[^}]*\}\}", prompt)
    if unresolved:
        logger.warning("Unresolved {{template}} variables detected in prompt: {}", unresolved)
        # Remove any remaining {{...}} patterns
        prompt = re.sub(r"\{\{[^}]*\}\}", "", prompt)
        # Clean up double spaces left behind
        prompt = re.sub(r"  +", " ", prompt)
    return prompt


def build_role_system_prompt(role: str, role_config: Optional[dict] = None, lead: Optional[dict] = None) -> str:
    """Construct the final system prompt for the model, including knowledge base content."""
    from loguru import logger
    
    prompt, rag = _resolved_prompt_and_rag(role, role_config)
    if rag:
        prompt += f"\n\n[KNOWLEDGE BASE]\n{rag}"
    
    # Replace {{name}} / {name} placeholders with actual lead name
    prompt = _replace_template_variables(prompt, lead)
    
    # Safety net: strip any remaining unresolved placeholders
    prompt = _validate_no_unresolved_placeholders(prompt)
    
    logger.debug("Final system prompt (first 500 chars): {}", prompt[:500])
    
    return prompt
