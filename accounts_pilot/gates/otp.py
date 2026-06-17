"""OTP gate — prompt the owner to enter the verification code.

When an OTA sends a one-time code to the owner's email or phone, the service
pauses and asks them to type it in. No inbox/SMS integration — the person who
owns the account reads their own code and enters it.

The prompt function is injectable: here it's a console prompt; in the product UI
it becomes an input box (pass your own `prompt_fn`).
"""
from __future__ import annotations

from typing import Callable, Optional

PromptFn = Callable[[str], Optional[str]]


def _console_prompt(channel: str) -> Optional[str]:
    where = "phone (SMS)" if channel == "sms" else "email"
    try:
        code = input(f"  ↳ Enter the OTP sent to your {where}: ").strip().lstrip("﻿")
        return code or None
    except (EOFError, KeyboardInterrupt):
        return None


class OTPResolver:
    def __init__(self, prompt_fn: Optional[PromptFn] = None):
        self.prompt_fn = prompt_fn or _console_prompt

    def try_resolve(self, *, channel: str = "email", timeout_s: int = 0) -> Optional[str]:
        """Ask the owner for the code they received (email or phone). Returns it, or None."""
        return self.prompt_fn(channel)
