import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scope

from eventyay.base.models import (
    CartPosition,
    Event,
    LogEntry,
    Order,
    OrderPosition,
    Organizer,
    Product,
    ProductVariation,
    Quota,
    SubEvent,
    Team,
    User,
    Voucher,
    WaitingListEntry,
)
from eventyay.control.views.voucher_bulk import VoucherQuotaBudget


pytestmark = pytest.mark.django_db


@pytest.fixture
def env(client, settings):
    settings.DEBUG = True
    organizer = Organizer.objects.create(name='Organizer', slug='organizer')
    event = Event.objects.create(organizer=organizer, name='Event', slug='event', date_from=now())
    user = User.objects.create_user('organizer@example.org', 'test')
    team = Team.objects.create(organizer=organizer, can_view_vouchers=True, can_change_vouchers=True)
    team.members.add(user)
    team.limit_events.add(event)
    with scope(organizer=organizer, event=event):
        product = Product.objects.create(event=event, name='Ticket', default_price=Decimal('30.00'))
        quota = Quota.objects.create(event=event, name='Tickets', size=10)
        quota.products.add(product)
    client.force_login(user)
    return SimpleNamespace(organizer=organizer, event=event, user=user, team=team, product=product, quota=quota)


def voucher(env, **kwargs):
    return Voucher.objects.create(event=env.event, tag='Group', **kwargs)


def url(env, action, obj=None):
    kwargs = {'organizer': env.organizer.slug, 'event': env.event.slug}
    if obj:
        kwargs['voucher'] = obj.pk
    return reverse(f'control:event.{action}', kwargs=kwargs)


def selection(response):
    assert response.status_code == 200
    return BeautifulSoup(response.content, 'lxml').select_one('input[name="selection"]')['value']


def preview(client, env, obj, changes):
    target = url(env, 'voucher.group.edit', obj)
    data = {
        'selection': selection(client.get(target)),
        'action': 'edit_preview',
        'fields_to_change': list(changes),
        **changes,
    }
    response = client.post(target, data)
    assert response.status_code == 200
    return target, data, response


def confirm(client, target, data, response):
    return client.post(target, {**data, 'action': 'edit_confirm', 'preview': response.context['preview']})


def test_group_edit_preserves_individual_settings_and_logs(client, env):
    first = voucher(env, max_usages=4, redeemed=1, product=env.product, comment='First')
    second = voucher(env, max_usages=7, comment='Second', valid_until=now() + timedelta(days=10))
    original_expiry = second.valid_until
    codes = (first.code, second.code)
    target, data, response = preview(client, env, first, {'price_mode': 'percent', 'value': '20'})
    assert response.context['updated_count'] == 2
    first.refresh_from_db()
    assert first.price_mode == 'none'
    assert not LogEntry.objects.filter(action_type='eventyay.voucher.changed').exists()
    assert confirm(client, target, data, response).status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.code, second.code) == codes
    assert first.product == env.product
    assert first.max_usages == 4 and first.redeemed == 1 and first.comment == 'First'
    assert second.max_usages == 7 and second.comment == 'Second' and second.valid_until == original_expiry
    assert first.value == second.value == Decimal('20')
    logs = LogEntry.objects.filter(action_type='eventyay.voucher.changed')
    assert logs.count() == 2
    assert all(json.loads(log.data) == {'price_mode': 'percent', 'value': '20'} for log in logs)


def test_skips_fully_used_and_usage_below_redemptions(client, env):
    first = voucher(env, max_usages=5, redeemed=2)
    second = voucher(env, max_usages=5, redeemed=3)
    full = voucher(env, max_usages=1, redeemed=1)
    target, data, response = preview(client, env, first, {'max_usages': '2', 'comment': 'Updated'})
    assert response.context['updated_count'] == 1
    assert response.context['skipped_count'] == 2
    assert confirm(client, target, data, response).status_code == 302
    for obj in (first, second, full):
        obj.refresh_from_db()
    assert first.max_usages == 2 and first.comment == 'Updated'
    assert second.max_usages == 5 and second.comment == ''
    assert full.comment == ''


