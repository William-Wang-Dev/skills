"""Unit tests for agy_job.py and fence_hook.py (stdlib unittest, fake agy on PATH).

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import agy_job  # noqa: E402
import fence_hook  # noqa: E402


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agyjob-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "config", "user.name", "Test", cwd=self.repo)
        sh("git", "config", "user.email", "test@example.invalid", cwd=self.repo)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text(
            "import unittest\nimport x\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(x.f(), 42)\n")
        (self.repo / "README.md").write_text("hi\n")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-qm", "init", cwd=self.repo)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "agy").symlink_to(HERE / "fake_agy.py")
        os.chmod(HERE / "fake_agy.py", 0o755)
        self.env_backup = dict(os.environ)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        os.environ.pop("AGY_BIN", None)
        os.environ["AGY_DELEGATE_WORKTREES"] = str(self.tmp / "wts")
        os.environ["FAKE_AGY_ARGV_LOG"] = str(self.tmp / "argv.log")
        os.environ["FAKE_AGY_MODE"] = "ok"
        os.environ["AGY_DELEGATE_LAUNCHER"] = "setsid"
        self.spec = self.tmp / "spec.md"
        self.spec.write_text("# Job\nImplement x.f() returning 42 in x.py\n")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = agy_job.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def start(self, job="j1", *extra):
        return self.cli("start", "--repo", str(self.repo), "--job", job, "--spec", str(self.spec),
                        "--accept", "python3 -m unittest discover -s tests", "--protect", "tests/*", *extra)

    def wait_state(self, job="j1", timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, out, _ = self.cli("status", "--repo", str(self.repo), "--job", job, "--json")
            st = json.loads(out)
            if st["state"] not in ("running",):
                return st
            time.sleep(0.2)
        return st

    def wt(self, job="j1"):
        return Path(os.environ["AGY_DELEGATE_WORKTREES"]).glob("*/" + job).__next__()


class TestParsing(Base):
    def test_bad_job_name(self):
        rc, _, err = self.cli("status", "--repo", str(self.repo), "--job", "Bad Name")
        self.assertEqual(rc, agy_job.USAGE)
        self.assertIn("bad job name", err)

    def test_start_requires_accept(self):
        rc, _, err = self.cli("start", "--repo", str(self.repo), "--job", "j", "--spec", str(self.spec))
        self.assertEqual(rc, agy_job.USAGE)
        self.assertIn("--accept", err)

    def test_matches(self):
        self.assertTrue(agy_job.matches("tests/test_x.py", ["tests/*"]))
        self.assertTrue(agy_job.matches("tests/sub/a.py", ["tests"]))
        self.assertTrue(agy_job.matches("src/a.py", ["src/"]))
        self.assertFalse(agy_job.matches("src2/a.py", ["src"]))
        self.assertTrue(agy_job.matches("a/.env", agy_job.ALWAYS_FORBIDDEN))

    def test_parse_stream(self):
        p = self.tmp / "s.jsonl"
        p.write_text("\n".join([
            json.dumps({"event": "init", "conversation_id": "c-1", "init": {"model": "m"}}),
            "not json",
            json.dumps({"event": "step_update", "step_update": {"state": "DONE", "step_type": "tool", "tool_name": "run_command",
                                                                 "tool_info": {"parameters": {"CommandLine": "ls"}}}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}),
        ]))
        info = agy_job.parse_stream(p)
        self.assertEqual(info["conversation_id"], "c-1")
        self.assertEqual(info["tools"], 1)
        self.assertEqual(info["result"]["status"], "SUCCESS")


class TestLifecycle(Base):
    def test_start_status_gate_accept_cleanup(self):
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n;PROGRESS.md=- [x] done\\n"
        rc, out, err = self.start()
        self.assertEqual(rc, 0, err)
        st = self.wait_state()
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(st["conversation_id"])
        self.assertIn("progress", st)
        self.assertTrue(any("Permission denied" in b for b in st["blocked_actions"]), st["blocked_actions"])
        # state lives in the git common dir, not in the user's tree
        self.assertTrue((self.repo / ".git" / "agy-jobs" / "j1" / "job.json").exists())
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.repo), "")
        # agy got per-job settings + stream-json, never the skip-permissions flag
        argv = json.loads((self.tmp / "argv.log").read_text().splitlines()[0])
        self.assertIn("--settings", argv)
        self.assertIn("stream-json", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertIn("command(git push)", settings["permissions"]["deny"])
        self.assertNotIn("_comment", settings)
        file_rules = [r for k in ("allow", "deny") for r in settings["permissions"][k] if r.startswith(("read_file(", "write_file("))]
        self.assertTrue(file_rules)
        for r in file_rules:  # sandbox rejects relative file rules
            self.assertTrue(r.split("(", 1)[1].startswith("/"), r)
        self.assertIn("write_file(%s/.env)" % self.wt(), settings["permissions"]["deny"])
        wt = self.wt()
        self.assertTrue((wt / ".agents" / "hooks.json").exists())
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1", "--json")
        res = json.loads(out)
        self.assertEqual(rc, 0, res)
        self.assertEqual(set(res["changed"]), {"x.py"})  # PROGRESS.md and hook are not deliverables
        rc, out, err = self.cli("accept", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, 0, err)
        files = sh("git", "show", "--name-only", "--format=", "agy/j1", cwd=self.repo).split()
        self.assertEqual(files, ["x.py"])
        rc, out, err = self.cli("cleanup", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, 0, err)
        self.assertFalse(wt.exists())
        self.assertIn("agy/j1", sh("git", "branch", "--list", "agy/j1", cwd=self.repo))

    def test_gate_fails_then_rework_same_conversation(self):
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 41\\n"
        self.start()
        st1 = self.wait_state()
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, agy_job.FAIL, out)
        self.assertIn("GATE FAIL", out)
        rc, _, err = self.cli("accept", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, agy_job.NOTREADY)
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n"
        rc, out, err = self.cli("rework", "--repo", str(self.repo), "--job", "j1", "--feedback", "f() must return 42")
        self.assertEqual(rc, 0, err)
        st2 = self.wait_state()
        self.assertEqual(st2["round"], 2)
        self.assertEqual(st2["conversation_id"], st1["conversation_id"])
        argv2 = json.loads((self.tmp / "argv.log").read_text().splitlines()[1])
        self.assertEqual(argv2[argv2.index("--conversation") + 1], st1["conversation_id"])
        self.assertIn("f() must return 42", argv2[argv2.index("-p") + 1])
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, 0, out)

    def test_max_rounds(self):
        self.start("j1", "--max-rounds", "1")
        self.wait_state()
        rc, _, err = self.cli("rework", "--repo", str(self.repo), "--job", "j1", "--feedback", "x")
        self.assertEqual(rc, agy_job.NOTREADY)
        self.assertIn("max rounds", err)

    def test_scope_violation_protected_tests(self):
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n;tests/test_x.py=print(1)\\n"
        self.start()
        self.wait_state()
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1", "--json")
        res = json.loads(out)
        self.assertEqual(rc, agy_job.SCOPE)
        self.assertTrue(any("tests/test_x.py" in v for v in res["scope_violations"]))

    def test_allow_scope_and_forbidden(self):
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n;other.txt=x;.env=SECRET=1"
        self.start("j1", "--allow", "x.py")
        self.wait_state()
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1", "--json")
        v = json.loads(out)["scope_violations"]
        self.assertEqual(rc, agy_job.SCOPE)
        self.assertTrue(any(s.startswith("other.txt") for s in v))
        self.assertTrue(any(s.startswith(".env") and "forbidden" in s for s in v))

    def test_hook_tamper_detected(self):
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n;.agents/hooks.json={}"
        self.start()
        self.wait_state()
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, agy_job.SCOPE)
        self.assertIn("fence hook", out)

    def test_failed_and_timeout_and_blocked(self):
        for mode, want in (("fail", "failed"), ("timeout", "timeout"), ("denied", "blocked")):
            os.environ["FAKE_AGY_MODE"] = mode
            job = "j-" + mode
            self.start(job)
            st = self.wait_state(job)
            self.assertEqual(st["state"], want, st)

    def test_stuck_then_stop(self):
        os.environ["FAKE_AGY_MODE"] = "sleep"
        self.start("j1", "--stuck", "0.01")  # 0.6 s
        time.sleep(2)
        st = self.wait_state()
        self.assertEqual(st["state"], "stuck", st)
        rc, _, err = self.cli("gate", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, agy_job.NOTREADY)
        rc, out, err = self.cli("stop", "--repo", str(self.repo), "--job", "j1")
        self.assertEqual(rc, 0, err)
        st = self.wait_state()
        self.assertEqual(st["state"], "stopped", st)

    def test_budget_enforced_by_runner(self):
        os.environ["FAKE_AGY_MODE"] = "sleep"
        os.environ["FAKE_AGY_SLEEP"] = "60"
        os.environ["AGY_DELEGATE_GRACE_S"] = "0"
        self.start("j1", "--budget", "0.02", "--stuck", "5")  # 1.2 s budget
        st = self.wait_state(timeout=30)
        self.assertEqual(st["state"], "timeout", st)
        time.sleep(8)  # runner polls every 5 s, then terminates agy and records the exit
        st = self.wait_state()
        self.assertTrue(st.get("rc") is not None, st)
        self.assertEqual(st["state"], "timeout", st)

    def test_dirty_main_checkout_warning_and_dry_run(self):
        (self.repo / "README.md").write_text("changed\n")
        rc, out, err = self.start("j1", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", out)
        st = self.wait_state()
        self.assertEqual(st["state"], "dry-run")
        self.assertFalse((self.tmp / "argv.log").exists())

    def test_duplicate_job_rejected(self):
        self.start("j1", "--dry-run")
        rc, _, err = self.start("j1", "--dry-run")
        self.assertEqual(rc, agy_job.USAGE)
        self.assertIn("already exists", err)

    @unittest.skipUnless(shutil.which("systemd-run") and
                         subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0
                         if shutil.which("systemctl") else False, "no user systemd")
    def test_systemd_launcher_roundtrip_and_stop(self):
        os.environ["AGY_DELEGATE_LAUNCHER"] = "systemd"
        os.environ["FAKE_AGY_WRITE"] = "x.py=def f():\\n    return 42\\n"
        self.start("j-sd")
        st = self.wait_state("j-sd", timeout=40)
        self.assertEqual(st["state"], "done", st)
        d = agy_job.Job(self.repo, "j-sd").load()
        self.assertEqual(d["rounds"][-1]["launcher"], "systemd")
        rc, out, _ = self.cli("gate", "--repo", str(self.repo), "--job", "j-sd")
        self.assertEqual(rc, 0, out)
        os.environ["FAKE_AGY_MODE"] = "sleep"
        self.cli("rework", "--repo", str(self.repo), "--job", "j-sd", "--feedback", "again")
        time.sleep(3)
        rc, _, err = self.cli("stop", "--repo", str(self.repo), "--job", "j-sd")
        self.assertEqual(rc, 0, err)
        st = self.wait_state("j-sd")
        self.assertEqual(st["state"], "stopped", st)

    def test_list(self):
        self.start("j1", "--dry-run")
        rc, out, _ = self.cli("list", "--repo", str(self.repo), "--json")
        self.assertEqual(json.loads(out)[0]["job"], "j1")


class TestFenceHook(unittest.TestCase):
    def d(self, name, root="/w", **args):
        return fence_hook.decide({"toolCall": {"name": name, "args": args}, "workspacePaths": [root],
                                  "artifactDirectoryPath": "/home/u/.gemini/x/brain/c1"}, root)[0]

    def test_bypass_denied(self):
        self.assertEqual(self.d("run_command", CommandLine="ls", BypassSandbox=True), "deny")

    def test_commands(self):
        for c in ("git push origin HEAD", "cd x && curl -s http://e", "sudo true", "rm -rf ~", "rm -rf /",
                  "git remote add o u", "bash -c 'wget x'", "git commit -m x",
                  "git -c user.name=x -c user.email=x@x commit --allow-empty -m x", "git -C . push", "git --no-pager push o"):
            self.assertEqual(self.d("run_command", CommandLine=c), "deny", c)
        for c in ("python3 -m unittest", "rm -rf build", "git status", "git diff HEAD", "pytest -q", "make test"):
            self.assertEqual(self.d("run_command", CommandLine=c), "allow", c)

    def test_writes(self):
        self.assertEqual(self.d("write_to_file", TargetFile="/w/src/a.py"), "allow")
        self.assertEqual(self.d("write_to_file", TargetFile="src/a.py"), "allow")
        self.assertEqual(self.d("write_to_file", TargetFile="/etc/passwd"), "deny")
        self.assertEqual(self.d("write_to_file", TargetFile="/w/.git/config"), "deny")
        self.assertEqual(self.d("replace_file_content", TargetFile="/w/.agents/hooks.json"), "deny")
        self.assertEqual(self.d("write_to_file", TargetFile="/w/.env"), "deny")
        self.assertEqual(self.d("write_to_file", TargetFile="/home/u/.gemini/x/brain/c1/plan.md"), "allow")
        # names/keys actually seen from agy 1.5.x in hook payloads
        self.assertEqual(self.d("write_file", Path="/w/roman.py", Content="x"), "allow")
        self.assertEqual(self.d("write_file", Path="/tmp/elsewhere.py", Content="x"), "deny")
        self.assertEqual(self.d("write_file", Path="/w/.git/HEAD", Content="x"), "deny")
        self.assertEqual(self.d("read_file", Path="/etc/hosts"), "allow")

    def test_main_io(self):
        p = subprocess.run([sys.executable, str(HERE.parent / "scripts" / "fence_hook.py")], text=True, capture_output=True,
                           input=json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": "curl x"}}}))
        self.assertEqual(json.loads(p.stdout)["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
