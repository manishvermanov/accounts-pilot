"""MIS integration — search the company MIS for a hotel and convert its
record into the PropertyProfile JSON the onboarding engine consumes.

The operator never pastes JSON: they search a hotel name, pick it, and this
package turns the raw MIS row into a validated profile behind the scenes.
"""
from accounts_pilot.mis.convert import normalize_to_profile, summarize
from accounts_pilot.mis.provider import MisProvider, get_provider

__all__ = ["normalize_to_profile", "summarize", "MisProvider", "get_provider"]
