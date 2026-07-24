"""Deterministic quality gate: clone the target repo, apply the staged plan,
install real dependencies, and run pytest + ruff for real.

Runs AFTER the LLM verifier passes, as an additional per-attempt check in the
SAME build<->verify retry loop used by builder_agent.py / reviewer_agent.py. A
gate failure is fed back into that loop exactly like a verifier rejection —
this is not a hard stop.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import flyte

from .config import load_settings
from .environments import gate_env

# --- bounded subprocess timeouts (seconds) --------------------------------
CLONE_TIMEOUT_S = 60
INSTALL_TIMEOUT_S = 180
RUFF_TIMEOUT_S = 30
PYTEST_TIMEOUT_S = 150
MAX_GATE_OUTPUT_CHARS = 8000  # tail-truncated, mirrors agents.MAX_FILE_CHARS


@dataclass
class GateResult:
    passed: bool
    stage: str  # "skipped" | "clone" | "apply" | "install" | "ruff" | "pytest" | "ok"
    summary: str
    ruff_output: str = ""
    pytest_output: str = ""
    install_output: str = ""
    error: str = ""
    timed_out: bool = False
    duration_s: float = 0.0


def _tail(text: str, limit: int = MAX_GATE_OUTPUT_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters omitted ...]\n" + text[-limit:]


def render_gate_feedback(result: "GateResult") -> str:
    """Render a failed GateResult as retry-prompt feedback text (mirrors render_plan_files)."""
    if result.passed:
        return "(quality gate passed)"
    lines = [f"AUTOMATED QUALITY GATE FAILED at stage '{result.stage}': {result.summary}"]
    if result.error:
        lines.append(f"Error: {result.error}")
    if result.install_output:
        lines.append(f"--- dependency install output (tail) ---\n{_tail(result.install_output)}")
    if result.ruff_output:
        lines.append(f"--- ruff check output (tail) ---\n{_tail(result.ruff_output)}")
    if result.pytest_output:
        lines.append(f"--- pytest output (tail) ---\n{_tail(result.pytest_output)}")
    if result.timed_out:
        lines.append(
            "NOTE: this stage TIMED OUT. Consider whether your change introduced a hang, an "
            "infinite loop, or an unusually slow test/install."
        )
    return "\n\n".join(lines)


def _safe_join(root: Path, rel_path: str) -> Path:
    """Join rel_path under root, rejecting path traversal (files keys are LLM-generated)."""
    p = (root / rel_path).resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"unsafe staged path escapes workspace: {rel_path!r}")
    return p


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _run(cmd: list[str], cwd: Path, timeout: float, token: str = "") -> tuple[int, str, bool]:
    """Run a subprocess with a hard timeout. Never raises — timeouts/failures become
    a (returncode, output, timed_out) tuple so a hung install/test can't stall or
    crash the task."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
        )
        out = _redact(proc.stdout + proc.stderr, token)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired as exc:
        out = _redact((exc.stdout or "") + (exc.stderr or ""), token)
        return -1, out + f"\n[TIMED OUT after {timeout}s]", True
    except Exception as exc:  # e.g. binary not found
        return -1, _redact(str(exc), token), False


def _clone(repo: str, ref: str, token: str, dest: Path) -> tuple[bool, str]:
    url = f"https://github.com/{repo}.git"
    code, out, timed_out = _run(
        ["git", "-c", f"http.extraHeader=Authorization: Bearer {token}",
         "clone", "--depth", "1", "--branch", ref, "--single-branch", url, str(dest)],
        cwd=dest.parent, timeout=CLONE_TIMEOUT_S, token=token,
    )
    return code == 0 and not timed_out, out


