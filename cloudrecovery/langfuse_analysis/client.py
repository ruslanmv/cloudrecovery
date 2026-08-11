"""Thin wrapper around the Langfuse Python SDK for read-heavy diagnostic queries.

Supports Langfuse as either a **cloud** or a **local / self-hosted** backend:

* Cloud — set only ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` and the
  SDK default host (``https://cloud.langfuse.com``) is used. Set
  ``LANGFUSE_HOST=https://us.cloud.langfuse.com`` for the US region.
* Local / self-hosted — additionally set ``LANGFUSE_HOST``, e.g.
  ``http://localhost:3000`` or ``https://langfuse.your-domain.com``.

``LANGFUSE_HOST`` is therefore optional, not required: demanding it would
break every Langfuse Cloud user, who has no host to supply.

Compatibility note
-------------------
Langfuse's query surface has moved around across SDK/server versions, so the
helpers below probe rather than assume:

* Trace listing lives at ``client.api.trace.list(...)`` on current SDKs and
  at ``client.api.legacy.trace`` on some self-hosted v3 servers.
* That listing is **page**-based (``page=`` + ``from_timestamp=``/
  ``to_timestamp=``) on langfuse>=3; older builds used ``start_time=``/
  ``end_time=``. Both spellings are attempted.
* Observations (``client.api.observations.get_many``) are **cursor**-based
  (``meta.cursor``), which is a different pagination scheme from traces.

Every network-facing helper is bounded (``max_traces``, ``max_observations``)
so a collector polling this on an interval cannot be dragged into an
unbounded scan by a busy Langfuse project.
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

try:  # python-dotenv is a CloudRecovery dependency, but stay importable without it
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - optional convenience only
    pass

try:
    from langfuse import get_client

    _LANGFUSE_SDK_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    get_client = None  # type: ignore[assignment]
    _LANGFUSE_SDK_AVAILABLE = False

# Only the credentials are mandatory. Host defaults to Langfuse Cloud and is
# required in practice only for local / self-hosted deployments.
REQUIRED_ENV_VARS = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]

DEFAULT_CLOUD_HOST = "https://cloud.langfuse.com"

DEFAULT_MAX_TRACES = 500
DEFAULT_MAX_OBSERVATIONS = 2000


def sdk_available() -> bool:
    """True when the optional ``langfuse`` SDK is importable."""
    return _LANGFUSE_SDK_AVAILABLE


def describe_target() -> dict:
    """Report which Langfuse deployment this process is pointed at.

    Useful for surfacing "cloud vs local" in health output without leaking
    credentials — only the host and whether keys are present.
    """
    host = os.getenv("LANGFUSE_HOST") or DEFAULT_CLOUD_HOST
    is_cloud = "cloud.langfuse.com" in host
    return {
        "host": host,
        "deployment": "cloud" if is_cloud else "local",
        "host_explicitly_set": bool(os.getenv("LANGFUSE_HOST")),
        "credentials_present": all(bool(os.getenv(v)) for v in REQUIRED_ENV_VARS),
        "sdk_available": _LANGFUSE_SDK_AVAILABLE,
    }


def build_client():
    """Instantiate a Langfuse client from environment variables.

    Raises:
        OSError: if the SDK is missing or credentials are unset.
            Callers (the signal collector, the MCP tools) treat this as a
            configuration problem and degrade gracefully rather than crashing.
    """
    if not _LANGFUSE_SDK_AVAILABLE:
        raise OSError(
            "The 'langfuse' SDK is not installed. Install the optional extra: "
            'pip install "cloudrecovery[langfuse]"'
        )

    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise OSError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in your environment or .env file. For a local / self-hosted "
            "Langfuse, also set LANGFUSE_HOST (e.g. http://localhost:3000); "
            "for Langfuse Cloud, LANGFUSE_HOST may be omitted."
        )
    return get_client()


def _trace_api(client):
    """Return the trace-list resource, falling back to the legacy path if needed."""
    api = client.api
    if hasattr(api, "trace"):
        return api.trace
    if hasattr(api, "legacy") and hasattr(api.legacy, "trace"):
        return api.legacy.trace
    raise AttributeError(
        "Could not find a trace-list API on this Langfuse client. "
        "See the compatibility note at the top of client.py."
    )


def _observations_api(client):
    api = client.api
    if hasattr(api, "observations"):
        return api.observations
    if hasattr(api, "legacy") and hasattr(api.legacy, "observations"):
        return api.legacy.observations
    raise AttributeError(
        "Could not find an observations API on this Langfuse client. "
        "See the compatibility note at the top of client.py."
    )


def _list_traces_page(
    trace_api,
    *,
    page: int,
    limit: int,
    start_time: datetime,
    end_time: datetime,
    name: str | None,
    tags: Sequence[str] | None,
):
    """Fetch one page of traces, tolerating both timestamp kwarg spellings."""
    base: dict = {"limit": limit, "page": page}
    if name:
        base["name"] = name
    if tags:
        base["tags"] = list(tags)

    attempts = [
        {**base, "from_timestamp": start_time, "to_timestamp": end_time},
        {**base, "start_time": start_time, "end_time": end_time},
        base,
    ]

    last_error: TypeError | None = None
    for kwargs in attempts:
        try:
            return trace_api.list(**kwargs)
        except TypeError as e:  # unsupported kwarg on this SDK build
            last_error = e
    raise TypeError(
        f"Langfuse trace.list() rejected every known argument spelling: {last_error}"
    )


def _page_is_last(page_obj, *, returned: int, limit: int, page: int) -> bool:
    """Decide whether to stop paginating, across meta shapes."""
    if returned == 0 or returned < limit:
        return True

    meta = getattr(page_obj, "meta", None)
    if meta is None:
        return False

    # Current SDK: pydantic MetaResponse with total_pages.
    total_pages = getattr(meta, "total_pages", None)
    if isinstance(total_pages, int) and total_pages > 0:
        return page >= total_pages

    # Some builds hand back a plain dict instead.
    if isinstance(meta, dict):
        total_pages = meta.get("totalPages") or meta.get("total_pages")
        if isinstance(total_pages, int) and total_pages > 0:
            return page >= total_pages
        if "next_cursor" in meta or "nextCursor" in meta:
            return not (meta.get("next_cursor") or meta.get("nextCursor"))

    return False


def iter_traces(
    client,
    *,
    since_hours: float = 24,
    tags: Sequence[str] | None = None,
    name: str | None = None,
    limit_per_page: int = 50,
    max_traces: int = DEFAULT_MAX_TRACES,
) -> Iterator:
    """Yield trace summary objects created within the lookback window."""
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(hours=since_hours)
    trace_api = _trace_api(client)

    page = 1
    yielded = 0
    while True:
        page_obj = _list_traces_page(
            trace_api,
            page=page,
            limit=limit_per_page,
            start_time=start_time,
            end_time=end_time,
            name=name,
            tags=tags,
        )
        data = getattr(page_obj, "data", None) or []
        for trace in data:
            yield trace
            yielded += 1
            if yielded >= max_traces:
                return

        if _page_is_last(page_obj, returned=len(data), limit=limit_per_page, page=page):
            return
        page += 1


def _start_time_sort_key(observation) -> float:
    """Sort observations chronologically without tripping over None or mixed tzinfo."""
    value = getattr(observation, "start_time", None)
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()
        except Exception:  # pragma: no cover - defensive
            return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def get_observations_for_trace(
    client,
    trace_id: str,
    *,
    limit_per_page: int = 100,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
) -> list[Any]:
    """Return all observations belonging to a trace, ordered by start time."""
    obs_api = _observations_api(client)
    collected: list[Any] = []
    cursor: str | None = None

    while True:
        kwargs: dict = {"trace_id": trace_id, "limit": limit_per_page}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            result = obs_api.get_many(**kwargs)
        except TypeError:
            # Build without cursor support: take the single page we can get.
            result = obs_api.get_many(trace_id=trace_id, limit=limit_per_page)
            collected.extend(getattr(result, "data", None) or [])
            break

        data = getattr(result, "data", None) or []
        collected.extend(data)

        meta = getattr(result, "meta", None)
        next_cursor = getattr(meta, "cursor", None)
        if next_cursor is None and isinstance(meta, dict):
            next_cursor = meta.get("cursor") or meta.get("next_cursor")

        if not next_cursor or not data or len(collected) >= max_observations:
            break
        cursor = next_cursor

    collected.sort(key=_start_time_sort_key)
    return collected[:max_observations]


def _usage_mapping_total(usage: dict) -> int | None:
    total = usage.get("total")
    if total is None:
        total = usage.get("total_tokens") or usage.get("totalTokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return int(total)

    input_t = usage.get("input") or usage.get("promptTokens") or usage.get("input_tokens") or 0
    output_t = (
        usage.get("output") or usage.get("completionTokens") or usage.get("output_tokens") or 0
    )
    if input_t or output_t:
        try:
            return int(input_t) + int(output_t)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
    return None


def observation_token_total(observation) -> int:
    """Best-effort extraction of total tokens for an observation across SDK shapes."""
    for attr in ("usage_details", "usage"):
        usage = getattr(observation, attr, None)
        if isinstance(usage, dict):
            total = _usage_mapping_total(usage)
            if total is not None:
                return total
        elif usage is not None:
            # Older SDKs expose a Usage model rather than a mapping.
            for field in ("total", "total_tokens", "totalTokens"):
                value = getattr(usage, field, None)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
            input_t = getattr(usage, "input", None) or getattr(usage, "prompt_tokens", None) or 0
            output_t = (
                getattr(usage, "output", None) or getattr(usage, "completion_tokens", None) or 0
            )
            if input_t or output_t:
                return int(input_t) + int(output_t)
    return 0
