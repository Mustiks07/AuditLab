"""Independent public-only financial verifier (SPEC §10). Never reads private/ or truth."""
from .core import verify_report, verify_snapshot

__all__ = ["verify_report", "verify_snapshot"]
