#!/usr/bin/env python3
"""agy_job.py - delegate a self-contained coding job to Antigravity CLI (`agy`)
and act as monitor + gate. Python 3 stdlib only; needs `git` and `agy`.

Subcommands: recommend, start, status, gate, diff, rework, accept, stop, cleanup, list.
Run `agy_job.py <cmd> -h` for options.

Layout per job (nothing is written into the user's tracked tree except the
agent's own work inside the job worktree):
  state:    <git-common-dir>/agy-jobs/<job>/   (job.json, spec.md, settings.json,
            round-N.jsonl / .err / .exit, gate-N.json, fence.log, runner.log)
  worktree: $AGY_DELEGATE_WORKTREES or ~/.local/state/agy-delegate/worktrees/
            <repo>-<hash>/<job>   on branch agy/<job>
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"
FENCE_HOOK = SKILL_DIR / "scripts" / "fence_hook.py"
HOOK_REL = ".agents/hooks.json"
PROGRESS = "PROGRESS.md"
HOOK_KEY = "agy-delegate-fence"
DEFAULT_MODEL = "gemini-4-hydrogen-high"
# agy execution mode. Probed on agy 1.5.16 (tests/e2e.md): print mode really runs in auto mode whether or not
# `--mode auto` is passed (the "not available ... rollout flag off" warning is printed before silent auth finishes
# and is wrong). `accept-edits` auto-denies every unlisted command headless, so it is unusable without allow rules,
# and allow rules let commands leave the sandbox. So we pin auto explicitly and keep the fence hook.
DEFAULT_AGY_MODE = "auto"
ALWAYS_FORBIDDEN = [".git", ".git/*", ".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key",
                    "id_rsa*", "*/id_rsa*", "id_ed25519*", "*/id_ed25519*"]
# untracked build/test byproducts never count as deliverables (they are left out of scope checks and commits)
JUNK = ["__pycache__", "*/__pycache__", "*.pyc", ".pytest_cache", "*/.pytest_cache", ".mypy_cache", ".ruff_cache",
        "*.egg-info", "*/*.egg-info", ".coverage", ".tox", ".nox", ".DS_Store", "*/.DS_Store"]
JOB_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")

# exit codes
OK, FAIL, SCOPE, NOTREADY, USAGE = 0, 1, 2, 3, 64


class JobError(Exception):
    def __init__(self, msg, code=USAGE):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- helpers
def now():
    return time.time()


def run(cmd, cwd=None, check=True, capture=True, timeout=None):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, timeout=timeout)
    if check and p.returncode != 0:
        raise JobError("command failed: %s\n%s" % (" ".join(map(shlex.quote, cmd)), (p.stderr or p.stdout or "").strip()), FAIL)
    return p


def git(cwd, *args, check=True):
    return run(["git", "-C", str(cwd)] + list(args), check=check).stdout


def repo_root(path):
    try:
        return Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve()
    except JobError:
        raise JobError("not a git repository: %s" % path)


def common_dir(repo):
    d = git(repo, "rev-parse", "--git-common-dir").strip()
    p = Path(d)
    return (p if p.is_absolute() else Path(repo) / p).resolve()


def state_root(repo):
    return common_dir(repo) / "agy-jobs"


def worktree_base(repo):
    base = os.environ.get("AGY_DELEGATE_WORKTREES")
    if base:
        root = Path(base).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        root = Path(xdg) / "agy-delegate" / "worktrees"
    tag = hashlib.sha1(str(repo).encode()).hexdigest()[:8]
    return root / ("%s-%s" % (Path(repo).name, tag))


def agy_bin():
    b = os.environ.get("AGY_BIN") or shutil.which("agy")
    if not b:
        raise JobError("agy not found on PATH (set AGY_BIN)")
    return b


def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def save_json(p, data):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:  # zombie check (linux)
        st = Path("/proc/%d/stat" % int(pid)).read_text()
        if st.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return ("%dh%02dm" % (h, m)) if h else ("%dm%02ds" % (m, s))


def matches(path, globs):
    for g in globs:
        g = g.rstrip("/")
        if fnmatch.fnmatch(path, g) or path == g or path.startswith(g + "/") or fnmatch.fnmatch(path, g + "/*"):
            return True
    return False


# ---------------------------------------------------------------- job object
class Job:
    def __init__(self, repo, name):
        if not JOB_RE.match(name or ""):
            raise JobError("bad job name %r (use lowercase letters, digits, . _ -)" % name)
        self.repo = repo_root(repo)
        self.name = name
        self.dir = state_root(self.repo) / name
        self.file = self.dir / "job.json"

    def exists(self):
        return self.file.exists()

    def load(self):
        d = load_json(self.file)
        if d is None:
            raise JobError("no such job %r in %s (see `list`)" % (self.name, self.repo))
        return d

    def save(self, d):
        save_json(self.file, d)

    def rpath(self, n, ext):
        return self.dir / ("round-%d.%s" % (n, ext))


def parse_stream(path):
    """Summarise an agy stream-json log."""
    info = {"conversation_id": None, "events": 0, "tools": 0, "last": [], "result": None,
            "errors": [], "model": None, "last_text": "", "swarm": None}
    text_buf = ""
    try:
        fh = open(path, errors="replace")
    except OSError:
        return info
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            info["events"] += 1
            ev = e.get("event")
            if ev == "init":
                info["conversation_id"] = e.get("conversation_id") or info["conversation_id"]
                info["model"] = (e.get("init") or {}).get("model")
            elif ev == "result":
                info["result"] = e.get("result") or {}
                info["conversation_id"] = info["conversation_id"] or info["result"].get("conversation_id")
            su = e.get("step_update")
            if not su:
                continue
            info["conversation_id"] = info["conversation_id"] or su.get("conversation_id")
            st, typ = su.get("state"), su.get("step_type")
            if typ == "tool" and st in ("DONE", "ERROR"):
                info["tools"] += 1
                ti = su.get("tool_info") or {}
                params = ti.get("parameters") or {}
                target = params.get("CommandLine") or params.get("TargetFile") or params.get("Path") or params.get("AbsolutePath") \
                    or params.get("DirectoryPath") or params.get("Query") or ""
                desc = "%s %s" % (su.get("tool_name"), str(target)[:100])
                if su.get("tool_name") in ("swarm", "read_board") and st == "DONE":
                    swarm_event(info, su.get("tool_name"), params, ti.get("output"))
                if st == "ERROR":
                    msg = ((ti.get("error") or {}).get("message") or "")[:200]
                    desc += "  [ERROR: %s]" % msg
                    info["errors"].append(msg)
                info["last"].append(desc.strip())
                info["last"] = info["last"][-8:]
            elif typ == "agent_response":
                text_buf += su.get("text_delta") or ""
                if st == "DONE" and text_buf.strip():
                    info["last_text"] = text_buf.strip()[-400:]
                    info["last"].append("say: " + text_buf.strip().replace("\n", " ")[:120])
                    info["last"] = info["last"][-8:]
                    text_buf = ""
    return info


def tool_json(output):
    """agy tool outputs are a few header lines followed by one JSON object."""
    if isinstance(output, dict):
        return output
    for line in reversed(str(output or "").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def swarm_event(info, tool, params, output):
    """Track swarm members and board posts as the coordinator sees them."""
    sw = info["swarm"] or {"title": None, "members": {}, "posts": 0}
    info["swarm"] = sw
    out = tool_json(output) or {}
    mem = sw["members"]
    if tool == "swarm":
        if out.get("title"):
            sw["title"] = out["title"]
        if out.get("status") == "added" and out.get("name"):
            mem.setdefault(out["name"], {}).update({"model": out.get("model"), "conversation_id": out.get("conversation_id"),
                                                    "state": "added"})
        for m in out.get("members") or []:
            if m.get("name") and m.get("role") != "coordinator":
                e = mem.setdefault(m["name"], {})
                e["model"] = m.get("model") or e.get("model")
                e["state"] = "killed" if m.get("killed") else m.get("state")
    elif tool == "read_board":
        for msg in out.get("messages") or []:
            sw["posts"] = max(sw["posts"], int(msg.get("seq") or 0))
            who = msg.get("agent")
            if who and who != "coordinator":
                e = mem.setdefault(who, {})
                text = str(msg.get("text") or "")
                e["last_post"] = text.replace("\n", " ")[:140]
                if "[done]" in text.lower():
                    e["done"] = True


def agy_error(err_path):
    try:
        for line in Path(err_path).read_text(errors="replace").splitlines():
            if line.startswith("AGY_ERROR:"):
                return load_json_str(line[len("AGY_ERROR:"):])
    except OSError:
        pass
    return None


def load_json_str(s):
    try:
        return json.loads(s)
    except ValueError:
        return {"short_error": s.strip()}


# ---------------------------------------------------------------- mode / model recommendation
HARD_WORDS = re.compile(r"\b(debug\w*|race|races|concurren\w*|deadlock\w*|flaky|perf\w*|optimi[sz]\w*|algorithm\w*|"
                        r"root.cause|memory.leak|segfault|crash\w*|bisect)\b", re.I)
VAGUE_WORDS = re.compile(r"\b(design|architect\w*|explore|investigate|figure out|refactor\w*|migrat\w*|redesign|"
                         r"rewrite|overhaul|clean ?up|modernis\w*|moderniz\w*|tbd|unclear)\b", re.I)
PLACEHOLDER = re.compile(r"<[^>]+>")


def spec_section(spec, title):
    """Lines of a '## <title>' section (until the next '## ' heading)."""
    out, on = [], False
    for line in spec.splitlines():
        if re.match(r"^##\s", line):
            on = bool(re.match(r"^##\s+%s\b" % re.escape(title), line, re.I))
            continue
        if on:
            out.append(line)
    return out


def spec_items(spec, title):
    """Top-level bullet / numbered items of a section, template placeholders ignored."""
    items = []
    for line in spec_section(spec, title):
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(.*\S)", line)
        if m and PLACEHOLDER.sub("", m.group(1)).strip(" -.:`"):
            items.append(m.group(1))
    return items


def recommend(spec):
    """Pick mode + model + budget from simple spec signals. Deliberately crude; see SKILL.md."""
    text = PLACEHOLDER.sub(" ", spec)
    words = len(text.split())
    parts = spec_items(spec, "Parts")
    scope = spec_items(spec, "Scope")
    hard = sorted({m.group(0).lower() for m in HARD_WORDS.finditer(text)})
    vague = sorted({m.group(0).lower() for m in VAGUE_WORDS.finditer(text)})
    big = len(parts) >= 4 or len(scope) >= 6 or words >= 900
    why = []
    if len(parts) >= 3 or (len(parts) >= 2 and big):
        mode = "swarm"
        why.append("%d independent parts%s -> swarm (one worker per part + verifier)" % (len(parts), ", big job" if big else ""))
        model = "gemini-4-neon-high" if hard else "gemini-4-hydrogen-high"
        why.append("workers inherit the model: %s" % ("neon-high for hard work (%s)" % ", ".join(hard) if hard
                                                       else "hydrogen-high (fast, cheap, parts are well specified)"))
        budget = min(90, 25 + 10 * len(parts))
    else:
        mode = "single"
        if len(parts) == 2:
            why.append("only 2 parts and not big -> single agent (a swarm's coordinator/verifier overhead is not worth it)")
        elif len(parts) < 2:
            why.append("no decomposed parts -> single agent")
        if hard and (big or vague):
            model, budget = "gemini-pro-orchestrator", 60
            why.append("hard (%s) and %s -> Orchestrator (Carbon)" % (", ".join(hard), "big" if big else "vague"))
        elif hard:
            model, budget = "gemini-4-neon-high", 45
            why.append("hard single-area work (%s) -> neon-high" % ", ".join(hard))
        elif big or vague or not scope:
            model, budget = "gemini-pro-orchestrator-neon", 45
            why.append("%s -> Orchestrator (Neon) plans then executes" % (
                "big multi-file job" if big else ("vague spec (%s)" % ", ".join(vague) if vague else "no explicit scope")))
        else:
            model, budget = DEFAULT_MODEL, 20
            why.append("small, well-specified -> hydrogen-high")
    return {"mode": mode, "model": model, "budget": budget, "parts": parts, "scope_items": len(scope),
            "words": words, "hard": hard, "vague": vague, "big": big, "why": why}


# ---------------------------------------------------------------- prompt
PREAMBLE = """You are working on a delegated job in an isolated git worktree (your workspace: {wt}).
A separate reviewer will independently re-run the acceptance commands and review your diff.

