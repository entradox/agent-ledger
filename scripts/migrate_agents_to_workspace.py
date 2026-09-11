#!/opt/miniconda3/bin/python3
"""One-time: assign every currently-claimed agent_id (pre-workspace era)
to a single default workspace, so nothing breaks after Tasks 1-8 deploy.
Run with --dry-run first. Idempotent: re-running skips agents that
already have a workspace_id.txt.

Pass --google-sub so the migrated workspace is the SAME one your Google
login resolves to — without it the script mints a workspace keyed to
nothing, which your dashboard session can never reach."""
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
        help="Your Google 'sub' claim — the stable subject id the OAuth "
             "callback keys workspaces by. Get it by logging in once at "
             "/login: the callback resolves your sub and creates a "
             "workspace under it, so the value is visible in that "
             "workspace's record (workspaces/<id>.json, field google_sub) "
             "on the data volume. Passing it here makes the MIGRATED "
             "workspace the same one your real login resolves to. Omit it "
             "and the migrated workspace is unreachable from the dashboard.")
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
            "\n!! WARNING: no --google-sub given.\n"
            "!! The workspace this creates is keyed to nothing, so logging in\n"
            "!! at /login will resolve to a DIFFERENT workspace and the\n"
            "!! migrated agents will not appear on your dashboard.\n"
            "!! Log in once, read your google_sub out of the workspace record\n"
            "!! it creates, and re-run with --google-sub <sub>.\n")
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
