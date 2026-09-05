"""Firewall manager for ufw and firewalld.

The manager auto-detects which firewall is in use and provides a
common API for status, rules, and rule manipulation. All write
operations require the user's app password (piped to ``sudo -S`` for
that single command), matching the systemd unit / cron toggle pattern.

Read operations (``is_available``, ``get_status``, ``list_rules``) work
without sudo.

If neither ufw nor firewalld is installed (or the right tools aren't
on PATH), :func:`is_available` returns ``False`` with a human-readable
reason so the UI can explain what's missing.
"""
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("FirewallManager")

DEFAULT_SUDO_TOOL = "sudo"


class FirewallError(Exception):
    """Raised when a firewall operation fails."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


@dataclass
class Rule:
    number: int
    action: str        # "ALLOW" | "DENY" | "REJECT" | "LIMIT"
    to: str            # "Anywhere", "192.168.1.0/24", etc.
    port_proto: str    # "22/tcp", "80", "Anywhere", ...

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "action": self.action,
            "to": self.to,
            "port_proto": self.port_proto,
        }


@dataclass
class Status:
    available: bool
    backend: str       # "ufw" | "firewalld" | "none"
    enabled: bool
    default_incoming: str
    default_outgoing: str
    default_routed: str
    rules: List[Rule]
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "backend": self.backend,
            "enabled": self.enabled,
            "default_incoming": self.default_incoming,
            "default_outgoing": self.default_outgoing,
            "default_routed": self.default_routed,
            "rules": [r.to_dict() for r in self.rules],
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def is_available() -> dict:
    """Probe which firewall backend (if any) is present on this host."""
    has_ufw = shutil.which("ufw") is not None
    has_firewalld = shutil.which("firewall-cmd") is not None
    if not has_ufw and not has_firewalld:
        return {
            "available": False,
            "backend": "none",
            "reason": "Neither ufw nor firewalld is installed on this system.",
        }
    if has_ufw:
        return {"available": True, "backend": "ufw", "reason": ""}
    return {"available": True, "backend": "firewalld", "reason": ""}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd: List[str], timeout: int = 10, password: Optional[str] = None) -> str:
    """Run a command; pipe ``password`` to sudo if provided."""
    try:
        if password is not None and cmd and cmd[0] == "sudo":
            proc = subprocess.run(
                cmd, input=password + "\n", capture_output=True, text=True, timeout=timeout,
            )
        else:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        raise FirewallError(f"command timed out: {' '.join(cmd)}", code="timeout")
    except FileNotFoundError as e:
        raise FirewallError(f"tool missing: {e}", code="missing_tool")

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit {proc.returncode}"
        first_line = next(
            (ln for ln in err.splitlines() if ln.strip() and "password for" not in ln),
            err.splitlines()[0] if err.splitlines() else err,
        )
        lower = err.lower()
        if "password" in lower or "incorrect" in lower or "not in the sudoers" in lower:
            code = "permission"
        elif "command not found" in lower or "no such file" in lower:
            code = "missing_tool"
        else:
            code = "error"
        raise FirewallError(first_line, code=code)
    return (proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# ufw backend
# ---------------------------------------------------------------------------

def _ufw_status(password: Optional[str] = None) -> Status:
    """Read ufw status and parse into a Status object.

    Tries ``sudo -S ufw status verbose numbered`` first (works if the
    user's sudo timestamp is fresh, or a password is provided). If
    that fails, falls back to plain ``ufw status`` (which works for
    root or NOPASSWD sudo). Returns a Status with whatever info could
    be read on partial failure.
    """
    out = ""
    try:
        out = _run(["sudo", "-S", "ufw", "status", "verbose", "numbered"],
                   password=password)
    except FirewallError:
        if password is not None:
            # Password was given but sudo didn't accept it; don't retry
            raise
        # No password provided; try plain ufw status as fallback
        try:
            out = _run(["ufw", "status"], timeout=10)
        except FirewallError as e:
            # Last-resort: return a minimal Status with the error reason
            return Status(
                available=True, backend="ufw", enabled=False,
                default_incoming="", default_outgoing="", default_routed="",
                rules=[], reason=f"ufw status unavailable: {e}",
            )

    enabled = "Status: active" in out
    backend = "ufw"

    # Default policies
    default_incoming = ""
    default_outgoing = ""
    default_routed = ""
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"Default:\s*(\w+)\s*\(\w+\)", line)
        if m:
            # The line lists incoming, outgoing, routed in that order
            parts = re.findall(r"(\w+)\s*\(\w+\)", line)
            if len(parts) >= 1:
                default_incoming = parts[0].lower()
            if len(parts) >= 2:
                default_outgoing = parts[1].lower()
            if len(parts) >= 3:
                default_routed = parts[2].lower()

    # Rules
    rules: List[Rule] = []
    in_rules = False
    for raw in out.splitlines():
        line = raw.rstrip()
        if "----" in line:
            in_rules = True
            continue
        if not in_rules:
            continue
        line = line.strip()
        if not line:
            continue
        # "To                         Action      From"
        if line.startswith("To") and "Action" in line:
            continue
        # Format: "[ 1] 22/tcp                     ALLOW IN    Anywhere"
        m = re.match(r"\[\s*(\d+)\]\s+(.+?)\s+(ALLOW|DENY|REJECT|LIMIT)\s+(IN|OUT)?\s*(.*)", line)
        if m:
            num = int(m.group(1))
            port_proto = m.group(2).strip()
            action = m.group(3)
            where = m.group(5).strip()
            rules.append(Rule(
                number=num,
                action=action,
                to=where or "Anywhere",
                port_proto=port_proto,
            ))
            continue
        # Unnumbered or app rule lines
        m2 = re.match(r"(.+?)\s+(ALLOW|DENY|REJECT|LIMIT)\s+(\S+)", line)
        if m2:
            port_proto = m2.group(1).strip()
            action = m2.group(2)
            where = m2.group(3).strip()
            rules.append(Rule(
                number=len(rules) + 1,
                action=action,
                to=where,
                port_proto=port_proto,
            ))

    return Status(
        available=True,
        backend=backend,
        enabled=enabled,
        default_incoming=default_incoming,
        default_outgoing=default_outgoing,
        default_routed=default_routed,
        rules=rules,
    )


def _ufw_apply(action: str, password: str) -> str:
    """Run an ufw action verb (enable, disable, reload)."""
    if action not in ("enable", "disable", "reload"):
        raise FirewallError(f"unknown ufw action: {action}", code="invalid")
    return _run(["sudo", "-S", "ufw", action], password=password, timeout=15)


def _ufw_set_default(policy: str, direction: str, password: str) -> str:
    """Set the default policy. ``policy`` in allow/deny/reject, ``direction`` in incoming/outgoing/routed."""
    if policy not in ("allow", "deny", "reject"):
        raise FirewallError(f"invalid policy: {policy}", code="invalid")
    if direction not in ("incoming", "outgoing", "routed"):
        raise FirewallError(f"invalid direction: {direction}", code="invalid")
    return _run(["sudo", "-S", "ufw", "default", policy, direction],
                password=password, timeout=15)


# Rule forms:
#   port/<proto>           — single port + protocol
#   from <ip> to any port <port>  — source-restricted rule
#   <port>:<proto>         — numeric port with colon-separated protocol

def _ufw_add_rule(spec: dict, password: str) -> str:
    """Add a ufw rule. ``spec`` keys: action, port, protocol, source."""
    action = spec.get("action", "allow").lower()
    port = str(spec.get("port", "")).strip()
    protocol = spec.get("protocol", "").lower().strip()
    source = spec.get("source", "").strip()

    if action not in ("allow", "deny", "reject", "limit"):
        raise FirewallError(f"invalid action: {action}", code="invalid")
    if not port:
        raise FirewallError("port is required", code="invalid")

    cmd = ["sudo", "-S", "ufw", action]
    if source:
        cmd += ["from", source]
    cmd += ["to", "any", "port", port]
    if protocol and protocol not in ("any", "tcp", "udp"):
        raise FirewallError(f"invalid protocol: {protocol}", code="invalid")
    if protocol in ("tcp", "udp"):
        cmd.append(protocol)
    return _run(cmd, password=password, timeout=15)


def _ufw_delete_rule(spec: dict, password: str) -> str:
    """Delete a rule either by number or by re-stating it.

    For spec-based delete, the ufw command is:
        ufw delete [allow|deny|reject] [<port>/<proto>]
    not the verbose "from X to any port Y" form used for adds.
    """
    if "number" in spec and spec["number"]:
        try:
            num = int(spec["number"])
        except (TypeError, ValueError):
            raise FirewallError(f"invalid rule number: {spec['number']}", code="invalid")
        return _run(["sudo", "-S", "ufw", "delete", str(num)],
                    password=password, timeout=15)
    action = spec.get("action", "allow").lower()
    port = str(spec.get("port", "")).strip()
    protocol = spec.get("protocol", "").lower().strip()
    if not port:
        raise FirewallError("port is required", code="invalid")
    cmd = ["sudo", "-S", "ufw", "delete", action, port]
    if protocol in ("tcp", "udp"):
        cmd.append(protocol)
    return _run(cmd, password=password, timeout=15)


# ---------------------------------------------------------------------------
# firewalld backend
# ---------------------------------------------------------------------------

def _firewalld_status(password: Optional[str] = None) -> Status:
    try:
        state_out = _run(["firewall-cmd", "--state"], timeout=10)
    except FirewallError as e:
        # ``firewall-cmd --state`` returns "not running" with exit 252
        # when the service is down. Treat that as a valid disabled state.
        if "not running" in str(e).lower():
            return Status(
                available=True, backend="firewalld", enabled=False,
                default_incoming="", default_outgoing="", default_routed="",
                rules=[],
                reason="firewalld is not running",
            )
        raise
    enabled = state_out.strip().lower() == "running"
    # Defaults
    default_zone = _run(["firewall-cmd", "--get-default-zone"], timeout=10).strip()
    # List all rules in the default zone
    list_out = _run(["firewall-cmd", "--zone", default_zone, "--list-all"], timeout=10)
    rules: List[Rule] = []
    for raw in list_out.splitlines():
        line = raw.strip()
        if line.startswith("ports:"):
            parts = line.split(":", 1)[1].strip()
            for p in parts.split():
                rules.append(Rule(
                    number=len(rules) + 1,
                    action="ALLOW",
                    to="Anywhere",
                    port_proto=p,
                ))
            continue
        if line.startswith("services:"):
            parts = line.split(":", 1)[1].strip()
            for s in parts.split():
                rules.append(Rule(
                    number=len(rules) + 1,
                    action="ALLOW",
                    to="Anywhere",
                    port_proto=f"service:{s}",
                ))
            continue
        if line.startswith("rich rules:"):
            parts = line.split(":", 1)[1].strip()
            for r in parts.split(";"):
                r = r.strip()
                if r:
                    rules.append(Rule(
                        number=len(rules) + 1,
                        action="ALLOW",
                        to="Anywhere",
                        port_proto=f"rule:{r[:60]}",
                    ))
    return Status(
        available=True,
        backend="firewalld",
        enabled=enabled,
        default_incoming=default_zone,
        default_outgoing="",
        default_routed="",
        rules=rules,
    )


def _firewalld_apply(action: str, password: str) -> str:
    """enable / disable the firewalld service (systemd-level, not runtime)."""
    if action not in ("enable", "disable", "reload"):
        raise FirewallError(f"unknown firewalld action: {action}", code="invalid")
    if action == "enable":
        return _run(["sudo", "-S", "systemctl", "enable", "--now", "firewalld"],
                    password=password, timeout=20)
    if action == "disable":
        return _run(["sudo", "-S", "systemctl", "disable", "--now", "firewalld"],
                    password=password, timeout=20)
    return _run(["sudo", "-S", "firewall-cmd", "--reload"], password=password, timeout=15)


def _firewalld_add_rule(spec: dict, password: str) -> str:
    action = spec.get("action", "allow").lower()
    port = str(spec.get("port", "")).strip()
    protocol = spec.get("protocol", "").lower().strip()
    source = spec.get("source", "").strip()

    if action not in ("allow", "deny", "reject"):
        raise FirewallError(f"invalid action: {action}", code="invalid")
    if not port:
        raise FirewallError("port is required", code="invalid")
    if protocol not in ("", "tcp", "udp"):
        raise FirewallError(f"invalid protocol: {protocol}", code="invalid")
    proto = protocol or "tcp"
    port_spec = f"{port}/{proto}"
    if source:
        # Use a rich rule for source-restricted rules
        rich = f"rule family=ipv4 source address={source} port port={port} protocol={proto} {action}"
        return _run(["sudo", "-S", "firewall-cmd", "--add-rich-rule", rich],
                    password=password, timeout=15)
    if action == "allow":
        return _run(["sudo", "-S", "firewall-cmd", "--add-port", port_spec],
                    password=password, timeout=15)
    if action == "deny":
        # firewalld has no direct "deny port" — use a rich rule
        rich = f"rule family=ipv4 port port={port} protocol={proto} drop"
        return _run(["sudo", "-S", "firewall-cmd", "--add-rich-rule", rich],
                    password=password, timeout=15)
    # reject
    rich = f"rule family=ipv4 port port={port} protocol={proto} reject"
    return _run(["sudo", "-S", "firewall-cmd", "--add-rich-rule", rich],
                password=password, timeout=15)


def _firewalld_delete_rule(spec: dict, password: str) -> str:
    port = str(spec.get("port", "")).strip()
    protocol = spec.get("protocol", "").lower().strip()
    source = spec.get("source", "").strip()
    if not port:
        raise FirewallError("port is required", code="invalid")
    if protocol not in ("", "tcp", "udp"):
        raise FirewallError(f"invalid protocol: {protocol}", code="invalid")
    proto = protocol or "tcp"
    port_spec = f"{port}/{proto}"
    if source:
        rich = f"rule family=ipv4 source address={source} port port={port} protocol={proto} drop"
        return _run(["sudo", "-S", "firewall-cmd", "--remove-rich-rule", rich],
                    password=password, timeout=15)
    return _run(["sudo", "-S", "firewall-cmd", "--remove-port", port_spec],
                password=password, timeout=15)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_status(password: Optional[str] = None) -> Status:
    """Return the current firewall status.

    If ``password`` is provided and the read operation requires sudo,
    it is piped to sudo. ufw status is typically readable without sudo
    for the active/inactive state, but numbered rules are not.
    """
    det = is_available()
    if not det["available"]:
        return Status(
            available=False, backend="none", enabled=False,
            default_incoming="", default_outgoing="", default_routed="",
            rules=[], reason=det["reason"],
        )
    if det["backend"] == "ufw":
        return _ufw_status(password=password)
    return _firewalld_status(password=password)


def enable(password: str) -> dict:
    det = is_available()
    if not det["available"]:
        raise FirewallError(det["reason"], code="missing_tool")
    if det["backend"] == "ufw":
        out = _ufw_apply("enable", password)
    else:
        out = _firewalld_apply("enable", password)
    return {"ok": True, "output": out or "enabled"}


def disable(password: str) -> dict:
    det = is_available()
    if not det["available"]:
        raise FirewallError(det["reason"], code="missing_tool")
    if det["backend"] == "ufw":
        out = _ufw_apply("disable", password)
    else:
        out = _firewalld_apply("disable", password)
    return {"ok": True, "output": out or "disabled"}


def reload(password: str) -> dict:
    det = is_available()
    if not det["available"]:
        raise FirewallError(det["reason"], code="missing_tool")
    if det["backend"] == "ufw":
        out = _ufw_apply("reload", password)
    else:
        out = _firewalld_apply("reload", password)
    return {"ok": True, "output": out or "reloaded"}


def set_default(policy: str, direction: str, password: str) -> dict:
    if is_available()["backend"] != "ufw":
        raise FirewallError("set_default is currently only supported for ufw", code="unsupported")
    out = _ufw_set_default(policy, direction, password)
    return {"ok": True, "output": out or f"default {direction} = {policy}"}


def build_rule_spec(
    action: str = "allow",
    port: str = "",
    protocol: str = "tcp",
    source: str = "",
    direction: str = "in",
) -> dict:
    """Validate + normalize a rule form into a spec dict (Laranode-inspired).

    Mirrors ``BuildUfwRuleSpecAction`` (direction/proto/from/to/port) but
    returns a structured spec plus a human preview string and an SSH
    lockout warning flag. Never touches the system; raises FirewallError
    with code=invalid on bad input so the API can return 400.
    """
    act = (action or "").strip().lower()
    if act not in ("allow", "deny", "reject", "limit"):
        raise FirewallError(f"invalid action: {action!r}", code="invalid")
    prt = str(port or "").strip()
    if not prt:
        raise FirewallError("port is required", code="invalid")
    # Accept "22", "22/tcp", "8000:9000". Reject junk early.
    prt_core = prt.split("/")[0]
    if ":" in prt_core:
        lo, _, hi = prt_core.partition(":")
        if not (lo.isdigit() and hi.isdigit() and 1 <= int(lo) <= 65535 and 1 <= int(hi) <= 65535):
            raise FirewallError(f"invalid port range: {port!r}", code="invalid")
    elif not (prt_core.isdigit() and 1 <= int(prt_core) <= 65535):
        # Also allow service names like "http"? No — require numeric to
        # avoid ufw app-profile ambiguity. Keep strict for safety.
        raise FirewallError(f"invalid port: {port!r} (1-65535)", code="invalid")
    proto = (protocol or "").strip().lower() or "tcp"
    if proto not in ("tcp", "udp", "any"):
        raise FirewallError(f"invalid protocol: {protocol!r}", code="invalid")
    src = (source or "").strip()
    if src.lower() in ("", "any", "anywhere"):
        src = ""
    direc = (direction or "in").strip().lower()
    if direc not in ("in", "out"):
        raise FirewallError(f"invalid direction: {direction!r}", code="invalid")
    spec = {"action": act, "port": prt_core, "protocol": proto, "source": src, "direction": direc}
    # Human preview, e.g. "deny in from any to any port 22/tcp".
    preview = (
        f"{act} {direc} proto {proto} from {src or 'any'} to any port {prt_core}"
    )
    # SSH lockout heuristic: denying inbound 22 without source restriction.
    lockout_warning = (
        act in ("deny", "reject") and direc == "in" and prt_core == "22" and not src
    )
    return {"spec": spec, "preview": preview, "lockout_warning": lockout_warning}


def add_rule(spec: dict, password: str) -> dict:
    det = is_available()
    if not det["available"]:
        raise FirewallError(det["reason"], code="missing_tool")
    if det["backend"] == "ufw":
        out = _ufw_add_rule(spec, password)
    else:
        out = _firewalld_add_rule(spec, password)
    return {"ok": True, "output": out or "rule added"}


def delete_rule(spec: dict, password: str) -> dict:
    det = is_available()
    if not det["available"]:
        raise FirewallError(det["reason"], code="missing_tool")
    if det["backend"] == "ufw":
        out = _ufw_delete_rule(spec, password)
    else:
        out = _firewalld_delete_rule(spec, password)
    return {"ok": True, "output": out or "rule removed"}