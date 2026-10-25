"""The one User-Agent used for outbound news requests."""
from importlib import metadata


def user_agent() -> str:
    """Return package identity, without falling back to httpx's default."""
    try:
        version = metadata.version("agentic-fx")
    except metadata.PackageNotFoundError:
        version = "unknown"
    try:
        repository = metadata.metadata("agentic-fx").get("Project-URL", "")
        # Metadata represents this as "Repository, URL".
        label, _, value = repository.partition(",")
        if label.strip().lower() == "repository" and value.strip():
            return f"agentic-fx/{version} (+{value.strip()})"
    except metadata.PackageNotFoundError:
        pass
    return f"agentic-fx/{version}"
