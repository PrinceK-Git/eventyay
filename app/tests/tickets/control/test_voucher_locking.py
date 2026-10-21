from contextlib import contextmanager
from datetime import timedelta
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils.timezone import now
from redis.exceptions import LockNotOwnedError, RedisError

from eventyay.base.models import EventLock
from eventyay.base.services import voucher_locking
from eventyay.base.services.voucher_locking import VoucherLockGuard, VoucherLockLost


@pytest.fixture
def redis_guard(settings, monkeypatch):
    settings.HAS_REDIS = True
    original = SimpleNamespace(redis=object(), name='test-voucher-lock', local=SimpleNamespace(token=b'owner'))
    renewal = Mock(local=SimpleNamespace(token=None))
    renewal.extend.return_value = True
    constructor = Mock(return_value=renewal)
    monkeypatch.setattr(voucher_locking, 'Lock', constructor)
    guard = VoucherLockGuard(SimpleNamespace(_lock=original))
    constructor.assert_called_once_with(
        redis=original.redis, name=original.name, timeout=voucher_locking.LOCK_TIMEOUT, thread_local=False
    )
    assert renewal.local.token == b'owner'
    return guard, renewal


@pytest.mark.parametrize('failure', [LockNotOwnedError('Expired'), RedisError('Disconnected')])
def test_redis_lock_failure_is_permanent(redis_guard, failure):
    guard, renewal = redis_guard
    renewal.extend.side_effect = failure
    with pytest.raises(VoucherLockLost):
        guard.check()
    renewal.extend.side_effect = None
    with pytest.raises(VoucherLockLost):
        guard.check()
    assert renewal.extend.call_count == 1


def test_background_renewal_runs_without_foreground_progress(redis_guard, monkeypatch):
    guard, renewal = redis_guard
    renewed = ThreadEvent()
    monkeypatch.setattr(voucher_locking, 'LOCK_TIMEOUT', 0.06)

    def extend(*args, **kwargs):
        if renewal.extend.call_count > 1:
            renewed.set()
        return True

    renewal.extend.side_effect = extend
    guard.start()
    try:
        assert renewed.wait(2), 'Lease was not renewed while foreground work was blocked'
        assert guard.thread.is_alive()
    finally:
        guard.stop()
    assert not guard.thread.is_alive()


def test_background_failure_is_seen_by_foreground(redis_guard, monkeypatch):
    guard, renewal = redis_guard
    monkeypatch.setattr(voucher_locking, 'LOCK_TIMEOUT', 0.06)
    renewal.extend.side_effect = [True, LockNotOwnedError('New owner')]
    guard.start()
    try:
        assert guard.lost.wait(2)
        with pytest.raises(VoucherLockLost):
            guard.check()
    finally:
        guard.stop()


def test_renewal_stays_running_through_commit(redis_guard, monkeypatch):
    guard, renewal = redis_guard
    release = Mock()

    @contextmanager
    def commit_transaction(**kwargs):
        assert kwargs == {'durable': True}
        yield
        assert guard.thread.is_alive(), 'Renewal stopped before the database commit'

    monkeypatch.setattr(voucher_locking, 'lock_event', Mock())
    monkeypatch.setattr(voucher_locking, 'release_event', release)
    monkeypatch.setattr(voucher_locking, 'VoucherLockGuard', lambda event: guard)
    monkeypatch.setattr(voucher_locking.transaction, 'atomic', commit_transaction)
    with voucher_locking.voucher_transaction(guard.event):
        assert guard.thread.is_alive()
    assert not guard.thread.is_alive()
    release.assert_called_once_with(guard.event)


@pytest.mark.django_db
@pytest.mark.parametrize('invalid', ['expired', 'replaced', 'missing'])
def test_database_lock_must_still_belong_to_batch(settings, invalid):
    settings.HAS_REDIS = False
    lock = EventLock.objects.create(event='voucher-lock-test')
    event = SimpleNamespace(pk=lock.event, _lock=lock)
    if invalid == 'expired':
        EventLock.objects.filter(pk=lock.pk).update(date=now() - timedelta(seconds=121))
    elif invalid == 'replaced':
        EventLock.objects.filter(pk=lock.pk).update(token='00000000-0000-0000-0000-000000000000')
    else:
        lock.delete()
    with pytest.raises(VoucherLockLost):
        VoucherLockGuard(event).start()
