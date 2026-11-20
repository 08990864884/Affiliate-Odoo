from collections import defaultdict
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


def _allocate_discount(bases, line_ids, discount):
    total = sum(bases[line_id] for line_id in line_ids)
    if total:
        factor = max(0, 1 + discount / total)
        for line_id in line_ids:
            bases[line_id] *= factor


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wa_referral_id = fields.Many2one('wa.affiliate', copy=False, check_company=True, readonly=True, ondelete='set null')
    wa_affiliate_id = fields.Many2one('wa.affiliate', copy=False, check_company=True, readonly=True, index=True)
    wa_source = fields.Selection([('link', 'Referral link'), ('coupon', 'Affiliate coupon'), ('repeat', 'Returning customer')],
                                 copy=False, readonly=True)
    wa_visitor_ip = fields.Char(copy=False, groups='base.group_system')
    wa_self_blocked = fields.Boolean(copy=False, readonly=True)
    wa_processed = fields.Boolean(copy=False, readonly=True, index=True)
    wa_volume = fields.Monetary(currency_field='company_currency_id', copy=False, readonly=True)
    wa_credited_at = fields.Date(copy=False, readonly=True, index=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Affiliate company currency')

    def _wa_lock_orders(self):
        if self:
            self.flush_recordset()
            self.env.cr.execute('SELECT id FROM sale_order WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            self.invalidate_recordset()

    def _wa_pick_affiliate(self):
        self.ensure_one()
        if not self.website_id or not self.company_id.aff_enabled:
            return
        programs = self.order_line.filtered('is_reward_line').reward_id.program_id
        affiliates = self.env['wa.affiliate']
        domain = [('company_id', '=', self.company_id.id), ('state', '=', 'active')]
        affiliate = affiliates.search(domain + [('program_id', 'in', programs.ids)], order='id', limit=1)
        source = 'coupon'
        if not affiliate:
            affiliate = self.wa_referral_id.filtered(lambda item: item.state == 'active')
            source = 'link'
        if not affiliate and self.company_id.aff_repeat_months:
            referral = self.env['wa.customer'].search([
                ('company_id', '=', self.company_id.id),
                ('partner_id', '=', self.partner_id.commercial_partner_id.id),
                ('expires', '>=', fields.Date.today()),
                ('affiliate_id.state', '=', 'active'),
            ], limit=1)
            affiliate = referral.affiliate_id
            source = 'repeat'
        if not affiliate:
            return
        contact_email = (self.partner_id.email_normalized or '').lower()
        affiliate_email = (affiliate.partner_id.email_normalized or '').lower()
        blocked = self.company_id.aff_block_self and (
            self.partner_id.commercial_partner_id == affiliate.partner_id.commercial_partner_id
            or bool(contact_email and contact_email == affiliate_email))
        self.write({'wa_affiliate_id': affiliate.id, 'wa_source': source, 'wa_self_blocked': blocked})

    def action_confirm(self):
        orders = self.sudo()
        orders._wa_lock_orders()
        candidates = orders.filtered(lambda item: item.state in ('draft', 'sent') and not item.wa_processed)
        result = super().action_confirm()
        for order in candidates.filtered(lambda item: item.state in ('sale', 'done')):
            order._wa_pick_affiliate()
        orders._wa_create_commissions()
        return result

    def _wa_is_paid(self):
        self.ensure_one()
        currency = self.currency_id
        transactions = self.transaction_ids.filtered(
            lambda tx: tx.state == 'done' and tx.operation != 'refund' and tx.amount > 0
            and tx.currency_id == currency and len(tx.sale_order_ids) == 1)
        # A capture can be represented by a child transaction while the authorization stays open.
        captured = transactions.filtered(lambda tx: not tx.child_transaction_ids.filtered(
            lambda child: child.state == 'done' and child.operation != 'refund' and child.amount > 0))
        if self.amount_total > 0 and currency.compare_amounts(sum(captured.mapped('amount')), self.amount_total) >= 0:
            return True
        invoices = self.invoice_ids.filtered(lambda move: move.move_type == 'out_invoice' and move.state != 'cancel')
        if not invoices or any(move.state != 'posted' or move.payment_state != 'paid' for move in invoices):
            return False
        invoice_lines = invoices.invoice_line_ids.filtered(lambda line: self in line.sale_line_ids.order_id)
        total = sum(line.currency_id._convert(line.price_total, currency, self.company_id, line.move_id.date)
                    for line in invoice_lines)
        return currency.compare_amounts(total, self.amount_total) >= 0

    def _wa_line_bases(self):
        self.ensure_one()
        lines = self.order_line.filtered(lambda line: not line.display_type and line.product_id
                                         and not line.is_reward_line and not line.is_downpayment
                                         and line.product_uom_qty > 0 and line.price_subtotal > 0)
        if self.company_id.aff_exclude_shipping:
            lines = lines.filtered(lambda line: not line.is_delivery)
        bases = {line.id: line.price_subtotal for line in lines}
        discounts = self.order_line.filtered(lambda line: line.is_reward_line
                                             and line.reward_id.reward_type == 'discount' and line.price_subtotal < 0)
        for discount in discounts:
            reward = discount.reward_id
            eligible = (lines & self._cheapest_line()) if reward.discount_applicability == 'cheapest' else lines
            if reward.discount_applicability == 'specific':
                eligible &= self._get_specific_discountable_lines(reward)
            if not reward.program_id.is_payment_program:
                eligible -= self._get_no_effect_on_threshold_lines()
            eligible = eligible.filtered(lambda line: discount.tax_id == line.tax_id.filtered(
                lambda tax: tax.amount_type != 'fixed'))
            _allocate_discount(bases, eligible.ids, discount.price_subtotal)
        return lines, bases

    def _wa_remember_customer(self, today):
        self.ensure_one()
        if not self.company_id.aff_repeat_months or self.wa_source == 'repeat':
            return
        partner = self.partner_id.commercial_partner_id
        # Repeat attribution has one row per company and customer, including simultaneous checkouts.
        self.env.cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', [partner.id])
        model = self.env['wa.customer']
        domain = [('partner_id', '=', partner.id), ('company_id', '=', self.company_id.id)]
        existing = model.search(domain, limit=1)
        vals = {'affiliate_id': self.wa_affiliate_id.id,
                'expires': today + relativedelta(months=self.company_id.aff_repeat_months)}
        if not existing:
            model.create(dict(vals, partner_id=partner.id, company_id=self.company_id.id))
        elif existing.expires < today:
            existing.write(vals)

    def _wa_create_commissions(self):
        orders = self.sudo().filtered(lambda item: item.wa_affiliate_id and item.state in ('sale', 'done')
                                      and not item.wa_processed and not item.wa_self_blocked)
        orders._wa_lock_orders()
        orders = orders.filtered(lambda item: item.state in ('sale', 'done') and not item.wa_processed)
        orders.wa_affiliate_id._lock()
        today = fields.Date.today()
        for order in orders:
            company = order.company_id
            if company.aff_trigger == 'paid' and not order._wa_is_paid():
                continue
            affiliate = order.wa_affiliate_id
            if affiliate.state != 'active':
                order.wa_processed = True
                continue
            lines, bases = order._wa_line_bases()
            currency = company.currency_id
            convert = lambda value: order.currency_id._convert(value, currency, company, order.date_order.date(), round=False)
            volume = currency.round(convert(sum(bases.values())))
            month_start = today.replace(day=1)
            month_total = self.sudo().read_group([
                ('wa_affiliate_id', '=', affiliate.id), ('wa_credited_at', '>=', month_start),
                ('wa_credited_at', '<=', today), ('state', 'in', ['sale', 'done']),
            ], ['wa_volume:sum'], [])[0]['wa_volume'] or 0
            rules = self.env['wa.rule'].sudo().search([
                ('company_id', '=', company.id),
                '|', ('monthly_minimum', '=', False), ('monthly_minimum', '<=', month_total + volume),
                '|', ('affiliate_ids', '=', False), ('affiliate_ids', 'in', affiliate.id),
            ])
            ranked = sorted(rules, key=lambda rule: (
                not bool(rule.affiliate_ids), ['product', 'category', 'all'].index(rule.scope),
                -rule.monthly_minimum, rule.sequence, rule.id))
            matches = {line.id: next((rule for rule in ranked if rule._matches_line(line)), False) for line in lines}
            fixed = next((rule for rule in ranked if rule.rate_type == 'order' and rule in matches.values()), False)
            fixed_lines = lines.filtered(lambda line: fixed and matches[line.id] == fixed and bases[line.id] > 0)
            fixed_base = sum(bases[line.id] for line in fixed_lines)
            fixed_assigned = 0
            suspicious = bool(company.aff_review_ip and order.wa_visitor_ip
                              and order.wa_visitor_ip == affiliate.application_ip)
            state = 'review' if suspicious else ('holding' if company.aff_hold_days else 'payable')
            vals = []
            for line in lines:
                rule = matches[line.id]
                base = bases[line.id]
                if not rule or base <= 0:
                    continue
                if rule.rate_type == 'percent':
                    amount = convert(base * rule.percent / 100)
                elif rule.rate_type == 'unit':
                    amount = rule.fixed_amount * line.product_uom_qty
                elif line in fixed_lines:
                    apportioned = fixed.fixed_amount * base / fixed_base
                    amount = (fixed.fixed_amount - fixed_assigned if line == fixed_lines[-1]
                              else min(fixed.fixed_amount - fixed_assigned, currency.round(apportioned)))
                    fixed_assigned += amount
                else:
                    continue
                amount = currency.round(amount)
                if amount <= 0:
                    continue
                vals.append({
                    'affiliate_id': affiliate.id, 'order_id': order.id, 'line_id': line.id, 'rule_id': rule.id,
                    'amount': amount, 'base_amount': base, 'state': state,
                    'ready_date': today + timedelta(days=company.aff_hold_days),
                    'reason': _('Affiliate registration and order IP match.') if suspicious else False,
                    'entry_key': 'sale:%s' % line.id,
                })
            self.env['wa.entry'].sudo().with_company(company).create(vals)
            order.write({'wa_processed': True, 'wa_volume': 0 if suspicious else volume, 'wa_credited_at': today})
            if vals and not suspicious:
                order._wa_remember_customer(today)
            order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund' and move.state == 'posted')._wa_apply_credit()

    def _wa_reverse_orders(self):
        self._wa_lock_orders()
        originals = self.env['wa.entry'].sudo().search([('order_id', 'in', self.ids), ('original_id', '=', False)])
        originals._lock_affiliates()
        for entry in originals:
            entry._wa_adjust('cancel:%s' % entry.id, entry.base_amount, reason=_('Order cancelled'))

    def _action_cancel(self):
        result = super()._action_cancel()
        self.sudo().filtered('wa_processed')._wa_reverse_orders()
        return result

    def action_draft(self):
        if self.sudo().filtered(lambda order: order.state == 'cancel' and order.wa_processed and order.wa_affiliate_id):
            raise UserError(_('Duplicate the cancelled affiliate order to create a new quotation. Its original commissions were reversed.'))
        return super().action_draft()

    @api.model
    def _cron_affiliate_payments(self):
        orders = self.sudo().search([
            ('state', 'in', ['sale', 'done']), ('wa_affiliate_id', '!=', False), ('wa_processed', '=', False),
            ('wa_self_blocked', '=', False), ('company_id.aff_trigger', '=', 'paid'),
            '|', ('transaction_ids.state', '=', 'done'), ('invoice_ids.payment_state', '=', 'paid'),
        ], order='id')
        orders.filtered(lambda order: order._wa_is_paid())[:500]._wa_create_commissions()


class CommissionAdjustment(models.Model):
    _inherit = 'wa.entry'

    def _wa_adjust(self, key, base, credit=None, reason=None):
        self.ensure_one()
        if self.state == 'void' or key in self.adjustment_ids.mapped('entry_key') or not self.base_amount:
            return
        previous = self.adjustment_ids
        previous_base = -sum(previous.mapped('base_amount'))
        base = min(max(base, 0), max(self.base_amount - previous_base, 0))
        if not base:
            return
        previous_amount = -sum(previous.mapped('amount'))
        cumulative = self.currency_id.round(self.amount * min(1, (base + previous_base) / self.base_amount))
        deduction = max(0, self.currency_id.round(cumulative - previous_amount))
        adjustment = self.sudo().create({
            'affiliate_id': self.affiliate_id.id, 'order_id': self.order_id.id,
            'line_id': self.line_id.id, 'rule_id': self.rule_id.id,
            'amount': -deduction, 'base_amount': -base, 'original_id': self.id,
            'credit_id': credit.id if credit else False, 'entry_key': key, 'ready_date': self.ready_date,
            'state': self.state if self.state in ('review', 'holding') else 'payable',
            'reason': reason,
        })
        if self.state == 'reserved' and self.payout_id:
            self.payout_id._apply_pending_refunds()
        return adjustment


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _wa_apply_credit(self):
        refunds = self.sudo()
        sale_lines = (refunds.invoice_line_ids.sale_line_ids
                      | refunds.reversed_entry_id.invoice_line_ids.sale_line_ids)
        sale_lines.order_id._wa_lock_orders()
        originals = self.env['wa.entry'].sudo().search([('line_id', 'in', sale_lines.ids), ('original_id', '=', False)])
        originals._lock_affiliates()
        by_line = {entry.line_id.id: entry for entry in originals}
        for credit in refunds:
            bases = defaultdict(float)
            related_lines = credit.reversed_entry_id.invoice_line_ids
            lines = credit.invoice_line_ids.filtered(lambda line: line.product_id and line.price_subtotal > 0)
            subtotals = {line.id: line.price_subtotal for line in lines}
            discounts = credit.invoice_line_ids.filtered(lambda line: line.price_subtotal < 0
                and 'discount' in line.sale_line_ids.reward_id.mapped('reward_type'))
            for discount in discounts:
                reward = discount.sale_line_ids.reward_id[:1]
                eligible = lines.filtered(lambda line: line.tax_ids.filtered(
                    lambda tax: tax.amount_type != 'fixed') == discount.tax_ids
                    and not any(line.sale_line_ids.mapped('is_delivery')))
                if reward.discount_applicability == 'specific':
                    eligible = eligible.filtered(lambda line: line.product_id.filtered_domain(
                        reward._get_discount_product_domain()))
                elif reward.discount_applicability == 'cheapest':
                    eligible = eligible.sorted(lambda line: (line.price_unit, line.id))[:1]
                _allocate_discount(subtotals, eligible.ids, discount.price_subtotal)
            for line in lines:
                # Reversals created by Odoo preserve the source sales lines. For an edited credit, match
                # the product on its original invoice only when the affected sales line is unambiguous.
                candidates = line.sale_line_ids or related_lines.filtered(
                    lambda old: old.product_id == line.product_id).sale_line_ids
                if len(candidates) != 1 or candidates.id not in by_line:
                    continue
                entry = by_line[candidates.id]
                bases[entry.id] += line.currency_id._convert(
                    subtotals[line.id], entry.order_currency_id, credit.company_id, credit.date, round=False)
            for entry in originals.filtered(lambda item: item.id in bases):
                entry._wa_adjust('refund:%s:%s' % (credit.id, entry.id), bases[entry.id], credit, _('Credit note'))

    def _post(self, soft=True):
        draft_bills = self.filtered(lambda move: move.state == 'draft' and move.move_type == 'in_invoice')
        if draft_bills:
            payouts = self.env['wa.payout'].sudo().search([('bill_id', 'in', draft_bills.ids)])
            payout_bills = draft_bills & payouts.bill_id.with_env(self.env)
            if payout_bills:
                payout_bills.check_access_rights('write')
                payout_bills.check_access_rule('write')
                payouts._check_bill_amount_before_posting()
        result = super()._post(soft=soft)
        result.filtered(lambda move: move.move_type == 'out_refund' and move.state == 'posted')._wa_apply_credit()
        return result

    def _wa_check_credit_reset(self):
        if self.env['wa.entry'].sudo().search_count([('credit_id', 'in', self.ids)], limit=1):
            raise UserError(_('This credit note has affiliate adjustments. Use a corrective accounting document before changing the payout ledger.'))

    def button_draft(self):
        self.filtered(lambda move: move.move_type == 'out_refund')._wa_check_credit_reset()
        return super().button_draft()

    def button_cancel(self):
        self.filtered(lambda move: move.move_type == 'out_refund')._wa_check_credit_reset()
        return super().button_cancel()


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _reconcile_after_done(self):
        result = super()._reconcile_after_done()
        self.sale_order_ids._wa_create_commissions()
        return result