"""LLM functions: translation, recording title and summary."""

import os

# since 1.104 litellm downloads its model price table on import and, offline, waits through
# retries on every start; the bundled table is enough: model limits come from the provider anyway
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from . import claude  # noqa: E402

__all__ = ["claude"]
