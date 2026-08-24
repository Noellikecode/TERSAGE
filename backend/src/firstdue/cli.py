"""FIRST DUE command line.

Four commands the project promises, plus the two that support them:

* ``firstdue serve``  -- run the API (honours ``PORT``)
* ``firstdue seed``   -- build deterministic demo state
* ``firstdue reset``  -- clear and rebuild it, printing the content hash
* ``firstdue schema`` -- write the OpenAPI document
* ``firstdue status`` -- print the resolved mode and configuration
* ``firstdue slow-loop`` -- run one complete slow-loop pass over a district
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from firstdue import __version__
from firstdue.city.san_francisco import SanFranciscoAdapter
from firstdue.demo.seed import build_seed, load_seed, profiles_from_seed, write_seed
from firstdue.errors import FirstDueError
from firstdue.registry.descriptors import FLEET, FLEET_VERSION
from firstdue.settings import Settings, get_settings


def _epoch(settings: Settings) -> datetime:
    parsed = datetime.fromisoformat(settings.demo_epoch)
    if parsed.tzinfo is None:
        raise FirstDueError("DEMO_EPOCH must be timezone-aware")
    return parsed


def cmd_seed(settings: Settings, *, quiet: bool = False) -> int:
    city = SanFranciscoAdapter(settings.fixtures_dir)
    document = build_seed(
        addresses=list(city.list_addresses()),
        epoch=_epoch(settings),
        seed=settings.demo_seed,
    )
    profiles = profiles_from_seed(document)
    path = write_seed(document, settings.demo_state_dir)
    if not quiet:
        print(f"seeded {len(profiles)} profiles -> {path}")
        print(f"content_hash {document['content_hash']}")
    return 0


def cmd_reset(settings: Settings) -> int:
    state_dir = settings.demo_state_dir
    if state_dir.exists():
        shutil.rmtree(state_dir)
        print(f"cleared {state_dir}")
    return cmd_seed(settings)


def cmd_verify_seed(settings: Settings) -> int:
    """Rebuild the seed and compare it to what is on disk."""
    stored = load_seed(settings.demo_state_dir)
    if stored is None:
        print("no seed on disk; run `firstdue seed` first", file=sys.stderr)
        return 1
    city = SanFranciscoAdapter(settings.fixtures_dir)
    rebuilt = build_seed(
        addresses=list(city.list_addresses()),
        epoch=_epoch(settings),
        seed=settings.demo_seed,
    )
    if rebuilt["content_hash"] != stored["content_hash"]:
        print(
            f"seed is not deterministic: {stored['content_hash']} != {rebuilt['content_hash']}",
            file=sys.stderr,
        )
        return 1
    print(f"seed deterministic: {rebuilt['content_hash']}")
    return 0


def cmd_schema(settings: Settings, out: Path) -> int:
    from firstdue.api.app import get_openapi_schema

    schema = get_openapi_schema()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths = schema.get("paths", {})
    print(f"wrote {out} ({len(paths) if isinstance(paths, dict) else 0} paths)")
    return 0


def cmd_status(settings: Settings) -> int:
    city = SanFranciscoAdapter(settings.fixtures_dir)
    print(f"firstdue {__version__}")
    print(f"  mode          {settings.mode_label}")
    print(f"  storage       {settings.storage_backend}")
    print(f"  events        {settings.event_backend}")
    print(f"  environment   {settings.app_env}")
    print(f"  municipality  {city.municipality_id}")
    print(f"  districts     {', '.join(city.list_districts())}")
    print(f"  sources       {len(city.source_ids())} configured")
    print(f"  agents        {len(FLEET)} published at {FLEET_VERSION}")
    print(f"  port          {settings.port}")
    print(f"  state dir     {settings.demo_state_dir}")
    from firstdue.api.dependencies import Role, console_token
    from firstdue.gateway.engine import POLICY_VERSION

    print(f"  policy        version {POLICY_VERSION}")
    for role in Role:
        role_token = console_token(settings, role)
        if role_token:
            print(f"  {role:<13} {role_token}")
    token = settings.resolved_internal_push_token
    if token:
        # Derived from DEMO_SEED, held in no file, and required by the internal
        # push endpoint. Printed here so the demo can exercise that endpoint
        # without a secret being committed anywhere.
        print(f"  push token    {token}  (fake mode; derived from DEMO_SEED)")
    return 0


def cmd_slow_loop(settings: Settings, *, approve: bool, district: str | None) -> int:
    """Run one complete slow-loop pass and print what it did."""
    import asyncio

    from firstdue.container import build_container
    from firstdue.demo.scenario import run_slow_loop

    container = build_container(settings)
    report = asyncio.run(run_slow_loop(container, district_id=district, approve=approve))

    print(f"slow loop - {report.district_id}  (mode: {settings.mode_label})")
    print(
        f"  facts written     {report.facts_written} (re-derived, not rewritten: "
        f"{report.facts_deduped})"
    )
    print(f"  conflicts found   {len(report.conflicts)}")
    for conflict_id in report.conflicts:
        print(f"                    {conflict_id}")
    if report.screen_findings:
        print(
            f"  screened          {', '.join(report.screen_findings)} "
            "(injection patterns removed from ingested documents)"
        )
    if report.unavailable_sources:
        print(
            f"  UNAVAILABLE       {', '.join(report.unavailable_sources)} "
            "(rendered as unavailable, never as absent)"
        )

    print(f"  survey queue      {report.queue_size} structures ranked")
    if report.top_address_id:
        print(f"  top of queue      {report.top_address_id}  score {report.top_score:.3f}")
        for reason in report.top_reasons:
            print(f"                    - {reason}")

    dispatch = report.dispatch
    if dispatch is not None:
        print("  autonomous actions")
        print(f"                    work order      {dispatch.work_order_ref}")
        print(f"                    calendar event  {dispatch.calendar_event_ref}")
        print(f"                    crew notified   {dispatch.notification_ref}")
        print(f"                    pre-plan        {dispatch.plan_uri}")
        if dispatch.referral_id:
            print(
                f"  referral          {dispatch.referral_id} AWAITING APPROVAL "
                "(a captain files this, not an agent)"
            )

    approval = report.approval
    if approval is not None:
        replay = " (replayed; no second case opened)" if approval.replayed else ""
        print(f"  approved by human case {approval.case_number}{replay}")
    return 0


def cmd_serve(settings: Settings, *, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "firstdue.api.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - containers bind all interfaces
        port=settings.port,
        reload=reload,
        log_config=None,
        access_log=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firstdue", description="TERSAGE control surface")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--reload", action="store_true", help="reload on source change")

    sub.add_parser("seed", help="build deterministic demo state")
    sub.add_parser("reset", help="clear and rebuild demo state")
    sub.add_parser("verify-seed", help="check the seed is byte-identical on rebuild")
    sub.add_parser("status", help="print resolved configuration")

    slow_loop = sub.add_parser("slow-loop", help="run one slow-loop pass over a district")
    slow_loop.add_argument("--district", default=None, help="district id (default: configured)")
    slow_loop.add_argument(
        "--no-approve",
        action="store_true",
        help="leave the referral waiting for a human instead of approving it",
    )

    schema = sub.add_parser("schema", help="write the OpenAPI document")
    schema.add_argument("--out", type=Path, default=Path("docs/openapi.json"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    try:
        match args.command:
            case "serve":
                return cmd_serve(settings, reload=args.reload)
            case "seed":
                return cmd_seed(settings)
            case "reset":
                return cmd_reset(settings)
            case "verify-seed":
                return cmd_verify_seed(settings)
            case "schema":
                return cmd_schema(settings, args.out)
            case "status":
                return cmd_status(settings)
            case "slow-loop":
                return cmd_slow_loop(settings, approve=not args.no_approve, district=args.district)
            case _:  # pragma: no cover - argparse enforces the set
                return 2
    except FirstDueError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
