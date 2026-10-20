"""Validate/apply the small versioned SSE integration to an independent checkout."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(project, bundle):
    manifest = json.loads((bundle / "changes.json").read_text(encoding="utf-8"))
    pending = []
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid integration path")
        target = project / relative
        if target.is_symlink() or not target.resolve().is_relative_to(project):
            raise ValueError("Integration target escapes the checkout")
        existing = target.read_bytes() if target.exists() else None
        if existing is not None and digest(existing) == item["after_sha256"]:
            continue
        existing_hash = digest(existing) if existing is not None else None
        edits = item.get("edits", [])
        if existing_hash != item["before_sha256"]:
            upgrade = next((version for version in item.get("upgrades", [])
                            if version["before_sha256"] == existing_hash), None)
            if upgrade is None:
                raise ValueError(f"{relative}: checkout changed; review the patch instead of overwriting it")
            edits = upgrade.get("edits", [])
        if "asset" in item:
            updated = (bundle / item["asset"]).read_bytes()
        else:
            lines = existing.decode("utf-8").splitlines(keepends=True)
            for edit in reversed(edits):
                if "".join(lines[edit["start"]:edit["stop"]]) != edit["old"]:
                    raise ValueError(f"{relative}: edit context mismatch")
                lines[edit["start"]:edit["stop"]] = edit["new"].splitlines(keepends=True)
            updated = "".join(lines).encode("utf-8")
        if digest(updated) != item["after_sha256"]:
            raise ValueError(f"{relative}: output checksum mismatch")
        pending.append((target, updated))
    return pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write only after every source checksum validates")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pending = prepare(args.project.resolve(strict=True), root / "integrations/knowledge-review")
    for target, updated in pending:
        print(("APPLY " if args.apply else "CHECK ") + str(target))
    if args.apply:
        for target, updated in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(updated)
    print(f"{len(pending)} file(s) {'applied' if args.apply else 'ready; use --apply to write'}")
