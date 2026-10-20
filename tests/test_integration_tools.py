"""Protect independent checkouts and keep private state outside image contexts."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


apply_tool = load_tool("apply_knowledge_review")
context_tool = load_tool("prepare_agent_context")


class IntegrationToolsTests(unittest.TestCase):
    def test_known_upgrade_and_fresh_install_reach_same_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle, project = root / 'bundle', root / 'project'
            bundle.mkdir()
            project.mkdir()
            digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
            manifest = {'files': [{'path': 'web_app.py', 'before_sha256': digest('base\n'),
                'after_sha256': digest('final\n'),
                'edits': [{'start': 0, 'stop': 1, 'old': 'base\n', 'new': 'final\n'}],
                'upgrades': [{'before_sha256': digest('sse\n'),
                             'edits': [{'start': 0, 'stop': 1, 'old': 'sse\n', 'new': 'final\n'}]}]}]}
            (bundle / 'changes.json').write_text(json.dumps(manifest), encoding='utf-8')
            for before in ('base\n', 'sse\n'):
                (project / 'web_app.py').write_text(before, encoding='utf-8')
                pending = apply_tool.prepare(project, bundle)
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0][1], b'final\n')
                pending[0][0].write_bytes(pending[0][1])
                self.assertEqual(apply_tool.prepare(project, bundle), [])

    def test_unknown_upgrade_aborts_before_writing_any_pending_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle, project = root / 'bundle', root / 'project'
            bundle.mkdir()
            project.mkdir()
            digest = lambda value: hashlib.sha256(value).hexdigest()
            (bundle / 'asset.py').write_bytes(b'final')
            (project / 'newer.py').write_bytes(b'independent change')
            manifest = {'files': [
                {'path': 'first.py', 'asset': 'asset.py', 'before_sha256': None, 'after_sha256': digest(b'final')},
                {'path': 'newer.py', 'asset': 'asset.py', 'before_sha256': digest(b'base'),
                 'after_sha256': digest(b'final'), 'upgrades': [{'before_sha256': digest(b'sse')}]}]}
            (bundle / 'changes.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'checkout changed'):
                apply_tool.prepare(project, bundle)
            self.assertFalse((project / 'first.py').exists())
            self.assertEqual((project / 'newer.py').read_bytes(), b'independent change')

    def test_context_excludes_credentials_business_data_models_and_tests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            for name in ["web_app.py", "agent_stream.py", "test_private.py", "quality_sample.py", "requirements.txt",
                         "requirements-local.txt", "requirements-pdf.txt", ".env"]:
                (project / name).write_text("public placeholder", encoding="utf-8")
            for name in ["web", "examples", ".data", ".models"]:
                (project / name).mkdir()
            (project / "web/index.html").write_text("public", encoding="utf-8")
            (project / "examples/public.md").write_text("public", encoding="utf-8")
            (project / ".data/private.sqlite3").write_text("do not copy", encoding="utf-8")
            (project / ".models/private.bin").write_text("do not copy", encoding="utf-8")
            output = root / "context"
            context_tool.prepare(project, output)
            names = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
            self.assertIn("web_app.py", names)
            self.assertIn("web/index.html", names)
            self.assertFalse({".env", ".data/private.sqlite3", ".models/private.bin", "test_private.py", "quality_sample.py"} & names)
            with self.assertRaises(ValueError):
                context_tool.prepare(project, output)

    def test_changed_checkout_is_rejected_without_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "web_app.py").write_text("a newer independent version", encoding="utf-8")
            before = (project / "web_app.py").read_bytes()
            with self.assertRaisesRegex(ValueError, "checkout changed"):
                apply_tool.prepare(project, ROOT / "integrations/knowledge-review")
            self.assertEqual((project / "web_app.py").read_bytes(), before)
            self.assertEqual(sorted(p.name for p in project.iterdir()), ["web_app.py"])


if __name__ == "__main__":
    unittest.main()