@pytest.mark.parametrize(
    'field,initial,value,expected',
    [
        ('comment', 'Old', '', ''),
        ('valid_until', now() + timedelta(days=1), '', None),
        ('budget', Decimal('50'), '', None),
        ('allow_ignore_quota', True, '', False),
        ('show_hidden_products', True, '', False),
        ('all_addons_included', True, '', False),
    ],
)
def test_can_clear_fields(client, env, field, initial, value, expected):
    obj = voucher(env, **{field: initial})
    changes = {field: value}
    target, data, response = preview(client, env, obj, changes)
    assert response.context['updated_count'] == 1, response.context['skipped']
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    assert getattr(obj, field) == expected


@pytest.mark.parametrize(
    'changes',
    [
        {'max_usages': '-1'},
        {'max_usages': 'abc'},
        {'value': '-1'},
        {'budget': '-1'},
        {'price_mode': 'invalid'},
        {'code': 'NEWCODE'},
        {'redeemed': '0'},
    ],
)
def test_invalid_input_does_not_write(client, env, changes):
    obj = voucher(env)
    target, data, response = preview(client, env, obj, changes)
    assert response.context['form'].errors
    assert not LogEntry.objects.filter(action_type='eventyay.voucher.changed').exists()


@pytest.mark.parametrize(
    'changes',
    [
        {'price_mode': 'percent', 'value': '101'},
        {'budget': '10', 'all_addons_included': 'on'},
        {'productvar': ['all'], 'block_quota': 'on'},
    ],
)
def test_cross_field_validation_skips_invalid_vouchers(client, env, changes):
    obj = voucher(env)
    target, data, response = preview(client, env, obj, changes)
    assert response.context['updated_count'] == 0
    assert response.context['skipped_count'] == 1
    assert not LogEntry.objects.filter(action_type='eventyay.voucher.changed').exists()


def test_no_fields_selected_and_noop(client, env):
    obj = voucher(env, comment='Same')
    target, data, response = preview(client, env, obj, {})
    assert 'fields_to_change' in response.context['form'].errors
    target, data, response = preview(client, env, obj, {'comment': 'Same'})
    assert response.context['updated_count'] == 0
    assert response.context['skipped_count'] == 1


def test_codes_and_unselected_fields_cannot_be_overwritten(client, env):
    obj = voucher(env, max_usages=5)
    original_code = obj.code
    target, data, response = preview(client, env, obj, {'comment': 'New'})
    data.update(code='HACKED', max_usages='100', redeemed='100')
    response = client.post(target, data)
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    assert obj.code == original_code and obj.max_usages == 5 and obj.redeemed == 0


def test_requires_preview_and_rechecks_changed_input(client, env):
    obj = voucher(env)
    target = url(env, 'voucher.group.edit', obj)
    data = {'selection': selection(client.get(target)), 'fields_to_change': ['comment'], 'comment': 'New'}
    response = client.post(target, {**data, 'action': 'edit_confirm'})
    obj.refresh_from_db()
    assert obj.comment == ''
    changed = {**data, 'comment': 'Different'}
    response = confirm(client, target, changed, response)
    assert response.status_code == 200
    obj.refresh_from_db()
    assert obj.comment == ''
    assert confirm(client, target, changed, response).status_code == 302
    obj.refresh_from_db()
    assert obj.comment == 'Different'


def test_redemption_after_preview_requires_updated_confirmation(client, env):
    obj = voucher(env)
    other = voucher(env)
    target, data, response = preview(client, env, obj, {'comment': 'New'})
    obj.redeemed = 1
    obj.save()
    response = confirm(client, target, data, response)
    assert response.status_code == 200
    assert response.context['updated_count'] == 1 and response.context['skipped_count'] == 1
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    other.refresh_from_db()
    assert obj.comment == '' and other.comment == 'New'


def test_membership_frozen_at_selection_and_unrelated_events_untouched(client, env):
    obj = voucher(env)
    foreign_event = Event.objects.create(organizer=env.organizer, slug='other', name='Other', date_from=now())
    foreign = Voucher.objects.create(event=foreign_event, tag='Group')
    target, data, response = preview(client, env, obj, {'comment': 'New'})
    added_later = voucher(env)
    assert confirm(client, target, data, response).status_code == 302
    for untouched in (foreign, added_later):
        untouched.refresh_from_db()
        assert untouched.comment == ''


