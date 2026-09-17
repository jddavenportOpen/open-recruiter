"""The verification engine: typeset a resume, prove a machine can read it back,
and turn a three-persona panel into one defensible number.

Zero third-party dependencies, by policy. Every module here is stdlib-only so the
engine can run in CI, in a cron job, or on a stranger's laptop with no install
step. `openrecruiter.channels` and the application loop are allowed dependencies;
this package must never import them.
"""
from . import pdfwrite, pdftext, render_resume, format_qa, parse_check, panel, goals  # noqa: F401

__all__ = ["pdfwrite", "pdftext", "render_resume", "format_qa",
           "parse_check", "panel", "goals"]
