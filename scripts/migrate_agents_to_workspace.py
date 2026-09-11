#!/opt/miniconda3/bin/python3
"""One-time: assign every currently-claimed agent_id (pre-workspace era)
to a single default workspace, so nothing breaks after Tasks 1-8 deploy.
Run with --dry-run first. Idempotent: re-running skips agents that
already have a workspace_id.txt."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workspace_engine

DATA_DIR = workspace_engine.DATA_DIR


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    agents_dir = DATA_DIR / "agents"
    to_migrate = [
        d for d in agents_dir.glob("*")
        if d.is_dir() and (d / "secret.txt").exists()
        and not (d / "workspace_id.txt").exists()
    ]
    print(f"Found {len(to_migrate)} agent(s) needing migration: {[d.name for d in to_migrate]}")
    if args.dry_run:
        print("--dry-run: no changes made")
        return

    workspace_id, raw_key = workspace_engine.create_workspace(owner_email=args.owner_email)
    print(f"Created default workspace {workspace_id} (key shown once): {raw_key}")
    for d in to_migrate:
        (d / "workspace_id.txt").write_text(workspace_id)
        print(f"  migrated {d.name}")


if __name__ == "__main__":
    main()