def test_edit_selected_expands_groups_without_duplicates(client, env):
    obj = voucher(env)
    other = voucher(env)
    single = Voucher.objects.create(event=env.event)
    unselected = Voucher.objects.create(event=env.event)
    target = url(env, 'vouchers.bulkaction')
    response = client.post(target, {'action': 'edit', 'voucher_group': [obj.pk], 'voucher': [obj.pk, single.pk]})
    data = {
        'selection': selection(response),
        'action': 'edit_preview',
        'fields_to_change': ['comment'],
        'comment': 'Selected',
    }
    response = client.post(target, data)
    assert response.context['updated_count'] == 3
    assert confirm(client, target, data, response).status_code == 302
    for selected in (obj, other, single):
        selected.refresh_from_db()
        assert selected.comment == 'Selected'
    unselected.refresh_from_db()
    assert unselected.comment == ''


@pytest.mark.parametrize('action', ['voucher.group.edit', 'voucher.group.delete'])
def test_permission_required(client, env, action):
    obj = voucher(env)
    env.team.can_change_vouchers = False
    env.team.save()
    target = url(env, action, obj)
    assert client.get(target).status_code == 403
    assert client.post(target, {}).status_code == 403


@pytest.mark.parametrize('action', ['voucher.group.edit', 'voucher.group.delete'])
def test_invalid_selection_rejected(client, env, action):
    obj = voucher(env)
    assert client.post(url(env, action, obj), {'selection': 'tampered'}).status_code == 400


def test_cross_event_targets_and_products_rejected(client, env):
    other = Event.objects.create(organizer=env.organizer, name='Other', slug='other', date_from=now())
    foreign = Voucher.objects.create(event=other, tag='Group')
    product = Product.objects.create(event=other, name='Other ticket', default_price=20)
    assert client.get(url(env, 'voucher.group.edit', foreign)).status_code == 404
    assert client.get(url(env, 'voucher.group.delete', foreign)).status_code == 404
    obj = voucher(env)
    target, data, response = preview(client, env, obj, {'productvar': [str(product.pk)]})
    assert response.context['updated_count'] == 0


def test_shared_quota_is_checked_for_whole_batch(client, env):
    env.quota.size = 3
    env.quota.save()
    obj = voucher(env, max_usages=2, product=env.product)
    other = voucher(env, max_usages=2, product=env.product)
    target, data, response = preview(client, env, obj, {'block_quota': 'on'})
    assert response.context['updated_count'] == 1
    assert response.context['skipped_count'] == 1
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    other.refresh_from_db()
    assert obj.block_quota and not other.block_quota


def test_increasing_existing_reservation_checks_additional_capacity(client, env):
    env.quota.size = 3
    env.quota.save()
    obj = voucher(env, max_usages=2, product=env.product, block_quota=True)
    target, data, response = preview(client, env, obj, {'max_usages': '4'})
    assert response.context['updated_count'] == 0
    assert response.context['skipped_count'] == 1


def test_product_changes_and_existing_orders_preserved(client, env):
    obj = voucher(env, max_usages=3, redeemed=1, product=env.product)
    other = Product.objects.create(event=env.event, name='Other', default_price=50)
    order = Order.objects.create(event=env.event, status=Order.STATUS_PAID, total=30)
    position = OrderPosition.objects.create(order=order, product=env.product, voucher=obj, price=30)
    target, data, response = preview(client, env, obj, {'productvar': [str(other.pk)], 'value': '10'})
    assert response.context['updated_count'] == 1
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    position.refresh_from_db()
    order.refresh_from_db()
    assert obj.product == other
    assert position.product == env.product and position.price == 30 and order.total == 30


