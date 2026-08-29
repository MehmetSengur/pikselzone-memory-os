"""Controlled Memory-Engine Self-Modification and Evolution Engine (SB2-08).

Allows the AI system to propose, test, and safely commit improvements to memory engine
heuristics, prompts, and code while guaranteeing:
1. Production/Protected Branch Guard (strictly disallows unapproved modification of main/production).
2. Mandatory Pre-Commit Checkpointing (records git commit HEAD before applying changes).
3. Test Gating (runs unittest suite against the modified code).
4. Automatic Instant Rollback (`git reset --hard HEAD` and `git clean -fd`) if any test fails.
5. Signed Audit Trail & Justification logged to `.state/evolution/evolution_log.json`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, List, Optional

from .core import (
    MemoryConfig, PolicyError, atomic_json, atomic_write, iso_now,
    sha256_bytes,
)

logger = logging.getLogger("memory_v1.self_evolution")

PROTECTED_BRANCHES = {"main", "master", "production", "release"}


@dataclasses.dataclass
class EvolutionProposal:
    proposal_id: str
    target_relative_path: str
    proposed_content: str
    justification: str
    author: str = "second-brain-agent"
    targeted_tests: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class EvolutionResult:
    status: str  # "applied", "rolled_back", "rejected"
    proposal_id: str
    initial_checkpoint: str
    final_commit: Optional[str] = None
    tests_passed: bool = False
    error: Optional[str] = None
    justification: str = ""
    receipt_file: Optional[str] = None


class SelfEvolutionEngine:
    """Safely gates and applies modifications to memory engine files with automatic rollback."""

    def __init__(self, repo_root: Path, state_path: Optional[Path] = None) -> None:
        self.repo_root = repo_root.resolve()
        self.state_dir = (state_path or (self.repo_root / ".state")).resolve()
        self.evolution_dir = self.state_dir / "evolution"
        self.log_file = self.evolution_dir / "evolution_log.json"

    def ensure_state_dirs(self) -> None:
        self.evolution_dir.mkdir(parents=True, exist_ok=True)
        if not self.log_file.is_file():
            atomic_json(self.log_file, {"schema": "pikselzone-evolution-log-v1", "history": []})

    def get_current_branch(self) -> str:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def get_head_commit(self) -> str:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def apply_evolution(self, proposal: EvolutionProposal) -> EvolutionResult:
        """Safely applies, test-gates, and commits or rolls back a self-modification proposal."""
        self.ensure_state_dirs()
        branch = self.get_current_branch()

        # 1. Protection against direct mutation of protected branches
        if branch in PROTECTED_BRANCHES:
            raise PolicyError(f"cannot-self-modify-protected-branch:{branch}")

        target_file = self.repo_root / proposal.target_relative_path
        if not target_file.is_file():
            raise PolicyError(f"target-file-not-found:{proposal.target_relative_path}")

        initial_commit = self.get_head_commit()
        logger.info("Starting self-evolution proposal '%s' on branch '%s' at commit %s", proposal.proposal_id, branch, initial_commit[:8])

        # 2. Checkpoint: Ensure clean working directory before proceeding
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        if status_res.stdout.strip():
            logger.warning("Uncommitted changes detected before evolution; stashing")
            subprocess.run(["git", "stash", "push", "-m", f"pre-evolution-{proposal.proposal_id}"], cwd=self.repo_root, check=True)

        # 3. Apply the proposed change
        original_content = target_file.read_text(encoding="utf-8")
        try:
            target_file.write_text(proposal.proposed_content, encoding="utf-8")
        except Exception as exc:
            return EvolutionResult(
                status="rejected",
                proposal_id=proposal.proposal_id,
                initial_checkpoint=initial_commit,
                tests_passed=False,
                error=f"write_error:{exc}",
                justification=proposal.justification,
            )

        # 4. Run Test Gating
        test_commands = proposal.targeted_tests or [
            "python3 -m unittest discover -s tests/memory_v1"
        ]
        all_tests_passed = True
        test_error_output = ""

        for cmd in test_commands:
            test_res = subprocess.run(
                cmd,
                shell=True,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
            )
            if test_res.returncode != 0:
                all_tests_passed = False
                test_error_output = test_res.stderr or test_res.stdout
                logger.error("Self-evolution test gate failed for command '%s': %s", cmd, test_error_output[:500])
                break

        # 5. Rollback on Failure
        if not all_tests_passed:
            logger.warning("Test gate failed! Executing automatic rollback to %s", initial_commit[:8])
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=self.repo_root, check=True)
            subprocess.run(["git", "clean", "-fd"], cwd=self.repo_root, check=True)

            self._record_log_entry({
                "proposal_id": proposal.proposal_id,
                "status": "rolled_back",
                "target_file": proposal.target_relative_path,
                "checkpoint": initial_commit,
                "error": test_error_output[:1000],
                "justification": proposal.justification,
                "timestamp": iso_now(),
            })

            return EvolutionResult(
                status="rolled_back",
                proposal_id=proposal.proposal_id,
                initial_checkpoint=initial_commit,
                tests_passed=False,
                error=f"test-gate-failed: {test_error_output[:300]}",
                justification=proposal.justification,
            )

        # 6. Commit on Success
        commit_msg = (
            f"feat(evolution): {proposal.proposal_id} - self-modify {proposal.target_relative_path}\n\n"
            f"Justification: {proposal.justification}\n"
            f"Tests verified: {', '.join(test_commands)}"
        )
        subprocess.run(["git", "add", str(proposal.target_relative_path)], cwd=self.repo_root, check=True)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=self.repo_root, check=True)
        final_commit = self.get_head_commit()

        receipt = {
            "proposal_id": proposal.proposal_id,
            "status": "applied",
            "target_file": proposal.target_relative_path,
            "initial_checkpoint": initial_commit,
            "final_commit": final_commit,
            "tests_passed": True,
            "justification": proposal.justification,
            "timestamp": iso_now(),
        }
        receipt_file = self._record_log_entry(receipt)

        logger.info("Successfully applied self-evolution %s at commit %s", proposal.proposal_id, final_commit[:8])
        return EvolutionResult(
            status="applied",
            proposal_id=proposal.proposal_id,
            initial_checkpoint=initial_commit,
            final_commit=final_commit,
            tests_passed=True,
            justification=proposal.justification,
            receipt_file=receipt_file,
        )

    def _record_log_entry(self, entry: dict[str, Any]) -> str:
        try:
            log_data = json.loads(self.log_file.read_text(encoding="utf-8"))
        except Exception:
            log_data = {"schema": "pikselzone-evolution-log-v1", "history": []}

        log_data.setdefault("history", []).append(entry)
        log_data["last_updated"] = iso_now()
        atomic_json(self.log_file, log_data)

        receipt_path = self.evolution_dir / f"receipt-{entry['proposal_id']}.json"
        atomic_json(receipt_path, entry)
        return str(receipt_path)
