import json
from collections import Counter, defaultdict
from decimal import Decimal
from hashlib import sha256

from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext as _
from django.views import View
from django_scopes import scope

from eventyay.base.models import CartPosition, SubEvent, Voucher
from eventyay.base.services.quotas import QuotaAvailability
from eventyay.control.forms.vouchers import VoucherBulkEditForm
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.helpers.json import CustomJSONEncoder


SELECTION_SALT = 'voucher-bulk-selection'
PREVIEW_SALT = 'voucher-bulk-preview'


def preview_value(value):
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, list):
        return sorted(value)
    if isinstance(value, SubEvent):
        return value.pk
    return value


def selected_vouchers(request):
    try:
        ids = [int(pk) for pk in request.POST.getlist('voucher')]
        groups = [int(pk) for pk in request.POST.getlist('voucher_group')]
    except ValueError as exc:
        raise signing.BadSignature('Invalid voucher selection.') from exc
    tags = request.event.vouchers.filter(pk__in=groups).exclude(tag='').values('tag')
    return request.event.vouchers.filter(Q(pk__in=ids) | Q(tag__in=tags), waitinglistentries__isnull=True).distinct()


class VoucherQuotaBudget:
    """Account for reservation changes across the entire batch before saving any voucher."""

    def __init__(self, event):
        self.event = event
        self.timestamp = now()
        self.quotas = list(event.quotas.prefetch_related('products', 'variations'))
        self.quota_tokens = {
            quota: {f'q-{quota.pk}'}
            | {str(product.pk) for product in quota.products.all()}
            | {f'{variation.product_id}-{variation.pk}' for variation in quota.variations.all()}
            for quota in self.quotas
        }
        self.remaining = {}
        self.deltas = defaultdict(int)
        # QuotaAvailability currently counts only the legacy product/variation/quota foreign keys.
        self.multiple_product_reservations = Counter()
        multiple_product_vouchers = (
            event.vouchers.filter(
                Q(limit_products__isnull=False) | Q(limit_variations__isnull=False),
                product__isnull=True,
                variation__isnull=True,
                quota__isnull=True,
                block_quota=True,
            )
            .select_related('subevent')
            .prefetch_related('limit_products', 'limit_variations')
            .distinct()
        )
        for voucher in multiple_product_vouchers:
            self.multiple_product_reservations.update(
                self.reservations(
                    {
                        'block_quota': True,
                        'valid_until': voucher.valid_until,
                        'max_usages': voucher.max_usages,
                        'subevent': voucher.subevent,
                        'productvar': [str(p.pk) for p in voucher.limit_products.all()]
                        + [f'{v.product_id}-{v.pk}' for v in voucher.limit_variations.all()],
                    },
                    voucher.redeemed,
                )
            )

    def reservations(self, data, redeemed):
        if not data.get('block_quota') or (data.get('valid_until') and data['valid_until'] < self.timestamp):
            return {}
        tokens = set(data['productvar'])
        subevent = data.get('subevent')
        subevent_id = subevent.pk if subevent else None
        return {
            quota: max(0, data['max_usages'] - redeemed)
            for quota in self.quotas
            if quota.subevent_id == subevent_id and tokens & self.quota_tokens[quota]
        }

    def accept(self, form):
        before = self.reservations(form.initial, form.instance.redeemed)
        after = self.reservations(form.cleaned_data, form.instance.redeemed)
        data = form.cleaned_data
        if (
            data['block_quota']
            and not data['allow_ignore_quota']
            and not set(data['productvar']).issubset(set().union(*(self.quota_tokens[quota] for quota in after)))
            and data['max_usages'] > form.instance.redeemed
            and (data['valid_until'] is None or data['valid_until'] >= self.timestamp)
        ):
            return False
        changes = {quota: after.get(quota, 0) - before.get(quota, 0) for quota in before.keys() | after.keys()}
        missing = [quota for quota, delta in changes.items() if delta > 0 and quota not in self.remaining]
        if missing:
            availability = QuotaAvailability(count_waitinglist=False, full_results=True, early_out=False)
            availability.queue(*missing)
            availability.compute(now_dt=self.timestamp)
            for quota in missing:
                remaining = availability.results[quota][1]
                if quota.size is not None:
                    # Keep an existing deficit: releasing reservations must first cover overselling.
                    capacity = (
                        quota.size
                        - availability.count_paid_orders[quota]
                        - availability.count_pending_orders[quota]
                        - availability.count_cart[quota]
                        - availability.count_vouchers[quota]
                        + availability.count_exited_orders[quota]
                    )
                    remaining = min(remaining, capacity) if remaining is not None else capacity
                self.remaining[quota] = (
                    remaining - self.multiple_product_reservations[quota] if remaining is not None else None
                )
        if not form.cleaned_data['allow_ignore_quota']:
            for quota, delta in changes.items():
                available = self.remaining.get(quota)
                if delta > 0 and (quota.closed or (available is not None and self.deltas[quota] + delta > available)):
                    return False
        for quota, delta in changes.items():
            self.deltas[quota] += delta
        return True