def test_group_delete_counts_preserves_used_and_cleans_carts(client, env):
    unused = voucher(env)
    cart = CartPosition.objects.create(
        event=env.event,
        product=env.product,
        voucher=unused,
        price=30,
        expires=now() + timedelta(minutes=15),
        cart_id='test',
    )
    addon = CartPosition.objects.create(
        event=env.event,
        product=env.product,
        addon_to=cart,
        price=5,
        expires=now() + timedelta(minutes=15),
        cart_id='test',
    )
    used = voucher(env, redeemed=1)
    historical = voucher(env)
    order = Order.objects.create(event=env.event, status=Order.STATUS_CANCELED, total=30)
    OrderPosition.objects.create(order=order, product=env.product, voucher=historical, price=30)
    target = url(env, 'voucher.group.delete', unused)
    response = client.get(target)
    assert response.context['allowed_count'] == 1 and response.context['kept_count'] == 2
    assert all(obj.code not in response.content.decode() for obj in (unused, used, historical))
    assert client.post(target, {'selection': selection(response)}).status_code == 302
    assert not Voucher.objects.filter(pk=unused.pk).exists()
    assert Voucher.objects.filter(pk__in=[used.pk, historical.pk]).count() == 2
    assert LogEntry.objects.filter(action_type='eventyay.voucher.deleted').count() == 1
    assert not CartPosition.objects.filter(pk__in=[cart.pk, addon.pk]).exists()


def test_group_delete_rechecks_usage(client, env):
    obj = voucher(env)
    target = url(env, 'voucher.group.delete', obj)
    token = selection(client.get(target))
    obj.redeemed = 1
    obj.save()
    assert client.post(target, {'selection': token}).status_code == 302
    assert Voucher.objects.filter(pk=obj.pk).exists()


def test_group_controls_in_list(client, env):
    obj = voucher(env)
    response = client.get(url(env, 'vouchers'))
    doc = BeautifulSoup(response.content, 'lxml')
    assert doc.select_one(f'a[href="{url(env, "voucher.group.edit", obj)}"]')['title'] == 'Edit group'
    assert doc.select_one(f'a[href="{url(env, "voucher.group.delete", obj)}"]')['title'] == 'Delete group'
    assert doc.select_one('button[name="action"][value="edit"]')


@pytest.mark.parametrize('target_kind', ['all', 'single', 'multiple', 'variation', 'quota'])
def test_change_existing_multiple_product_restrictions(client, env, target_kind):
    obj = voucher(env)
    second = Product.objects.create(event=env.event, name='Second', default_price=40)
    variable = Product.objects.create(event=env.event, name='Shirt', default_price=20)
    variation = ProductVariation.objects.create(product=variable, value='Red')
    obj.limit_products.set([env.product, second])
    targets = {
        'all': ['all'],
        'single': [str(second.pk)],
        'multiple': [str(second.pk), str(variable.pk)],
        'variation': [f'{variable.pk}-{variation.pk}'],
        'quota': [f'q-{env.quota.pk}'],
    }
    target, data, response = preview(client, env, obj, {'productvar': targets[target_kind]})
    assert response.context['updated_count'] == 1, list(response.context['skipped'])
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    if target_kind == 'multiple':
        assert set(obj.limit_products.all()) == {second, variable}
    else:
        assert not obj.limit_products.exists()
    assert obj.quota_id == (env.quota.pk if target_kind == 'quota' else None)
    assert obj.variation_id == (variation.pk if target_kind == 'variation' else None)


def test_untouched_product_limits_and_whitespace_preserved(client, env):
    obj = voucher(env, comment='  Keep whitespace  ')
    second = Product.objects.create(event=env.event, name='Second', default_price=40)
    obj.limit_products.set([env.product, second])
    target, data, response = preview(client, env, obj, {'value': '10'})
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    assert obj.comment == '  Keep whitespace  '
    assert set(obj.limit_products.all()) == {env.product, second}


def test_budget_exhaustion_skips_voucher(client, env):
    obj = voucher(env, budget=10, max_usages=5, redeemed=1)
    order = Order.objects.create(event=env.event, status=Order.STATUS_PAID, total=20)
    OrderPosition.objects.create(
        order=order,
        product=env.product,
        voucher=obj,
        price=20,
        price_before_voucher=30,
    )
    target, data, response = preview(client, env, obj, {'budget': '100'})
    assert response.context['updated_count'] == 0
    assert response.context['skipped_count'] == 1


