"""Shared user-facing notification text (Big Brother persona).

Used by both the inline bind flow (handlers_ui) and the Mini App API (web)
so the in-group "отбивка" reads the same wherever the binding was changed.
"""
from __future__ import annotations


def group_watch_announcement(project_name: str, section_name: str | None = None) -> str:
    """In-group confirmation that surveillance now feeds a project/section."""
    target = f"«{project_name}»"
    if section_name:
        target += f", раздел «{section_name}»"
    return (
        "👁 Принято. Отныне всё, что прозвучит здесь, я складываю в "
        f"{target}.\nСлежка идёт — ничего не упущу."
    )