class VoucherSelectionMixin:
    permission = 'can_change_vouchers'

    def list_url(self):
        return reverse(
            'control:event.vouchers',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        )

    def make_selection(self, vouchers, group=None):
        return signing.dumps(
            {
                'event': self.request.event.pk,
                'user': self.request.user.pk,
                'vouchers': list(vouchers.order_by('pk').values_list('pk', flat=True)),
                'group': group,
            },
            salt=SELECTION_SALT,
            compress=True,
        )

    def read_selection(self, token):
        selection = signing.loads(token, salt=SELECTION_SALT, max_age=3600)
        if selection['event'] != self.request.event.pk or selection['user'] != self.request.user.pk:
            raise signing.BadSignature('Invalid voucher selection.')
        return selection

    def group_selection(self):
        try:
            representative = self.request.event.vouchers.get(
                pk=self.kwargs['voucher'],
                waitinglistentries__isnull=True,
            )
        except Voucher.DoesNotExist as exc:
            raise Http404(_('The requested voucher does not exist.')) from exc
        if not representative.tag:
            raise Http404(_('The requested voucher does not belong to a group.'))
        vouchers = self.request.event.vouchers.filter(tag=representative.tag, waitinglistentries__isnull=True)
        return self.make_selection(vouchers, group=representative.tag)

    def queryset(self, selection):
        return self.request.event.vouchers.filter(
            pk__in=selection['vouchers'],
            waitinglistentries__isnull=True,
        ).order_by('pk')


