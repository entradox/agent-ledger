#!/opt/miniconda3/bin/python3
"""One-time: assign every currently-claimed agent_id (pre-workspace era)
to a single default workspace, so nothing breaks after Tasks 1-8 deploy.
Run with --dry-run first. Idempotent: re-running skips agents that
already have a workspace_id.txt.

STALE AS WRITTEN (D-1162): Google auth was deleted, so `--google-sub` no
longer has a source and "your dashboard session" no longer exists. The
mechanism below is still correct — bind the pre-existing agents to a
workspace so their claims keep resolving — but the identity must now come
from a workspace YOU created at POST /start (or via the x402 mint), passed
in as that workspace's id. Re-point this script before running it; the
migration itself is deferred until the right workspace for the 4
pre-existing agents is decided."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workspace_engine

DATA_DIR = workspace_engine.DATA_DIR


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument(
        "--google-sub",
        help="DEPRECATED (D-1162): Google OAuth was removed, so there is no "
             "'sub' to look up and no login that will ever resolve this "
             "workspace. Kept only because the mechanism is still tested. "
             "Before this script may be run, it must take a workspace_id of "
             "a workspace you created at /start instead.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    agents_dir = DATA_DIR / "agents"
    to_migrate = [
        d for d in agents_dir.glob("*")
        if d.is_dir() and (d / "secret.txt").exists()
        and not (d / "workspace_id.txt").exists()
    ]
    print(f"Found {len(to_migrate)} agent(s) needing migration: {[d.name for d in to_migrate]}")
    if not args.google_sub:
        print(
            "\n!! WARNING: no --google-sub given, and --google-sub is itself\n"
            "!! DEPRECATED (D-1162): Google auth was deleted, so nothing will\n"
            "!! ever resolve this workspace by login again. The workspace this\n"
            "!! run creates is reachable only through the workspace_key printed\n"
            "!! below — save it from this output, because it is shown once.\n"
            "!! Before re-running, re-point this script at an existing\n"
            "!! workspace_id created at POST /start.\n")
    if not to_migrate:
        # create_workspace() is not free: inside the launch window it consumes
        # one of the 50 scarcity slots. Minting a workspace for zero agents
        # burns a slot nobody asked for and cannot be undone.
        print("Nothing to migrate — no workspace created "
              "(minting one would consume a scarcity slot for no agents).")
        return

    if args.dry_run:
        print("--dry-run: no changes made")
        return

    workspace_id, raw_key = workspace_engine.create_workspace(
        owner_email=args.owner_email, google_sub=args.google_sub or None)
    if raw_key:
        print(f"Created default workspace {workspace_id} (key shown once): {raw_key}")
    else:
        # create_workspace returns no key for an identity that already has a
        # workspace — the key was shown once at its creation and only its
        # hash is stored. Correct and non-destructive: nothing is reissued.
        print(f"Reusing existing workspace {workspace_id} for this google_sub "
              "(its workspace_key was shown once at signup and is not "
              "retrievable — this run did not change it)")
    for d in to_migrate:
        (d / "workspace_id.txt").write_text(workspace_id)
        print(f"  migrated {d.name}")


if __name__ == "__main__":
    main()
