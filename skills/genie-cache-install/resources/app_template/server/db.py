import os
import psycopg
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector
from .config import get_workspace_client


INSTANCE_NAME = os.environ["LAKEBASE_INSTANCE_NAME"]
PGHOST = os.environ["PGHOST"]
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ["PGDATABASE"]
PGSSLMODE = os.environ.get("PGSSLMODE", "require")


def _resolve_username() -> str:
    """Username for Postgres login.

    In a Databricks App, the runtime injects PGUSER (service principal ID).
    Locally, fall back to the current user's email.
    """
    pg_user = os.environ.get("PGUSER")
    if pg_user:
        return pg_user
    w = get_workspace_client()
    me = w.current_user.me()
    return me.user_name


def _generate_pg_token() -> str:
    """Generate a short-lived OAuth token for the Lakebase instance."""
    w = get_workspace_client()
    cred = w.database.generate_database_credential(
        instance_names=[INSTANCE_NAME],
        request_id="genie-cache-app",
    )
    return cred.token


class OAuthConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        kwargs["password"] = _generate_pg_token()
        conn = super().connect(conninfo, **kwargs)
        register_vector(conn)
        return conn


_username = _resolve_username()

pool = ConnectionPool(
    conninfo=(
        f"dbname={PGDATABASE} user={_username} host={PGHOST} "
        f"port={PGPORT} sslmode={PGSSLMODE}"
    ),
    connection_class=OAuthConnection,
    min_size=1,
    max_size=8,
    max_lifetime=2700,
    open=False,
)
