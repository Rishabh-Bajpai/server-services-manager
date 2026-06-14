"""Command palette: fuzzy-searchable index of everything the app can do.

Items have a kind (page / program / system_service / control / cron /
notification / activity), a label, optional description, and a
``run`` dict describing what the frontend should do when the user
selects the item.

The fuzzy match is a simple subsequence + character-bonus scorer —
no external dependency.
"""
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("Palette")


@dataclass
class PaletteItem:
    kind: str
    id: str
    label: str
    description: str = ""
    run: dict = field(default_factory=dict)  # {"type": "navigate"|"action", "url": ..., "method": ...}


def _normalize(s: str) -> str:
    return s.lower().strip()


def _score(needle: str, haystack: str) -> int:
    """Subsequence match scorer. Higher = better.

    Bonus for matching at word boundaries, contiguous runs, and exact
    prefix. Returns 0 if ``needle`` is not a subsequence of ``haystack``.
    """
    if not needle:
        return 0
    h = _normalize(haystack)
    n = _normalize(needle)
    hi = 0
    score = 0
    streak = 0
    matched_positions = []
    while hi < len(h) and len(matched_positions) < len(n):
        if h[hi] == n[len(matched_positions)]:
            matched_positions.append(hi)
            streak += 1
            score += 1 + streak
            # Boundary bonus
            if hi == 0 or not h[hi - 1].isalnum():
                score += 3
        else:
            streak = 0
        hi += 1
    if len(matched_positions) != len(n):
        return 0
    # Earlier match = better
    score += max(0, 10 - matched_positions[0])
    # Exact-prefix bonus
    if h.startswith(n):
        score += 20
    # Shorter haystack = better (more focused)
    score += max(0, 30 - len(h))
    return score


def search(items: List[PaletteItem], query: str, limit: int = 20) -> List[dict]:
    """Return up to ``limit`` items matching ``query``, best first."""
    q = _normalize(query)
    if not q:
        # Empty query: surface common actions
        results = items[:limit]
    else:
        scored = []
        for item in items:
            s = _score(q, item.label) * 2
            s += _score(q, item.description)
            s += _score(q, item.kind)
            s += _score(q, item.id)
            if s > 0:
                scored.append((s, item))
        scored.sort(key=lambda x: (-x[0], x[1].label))
        results = [item for _, item in scored[:limit]]
    return [_to_dict(i) for i in results]


def _to_dict(item: PaletteItem) -> dict:
    return {
        "kind": item.kind,
        "id": item.id,
        "label": item.label,
        "description": item.description,
        "run": item.run,
    }


def build_palette_index(get_programs, get_control_commands, get_system_units) -> List[PaletteItem]:
    """Build the full index of palette items.

    All three callbacks are lazy so config changes are picked up.
    """
    items: List[PaletteItem] = []

    # Pages
    items += [
        PaletteItem("page", "dashboard", "Dashboard", "Main service dashboard", {"type": "navigate", "url": "/"}),
        PaletteItem("page", "monitor", "System Monitor", "Real-time CPU/memory/network charts", {"type": "navigate", "url": "/monitor"}),
        PaletteItem("page", "control", "Control Panel", "Quick system commands", {"type": "navigate", "url": "/control"}),
        PaletteItem("page", "system-services", "System Services", "Browse and control systemd units", {"type": "navigate", "url": "/system-services"}),
        PaletteItem("page", "cron", "Cron Jobs", "Inspect and toggle cron entries", {"type": "navigate", "url": "/cron"}),
        PaletteItem("page", "notifications", "Health & Notifications", "Monitored services and events", {"type": "navigate", "url": "/notifications"}),
        PaletteItem("page", "activity", "Activity Log", "Audit trail of all actions", {"type": "navigate", "url": "/activity"}),
    ]

    # Managed programs
    for p in (get_programs() or []):
        name = p.config.name
        items += [
            PaletteItem("program", f"{name}:start", f"Start {name}",
                        "Start the managed service",
                        {"type": "action", "method": "POST",
                         "url": f"/programs/{name}/start"}),
            PaletteItem("program", f"{name}:stop", f"Stop {name}",
                        "Stop the managed service",
                        {"type": "action", "method": "POST",
                         "url": f"/programs/{name}/stop"}),
            PaletteItem("program", f"{name}:restart", f"Restart {name}",
                        "Restart the managed service",
                        {"type": "action", "method": "POST",
                         "url": f"/programs/{name}/restart"}),
        ]

    # Systemd units (top of list, so common units are easy to find)
    for u in (get_system_units() or [])[:50]:
        items += [
            PaletteItem("system_service", f"{u.name}:start", f"Start {u.name}",
                        f"systemd: {u.description[:50]}",
                        {"type": "action_with_auth", "method": "POST",
                         "url": f"/api/system-services/{u.name}/start"}),
            PaletteItem("system_service", f"{u.name}:stop", f"Stop {u.name}",
                        f"systemd: {u.description[:50]}",
                        {"type": "action_with_auth", "method": "POST",
                         "url": f"/api/system-services/{u.name}/stop"}),
        ]

    # Control panel commands
    for cmd in (get_control_commands() or []):
        items.append(PaletteItem(
            "control", cmd["id"], cmd["name"], cmd.get("description", ""),
            {"type": "action_with_auth", "method": "POST",
             "url": "/api/control/run", "body": {"command": cmd["id"]}},
        ))

    return items
