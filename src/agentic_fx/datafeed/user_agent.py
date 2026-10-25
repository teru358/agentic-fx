"""The one User-Agent used for outbound news requests."""
from functools import lru_cache
from importlib import metadata


@lru_cache(maxsize=1)
def user_agent() -> str:
    """Return package identity, without falling back to httpx's default."""
    try:
        version = metadata.version("agentic-fx")
    except metadata.PackageNotFoundError:
        version = "unknown"
    try:
        project_urls = metadata.metadata("agentic-fx").get_all("Project-URL", [])
        for project_url in project_urls:
            label, _, value = project_url.partition(",")
            if label.strip().lower() == "repository" and value.strip():
                return f"agentic-fx/{version} (+{value.strip()})"
    except metadata.PackageNotFoundError:
        pass
    return f"agentic-fx/{version}"
