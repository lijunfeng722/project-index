#!/usr/bin/env python3
"""用临时仓库验证规则同步、提交范围和文档影响，不访问真实服务。"""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_policy", ROOT / "scripts/check_dev_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="friday-dev-policy-")
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "product"
        self.make_repo(self.source, policy.SOURCE)
        self.make_repo(self.target, "https://github.com/Geeprox/Friday")
        files = ["docs/development.md", "repos.json", *policy.SIGNATURES,
                 "skills/friday-development/SKILL.md"]
        for name in files:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.original = "# 原仓库规则\n\n保留设备授权边界，普通测试不执行真实动作。\n"
        (self.target / "AGENTS.md").write_text(self.original)
        policy.sync(self.source, [self.target])

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, root, *args):
        return subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false",
                               "-c", "core.hooksPath=/dev/null", *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def make_repo(self, root, remote):
        root.mkdir()
        self.command(root, "init", "-b", "main")
        self.command(root, "remote", "add", "origin", remote)
        self.command(root, "config", "user.name", "SYNTHETIC")
        self.command(root, "config", "user.email", "synthetic@example.invalid")

    def commit(self, message, name=None, email=None):
        args = []
        if name:
            args += ["-c", "user.name=" + name]
        if email:
            args += ["-c", "user.email=" + email]
        self.command(self.target, *args, "commit", "--allow-empty", "-m", message)
        return self.command(self.target, "rev-parse", "HEAD")

    def test_sync_preserves_repository_rules_and_is_idempotent(self):
        self.assertTrue((self.target / "AGENTS.md").read_text().startswith(self.original))
        self.assertFalse(policy.check_repo(self.source)["errors"])
        self.assertFalse(policy.check_repo(self.target)["errors"])
        again = policy.sync(self.source, [self.target])
        self.assertEqual(again["changed_files"], 0)
        self.assertFalse(policy.sync(self.source, [self.target], check=True)["errors"])

    def test_remote_identity_accepts_equivalent_ssh_and_https_urls(self):
        host, _, path = policy.SOURCE.removeprefix("https://").partition("/")
        ssh = f"git@{host}:{path}.git"
        self.assertEqual(policy.remote_url(ssh), policy.SOURCE)
        self.assertEqual(policy.remote_url(policy.SOURCE + ".git/"), policy.SOURCE)
        self.command(self.source, "remote", "set-url", "origin", ssh)
        self.assertFalse(policy.sync(self.source, [self.target], check=True)["errors"])

    def test_satellite_checks_without_canonical_repository_or_network(self):
        shutil.rmtree(self.source)
        shutil.rmtree(self.target / ".git")
        result = subprocess.run([sys.executable, str(self.target / policy.CHECKER), "check"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_invalid_json_shape_fails_without_exposing_its_contents(self):
        private = "SYNTHETIC_PRIVATE_MARKER"
        (self.target / policy.SNAPSHOT).write_text(json.dumps([private]))
        result = subprocess.run([sys.executable, str(self.target / policy.CHECKER), "check"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid_policy_or_event_json", result.stdout)
        self.assertNotIn(private, result.stdout + result.stderr)

    def test_shared_rules_and_checker_drift_are_detected(self):
        agents = self.target / "AGENTS.md"
        agents.write_text(agents.read_text().replace(policy.START, policy.START + "\n规则已偏离同步版本。", 1))
        self.assertIn("agents_shared_rules_drift", policy.check_repo(self.target)["errors"])
        checker = self.target / policy.CHECKER
        checker.write_text(checker.read_text() + "\n# 本地意外修改\n")
        self.assertIn("managed_asset_drift:" + policy.CHECKER, policy.check_repo(self.target)["errors"])
        self.assertTrue(policy.sync(self.source, [self.target], check=True)["errors"])

    def test_canonical_change_requires_refreshing_skill_reference(self):
        doc = self.source / "docs/development.md"
        doc.write_text(doc.read_text() + "\n补充开发说明。\n")
        self.assertIn("skill_reference_not_synced", policy.check_repo(self.source)["errors"])
        policy.sync(self.source, [self.target])
        self.assertFalse(policy.check_repo(self.source)["errors"])

    def test_unknown_files_are_not_overwritten_even_partially(self):
        other = self.root / "existing-product"
        self.make_repo(other, "https://github.com/Geeprox/Friday")
        (other / "AGENTS.md").write_text(self.original)
        path = other / policy.CHECKER
        path.parent.mkdir()
        path.write_text("# 使用者自己的工具\n")
        with self.assertRaises(policy.PolicyError):
            policy.sync(self.source, [other])
        self.assertEqual(path.read_text(), "# 使用者自己的工具\n")
        self.assertEqual((other / "AGENTS.md").read_text(), self.original)

    def test_unmanaged_repository_and_symlink_are_refused(self):
        self.command(self.target, "remote", "set-url", "origin", "https://github.com/home-assistant/core")
        with self.assertRaises(policy.PolicyError):
            policy.sync(self.source, [self.target])
        self.command(self.target, "remote", "set-url", "origin", "https://github.com/Geeprox/Friday")
        checker = self.target / policy.CHECKER
        checker.unlink()
        outside = self.root / "untouched.py"
        outside.write_text("# 外部文件\n")
        checker.symlink_to(outside)
        with self.assertRaises(policy.PolicyError):
            policy.sync(self.source, [self.target])
        self.assertEqual(outside.read_text(), "# 外部文件\n")

    def test_commit_check_ignores_old_history_and_does_not_echo_messages(self):
        baseline = self.commit("historical upstream baseline")
        self.commit("修复：补充健康检查")
        self.assertFalse(policy.commit_language_errors(self.target, baseline)["errors"])
        private = "SYNTHETIC_PRIVATE_MARKER"
        sha = self.commit(private)
        report = policy.commit_language_errors(self.target, baseline)
        self.assertEqual(report["checked_commits"], 2)
        self.assertEqual(report["errors"], ["commit_subject_requires_chinese:" + sha[:12]])
        self.assertNotIn(private, json.dumps(report))

    def test_merge_keeps_second_parent_upstream_messages(self):
        baseline = self.commit("historical baseline")
        self.commit("功能：新增检查入口")
        self.command(self.target, "checkout", "-b", "upstream", baseline)
        self.commit("upstream implementation")
        self.command(self.target, "checkout", "main")
        self.command(self.target, "merge", "--no-ff", "upstream", "-m", "Merge upstream")
        report = policy.commit_language_errors(self.target, baseline)
        self.assertFalse(report["errors"])
        self.assertEqual(report["checked_commits"], 1)
        self.assertEqual(report["exempt_commits"], 1)

    def test_standard_revert_and_github_bot_are_exempt(self):
        baseline = self.commit("历史基线")
        self.commit("chore: dependency update", "dependabot[bot]",
                    "dependabot[bot]@users.noreply.github.com")
        self.commit('Revert "historical change"\n\nThis reverts commit ' + baseline + '.')
        report = policy.commit_language_errors(self.target, baseline)
        self.assertFalse(report["errors"])
        self.assertEqual(report["exempt_commits"], 2)

    def test_documentation_impacts_require_evidence(self):
        body = "\n".join([
            "- 用户文档：无影响", "- 用户文档依据：仅修改开发规范，不改变家庭使用方式。",
            "- 技术文档：已更新", "- 技术文档依据：docs/development.md",
            "- 顶级索引：不适用", "- 顶级索引依据：仓库关系与部署入口保持原样。"])
        self.assertFalse(policy.documentation_errors(body))
        self.assertTrue(policy.documentation_errors(body.replace("docs/development.md", "待填写")))
        self.assertTrue(policy.documentation_errors(body.replace("仅修改开发规范，不改变家庭使用方式。", "")))
        self.assertTrue(policy.documentation_errors(body.replace("无影响", "待填写", 1)))
        self.assertTrue(policy.documentation_errors(body + "\n- 用户文档：已更新\n"))
        self.assertFalse(policy.documentation_errors(None, bot=True))

    def test_pr_event_combines_commit_and_documentation_checks(self):
        baseline = self.commit("old upstream baseline")
        head = self.commit("功能：新增开发约定检查")
        body = "\n".join(["用户文档：无影响", "用户文档依据：只改变开发工具与规范。",
                           "技术文档：已更新", "技术文档依据：docs/development.md",
                           "顶级索引：已更新", "顶级索引依据：README.md"])
        event = {"pull_request": {"base": {"sha": baseline}, "head": {"sha": head},
                                  "body": body, "user": {"type": "User"}}}
        self.assertFalse(policy.event_checks(self.target, event)["errors"])
        event["pull_request"]["body"] = None
        self.assertIn("pr_documentation_impact_missing", policy.event_checks(self.target, event)["errors"])
        event["pull_request"]["user"]["type"] = "Bot"
        self.assertFalse(policy.event_checks(self.target, event)["errors"])

    def test_new_branch_uses_default_branch_baseline(self):
        baseline = self.commit("existing upstream history")
        self.command(self.target, "update-ref", "refs/remotes/origin/main", baseline)
        self.command(self.target, "checkout", "-b", "feature")
        head = self.commit("功能：明确提交范围")
        report = policy.event_checks(self.target, {"before": "0" * 40, "after": head,
                  "ref": "refs/heads/feature", "repository": {"default_branch": "main"}})
        self.assertFalse(report["errors"])
        self.assertEqual(report["checked_commits"], 1)

    def test_push_with_unavailable_before_uses_default_branch_baseline(self):
        baseline = self.commit("existing upstream history")
        self.command(self.target, "update-ref", "refs/remotes/origin/main", baseline)
        head = self.commit("功能：压缩后的提交")
        report = policy.event_checks(self.target, {"before": "f" * 40, "after": head,
                  "ref": "refs/heads/feature", "repository": {"default_branch": "main"}})
        self.assertFalse(report["errors"])
        self.assertEqual(report["checked_commits"], 1)

    def test_default_branch_force_push_checks_all_commits_after_available_before(self):
        baseline = self.commit("existing upstream history")
        before = self.commit("old branch tip")
        self.command(self.target, "checkout", "--detach", baseline)
        intermediate = self.commit("new intermediate commit")
        head = self.commit("修复：新分支末端提交")
        self.command(self.target, "update-ref", "refs/remotes/origin/main", head)
        report = policy.event_checks(self.target, {"before": before, "after": head,
                  "ref": "refs/heads/main", "repository": {"default_branch": "main"}})
        self.assertEqual(report["checked_commits"], 2)
        self.assertEqual(report["errors"],
                         ["commit_subject_requires_chinese:" + intermediate[:12]])

    def test_default_branch_push_without_available_scope_has_public_error(self):
        head = self.commit("修复：当前提交")
        self.command(self.target, "update-ref", "refs/remotes/origin/main", head)
        event = {"before": "f" * 40, "after": head, "ref": "refs/heads/main",
                 "repository": {"default_branch": "main"}}
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(event))
        result = subprocess.run(
            [sys.executable, str(self.target / policy.CHECKER), "check",
             "--event-file", str(event_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"],
                         ["event_commit_scope_unavailable"])

    def test_first_import_does_not_rescan_history_and_unknown_scope_is_refused(self):
        self.commit("existing imported history")
        head = self.commit("维护：首次接入规则")
        event = {"before": "0" * 40, "after": head, "ref": "refs/heads/main",
                 "repository": {"default_branch": "main"}}
        report = policy.event_checks(self.target, event)
        self.assertFalse(report["errors"])
        self.assertEqual(report["checked_commits"], 1)
        event["ref"] = "refs/heads/unknown-feature"
        with self.assertRaises(policy.PolicyError):
            policy.event_checks(self.target, event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
