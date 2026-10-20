"""Export runtime code/public assets, never .env, databases or model caches."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def prepare(project, output):
    if output.exists():
        raise ValueError("Output already exists; choose a fresh build-context directory")
    runtime = [p for p in project.glob("*.py") if not p.name.startswith(("test_", "quality_"))]
    assets = [p for p in (project / "web").rglob("*") if p.is_file()]
    assets += list((project / "examples").glob("*.md"))
    requirements = [project / name for name in ["requirements.txt", "requirements-local.txt", "requirements-pdf.txt"]]
    files = runtime + assets + requirements
    if not (project / "agent_stream.py").is_file():
        raise ValueError("Apply the SSE integration before preparing the image context")
    for path in files:
        if not path.is_file():
            raise ValueError(f"Required runtime file missing: {path.name}")
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise ValueError("Symlinks outside the source tree cannot enter the image")
    output.mkdir(parents=True)
    manifest = {}
    for path in files:
        relative = path.relative_to(project)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    deploy = Path(__file__).resolve().parents[1] / "deploy"
    shutil.copyfile(deploy / "Dockerfile.agent", output / "Dockerfile")
    (output / ".dockerignore").write_text(".env\n.data/\n.models/\n.venv/\n__pycache__/\n", encoding="utf-8")
    (output / "context-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("build/agent-context"))
    args = parser.parse_args()
    print(f"Exported {prepare(args.project.resolve(strict=True), args.output.resolve())} public runtime files")
