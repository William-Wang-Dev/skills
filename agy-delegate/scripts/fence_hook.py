#!/usr/bin/env python3
"""PreToolUse fence hook for delegated agy jobs (stdlib only).

agy runs this before every matched tool call. It reads the hook payload on
stdin and prints {"decision": "deny"|"allow", "reason": ...} on stdout.

Why it exists: in headless print mode on consumer accounts, a run_command
call with BypassSandbox=true can run outside the terminal sandbox without a
prompt (and any `command(...)` allow rule also lets that command leave the
sandbox). Deny rules in settings still apply, but this hook adds a second,
independent fence:
  * no sandbox bypass at all
  * no network / remote / privilege commands anywhere in a command line
  * no file-tool writes outside the job worktree or into protected paths
    (auto mode would otherwise allow new files anywhere and edits in other repos)
  * no file-tool reads of identity/credential files in $HOME (~/.gitconfig, ~/.netrc, ...)
  * no subagent / swarm worker with its own `share` or `branch` workspace (they would
    work outside the gated worktree and need commits)

It runs for swarm workers and subagents too (they load the same workspace hooks).

Usage (from hooks.json): python3 fence_hook.py --log <file> --root <worktree> [--log-all] [--readonly <dir>]...
(--readonly: extra workspace dirs, e.g. `ask --context-dir`, where file tools may read but never write)
"""
import argparse
import json
import os
import re
import sys
import time

G = r"\bgit\s+(?:-[cC]\s*\S+\s+|--\S+\s+)*"  # git plus global options such as -c k=v / -C dir
DENY_COMMAND_PATTERNS = [
    (G + r"push\b", "git push is not allowed (the gate owns the branch)"),
    (G + r"remote\s+(add|set-url|rename|remove|rm)\b", "changing git remotes is not allowed"),
    (G + r"config\s+(--global|--system)\b", "changing global git config is not allowed"),
    (G + r"(commit|reset\s+--hard|rebase|checkout\s+-B|branch\s+-[dD]|tag)\b",
     "git history changes are not allowed; leave changes uncommitted for the gate"),
    (r"(^|[\s;&|(`$'\"])(curl|wget|ssh|scp|sftp|rsync|nc|ncat|telnet|ftp)\b", "network/remote tools are not allowed"),
    (r"(^|[\s;&|(`$'\"])(sudo|su|doas|pkexec)\b", "privilege escalation is not allowed"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME)(\s|/|$)", "deleting outside the workspace is not allowed"),
]

PROTECTED_PARTS = (".git", ".agents")
SENSITIVE_NAMES = re.compile(r"(^|/)(\.env(\..*)?|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key)$")
# identity / credential files under $HOME that a delegated job never needs to read
SECRET_HOME = (".gitconfig", ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".docker/config.json", ".ssh",
               ".gnupg", ".aws", ".kube", ".config", ".cargo/credentials", ".cargo/credentials.toml",
               ".gem/credentials", ".claude", ".claude.json", ".gemini/oauth_creds.json")
SECRET_CMD = re.compile(r"(~|\$HOME|\$\{HOME\}|" + re.escape(os.path.expanduser("~")) + r")/(\.gitconfig|\.git-credentials|"
                        r"\.netrc|\.npmrc|\.pypirc|\.docker/config\.json|\.ssh|\.gnupg|\.aws|\.kube|\.cargo/credentials)")
WRITE_TOOLS = {"write_to_file", "write_file", "replace_file_content", "multi_replace_file_content", "edit_file",
               "sed_file", "notebook_edit"}


def is_write_tool(name):
    return name in WRITE_TOOLS or any(k in name for k in ("write", "replace", "edit", "sed_"))


def secret_home_path(val):
    if not isinstance(val, str) or not val:
        return False
    home = os.path.realpath(os.path.expanduser("~"))
    p = os.path.realpath(os.path.expanduser(val))
    for s in SECRET_HOME:
        t = os.path.join(home, s)
        if p == t or p.startswith(t + os.sep):
            return True
    return False


