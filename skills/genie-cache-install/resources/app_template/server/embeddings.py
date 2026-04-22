import os
import requests
from .config import get_oauth_token, get_workspace_host


EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")


def embed_text(text: str) -> list[float]:
    """Return a 1024-dim embedding for the given text.

    Uses Databricks Foundation Model serving endpoint (OpenAI-compatible).
    """
    host = get_workspace_host()
    token = get_oauth_token()
    url = f"{host}/serving-endpoints/{EMBEDDING_ENDPOINT}/invocations"
    payload = {"input": [text]}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Coerce every element to float; pgvector/psycopg rejects mixed int/float lists.
    return [float(x) for x in data["data"][0]["embedding"]]