Hard rules:
- Work only inside the workspace. Do not create commits, branches or tags; leave all changes uncommitted (.git is read-only for you).
- No network access: do not run curl/wget/ssh/git push or install packages from the internet. Use only what is already installed.
- Do not try to run commands outside the sandbox (never set BypassSandbox).
- Do not edit .agents/, .git/ or any file listed as out of scope or protected.
- Keep {progress} at the workspace root updated with a short checklist of what is done / left / decisions.
- Before you finish, run the acceptance command(s) yourself and make them pass.
- Finish with a short summary: what changed (files), how you verified it, anything left open.

Acceptance command(s):
{accept}
{scope}
==== JOB SPEC ====
{spec}
"""

SWARM = """
==== SWARM RULES (you are the coordinator; apply to every worker and put them in every brief) ====
- Every hard rule above applies to every worker: no commits, no network, no sandbox bypass, only in-scope files.
- All workers use this shared workspace ({wt}) with the default workspace ("inherit"). Never give a worker a
  "share" or "branch" workspace: nobody can commit here (.git is read-only), so changes made in a separate copy
  would be lost, and the job's fence denies those workspaces anyway.
- One owner per part and per file; no two workers edit the same file. {parts_line}
- Add one verifier that runs the acceptance command(s) after the authors report [done] and sends failures back
  to the owning worker. Only the verifier updates {progress}.
