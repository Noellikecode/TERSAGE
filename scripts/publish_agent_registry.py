"""Publish the fleet into Google Cloud Agent Registry.

    .venv/bin/python scripts/publish_agent_registry.py --project firstdue-dev

Idempotent: a service id is derived from the agent id, so republishing patches
the same entry rather than adding one per run. Needs Application Default
Credentials and `agentregistry.googleapis.com` enabled.

The local catalog stays the source of truth -- Terraform derives the topics,
service accounts and workers from it. This is the discovery surface, so an
operator browsing Google Cloud sees the fleet without reading the repository.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from firstdue.registry.publish import MAX_CARD_BYTES, publishable, service_body, service_id_for

API = "https://agentregistry.googleapis.com/v1"


def token() -> str:
    """An access token from the ambient gcloud credentials."""
    out = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def call(method: str, url: str, bearer: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {bearer}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def await_operation(name: str, bearer: str, *, tries: int = 30) -> tuple[bool, Any]:
    """Wait for a create or patch to finish.

    Registry writes are long-running operations: the POST returns 200 with an
    operation that is not done, and validation happens *inside* it. Reading the
    200 as success reported nine agents published when none were -- the
    operations failed a second later on a card-shape rejection nobody saw.
    """
    for _ in range(tries):
        status, payload = call("GET", f"{API}/{name}", bearer)
        if status == 200 and payload.get("done"):
            if payload.get("error"):
                return False, payload["error"].get("message", payload["error"])
            return True, payload.get("response", {})
        time.sleep(1)
    return False, "operation did not finish"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="firstdue-dev")
    parser.add_argument("--location", default="us-central1")
    parser.add_argument(
        "--base-url",
        default="https://firstdue-incident-kaw7xwxu7a-uc.a.run.app",
        help="the deployment the catalog points at",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    parent = f"projects/{args.project}/locations/{args.location}"
    agents = publishable()
    print(f"{len(agents)} scheduled agents -> {parent}\n")

    if args.dry_run:
        for descriptor in agents:
            body = service_body(descriptor, base_url=args.base_url)
            size = len(json.dumps(body["agentSpec"]["content"]))
            print(f"  {descriptor.agent_id:<24} card {size:>4}B  {descriptor.publisher_department}")
        return 0

    bearer = token()
    published = updated = failed = 0

    for descriptor in agents:
        service_id = service_id_for(descriptor)
        body = service_body(descriptor, base_url=args.base_url)
        card_size = len(json.dumps(body["agentSpec"]["content"]))
        if card_size > MAX_CARD_BYTES:
            print(f"  {descriptor.agent_id:<24} SKIPPED: card is {card_size}B")
            failed += 1
            continue

        status, payload = call(
            "POST", f"{API}/{parent}/services?serviceId={service_id}", bearer, body
        )
        if status == 200:
            ok, detail = await_operation(payload["name"], bearer)
            if ok:
                published += 1
                print(f"  {descriptor.agent_id:<24} published")
                continue
            failed += 1
            print(f"  {descriptor.agent_id:<24} FAILED: {str(detail)[:130]}")
            continue
        if status == 409:
            # Already there: patch it, so a version bump lands rather than
            # leaving the catalog describing the previous release.
            status, payload = call("PATCH", f"{API}/{parent}/services/{service_id}", bearer, body)
            if status == 200:
                ok, detail = await_operation(payload["name"], bearer)
                if ok:
                    updated += 1
                    print(f"  {descriptor.agent_id:<24} updated")
                    continue
                failed += 1
                print(f"  {descriptor.agent_id:<24} FAILED: {str(detail)[:130]}")
                continue
        failed += 1
        message = payload.get("error", {}).get("message", payload)
        print(f"  {descriptor.agent_id:<24} FAILED {status}: {str(message)[:120]}")

    print(f"\npublished {published}, updated {updated}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
