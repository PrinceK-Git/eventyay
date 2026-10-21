"""Keep voucher batches protected until their database transaction finishes."""

import logging
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread

from django.conf import settings
from django.db import transaction
from django.utils.timezone import now
from redis.exceptions import RedisError
from redis.lock import Lock

from eventyay.base.models import EventLock
from eventyay.base.services.locking import (
    LOCK_TIMEOUT,
    LockReleaseException,
    LockTimeoutException,
    lock_event,
    release_event,
)


logger = logging.getLogger(__name__)


class VoucherLockLost(Exception):
    """Abort the entire batch when exclusive access can no longer be established."""


class VoucherLockGuard:
    """Renew Redis leases in the background, including while SQL commits.

    Database locks are instead pinned by a row lock for the whole transaction.
    Redis renewal never recreates a missing lock or takes another owner's token.
    """

    def __init__(self, event):
        self.event = event
        self.stopped = Event()
        self.lost = Event()
        self.thread = None
        self.redis_lock = None
        # Redis locks normally use thread-local tokens; capture the owner's token
        # before starting the worker rather than reading its empty thread-local state.
        if settings.HAS_REDIS:
            self.redis_lock = Lock(
                redis=event._lock.redis, name=event._lock.name, timeout=LOCK_TIMEOUT, thread_local=False
            )
            self.redis_lock.local.token = event._lock.local.token

    def check(self):
        """Renew only our live lease; once lost, this operation may never resume."""
        if self.lost.is_set():
            raise VoucherLockLost()
        if self.redis_lock:
            try:
                renewed = self.redis_lock.extend(LOCK_TIMEOUT, replace_ttl=True)
            except RedisError as exc:
                self.lost.set()
                raise VoucherLockLost() from exc
            if not renewed:
                self.lost.set()
                raise VoucherLockLost()

    def renew(self):
        while not self.stopped.wait(LOCK_TIMEOUT / 3):
            try:
                self.check()
            except VoucherLockLost:
                return

    def start(self):
        if self.redis_lock:
            self.check()
            self.thread = Thread(target=self.renew, daemon=True)
            self.thread.start()
        else:
            lock = (
                EventLock.objects.select_for_update().filter(event=self.event.pk, token=self.event._lock.token).first()
            )
            if lock is None or lock.date < now() - timedelta(seconds=LOCK_TIMEOUT):
                raise VoucherLockLost()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join()


@contextmanager
def voucher_transaction(event):
    """Roll back vouchers, related rows and logs together if ownership is lost.

    Keep renewal alive through transaction exit, not merely until the last save.
    Callers also check between vouchers to stop a failed batch promptly.
    """
    lock_event(event)
    guard = VoucherLockGuard(event)
    try:
        with transaction.atomic(durable=True):
            guard.start()
            yield guard
            guard.check()
    finally:
        guard.stop()
        try:
            release_event(event)
        except (LockReleaseException, LockTimeoutException):
            # A lost lease must never release the replacement owner's lock, or
            # mask the exception that caused the transaction to roll back.
            logger.warning('Voucher event lock could not be released for event %s', event.pk)
        finally:
            event._lock = None