def test_hidden_products_use_new_visibility_setting(client, env):
    env.product.hide_without_voucher = True
    env.product.save()
    obj = voucher(env)
    target, data, response = preview(
        client,
        env,
        obj,
        {
            'productvar': [str(env.product.pk)],
            'show_hidden_products': '',
        },
    )
    assert response.context['updated_count'] == 0


def test_event_series_requires_date_for_reservations(client, env):
    env.event.has_subevents = True
    env.event.save()
    obj = voucher(env, product=env.product)
    target, data, response = preview(client, env, obj, {'block_quota': 'on'})
    assert response.context['updated_count'] == 0
    date = SubEvent.objects.create(event=env.event, name='Day', date_from=now())
    env.quota.subevent = date
    env.quota.save()
    target, data, response = preview(client, env, obj, {'block_quota': 'on', 'subevent': str(date.pk)})
    assert response.context['updated_count'] == 1
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    assert obj.subevent == date and obj.block_quota


def test_waiting_list_vouchers_excluded(client, env):
    obj = voucher(env)
    waiting = voucher(env)
    WaitingListEntry.objects.create(event=env.event, product=env.product, voucher=waiting, email='waiting@example.org')
    target, data, response = preview(client, env, obj, {'comment': 'New'})
    assert response.context['selected_count'] == 1
    assert confirm(client, target, data, response).status_code == 302
    waiting.refresh_from_db()
    assert waiting.comment == ''


def test_quota_change_after_preview_requires_confirmation(client, env):
    obj = voucher(env, product=env.product, max_usages=2)
    target, data, response = preview(client, env, obj, {'block_quota': 'on'})
    env.quota.size = 1
    env.quota.save()
    response = confirm(client, target, data, response)
    assert response.status_code == 200
    assert response.context['updated_count'] == 0
    obj.refresh_from_db()
    assert not obj.block_quota


def test_reservation_without_quota_rejected(client, env):
    product = Product.objects.create(event=env.event, name='No quota', default_price=40)
    obj = voucher(env, product=product)
    target, data, response = preview(client, env, obj, {'block_quota': 'on'})
    assert response.context['updated_count'] == 0


def test_selection_cannot_be_reused_for_another_event(client, env):
    obj = voucher(env)
    other_event = Event.objects.create(organizer=env.organizer, name='Other', slug='other', date_from=now())
    other = Voucher.objects.create(event=other_event, tag='Other')
    env.team.limit_events.add(other_event)
    token = selection(client.get(url(env, 'voucher.group.edit', obj)))
    target = reverse(
        'control:event.voucher.group.edit',
        kwargs={
            'organizer': env.organizer.slug,
            'event': other_event.slug,
            'voucher': other.pk,
        },
    )
    assert (
        client.post(
            target,
            {
                'selection': token,
                'fields_to_change': ['comment'],
                'comment': 'New',
            },
        ).status_code
        == 400
    )


def test_empty_and_malformed_selection(client, env):
    target = url(env, 'vouchers.bulkaction')
    response = client.post(target, {'action': 'edit'})
    assert response.status_code == 200
    assert response.context['selected_count'] == 0
    assert client.post(target, {'action': 'edit', 'voucher': ['not-a-number']}).status_code == 400


def test_save_rolls_back_all_changes_if_one_save_fails(client, env, monkeypatch):
    first = voucher(env)
    second = voucher(env)
    target, data, response = preview(client, env, first, {'comment': 'New'})
    original_save = Voucher.save

    def fail_second(self, *args, **kwargs):
        if self.pk == second.pk:
            raise ValueError('Failed to save voucher')
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Voucher, 'save', fail_second)
    with pytest.raises(ValueError, match='Failed to save voucher'):
        confirm(client, target, data, response)
    first.refresh_from_db()
    assert first.comment == ''
    assert not LogEntry.objects.filter(action_type='eventyay.voucher.changed').exists()


