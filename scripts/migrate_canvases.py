#!/usr/bin/env python
"""Backfill stored canvases to the current schema version.

Reading a workflow already migrates its canvas on the fly, so the app works
without this script.  What the script adds is persistence: it writes the
migrated canvas back and recompiles the IR, so old workflows become executable
and packageable again instead of asking the user to Save & Compile.

    # See what would change, without writing
    python -m scripts.migrate_canvases --dry-run

    # Apply
    python -m scripts.migrate_canvases

    # One workflow only
    python -m scripts.migrate_canvases --workflow-id <uuid>

Safe to re-run: migrations are keyed on the canvas `schema_version` and a
canvas already at the current version is skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.compiler import run_compiler
from app.compiler.canvas_migrations import migrate_canvas, needs_migration
from app.database import AsyncSessionLocal
from app.models.workflow import Workflow, WorkflowVersion
from app.schemas.canvas import CanvasPayload


async def migrate(dry_run: bool, workflow_id: str | None) -> int:
    """Returns a process exit code."""
    migrated = 0
    recompiled = 0
    still_invalid = 0
    skipped = 0
    failed = 0

    async with AsyncSessionLocal() as session:
        query = select(WorkflowVersion).order_by(
            WorkflowVersion.workflow_id, WorkflowVersion.version_number
        )
        if workflow_id:
            query = query.where(WorkflowVersion.workflow_id == workflow_id)

        versions = (await session.execute(query)).scalars().all()
        if not versions:
            print("No workflow versions found.")
            return 0

        names = {
            w.id: w.name
            for w in (await session.execute(select(Workflow))).scalars().all()
        }

        print(f"Inspecting {len(versions)} workflow version(s)\n")

        for version in versions:
            label = (
                f"{names.get(version.workflow_id, version.workflow_id)} "
                f"v{version.version_number}"
            )

            canvas = version.canvas_json
            if not isinstance(canvas, dict) or not needs_migration(canvas):
                skipped += 1
                continue

            try:
                new_canvas, notes = migrate_canvas(canvas)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print(f"  FAILED  {label}: {type(exc).__name__}: {exc}")
                failed += 1
                continue

            print(f"  {label}")
            for note in notes:
                print(f"      - {note}")
            if not notes:
                print("      - schema_version stamped; no node changes needed")
            migrated += 1

            # Recompile so the version is executable and packageable again.
            errors: list[str] = []
            ir_dict = None
            try:
                payload = CanvasPayload.model_validate(new_canvas)
                errors, warnings, ir = run_compiler(payload, version.id)
                ir_dict = ir.to_dict() if ir else None
                for warning in warnings:
                    print(f"      ! warning: {warning}")
            except Exception as exc:  # noqa: BLE001
                errors = [f"{type(exc).__name__}: {exc}"]

            if errors:
                still_invalid += 1
                for error in errors:
                    print(f"      x {error}")
                print("      -> migrated but still invalid; needs manual edits")
            else:
                recompiled += 1
                print("      -> recompiled successfully")

            if not dry_run:
                version.canvas_json = new_canvas
                version.ir_json = ir_dict
                version.validation_errors = errors
                version.is_valid = not errors

        if dry_run:
            print("\nDry run — nothing written.")
        else:
            await session.commit()
            print("\nChanges committed.")

    print(
        f"\nmigrated {migrated} · recompiled {recompiled} · "
        f"still invalid {still_invalid} · already current {skipped} · failed {failed}"
    )
    if failed:
        return 1
    return 0


async def _run(dry_run: bool, workflow_id: str | None) -> int:
    """Wrap migrate() so an unreachable database reads as one clear line."""
    try:
        return await migrate(dry_run, workflow_id)
    except OSError as exc:
        from app.config import get_settings

        print(
            f"Cannot reach the database: {exc}\n"
            f"  DATABASE_URL = {get_settings().database_url}\n"
            "  Start it with:  docker compose up -d postgres",
            file=sys.stderr,
        )
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    parser.add_argument(
        "--workflow-id",
        default=None,
        help="migrate only this workflow's versions",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.dry_run, args.workflow_id))


if __name__ == "__main__":
    sys.exit(main())