class VoucherBulkEdit(VoucherSelectionMixin, EventPermissionRequiredMixin, View):
    template_name = 'pretixcontrol/vouchers/edit_bulk.html'

    def get(self, request, *args, **kwargs):
        with scope(event=request.event):
            token = self.group_selection()
            return self.render_form(token, VoucherBulkEditForm(event=request.event))

    def render_form(self, token, form, **context):
        selection = self.read_selection(token)
        return render(
            self.request,
            self.template_name,
            {
                'form': form,
                'selection': token,
                'group': selection['group'],
                'selected_count': len(selection['vouchers']),
                **context,
            },
        )

    def plan(self, selection, form):
        vouchers = Voucher.annotate_budget_used_orders(
            self.queryset(selection)
            .select_for_update(of=('self',))
            .select_related(
                'event',
                'product',
                'variation',
                'quota',
                'seat',
                'subevent',
            )
            .prefetch_related('limit_products', 'limit_variations')
        )
        updates = []
        skipped = Counter()
        quota_budget = None
        seen = 0
        for voucher in vouchers:
            seen += 1
            if voucher.redeemed >= voucher.max_usages or (
                voucher.budget is not None and voucher.budget_used_orders >= voucher.budget
            ):
                skipped[_('Fully used')] += 1
                continue
            if (
                'max_usages' in form.cleaned_data['fields_to_change']
                and form.cleaned_data['max_usages'] < voucher.redeemed
            ):
                skipped[_('Maximum usages would be lower than existing redemptions')] += 1
                continue
            voucher_form = form.voucher_form(voucher)
            if not voucher_form.is_valid():
                for reason in {str(error) for errors in voucher_form.errors.values() for error in errors}:
                    skipped[reason] += 1
                continue
            if not voucher_form.has_changed():
                skipped[_('Already has the requested settings')] += 1
                continue
            if voucher_form.cleaned_data['block_quota'] or voucher_form.initial['block_quota']:
                if quota_budget is None:
                    quota_budget = VoucherQuotaBudget(self.request.event)
                if not quota_budget.accept(voucher_form):
                    skipped[_('Not enough quota available')] += 1
                    continue
            updates.append(voucher_form)
        if seen < len(selection['vouchers']):
            skipped[_('No longer available')] += len(selection['vouchers']) - seen
        return updates, skipped, len(selection['vouchers']) - len(updates)

    def post(self, request, *args, **kwargs):
        with scope(event=request.event):
            try:
                token = request.POST.get('selection')
                if not token:
                    if request.POST.get('action') != 'edit':
                        return HttpResponseBadRequest()
                    token = self.make_selection(selected_vouchers(request))
                    return self.render_form(token, VoucherBulkEditForm(event=request.event))
                selection = self.read_selection(token)
                form = VoucherBulkEditForm(request.POST, event=request.event)
                if not form.is_valid():
                    return self.render_form(token, form)
                with request.event.lock(), transaction.atomic():
                    updates, skipped, skipped_count = self.plan(selection, form)
                    state = {
                        'selection': token,
                        'data': {
                            name: preview_value(form.cleaned_data[name])
                            for name in form.cleaned_data['fields_to_change']
                        },
                        'updated': [f.instance.pk for f in updates],
                        'skipped': dict(skipped),
                    }
                    digest = sha256(json.dumps(state, sort_keys=True, cls=CustomJSONEncoder).encode()).hexdigest()
                    confirmed = False
                    if request.POST.get('action') == 'edit_confirm' and request.POST.get('preview'):
                        confirmed = signing.loads(request.POST['preview'], salt=PREVIEW_SALT, max_age=3600) == digest
                        if not confirmed:
                            messages.warning(
                                request, _('The vouchers or changes have changed. Please review the updated preview.')
                            )
                    if confirmed and updates:
                        for voucher_form in updates:
                            changed = {name: voucher_form.cleaned_data[name] for name in voucher_form.changed_data}
                            voucher = form.save_voucher(voucher_form)
                            voucher.log_action('eventyay.voucher.changed', user=request.user, data=changed)
                        messages.success(request, _('%(count)s vouchers have been updated.') % {'count': len(updates)})
                        return redirect(self.list_url())
                    return self.render_form(
                        token,
                        form,
                        preview=signing.dumps(digest, salt=PREVIEW_SALT),
                        updated_count=len(updates),
                        skipped_count=skipped_count,
                        skipped=skipped.items(),
                    )
            except signing.BadSignature:
                return HttpResponseBadRequest(
                    _('This selection has expired or is invalid. Please select the vouchers again.')
                )


class VoucherGroupDelete(VoucherSelectionMixin, EventPermissionRequiredMixin, View):
    template_name = 'pretixcontrol/vouchers/delete_group.html'

    def confirmation(self, token):
        selection = self.read_selection(token)
        vouchers = self.queryset(selection)
        allowed = vouchers.filter(redeemed=0, orderposition__isnull=True)
        return render(
            self.request,
            self.template_name,
            {
                'selection': token,
                'group': selection['group'],
                'allowed_count': allowed.count(),
                'kept_count': vouchers.exclude(pk__in=allowed).count(),
            },
        )

    def get(self, request, *args, **kwargs):
        with scope(event=request.event):
            return self.confirmation(self.group_selection())

    def post(self, request, *args, **kwargs):
        with scope(event=request.event):
            try:
                selection = self.read_selection(request.POST.get('selection', ''))
            except signing.BadSignature:
                return HttpResponseBadRequest(
                    _('This selection has expired or is invalid. Please select the vouchers again.')
                )
            with request.event.lock(), transaction.atomic():
                vouchers = self.queryset(selection).select_for_update(of=('self',))
                deleted = 0
                kept = 0
                for voucher in vouchers:
                    if not voucher.allow_delete():
                        kept += 1
                        continue
                    voucher.log_action('eventyay.voucher.deleted', user=request.user)
                    CartPosition.objects.filter(addon_to__voucher=voucher).delete()
                    voucher.cartposition_set.all().delete()
                    voucher.delete()
                    deleted += 1
                messages.success(
                    request,
                    _('%(deleted)s vouchers deleted; %(kept)s used vouchers kept.')
                    % {
                        'deleted': deleted,
                        'kept': kept,
                    },
                )
            return redirect(self.list_url())