def test_equivalent_form_formatting_can_confirm(client, env):
    obj = voucher(env)
    target, data, response = preview(client, env, obj, {'value': '20.00', 'comment': ' New '})
    data.update(value='20', comment='New')
    assert confirm(client, target, data, response).status_code == 302
    obj.refresh_from_db()
    assert obj.value == Decimal('20') and obj.comment == 'New'


@pytest.mark.parametrize('action', ['voucher.group.edit', 'voucher.group.delete'])
def test_expired_selection_rejected(client, env, action, monkeypatch):
    obj = voucher(env)
    target = url(env, action, obj)
    token = selection(client.get(target))
    monkeypatch.setattr('django.core.signing.time.time', lambda: now().timestamp() + 3601)
    assert client.post(target, {'selection': token}).status_code == 400
    assert Voucher.objects.filter(pk=obj.pk).exists()


def test_selection_bound_to_user(client, env):
    obj = voucher(env)
    target = url(env, 'voucher.group.edit', obj)
    token = selection(client.get(target))
    other = User.objects.create_user('other@example.org', 'test')
    env.team.members.add(other)
    client.force_login(other)
    assert client.post(target, {'selection': token}).status_code == 400


def test_deleted_voucher_requires_updated_preview(client, env):
    obj = voucher(env)
    other = voucher(env)
    target, data, response = preview(client, env, obj, {'comment': 'New'})
    other.delete()
    response = confirm(client, target, data, response)
    assert response.status_code == 200
    assert response.context['updated_count'] == 1
    assert dict(response.context['skipped']) == {'No longer available': 1}
    obj.refresh_from_db()
    assert obj.comment == ''
    assert confirm(client, target, data, response).status_code == 302


def test_existing_multiple_product_reservations_count_towards_capacity(client, env):
    existing = Voucher.objects.create(event=env.event, block_quota=True, max_usages=9)
    existing.limit_products.add(env.product)
    obj = voucher(env, product=env.product, max_usages=2)
    target, data, response = preview(client, env, obj, {'block_quota': 'on'})
    assert response.context['updated_count'] == 0
    assert dict(response.context['skipped']) == {'Not enough quota available': 1}


@pytest.mark.parametrize('bypass,expected', [('', 0), ('on', 1)])
def test_closed_quota_requires_explicit_bypass(client, env, bypass, expected):
    env.quota.closed = True
    env.quota.save()
    obj = voucher(env, product=env.product)
    target, data, response = preview(client, env, obj, {'block_quota': 'on', 'allow_ignore_quota': bypass})
    assert response.context['updated_count'] == expected


def test_released_reservations_first_cover_existing_quota_deficit(env):
    Voucher.objects.create(event=env.event, product=env.product, block_quota=True, max_usages=15)
    budget = VoucherQuotaBudget(env.event)
    initial = {
        'productvar': [str(env.product.pk)],
        'block_quota': True,
        'max_usages': 15,
        'valid_until': None,
        'subevent': None,
        'allow_ignore_quota': False,
    }
    release = SimpleNamespace(
        initial=initial, cleaned_data={**initial, 'max_usages': 12}, instance=SimpleNamespace(redeemed=0)
    )
    assert budget.accept(release)
    reserve = SimpleNamespace(
        initial={**initial, 'block_quota': False, 'max_usages': 2},
        cleaned_data={**initial, 'max_usages': 2},
        instance=SimpleNamespace(redeemed=0),
    )
    assert not budget.accept(reserve)


def test_selection_includes_group_beyond_list_page(client, env):
    objects = [voucher(env) for _ in range(101)]
    target, data, response = preview(client, env, objects[0], {'comment': 'Entire group'})
    assert response.context['updated_count'] == 101
    assert confirm(client, target, data, response).status_code == 302
    assert env.event.vouchers.filter(comment='Entire group').count() == 101


def test_every_product_requires_quota_when_reserving_multiple_products(client, env):
    other = Product.objects.create(event=env.event, name='Without quota', default_price=30)
    obj = voucher(env)
    target, data, response = preview(
        client, env, obj, {'productvar': [str(env.product.pk), str(other.pk)], 'block_quota': 'on'}
    )
    assert response.context['updated_count'] == 0
    assert dict(response.context['skipped']) == {'Not enough quota available': 1}