def _apply_files(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = _safe_join(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _detect_and_install(root: Path) -> tuple[bool, str]:
    """Set up a venv and install real deps with uv, AFTER files are applied
    (so a staged pyproject.toml/requirements.txt is picked up). Returns
    (has_python_project, combined_output)."""
    has_pyproject = (root / "pyproject.toml").exists()
    has_requirements = (root / "requirements.txt").exists()
    if not has_pyproject and not has_requirements:
        return False, ""

    out_parts = []
    code, out, _ = _run(["uv", "venv"], cwd=root, timeout=30)
    out_parts.append(out)
    if code != 0:
        return True, "\n".join(out_parts)

    if has_pyproject:
        code, out, timed_out = _run(["uv", "sync", "--all-extras"], cwd=root, timeout=INSTALL_TIMEOUT_S)
        out_parts.append(out)
        if code != 0 or timed_out:
            code, out, _ = _run(["uv", "pip", "install", "-e", "."], cwd=root, timeout=INSTALL_TIMEOUT_S)
            out_parts.append(out)
    elif has_requirements:
        code, out, _ = _run(
            ["uv", "pip", "install", "-r", "requirements.txt"], cwd=root, timeout=INSTALL_TIMEOUT_S
        )
        out_parts.append(out)

    # Always guarantee the gate's own tools exist, regardless of what the target
    # repo declares as dev dependencies.
    code, out, _ = _run(["uv", "pip", "install", "pytest", "ruff"], cwd=root, timeout=60)
    out_parts.append(out)
    return True, "\n".join(out_parts)


@gate_env.task(timeout=flyte.Timeout(max_runtime=360))  # 6 min outer safety net
async def run_quality_gate(ref: str, files: dict[str, str]) -> GateResult:
    """Clone `ref` of the configured target repo, apply `files` on top, install
    real deps, and run ruff + pytest for real. Never raises: every failure mode
    (clone, apply, install, timeout, lint, test) becomes a GateResult.
    """
    settings = load_settings()
    start = time.monotonic()
    workspace = Path(tempfile.mkdtemp(prefix="agent-loop-gate-"))
    try:
        repo_dir = workspace / "repo"
        ok, clone_out = await asyncio.to_thread(_clone, settings.repo, ref, settings.github_token, repo_dir)
        if not ok:
            return GateResult(False, "clone", "git clone failed or timed out",
                               error=_tail(clone_out), duration_s=time.monotonic() - start,
                               timed_out="TIMED OUT" in clone_out)

        try:
            await asyncio.to_thread(_apply_files, repo_dir, files)
        except Exception as exc:
            return GateResult(False, "apply", f"failed to apply staged files: {exc}",
                               duration_s=time.monotonic() - start)

        has_py, install_out = await asyncio.to_thread(_detect_and_install, repo_dir)
        if not has_py:
            return GateResult(True, "skipped", "no Python project detected (no pyproject.toml/"
                               "requirements.txt) — gate skipped", duration_s=time.monotonic() - start)

        venv_python = repo_dir / ".venv" / "bin" / "python"
        if not venv_python.exists():
            return GateResult(False, "install", "dependency install failed (no venv produced)",
                               install_output=_tail(install_out), duration_s=time.monotonic() - start)

        # Scope ruff to ONLY the files the agent touched — a repo-wide ruff run
        # would fail the gate on pre-existing violations unrelated to this change.
        changed_paths = [p for p in files if p.endswith(".py")]
        ruff_out = "(no changed .py files — ruff skipped)"
        if changed_paths:
            code, ruff_out, ruff_to = await asyncio.to_thread(
                _run, [str(venv_python), "-m", "ruff", "check", *changed_paths],
                repo_dir, RUFF_TIMEOUT_S,
            )
            if code != 0:
                return GateResult(False, "ruff", "ruff check found issues in the changed files",
                                   ruff_output=_tail(ruff_out), install_output=_tail(install_out),
                                   timed_out=ruff_to, duration_s=time.monotonic() - start)

        # pytest runs over the WHOLE suite (unscoped): a change can break tests
        # elsewhere in the repo, and that's exactly what this gate exists to catch.
        code, pytest_out, pytest_to = await asyncio.to_thread(
            _run, [str(venv_python), "-m", "pytest", "-q"], repo_dir, PYTEST_TIMEOUT_S,
        )
        if code != 0:
            return GateResult(False, "pytest", "pytest failed" + (" (timed out)" if pytest_to else ""),
                               pytest_output=_tail(pytest_out), ruff_output=ruff_out,
                               install_output=_tail(install_out), timed_out=pytest_to,
                               duration_s=time.monotonic() - start)

        return GateResult(True, "ok", "ruff + pytest passed", ruff_output=ruff_out,
                           pytest_output=_tail(pytest_out), duration_s=time.monotonic() - start)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
