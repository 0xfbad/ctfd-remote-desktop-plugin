from __future__ import annotations

import time
import random
import logging

from sqlalchemy.exc import IntegrityError
from CTFd.models import db

from .models import DesktopNetworkSlotModel, get_setting, network_slot_name
from .event_logger import event_logger
from .exceptions import NetworkPoolExhaustedException

logger = logging.getLogger(__name__)

# user-facing: the background greenlet surfaces str(exception) into creation_status
POOL_EXHAUSTED_MESSAGE = "All servers are at capacity, please try again shortly."

CLAIM_ATTEMPTS = 5

# invariant: a slot is released ONLY when its container is confirmed stopped or
# confirmed absent (strict listing). releasing on an ambiguous signal (stop
# raised, lenient listing, is_container_running False) can double-tenant a
# network. note the residual benign race: after a successful stop, auto_remove's
# endpoint detach is async, so a fresh claim of the same slot can momentarily
# coexist with the dead container's endpoint - no processes, IPAM prevents IP
# reuse, and random slot choice widens the gap.


def claim_network_slot(context_name: str, container_name: str, user_id: int) -> str:
    pool_size = int(get_setting("network_pool_size") or 0)

    for _ in range(CLAIM_ATTEMPTS):
        claimed = {r.slot_index for r in DesktopNetworkSlotModel.query.filter_by(docker_context=context_name).all()}
        free = sorted(set(range(pool_size)) - claimed)
        if not free:
            event_logger.log_event(
                "network_pool_exhausted",
                f"network pool exhausted on {context_name}",
                level="warning",
                metadata={"context": context_name, "pool_size": pool_size},
            )
            raise NetworkPoolExhaustedException(POOL_EXHAUSTED_MESSAGE)

        slot = random.choice(free)
        row = DesktopNetworkSlotModel(
            docker_context=context_name,
            slot_index=slot,
            network_name=network_slot_name(slot),
            container_name=container_name,
            user_id=user_id,
            claimed_at=time.time(),
        )
        try:
            db.session.add(row)
            db.session.commit()
            return network_slot_name(slot)
        except IntegrityError:
            # lost the insert race to another worker; re-read and retry
            db.session.rollback()
            continue

    raise NetworkPoolExhaustedException(POOL_EXHAUSTED_MESSAGE)


def release_network_slot(context_name: str, container_name: str) -> None:
    # missing row is a silent no-op. never gated on the network_isolation
    # setting: toggling isolation off must not strand slots of draining
    # sessions. must never break teardown, hence the broad guard
    try:
        DesktopNetworkSlotModel.query.filter_by(
            docker_context=context_name, container_name=container_name
        ).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.error(f"release_network_slot failed for {container_name} on {context_name}", exc_info=True)


def reap_stale_slots(
    context_name: str,
    live_container_names: set[str],
    db_container_names: set[str],
    now: float,
    safety_age_seconds: float = 300,
) -> int:
    # caller contract: only invoked when the STRICT container listing for this
    # context succeeded - a lenient/empty-on-error listing would mass-free
    # claimed slots on a flapping host and double-tenant networks
    freed = 0
    rows = DesktopNetworkSlotModel.query.filter_by(docker_context=context_name).all()
    for row in rows:
        if now - row.claimed_at <= safety_age_seconds:
            continue
        if row.container_name in live_container_names or row.container_name in db_container_names:
            continue
        event_logger.log_event(
            "network_slot_reclaimed",
            f"reclaimed network slot {row.network_name} on {context_name}",
            level="warning",
            metadata={
                "context": context_name,
                "network_name": row.network_name,
                "container_name": row.container_name,
                "age_seconds": int(now - row.claimed_at),
            },
        )
        db.session.delete(row)
        freed += 1
    if freed:
        db.session.commit()
    return freed


def reap_deleted_context_slots(known_context_names: set[str], now: float, safety_age_seconds: float = 300) -> int:
    # frees slot rows stranded when an admin deletes a context outright
    freed = 0
    for row in DesktopNetworkSlotModel.query.all():
        if row.docker_context in known_context_names:
            continue
        if now - row.claimed_at <= safety_age_seconds:
            continue
        db.session.delete(row)
        freed += 1
    if freed:
        db.session.commit()
    return freed
