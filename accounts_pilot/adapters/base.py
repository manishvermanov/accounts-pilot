"""OTA Adapter contract.

An adapter encodes ONE OTA's onboarding wizard:
  - the ordered step graph
  - which steps are AUTO (engine fills) vs GATE (human/credential/verification)
  - the Profile -> OTA field mapping
  - the selectors

Adding a new OTA = writing one subclass. The engine, runtime, gates, state
machine, and audit log are all shared and OTA-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime


class StepKind(str, Enum):
    AUTO = "auto"     # engine fills from the profile
    GATE = "gate"     # human / credential / verification seam
    SYSTEM = "system" # OTA-side (submit, review) — engine waits/polls


@dataclass
class Step:
    key: str
    title: str
    kind: StepKind
    gate: Optional[GateKind] = None        # set when kind == GATE
    needs_stealth: bool = False            # launch CloakBrowser for this step
    url: Optional[str] = None              # wizard page, if it has a stable URL


class GateRequired(Exception):
    """Raised by an AUTO/GATE step when it hits a point only a human (or an
    auto-resolver: OTP/CAPTCHA) can pass. The state machine parks the job."""

    def __init__(self, gate: GateKind, message: str = ""):
        self.gate = gate
        super().__init__(message or f"gate required: {gate.value}")


class OTAAdapter(ABC):
    ota: str = "base"

    @abstractmethod
    def steps(self) -> list[Step]:
        """The ordered wizard step graph."""

    @abstractmethod
    def run_step(self, step: Step, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        """Execute one step. AUTO steps fill fields; GATE steps raise GateRequired
        (unless an auto-resolver handles them upstream)."""

    # convenience
    def step_by_key(self, key: str) -> Step:
        for s in self.steps():
            if s.key == key:
                return s
        raise KeyError(f"{self.ota}: no step '{key}'")

    def next_step(self, after: Optional[str]) -> Optional[Step]:
        steps = self.steps()
        if after is None:
            return steps[0] if steps else None
        for i, s in enumerate(steps):
            if s.key == after and i + 1 < len(steps):
                return steps[i + 1]
        return None
