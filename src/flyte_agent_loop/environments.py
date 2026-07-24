"""Shared Flyte task environments, image, and secrets for all pipelines.

One :class:`flyte.Image` (flyte + litellm + httpx) backs every pipeline. Two
secrets are injected as environment variables:

* ``github-token``  -> ``GITHUB_TOKEN``       (repo read/write + PR/comment API)
* the model provider key, selected by ``FLYTE_AGENT_PROVIDER`` (default
  ``anthropic``): ``anthropic-api-key`` -> ``ANTHROPIC_API_KEY``, or
  ``openrouter-api-key`` -> ``OPENROUTER_API_KEY`` when set to ``openrouter``
  (pair with a ``FLYTE_AGENT_MODEL`` like ``openrouter/anthropic/claude-sonnet-4.5``
  — litellm's provider-prefixed convention). Trial addition: no standalone
  Anthropic Console key was available, but a personal OpenRouter key already
  was, so the provider became selectable instead of hardcoded.

Non-secret configuration (target repo, model, agent id, dibs TTL, memory key)
is captured from the deploying machine's environment into ``env_vars`` so a
plain ``flyte deploy`` bakes it into the scheduled runs. Override any of them at
deploy time, e.g.::

    FLYTE_AGENT_REPO=unionai/flyte FLYTE_AGENT_MODEL=claude-sonnet-4-5 \\
        python -m flyte_agent_loop.deploy
"""

from __future__ import annotations

import os

import flyte

from . import config

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
image = (
    flyte.Image.from_debian_base()
    .with_pip_packages(
        "litellm>=1.55",
        "httpx>=0.27",
    )
)

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
GITHUB_SECRET = flyte.Secret(key="github-token", as_env_var=config.ENV_GITHUB_TOKEN)
# Provider key, resolved at DEPLOY time (read from the deploying machine's own
# env, same as _passthrough_env_vars below) so a `flyte create secret` under
# either name works without editing this file per run.
_PROVIDER = os.environ.get("FLYTE_AGENT_PROVIDER", "anthropic").strip().lower()
PROVIDER_SECRET = (
    flyte.Secret(key="openrouter-api-key", as_env_var="OPENROUTER_API_KEY")
    if _PROVIDER == "openrouter"
    else flyte.Secret(key="anthropic-api-key", as_env_var="ANTHROPIC_API_KEY")
)
SECRETS = [GITHUB_SECRET, PROVIDER_SECRET]


def _passthrough_env_vars() -> dict[str, str]:
    """Capture non-secret config from the deploy-time environment.

    Only keys that are actually set are included; unset ones fall back to the
    defaults in :mod:`flyte_agent_loop.config` at runtime.
    """
    keys = [
        config.ENV_REPO,
        config.ENV_MODEL,
        config.ENV_AGENT_ID,
        config.ENV_DIBS_TTL_MINUTES,
        config.ENV_MEMORY_KEY,
        config.ENV_GITHUB_API,
        config.ENV_MAX_TOKENS,
        config.ENV_MAX_TRIES,
        config.ENV_HTTP_TIMEOUT,
        config.ENV_HTTP_RETRIES,
    ]
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


env = flyte.TaskEnvironment(
    name="flyte_agent_loop",
    image=image,
    resources=flyte.Resources(cpu="1", memory="2Gi"),
    secrets=SECRETS,
    env_vars=_passthrough_env_vars(),
)