- Keep the swarm small: at most {max_workers} workers besides the verifier and the monitor.
- Do not end your final turn until the verifier has reported the acceptance command(s) passing, or you are sure
  they cannot pass. Then reply with a short summary per worker (name, files, result).
"""

SWARM_REWORK = """
You are still the swarm coordinator. Earlier workers may be gone: check `swarm` status, give each fix to its
owning worker if it is still alive, otherwise add a fresh worker for it (same swarm rules as before: shared
workspace only, one owner per file, no commits). Have the verifier re-run the acceptance command(s).
"""

REWORK = """REWORK (round {n} of max {maxr}). The reviewer's gate found problems with your previous result in {wt}.

Reviewer feedback:
{feedback}

Fix exactly these points, keep everything else as is, keep the same hard rules as before
(no commits, no network, no sandbox bypass, only in-scope files). Update {progress}.
Re-run the acceptance command(s) and make them pass:
{accept}
Finish with a short summary of what you changed.
"""


def scope_text(d):
    s = ""
    if d.get("allow_paths"):
        s += "\nYou may change only these paths: %s\n" % ", ".join(d["allow_paths"])
    if d.get("protect"):
        s += "Protected (must not change): %s\n" % ", ".join(d["protect"])
    return s


def accept_text(d):
    return "\n".join("  " + c for c in d["accept"]) or "  (none given)"


def initial_prompt(d, spec):
    p = PREAMBLE.format(wt=d["worktree"], progress=PROGRESS, accept=accept_text(d), scope=scope_text(d), spec=spec)
    if d.get("mode") == "swarm":
        parts = spec_items(spec, "Parts")
        parts_line = ("Use the spec's Parts section as the split (one worker per part)." if parts
                      else "Split by file or module so that pieces do not collide.")
        # the slash command must be the very first token of the prompt for agy to start a swarm
        p = "/swarm " + p + SWARM.format(wt=d["worktree"], progress=PROGRESS, parts_line=parts_line,
                                         max_workers=max(2, min(8, len(parts) or 4)))
    return p


# ---------------------------------------------------------------- settings / hook
def build_settings(d, template=None):
    tpl = load_json(template or (TEMPLATES / "permissions.json"))
    if tpl is None:
        raise JobError("cannot read permissions template %s" % (template or TEMPLATES / "permissions.json"))
    raw = json.dumps({k: v for k, v in tpl.items() if not k.startswith("_")})
    # the Linux sandbox rejects non-absolute file rules ("deny .env: non-absolute file path"), so every
    # read_file/write_file target is made absolute: ~ -> home, relative -> inside the job worktree
    raw = raw.replace("{GIT_COMMON_DIR}", d["git_common_dir"]).replace("{WORKTREE}", d["worktree"])
    raw = raw.replace("(~/", "(" + str(Path.home()) + "/")
    data = json.loads(raw)

    def absolutize(rule):
        m = re.match(r"^(read_file|write_file)\((.*)\)$", rule)
        if not m or m.group(2) in ("*", "") or m.group(2).startswith("/"):
            return rule
        return "%s(%s)" % (m.group(1), os.path.join(d["worktree"], m.group(2)).rstrip("/"))
    for key in ("allow", "deny", "ask"):
        if key in data.get("permissions", {}):
            data["permissions"][key] = [absolutize(r) for r in data["permissions"][key]]
    return data


def install_hook(job, d):
    wt = Path(d["worktree"])
    hp = wt / HOOK_REL
    entry = {"PreToolUse": [{"matcher": "*",
                             "hooks": [{"type": "command", "timeout": 15,
                                        "command": " ".join(shlex.quote(x) for x in [
                                            sys.executable, str(FENCE_HOOK), "--log", str(job.dir / "fence.log"),
                                            "--root", str(wt)] + (["--log-all"] if d.get("mode") == "swarm" else []))}]}]}
    tracked = bool(git(wt, "ls-files", HOOK_REL).strip())
    existing = load_json(hp, {}) if hp.exists() else {}
    if hp.exists() and not isinstance(existing, dict):
        raise JobError("%s exists but is not a JSON object; cannot add fence hook" % HOOK_REL)
    existing[HOOK_KEY] = entry
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps(existing, indent=2))
    d["managed"] = {HOOK_REL: "tracked" if tracked else "new"}
    d["hook_sha"] = hashlib.sha256(hp.read_bytes()).hexdigest()


def restore_managed(d):
    wt = Path(d["worktree"])
    for rel, kind in (d.get("managed") or {}).items():
        p = wt / rel
        if kind == "tracked":
            git(wt, "checkout", d["base_commit"], "--", rel)
        elif p.exists():
            p.unlink()
            try:
                p.parent.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------- launching
def launch_round(job, d, prompt, conversation=None):
    n = len(d["rounds"]) + 1
    budget_s = int(d["budget_min"] * 60)
    cmd = [agy_bin(), "-p", prompt, "--model", d["model"], "--output-format", "stream-json",
           "--print-timeout", "%ds" % budget_s, "--settings", str(job.dir / "settings.json"),
           "--log-file", str(job.rpath(n, "agy.log"))]
    if d.get("agy_mode", DEFAULT_AGY_MODE) != "default":
        cmd += ["--mode", d.get("agy_mode", DEFAULT_AGY_MODE)]
    if d.get("mode") != "swarm":
        # single mode: a spec that happens to start with "/swarm" or "/boost" must not start a quota-heavy run
        cmd.append("--disable-slash-commands")
    if conversation:
        cmd += ["--conversation", conversation]
    rnd = {"n": n, "started": now(), "cmd": cmd, "conversation_in": conversation,
           "prompt_file": str(job.rpath(n, "prompt.txt"))}
    job.rpath(n, "prompt.txt").write_text(prompt)
    d["rounds"].append(rnd)
    d["status_override"] = None
    job.save(d)
    if d.get("dry_run"):
        return rnd
    runner = [sys.executable, str(Path(__file__).resolve()), "_run", str(job.dir), str(n)]
    launcher = pick_launcher(d.get("launcher", "auto"))
    rnd["launcher"] = launcher
    if launcher == "systemd":
        # a transient user unit survives agent harnesses that kill every descendant when a tool call returns
        unit = "agy-delegate-%s-%d-%s" % (re.sub(r"[^a-zA-Z0-9_-]", "_", job.name), n, os.urandom(3).hex())
        envs = ["--setenv=%s=%s" % (k, v) for k, v in os.environ.items() if passthrough_env(k)]
        hard = int(d["budget_min"] * 60 + d.get("grace_s", 120) + 120)
        p = run(["systemd-run", "--user", "--collect", "--quiet", "--unit", unit, "--working-directory", d["worktree"],
                 "-p", "RuntimeMaxSec=%d" % hard, "-p", "StandardOutput=append:%s" % (job.dir / "runner.log"),
                 "-p", "StandardError=append:%s" % (job.dir / "runner.log")] + envs + runner, check=True)
        rnd["unit"] = unit
    else:
        log = open(job.dir / "runner.log", "a")
        p = subprocess.Popen(runner, cwd=d["worktree"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
        rnd["runner_pid"] = p.pid
    job.save(d)
    return rnd


ENV_PASS = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|LANG|LC_\w+|TZ|TMPDIR|XDG_\w+|AGY_\w+|FAKE_AGY_\w+|GEMINI_\w+|"
                      r"GOOGLE_\w+|PYTHON\w*|VIRTUAL_ENV|SSL_CERT_\w+|REQUESTS_CA_BUNDLE|NODE_EXTRA_CA_CERTS|"
                      r"(HTTPS?|NO|ALL)_PROXY|(https?|no|all)_proxy)$")


def passthrough_env(k):
    return bool(ENV_PASS.match(k))


def pick_launcher(pref):
    pref = os.environ.get("AGY_DELEGATE_LAUNCHER") or pref or "auto"
    if pref in ("systemd", "setsid"):
        return pref
    if shutil.which("systemd-run") and shutil.which("systemctl"):
        try:
            if subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5).returncode == 0:
                return "systemd"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "setsid"


def runner_pid(job, rnd):
    return rnd.get("runner_pid") or load_pid(job.rpath(rnd["n"], "runner.pid"))


def cmd__run(a):
    """Internal: detached runner. Runs agy, enforces the time budget, records exit."""
    sdir = Path(a.state_dir)
    n = int(a.round)
    try:
        os.setsid()
    except OSError:
        pass
    (sdir / ("round-%d.runner.pid" % n)).write_text(str(os.getpid()))
    d = load_json(sdir / "job.json")
    rnd = d["rounds"][n - 1]
    hard = d["budget_min"] * 60 + d.get("grace_s", 120)
    out = open(sdir / ("round-%d.jsonl" % n), "ab")
    err = open(sdir / ("round-%d.err" % n), "ab")
    t0 = now()
    p = subprocess.Popen(rnd["cmd"], cwd=d["worktree"], stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    (sdir / ("round-%d.pid" % n)).write_text(str(p.pid))
    killed = False

    def term(*_):
        try:
            p.terminate()
        except OSError:
            pass
    signal.signal(signal.SIGTERM, term)
    while True:
        try:
            rc = p.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            if now() - t0 > hard and not killed:
                killed = True
                p.terminate()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
    save_json(sdir / ("round-%d.exit" % n), {"rc": rc, "ended": now(), "killed_by_budget": killed})
    return 0


# ---------------------------------------------------------------- state evaluation
def evaluate(job, d):
    """Return a status dict for the latest round."""
    st = {"job": job.name, "repo": str(job.repo), "worktree": d["worktree"], "branch": d["branch"],
          "model": d["model"], "mode": d.get("mode", "single"), "round": len(d["rounds"]), "max_rounds": d["max_rounds"]}
    if not d["rounds"]:
        st["state"] = "new"
        return st
    rnd = d["rounds"][-1]
    n = rnd["n"]
    log = job.rpath(n, "jsonl")
    info = parse_stream(log)
    ex = load_json(job.rpath(n, "exit"))
    t = now()
    last_out = log.stat().st_mtime if log.exists() else rnd["started"]
    ended = ex["ended"] if ex else None
    st.update({"elapsed_s": int((ended or t) - rnd["started"]), "quiet_s": int(t - last_out) if not ex else 0,
               "conversation_id": info["conversation_id"] or rnd.get("conversation_in"), "tool_calls": info["tools"],
               "last_events": info["last"][-5:], "budget_min": d["budget_min"]})
    res = info["result"]
    denied = (res or {}).get("denied_actions") or []
    fence, conv_tools = [], {}
    for line in (job.dir / "fence.log").read_text().splitlines() if (job.dir / "fence.log").exists() else []:
        e = load_json_str(line)
        if e.get("ts", 0) < rnd["started"]:
            continue
        if e.get("conv"):
            conv_tools[e["conv"]] = conv_tools.get(e["conv"], 0) + 1
        if e.get("decision", "deny") == "deny":
            fence.append(e.get("reason", ""))
    mode_eff = effective_agy_mode(job.rpath(n, "agy.log"))
    if mode_eff:
        st["agy_mode"] = mode_eff
    if info["swarm"]:
        sw = info["swarm"]
        members = []
        for name, m in sorted(sw["members"].items()):
            m = dict(m, name=name, tools=conv_tools.get(m.get("conversation_id")))
            members.append(m)
        st["swarm"] = {"title": sw["title"], "posts": sw["posts"], "members": members,
                       "done": sum(1 for m in members if m.get("done"))}
    st["blocked_actions"] = [x.get("action") for x in denied] + ["fence: " + f for f in fence] + \
        [e for e in info["errors"] if "permission" in e.lower() or "denied" in e.lower()][:5]
    if d.get("status_override") == "stopped" and not (ex is None and pid_alive(runner_pid(job, rnd))):
        state = "stopped"
    elif ex is None:
        if d.get("dry_run"):
            state = "dry-run"
        elif not pid_alive(runner_pid(job, rnd)) and not (now() - rnd["started"] < 15 and not runner_pid(job, rnd)):
            state = "failed"
            st["reason"] = "runner exited without recording a result (see runner.log)"
        elif st["elapsed_s"] > d["budget_min"] * 60:
            state = "timeout"
            st["reason"] = "over time budget; runner is terminating agy"
        elif st["quiet_s"] > d["stuck_min"] * 60:
            state = "stuck"
            st["reason"] = "no new output for %s" % fmt_dur(st["quiet_s"])
        else:
            state = "running"
    else:
        err = agy_error(job.rpath(n, "err"))
        st["rc"] = ex["rc"]
        if ex.get("killed_by_budget") or ex["rc"] == 3 or (err or {}).get("reason") == "print_timeout":
            state = "timeout"
            st["reason"] = (err or {}).get("short_error", "time budget exceeded")[:300]
        elif ex["rc"] == 0 and res and res.get("status") == "SUCCESS":
            state = "done"
            if denied and not (res.get("response") or "").strip():
                state = "blocked"
                st["reason"] = "agy stopped: a tool needed approval that headless mode cannot give"
        else:
            state = "failed"
            st["reason"] = ((err or {}).get("short_error") or (res or {}).get("error") or
                            tail(job.rpath(n, "err"), 3) or "agy exit %s" % ex["rc"])[:400]
    st["state"] = state
    if res:
        st["summary"] = (res.get("response") or "")[-1200:]
        u = res.get("usage") or {}
        st["tokens"] = u.get("total_tokens")
    elif info["last_text"]:
        st["summary"] = info["last_text"]
    pg = Path(d["worktree"]) / PROGRESS
    if pg.exists():
        st["progress"] = "\n".join(pg.read_text(errors="replace").splitlines()[:25])
    try:
        st["changed_files"] = len(deliverable_files(d))
    except JobError:
        st["changed_files"] = None
    if d.get("accepted"):
        st["accepted"] = d["accepted"]
    if d.get("last_gate"):
        st["last_gate"] = d["last_gate"]
    return st


def effective_agy_mode(log_path):
    """The mode agy really ran in, from its own log (`Resolved active tags: [jetski_mode_auto ...]`)."""
    try:
        with open(log_path, errors="replace") as fh:
            for line in fh:
                m = re.search(r"Resolved active tags: \[[^\]]*\bjetski_mode_(\w+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def tail(p, n):
    try:
        return "\n".join(Path(p).read_text(errors="replace").strip().splitlines()[-n:])
    except OSError:
        return ""


def changed_files(d):
    """Files changed in the worktree relative to the base commit (tracked + untracked)."""
    wt = d["worktree"]
    if not Path(wt).exists():
        raise JobError("worktree missing: %s" % wt, NOTREADY)
    out = {}
    for line in git(wt, "diff", "--name-status", "--no-renames", d["base_commit"]).splitlines():
        if line.strip():
            s, p = line.split("\t", 1)
            out[p] = s[0]
    for p in git(wt, "ls-files", "--others", "--exclude-standard").splitlines():
        if p.strip() and not matches(p, JUNK):
            out[p] = "A"
    return out


def deliverable_files(d):
    ch = changed_files(d)
    for rel in list(d.get("managed") or {}) + [PROGRESS]:
        if rel == PROGRESS and d.get("progress_tracked"):
            continue
        ch.pop(rel, None)
    return ch


def fingerprint(d):
    wt = d["worktree"]
    h = hashlib.sha256()
    h.update(git(wt, "diff", "--binary", d["base_commit"]).encode())
    for p in sorted(git(wt, "ls-files", "--others", "--exclude-standard").splitlines()):
        if matches(p, JUNK):
            continue
        h.update(p.encode())
        try:
            h.update(Path(wt, p).read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def scope_violations(d, files):
    v = []
    for p, s in sorted(files.items()):
        if matches(p, ALWAYS_FORBIDDEN):
            v.append("%s: forbidden path" % p)
        elif matches(p, d.get("protect") or []):
            v.append("%s: protected path changed (%s)" % (p, s))
        elif d.get("allow_paths") and not matches(p, d["allow_paths"]):
            v.append("%s: outside allowed scope" % p)
    return v


# ---------------------------------------------------------------- commands
def repo_side_refs(repo):
    """Worktrees and branches of the repo (to spot extra ones created during a job)."""
    wts = [ln[9:] for ln in git(repo, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]
    brs = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    return sorted(wts), sorted(brs)


def resolve_choice(a, spec):
    """Fill in mode/model/budget: explicit flags win, `auto` takes the recommendation."""
    rec = recommend(spec)
    mode = rec["mode"] if a.mode == "auto" else a.mode
    if a.model:
        model = a.model
    elif a.mode == "auto" or mode == rec["mode"]:
        model = rec["model"]
    else:  # caller forced a mode the heuristic did not pick: keep a sensible model for it
        model = DEFAULT_MODEL if mode == "swarm" else rec["model"]
    budget = a.budget if a.budget else (rec["budget"] if a.mode == "auto" else 45)
    reason = "; ".join(rec["why"]) if a.mode == "auto" else "mode %s set by caller" % mode
    return mode, model, budget, reason, rec


def cmd_recommend(a):
    spec = Path(a.spec).read_text()
    rec = recommend(spec)
    if a.json:
        print(json.dumps(rec, indent=2))
    else:
        print("recommend: --mode %s --model %s --budget %d" % (rec["mode"], rec["model"], rec["budget"]))
        print("  parts=%d scope_items=%d words=%d big=%s hard=%s vague=%s" % (
            len(rec["parts"]), rec["scope_items"], rec["words"], rec["big"], ",".join(rec["hard"]) or "-",
            ",".join(rec["vague"]) or "-"))
        for w in rec["why"]:
            print("  - " + w)
    return OK


def cmd_start(a):
    job = Job(a.repo, a.job)
    if job.exists():
        raise JobError("job %r already exists (use status/rework, or cleanup --purge first)" % a.job)
    spec = Path(a.spec).read_text()
    if not spec.strip():
        raise JobError("spec file is empty")
    mode, model, budget, reason, _rec = resolve_choice(a, spec)
    a.model, a.budget = model, budget
    if not a.accept:
        raise JobError("give at least one --accept command (the gate needs something objective to run)")
    repo = job.repo
    base = git(repo, "rev-parse", "--verify", a.base + "^{commit}").strip()
    branch = "agy/" + a.job
    if git(repo, "branch", "--list", branch).strip():
        raise JobError("branch %s already exists" % branch)
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    wt = Path(a.worktree).expanduser().resolve() if a.worktree else worktree_base(repo) / a.job
    if wt.exists():
        raise JobError("worktree path already exists: %s" % wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    job.dir.mkdir(parents=True, exist_ok=False)
    try:
        git(repo, "worktree", "add", "-q", "-b", branch, str(wt), base)
        d = {"name": a.job, "repo": str(repo), "worktree": str(wt), "branch": branch, "base_commit": base,
             "base_ref": a.base, "model": a.model, "mode": mode, "mode_reason": reason, "agy_mode": a.agy_mode,
             "side_refs": repo_side_refs(repo), "accept": a.accept, "allow_paths": a.allow or [],
             "protect": a.protect or [], "budget_min": a.budget, "stuck_min": a.stuck, "max_rounds": a.max_rounds,
             "created": now(), "rounds": [], "launcher": a.launcher, "grace_s": int(os.environ.get("AGY_DELEGATE_GRACE_S", "120")), "git_common_dir": str(common_dir(repo)), "dry_run": a.dry_run,
             "progress_tracked": bool(git(wt, "ls-files", PROGRESS).strip())}
        (job.dir / "spec.md").write_text(spec)
        save_json(job.dir / "settings.json", build_settings(d, a.permissions))
        install_hook(job, d)
        launch_round(job, d, initial_prompt(d, spec))
    except Exception:
        if not job.file.exists():
            shutil.rmtree(job.dir, ignore_errors=True)
        raise
    out = {"job": a.job, "worktree": str(wt), "branch": branch, "base": base[:12], "model": a.model,
           "mode": mode, "budget_min": budget, "why": reason, "state_dir": str(job.dir), "dry_run": a.dry_run}
    if dirty:
        out["warning"] = "main checkout has uncommitted changes; they are NOT in the job worktree (base=%s)" % a.base
    emit(a, out, ("started job %(job)s on %(branch)s (base %(base)s, %(mode)s, model %(model)s, budget %(budget_min)gm)\n"
                  "  why:      %(why)s\n  worktree: %(worktree)s\n  state:    %(state_dir)s") % out
         + ("\n  WARNING: " + out["warning"] if dirty else "")
         + "\nnext: %s status --job %s --wait 20   (run it in the background)" % (Path(__file__).name, a.job))
    return OK


def cmd_status(a):
    job = Job(a.repo, a.job)
    deadline = now() + (a.wait or 0) * 60
    while True:
        d = job.load()
        st = evaluate(job, d)
        if not a.wait or st["state"] not in ("running",) or now() >= deadline:
            break
        time.sleep(min(a.poll, max(1, deadline - now())))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print(render_status(st, a.verbose))
    return OK if st["state"] in ("done", "running", "dry-run") else FAIL


def render_status(st, verbose=False):
    lines = ["job %s: %s  (round %s/%s, %s elapsed, budget %sm, %s, model %s%s)" % (
        st["job"], st["state"].upper(), st.get("round"), st.get("max_rounds"), fmt_dur(st.get("elapsed_s", 0)),
        st.get("budget_min"), st.get("mode", "single"), st.get("model"),
        ", agy mode %s" % st["agy_mode"] if st.get("agy_mode") else "")]
    if st.get("reason"):
        lines.append("  reason: " + st["reason"])
    lines.append("  tools: %s  changed files: %s  conversation: %s" % (
        st.get("tool_calls"), st.get("changed_files"), st.get("conversation_id")))
    if st.get("blocked_actions"):
        lines.append("  blocked: " + "; ".join(str(x) for x in st["blocked_actions"][:5]))
    sw = st.get("swarm")
    if sw:
        lines.append("  swarm %r: %d workers, %d reported [done], %d board posts" % (
            sw.get("title") or "", len(sw["members"]), sw["done"], sw["posts"]))
        for m in sw["members"]:
            lines.append("    @%-12s %-8s %-24s tools=%-4s %s" % (
                m["name"], "done" if m.get("done") else (m.get("state") or "?"), m.get("model") or "",
                m.get("tools") if m.get("tools") is not None else "-", (m.get("last_post") or "")[:80 if not verbose else 140]))
    if st.get("last_events"):
        lines.append("  recent:")
        lines += ["    - " + e for e in st["last_events"]]
    if st.get("progress"):
        pr = st["progress"].splitlines()
        lines.append("  PROGRESS.md:")
        lines += ["    " + x for x in (pr if verbose else pr[:10])]
    if st.get("summary") and st["state"] != "running":
        s = st["summary"] if verbose else st["summary"][-600:]
        lines.append("  agent summary:\n    " + s.replace("\n", "\n    "))
    if st.get("last_gate"):
        lines.append("  last gate: %s" % st["last_gate"])
    return "\n".join(lines)


def cmd_gate(a):
    job = Job(a.repo, a.job)
    d = job.load()
    st = evaluate(job, d)
    if st["state"] in ("running", "stuck") and not a.force:
        raise JobError("job is still %s; wait, or `stop` it first (or --force)" % st["state"], NOTREADY)
    wt = Path(d["worktree"])
    for c in a.add_accept or []:
        if c not in d["accept"]:
            d["accept"].append(c)
    res = {"job": job.name, "round": len(d["rounds"]), "agent_state": st["state"], "checks": []}
    viol = []
    # tamper / history checks
    hp = wt / HOOK_REL
    if d.get("hook_sha") and (not hp.exists() or hashlib.sha256(hp.read_bytes()).hexdigest() != d["hook_sha"]):
        viol.append("%s: fence hook file was modified or removed by the agent" % HOOK_REL)
    head = git(wt, "rev-parse", "HEAD").strip()
    if head != d["base_commit"] and not d.get("accepted"):
        viol.append("worktree HEAD moved from base (%s -> %s): agent created commits" % (d["base_commit"][:10], head[:10]))
    if d.get("side_refs"):
        wts, brs = repo_side_refs(job.repo)
        new_wt = [w for w in wts if w not in d["side_refs"][0] and w != d["worktree"]]
        new_br = [b for b in brs if b not in d["side_refs"][1] and b != d["branch"]]
        if new_wt or new_br:
            viol.append("extra worktrees/branches appeared during the job (work done there is not in the gated "
                        "worktree): %s" % ", ".join(new_wt + new_br))
    files = deliverable_files(d)
    viol += scope_violations(d, files)
    res["changed"] = files
    res["scope_violations"] = viol
    res["diffstat"] = git(wt, "diff", "--stat", d["base_commit"]).strip()
    tracked_changed = set(git(wt, "diff", "--name-only", d["base_commit"]).splitlines())
    untracked = [p for p in files if p not in tracked_changed]
    if untracked:
        res["diffstat"] += ("\n" if res["diffstat"] else "") + "\n".join(
            " %s (new, untracked, %d lines)" % (p, count_lines(wt / p)) for p in untracked)
    allpass = True
    for c in d["accept"]:
        t0 = now()
        try:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            p = subprocess.run(c, shell=True, cwd=wt, text=True, capture_output=True, timeout=a.timeout, env=env)
            rc, out = p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired as e:
            rc, out = 124, "TIMEOUT after %ss\n%s" % (a.timeout, (e.stdout or "") if isinstance(e.stdout, str) else "")
        allpass = allpass and rc == 0
        res["checks"].append({"cmd": c, "rc": rc, "seconds": round(now() - t0, 1),
                              "output_tail": "\n".join(out.strip().splitlines()[-a.tail:])})
    if not files:
        viol.append("no deliverable changes in the worktree")
    res["fingerprint"] = fingerprint(d)
    res["verdict"] = "pass" if allpass and not viol else ("scope" if viol else "fail")
    code = OK if res["verdict"] == "pass" else (SCOPE if viol else FAIL)
    save_json(job.dir / ("gate-%d.json" % res["round"]), res)
    d["last_gate"] = "%s (round %d, %s)" % (res["verdict"], res["round"], time.strftime("%H:%M:%S"))
    d["gate_ok_fingerprint"] = res["fingerprint"] if code == OK else None
    job.save(d)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        out = ["GATE %s  job %s round %d (agent state: %s)" % (res["verdict"].upper(), job.name, res["round"], st["state"])]
        for c in res["checks"]:
            out.append("  [%s] %s  (rc=%d, %.1fs)" % ("PASS" if c["rc"] == 0 else "FAIL", c["cmd"], c["rc"], c["seconds"]))
            if c["rc"] != 0 or a.verbose:
                out += ["      " + x for x in c["output_tail"].splitlines()]
        out.append("  diffstat:\n    " + (res["diffstat"].replace("\n", "\n    ") or "(none)"))
        if viol:
            out.append("  scope violations:")
            out += ["    - " + v for v in viol]
        out.append("  review the diff: git -C %s diff %s   (+ untracked files above)" % (shlex.quote(str(wt)), d["base_commit"][:12]))
        print("\n".join(out))
    return code


def count_lines(p):
    try:
        return len(Path(p).read_bytes().splitlines())
    except OSError:
        return 0


def cmd_diff(a):
    """Full review diff vs base, including new untracked files (scratch/junk excluded)."""
    job = Job(a.repo, a.job)
    d = job.load()
    wt = Path(d["worktree"])
    files = deliverable_files(d)
    tracked = [p for p in files if p in set(git(wt, "diff", "--name-only", d["base_commit"]).splitlines())]
    out = git(wt, "diff", d["base_commit"], "--", *tracked) if tracked else ""
    for p in sorted(set(files) - set(tracked)):
        q = run(["git", "-C", str(wt), "diff", "--no-index", "--", "/dev/null", p], check=False)
        out += q.stdout
    if a.stat:
        out = "\n".join("%s %s" % (s_, p) for p, s_ in sorted(files.items()))
    sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
    return OK


def cmd_rework(a):
    job = Job(a.repo, a.job)
    d = job.load()
    st = evaluate(job, d)
    if st["state"] in ("running", "stuck"):
        raise JobError("job is still %s; stop it first" % st["state"], NOTREADY)
    if d.get("accepted"):
        raise JobError("job already accepted")
    if len(d["rounds"]) >= d["max_rounds"] and not a.force:
        raise JobError("max rounds (%d) reached; escalate to the user instead (or --force)" % d["max_rounds"], NOTREADY)
    fb = a.feedback or ""
    if a.feedback_file:
        fb += ("\n" if fb else "") + Path(a.feedback_file).read_text()
    if not fb.strip():
        raise JobError("give --feedback or --feedback-file with concrete, checkable points")
    if a.budget:
        d["budget_min"] = a.budget
    for c in a.add_accept or []:
        if c not in d["accept"]:
            d["accept"].append(c)
    cid = st.get("conversation_id")
    n = len(d["rounds"]) + 1
    prompt = REWORK.format(n=n, maxr=d["max_rounds"], wt=d["worktree"], feedback=fb.strip(),
                           progress=PROGRESS, accept=accept_text(d))
    if d.get("mode") == "swarm":
        prompt += SWARM_REWORK
    if not cid:  # previous round never got a conversation: start over with full context
        prompt = initial_prompt(d, (job.dir / "spec.md").read_text()) + "\n\n" + prompt
    rnd = launch_round(job, d, prompt, conversation=cid)
    emit(a, {"job": job.name, "round": rnd["n"], "conversation": cid},
         "rework round %d launched for %s (conversation %s)" % (rnd["n"], job.name, cid or "new"))
    return OK


def cmd_accept(a):
    job = Job(a.repo, a.job)
    d = job.load()
    if d.get("accepted"):
        raise JobError("already accepted: %s" % d["accepted"])
    st = evaluate(job, d)
    if st["state"] in ("running", "stuck"):
        raise JobError("job is still %s" % st["state"], NOTREADY)
    if not a.force:
        if not d.get("gate_ok_fingerprint"):
            raise JobError("no passing gate on record; run `gate` first (or --force)", NOTREADY)
        if fingerprint(d) != d["gate_ok_fingerprint"]:
            raise JobError("worktree changed since the passing gate; re-run `gate`", NOTREADY)
    wt = Path(d["worktree"])
    files = sorted(deliverable_files(d))
    restore_managed(d)
    if not files:
        raise JobError("nothing to commit", FAIL)
    git(wt, "add", "-A", "--", *files)
    if not git(wt, "diff", "--cached", "--name-only").strip():
        raise JobError("nothing to commit", FAIL)
    title = a.message or "agy(%s): delegated job" % job.name
    body = "Delegated to agy (%s), %d round(s); gate: %s" % (d["model"], len(d["rounds"]), d.get("last_gate"))
    git(wt, "commit", "-q", "-m", title, "-m", body)
    sha = git(wt, "rev-parse", "HEAD").strip()
    d["accepted"] = "%s at %s" % (sha[:12], time.strftime("%Y-%m-%d %H:%M"))
    job.save(d)
    msg = "accepted: committed %s on branch %s (not merged, not pushed)" % (sha[:12], d["branch"])
    if a.merge:
        repo = job.repo
        if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            raise JobError(msg + "\nmerge skipped: main checkout has uncommitted changes", FAIL)
        git(repo, "merge", "--no-ff", "-q", "-m", "Merge %s" % d["branch"], d["branch"])
        msg += "\nmerged %s into %s" % (d["branch"], git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip())
    emit(a, {"job": job.name, "commit": sha, "branch": d["branch"], "merged": bool(a.merge)}, msg)
    return OK


def kill_round(job, rnd):
    killed = False
    if rnd.get("unit"):
        killed = run(["systemctl", "--user", "stop", rnd["unit"]], check=False).returncode == 0 or killed
    for pid in (runner_pid(job, rnd), load_pid(job.rpath(rnd["n"], "pid"))):
        if pid and pid_alive(pid):
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
            killed = True
    return killed


def load_pid(p):
    try:
        return int(Path(p).read_text().strip())
    except (OSError, ValueError):
        return None


def cmd_stop(a):
    job = Job(a.repo, a.job)
    d = job.load()
    if not d["rounds"]:
        raise JobError("nothing running")
    rnd = d["rounds"][-1]
    killed = kill_round(job, rnd)
    deadline = now() + 30
    while now() < deadline and (pid_alive(runner_pid(job, rnd)) or pid_alive(load_pid(job.rpath(rnd["n"], "pid")))):
        time.sleep(0.5)
    d["status_override"] = "stopped"
    job.save(d)
    emit(a, {"job": job.name, "signalled": killed}, "stopped %s (%s)" % (job.name, "signalled" if killed else "was not running"))
    return OK


def cmd_cleanup(a):
    job = Job(a.repo, a.job)
    d = job.load()
    if d["rounds"]:
        st = evaluate(job, d)
        if st["state"] in ("running", "stuck"):
            raise JobError("job is still %s; stop it first" % st["state"], NOTREADY)
    wt = Path(d["worktree"])
    msgs = []
    if wt.exists():
        if not d.get("accepted"):
            restore_managed(d)
        ignore = set(d.get("managed") or {}) | ({PROGRESS} if not d.get("progress_tracked") else set())
        leftovers = [ln[3:] for ln in git(wt, "status", "--porcelain", "--untracked-files=all").splitlines()
                     if ln[3:] not in ignore and not matches(ln[3:], JUNK)]
        if leftovers and not a.force:
            raise JobError("worktree has uncommitted (unaccepted) changes: %s; `accept` them or pass --force to discard"
                           % ", ".join(leftovers[:8]), NOTREADY)
        # only agent scratch (PROGRESS.md, caches, fence hook) remains at this point, or the caller passed --force
        git(job.repo, "worktree", "remove", "--force", str(wt))
        msgs.append("removed worktree %s" % wt)
    git(job.repo, "worktree", "prune")
    if a.delete_branch:
        git(job.repo, "branch", "-D", d["branch"])
        msgs.append("deleted branch %s" % d["branch"])
    else:
        msgs.append("kept branch %s" % d["branch"])
    if a.purge:
        shutil.rmtree(job.dir)
        msgs.append("purged state %s" % job.dir)
    emit(a, {"job": job.name, "actions": msgs}, "\n".join(msgs))
    return OK


def cmd_list(a):
    root = state_root(repo_root(a.repo))
    rows = []
    for p in sorted(root.glob("*/job.json")) if root.exists() else []:
        job = Job(a.repo, p.parent.name)
        st = evaluate(job, job.load())
        rows.append({k: st.get(k) for k in ("job", "state", "mode", "round", "model", "branch", "elapsed_s", "last_gate", "accepted")})
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print("%-24s %-9s %-6s round %s  %-28s %s  gate=%s%s" % (r["job"], r["state"], r["mode"], r["round"], r["model"], r["branch"],
                                                              r["last_gate"], "  accepted" if r["accepted"] else ""))
        if not rows:
            print("no jobs")
    return OK


def emit(a, data, text):
    print(json.dumps(data, indent=2) if getattr(a, "json", False) else text)


# ---------------------------------------------------------------- cli
def build_parser():
    ap = argparse.ArgumentParser(prog="agy_job.py", description="Delegate a coding job to agy and gate the result.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, job=True):
        p.add_argument("--repo", default=".", help="git repo (default: cwd)")
        if job:
            p.add_argument("--job", required=True, help="job name (slug); branch is agy/<job>")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = common(sub.add_parser("recommend", help="suggest --mode/--model/--budget from a job spec (no side effects)"), job=False)
    p.add_argument("--spec", required=True, help="job spec markdown file")
    p.set_defaults(fn=cmd_recommend)

    p = common(sub.add_parser("start", help="create worktree + branch and launch agy in the background"))
    p.add_argument("--spec", required=True, help="job spec markdown file (see templates/job-spec.md)")
    p.add_argument("--accept", action="append", help="acceptance shell command run in the worktree (repeatable)")
    p.add_argument("--mode", choices=["auto", "single", "swarm"], default="auto",
                   help="single agent, agy /swarm (coordinator + parallel workers + verifier), or auto (default): "
                        "take `recommend`'s pick from the spec")
    p.add_argument("--model", help="agy model id; effort is part of the id (default: recommended for the spec, "
                                   "else %s). In swarm mode the workers inherit it" % DEFAULT_MODEL)
    p.add_argument("--budget", type=float, help="time budget per round, minutes (default: recommended with --mode auto, else 45)")
    p.add_argument("--agy-mode", choices=["auto", "accept-edits", "default"], default=DEFAULT_AGY_MODE,
                   help="agy execution mode (default auto; `default` passes no --mode). accept-edits auto-denies every "
                        "unlisted command headless")
    p.add_argument("--stuck", type=float, default=12, help="minutes without output before status says stuck (default 12)")
    p.add_argument("--max-rounds", type=int, default=3, help="max rounds incl. the first (default 3)")
    p.add_argument("--allow", action="append", help="glob of paths the agent may change (repeatable; default: any)")
    p.add_argument("--protect", action="append", help="glob of paths that must not change, e.g. 'tests/*' (repeatable)")
    p.add_argument("--base", default="HEAD", help="base ref for the worktree (default HEAD)")
    p.add_argument("--worktree", help="worktree path (default under ~/.local/state/agy-delegate/worktrees)")
    p.add_argument("--permissions", help="alternative permissions template JSON")
    p.add_argument("--launcher", choices=["auto", "systemd", "setsid"], default="auto",
                   help="how to detach agy: systemd-run --user (survives harnesses that kill child processes) or setsid; "
                        "auto picks systemd when a user systemd is available (env AGY_DELEGATE_LAUNCHER overrides)")
    p.add_argument("--dry-run", action="store_true", help="set up everything but do not launch agy")
    p.set_defaults(fn=cmd_start)

    p = common(sub.add_parser("status", help="cheap progress check"))
    p.add_argument("--wait", type=float, default=0, help="block up to N minutes until the job leaves 'running'")
    p.add_argument("--poll", type=float, default=30, help="poll interval seconds for --wait")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = common(sub.add_parser("gate", help="independently run acceptance + scope checks; exit 0 pass, 1 fail, 2 scope, 3 not ready"))
    p.add_argument("--timeout", type=int, default=900, help="seconds per acceptance command")
    p.add_argument("--tail", type=int, default=25, help="output lines kept per command")
    p.add_argument("--force", action="store_true", help="gate even if the agent is still running")
    p.add_argument("--add-accept", action="append", help="add an acceptance command to the job (persisted)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_gate)

    p = common(sub.add_parser("diff", help="print the full review diff vs base (incl. new files)"))
    p.add_argument("--stat", action="store_true", help="only list changed deliverable files")
    p.set_defaults(fn=cmd_diff)

    p = common(sub.add_parser("rework", help="send feedback to the same agy conversation"))
    p.add_argument("--feedback", help="concrete feedback text")
    p.add_argument("--feedback-file", help="file with feedback")
    p.add_argument("--budget", type=float, help="new time budget for this round, minutes")
    p.add_argument("--add-accept", action="append", help="add an acceptance command the agent must also satisfy")
    p.add_argument("--force", action="store_true", help="exceed max rounds")
    p.set_defaults(fn=cmd_rework)

    p = common(sub.add_parser("accept", help="commit the gated result on agy/<job> (no push)"))
    p.add_argument("--message", "-m", help="commit title")
    p.add_argument("--merge", action="store_true", help="also merge into the main checkout's current branch (only if the user asked)")
    p.add_argument("--force", action="store_true", help="skip the passing-gate requirement")
    p.set_defaults(fn=cmd_accept)

    p = common(sub.add_parser("stop", help="terminate the running agy round"))
    p.set_defaults(fn=cmd_stop)

    p = common(sub.add_parser("cleanup", help="remove the worktree (branch kept unless --delete-branch)"))
    p.add_argument("--delete-branch", action="store_true")
    p.add_argument("--purge", action="store_true", help="also delete the job state dir")
    p.add_argument("--force", action="store_true", help="discard unaccepted worktree changes")
    p.set_defaults(fn=cmd_cleanup)

    p = common(sub.add_parser("list", help="list jobs in this repo"), job=False)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("_run", help=argparse.SUPPRESS)
    p.add_argument("state_dir")
    p.add_argument("round")
    p.set_defaults(fn=cmd__run)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except JobError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
