"""Shared Flyte task environments, image, and secrets for all pipelines.

One :class:`flyte.Image` (flyte + litellm + httpx) backs every pipeline. Two
secrets are injected as environment variables:

* ``github-token``  -> ``GITHUB_TOKEN``       (repo read/write + PR/comment API)
* ``anthropic-api-key`` -> ``ANTHROPIC_API_KEY`` (litellm provider key)

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
ANTHROPIC_SECRET = flyte.Secret(key="anthropic-api-key", as_env_var="ANTHROPIC_API_KEY")
SECRETS = [GITHUB_SECRET, ANTHROPIC_SECRET]


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

# ---------------------------------------------------------------------------
# Deterministic quality gate environment
# ---------------------------------------------------------------------------
# Separate image/env from the lightweight LLM-only `env` above: the gate needs
# git + uv (to clone the target repo and install its real dependencies) and
# more CPU/memory/disk, but makes no LLM calls, so it doesn't need the
# Anthropic secret.
gate_image = (
    flyte.Image.from_debian_base()
    .with_apt_packages("git")
    .with_pip_packages("uv>=0.4")
)

gate_env = flyte.TaskEnvironment(
    name="flyte_agent_loop_gate",
    image=gate_image,
    resources=flyte.Resources(cpu="2", memory="4Gi", disk="10Gi"),
    secrets=[GITHUB_SECRET],
    env_vars=_passthrough_env_vars(),
)
env.add_dependency(gate_env)