def decide(payload, root=None, readonly=()):
    call = payload.get("toolCall") or {}
    name = call.get("name") or ""
    args = call.get("args") or {}
    ws = str(args.get("workspace") or args.get("Workspace") or "").lower()
    if ws in ("share", "branch"):
        return "deny", ("agy-delegate fence: subagents/workers must use the shared job workspace (workspace "
                        "'inherit'); separate 'share'/'branch' copies are not gated and would need commits")
    for key, val in args.items():
        if (key.endswith("Path") or key.endswith("File") or key in ("path", "file")) and secret_home_path(val):
            return "deny", "agy-delegate fence: reading or writing identity/credential files in $HOME is not allowed"
    if name == "run_command":
        if args.get("BypassSandbox"):
            return "deny", "agy-delegate fence: running outside the sandbox is not allowed for delegated jobs"
        cmd = str(args.get("CommandLine") or "")
        for pat, reason in DENY_COMMAND_PATTERNS:
            if re.search(pat, cmd):
                return "deny", "agy-delegate fence: " + reason
        if SECRET_CMD.search(cmd):
            return "deny", "agy-delegate fence: reading identity/credential files in $HOME is not allowed"
        return "allow", ""
    if is_write_tool(name) and not name.startswith("browser"):
        roots = [os.path.realpath(r) for r in ([root] if root else []) + list(payload.get("workspacePaths") or [])]
        art = payload.get("artifactDirectoryPath")
        art = os.path.realpath(os.path.expanduser(art)) if art else None
        for key, val in args.items():
            if not isinstance(val, str) or not (key.endswith("File") or key.endswith("Path")):
                continue
            path = os.path.realpath(val if os.path.isabs(val) else os.path.join(roots[0] if roots else os.getcwd(), val))
            if art and (path == art or path.startswith(art + os.sep)):
                continue  # agy's own plan/walkthrough artifacts live here
            ro = [os.path.realpath(r) for r in readonly]
            if any(path == r or path.startswith(r + os.sep) for r in ro):
                return "deny", "agy-delegate fence: this directory is read-only context"
            if roots and not any(path == r or path.startswith(r + os.sep) for r in roots):
                return "deny", "agy-delegate fence: writing outside the job worktree is not allowed"
            rel = path
            for r in roots:
                if path.startswith(r + os.sep):
                    rel = path[len(r) + 1:]
                    break
            parts = rel.split(os.sep)
            if any(p in PROTECTED_PARTS for p in parts):
                return "deny", "agy-delegate fence: writing to .git/ or .agents/ is not allowed"
            if SENSITIVE_NAMES.search(rel):
                return "deny", "agy-delegate fence: writing secrets/key files is not allowed"
    return "allow", ""


def conversation_of(payload):
    """Best-effort id of the calling conversation (main agent, subagent or swarm worker)."""
    for k, v in payload.items():
        if "onversation" in k and isinstance(v, str) and v:
            return v
    art = payload.get("artifactDirectoryPath") or ""
    return os.path.basename(art.rstrip("/")) if art else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--log")
    ap.add_argument("--root")
    ap.add_argument("--log-all", action="store_true", help="log allowed calls too (used for swarm per-worker status)")
    ap.add_argument("--readonly", action="append", default=[], help="directory file tools must not write (repeatable)")
    a = ap.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    decision, reason = decide(payload, a.root, a.readonly)
    if a.log and (decision == "deny" or a.log_all or os.environ.get("AGY_DELEGATE_HOOK_DEBUG")):
        try:
            call = payload.get("toolCall") or {}
            rec = {"ts": time.time(), "tool": call.get("name"),
                   "args": {k: str(v)[:300] for k, v in (call.get("args") or {}).items()},
                   "decision": decision, "reason": reason, "conv": conversation_of(payload)}
            if os.environ.get("AGY_DELEGATE_HOOK_DEBUG"):
                rec["payload_keys"] = sorted(payload)
            with open(a.log, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
    out = {"decision": decision}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
