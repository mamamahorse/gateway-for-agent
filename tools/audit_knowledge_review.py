"""Read source code without importing the app or opening its databases."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
EVIDENCE_FILES = (
    "web_app.py", "retrieval_review_app.py", "agent_loop.py", "model_client.py",
    "structured_output.py", "knowledge_tools.py", "sources.py", "document_model.py",
    "pdf_parser.py", "pdf_vision.py", "embeddings.py", "embedding_config.py",
    "vector_index.py", "conversation_store.py", "conversation_context.py",
    "conversation_memory.py", "conversation_title.py", "practice_store.py",
    "practice_service.py", "practice_model.py", "library_store.py",
    "learning_store.py", "learning_service.py", "review_scheduler.py",
    "outline_service.py", "outline_store.py", "web/static/app.js",
    "requirements.txt", "requirements-local.txt", "requirements-pdf.txt",
    "requirements-docling.txt", "test_web_app.py", "test_agent_loop.py",
    "test_conversations.py", "test_pdf_documents.py", "test_learning.py",
)


def inspect_project(project: Path) -> dict:
    """Only parse public source/config declarations; never execute project code."""
    routes = []
    request_models = []
    for filename in ("web_app.py", "retrieval_review_app.py"):
        tree = ast.parse((project / filename).read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    target = decorator.func
                    if not (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in {"app", "router"}
                            and target.attr in METHODS):
                        continue
                    path = ast.literal_eval(decorator.args[0])
                    status = next((ast.literal_eval(k.value) for k in decorator.keywords
                                   if k.arg == "status_code"), 200)
                    routes.append({
                        "method": target.attr.upper(), "path": path,
                        "status_code": status, "file": filename,
                        "line": decorator.lineno, "function": node.name,
                        "async": isinstance(node, ast.AsyncFunctionDef),
                        "parameters": ast.unparse(node.args),
                    })
            if isinstance(node, ast.ClassDef) and node.name.endswith("Request"):
                request_models.append({
                    "name": node.name, "file": filename, "line": node.lineno,
                    "bases": [ast.unparse(base) for base in node.bases],
                    "fields": [ast.unparse(field) for field in node.body
                               if isinstance(field, ast.AnnAssign)],
                })

    files = {}
    for filename in EVIDENCE_FILES:
        content = (project / filename).read_bytes()
        files[filename] = {"sha256": hashlib.sha256(content).hexdigest(),
                           "bytes": len(content)}
    test_files = sorted(project.glob("test_*.py"))
    return {
        "source_root": str(project),
        "scope": "Static source audit; no imports, model calls, DB reads or runtime tests.",
        "route_scope": "Explicit decorators only; excludes framework-generated OpenAPI and static mounts.",
        "routes": sorted(routes, key=lambda row: (row["file"], row["line"])),
        "request_models": sorted(request_models, key=lambda row: (row["file"], row["line"])),
        "test_files": [path.name for path in test_files],
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    output = args.output.resolve()
    if output == project or project in output.parents:
        parser.error("Write evidence outside Knowledge Review to keep the source project read-only.")
    report = inspect_project(project)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(report['routes'])} explicit routes, "
          f"{len(report['request_models'])} request models, "
          f"{len(report['test_files'])} test files and {len(report['files'])} file hashes.")


if __name__ == "__main__":
    main()
