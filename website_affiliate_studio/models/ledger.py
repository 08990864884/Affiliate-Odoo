from datetime import timedelta

from odoo import _, api, Command, fields, models
from odoo.exceptions import UserError
from odoo.osv import expression


class CommissionEntry(models.Model):
    _name = 'wa.entry'
    _description = 'Affiliate Commission Entry'
    _order = 'id desc'
    _check_company_auto = True

    affiliate_id = fields.Many2one('wa.affiliate', required=True, check_company=True, index=True, ondelete='restrict')
    company_id = fields.Many2one(related='affiliate_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    order_id = fields.Many2one('sale.order', required=True, check_company=True, index=True, ondelete='restrict')
    line_id = fields.Many2one('sale.order.line', required=True, ondelete='restrict')
    rule_id = fields.Many2one('wa.rule', ondelete='set null')
    amount = fields.Monetary(required=True, group_operator='sum')
    order_currency_id = fields.Many2one(related='order_id.currency_id', string='Order currency')
    base_amount = fields.Monetary(currency_field='order_currency_id')
    state = fields.Selection([
        ('review', 'Under review'), ('holding', 'Holding'), ('payable', 'Available'),
        ('reserved', 'Reserved'), ('paid', 'Paid'), ('void', 'Void'),
    ], required=True, default='holding', index=True)
    ready_date = fields.Date(required=True, index=True)
    reason = fields.Char()
    entry_key = fields.Char(required=True, index=True, copy=False)
    original_id = fields.Many2one('wa.entry', index=True, ondelete='restrict')
    adjustment_ids = fields.One2many('wa.entry', 'original_id')
    credit_id = fields.Many2one('account.move', check_company=True, ondelete='restrict')
    payout_id = fields.Many2one('wa.payout', check_company=True, index=True, ondelete='restrict')

    _sql_constraints = [('entry_key_unique', 'unique(entry_key)', 'This commission entry already exists.')]

    def _check_reviewer(self):
        self.check_access_rights('read')
        self.check_access_rule('read')
        if not self.env.su and not self.env.user.has_group('website_affiliate_studio.group_affiliate_user'):
            raise UserError(_('Affiliate staff must review commission entries.'))

    def _lock_affiliates(self):
        self.sudo().affiliate_id._lock()
        self.invalidate_recordset()

    def action_accept_review(self):
        self._check_reviewer()
        self._lock_affiliates()
        today = fields.Date.today()
        for entry in self.sudo().filtered(lambda r: r.state == 'review' and not r.original_id):
            state = 'holding' if entry.ready_date > today else 'payable'
            (entry | entry.adjustment_ids.filtered(lambda r: r.state == 'review')).write({'state': state})

    def action_reject_review(self):
        self._check_reviewer()
        self._lock_affiliates()
        entries = self.sudo().filtered(lambda r: r.state == 'review' and not r.original_id)
        (entries | entries.adjustment_ids.filtered(lambda r: r.state == 'review')).write({'state': 'void'})

    @api.model
    def _release_due(self):
        due = self.sudo().search([('state', '=', 'holding'), ('ready_date', '<=', fields.Date.today())],
                                 order='ready_date, id', limit=2000)
        due._lock_affiliates()
        due.filtered(lambda r: r.state == 'holding').write({'state': 'payable'})


class AffiliatePayout(models.Model):
    _name = 'wa.payout'
    _description = 'Affiliate Payout'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True
    _rec_name = 'reference'

    reference = fields.Char(required=True, readonly=True, copy=False)
    affiliate_id = fields.Many2one('wa.affiliate', required=True, check_company=True, index=True, ondelete='restrict')
    company_id = fields.Many2one(related='affiliate_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    amount = fields.Monetary(required=True, readonly=True)
    state = fields.Selection([
        ('requested', 'Requested'), ('billed', 'Billed'), ('paid', 'Paid'), ('cancelled', 'Cancelled'),
    ], required=True, default='requested', tracking=True, index=True)
    source = fields.Selection([('portal', 'Portal request'), ('batch', 'Batch')], required=True)
    bill_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, ondelete='restrict', index=True)
    entry_ids = fields.One2many('wa.entry', 'payout_id')
    paid_date = fields.Date(readonly=True)

    @api.model
    def _reserve(self, affiliate, source):
        affiliate.ensure_one()
        affiliate._lock()
        if affiliate.state != 'active':
            return self.browse()
        entries = self.env['wa.entry'].sudo().search([
            ('affiliate_id', '=', affiliate.id), ('state', '=', 'payable'), ('payout_id', '=', False),
        ])
        currency = affiliate.currency_id
        amount = currency.round(sum(entries.mapped('amount')))
        if (currency.compare_amounts(amount, 0) <= 0
                or currency.compare_amounts(amount, affiliate.company_id.aff_min_payout) < 0):
            return self.browse()
        payout = self.sudo().with_company(affiliate.company_id).create({
            'reference': self.env['ir.sequence'].sudo().next_by_code('wa.payout'),
            'affiliate_id': affiliate.id, 'amount': amount, 'source': source,
        })
        entries.write({'state': 'reserved', 'payout_id': payout.id})
        return payout

    @api.model
    def _batch(self, affiliates):
        affiliates.check_access_rule('read')
        affiliates = affiliates.sudo()
        affiliates._lock()
        payouts = self.sudo().browse()
        for affiliate in affiliates.sorted('id'):
            payouts |= self._reserve(affiliate, 'batch')
        payouts._create_bills()
        return payouts

    def _apply_pending_refunds(self):
        payouts = self.sudo()
        payouts.affiliate_id._lock()
        payouts.invalidate_recordset()
        for payout in payouts.filtered(lambda item: item.state == 'requested'
                                       or (item.state == 'billed' and item.bill_id.state == 'draft')):
            adjustments = self.env['wa.entry'].sudo().search([
                ('affiliate_id', '=', payout.affiliate_id.id), ('state', '=', 'payable'),
                ('payout_id', '=', False), ('original_id.payout_id', '=', payout.id),
            ])
            if not adjustments:
                continue

            previous_amount = payout.amount
            adjustments.write({'state': 'reserved', 'payout_id': payout.id})
            amount = payout.currency_id.round(sum(payout.entry_ids.mapped('amount')))
            payout.amount = max(0, amount)
            if payout.state == 'requested' and payout.amount == 0:
                payout._release_entries()
            elif payout.bill_id:
                payout._update_draft_bill(previous_amount)

    def _update_draft_bill(self, previous_amount):
        self.ensure_one()
        bill = self.bill_id
        lines = bill.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        # Synchronize the generated, untaxed settlement line. Accounting edits remain available for review.
        if (len(lines) == 1 and bill.currency_id == self.currency_id and lines.quantity == 1
                and not lines.discount and not lines.tax_ids
                and self.currency_id.compare_amounts(lines.price_unit, previous_amount) == 0):
            lines.price_unit = self.amount

    def _check_bill_amount_before_posting(self):
        self._apply_pending_refunds()
        for payout in self:
            bill = payout.bill_id
            if (payout.state != 'billed' or bill.currency_id != payout.currency_id or payout.amount <= 0
                    or payout.currency_id.compare_amounts(bill.amount_total, payout.amount) != 0):
                raise UserError(_(
                    'Payout %(payout)s has a net amount of %(amount)s %(currency)s. '
                    'Match the draft vendor bill to this amount before posting, or cancel the bill and release the payout.',
                    payout=payout.reference, amount=payout.amount, currency=payout.currency_id.name,
                ))

    def _create_bills(self):
        payouts = self.sudo()
        payouts._apply_pending_refunds()
        for payout in payouts.filtered(lambda r: r.state == 'requested'):
            company = payout.company_id
            if not company.aff_expense_account_id or not company.aff_purchase_journal_id:
                raise UserError(_('Configure the affiliate expense account and purchase journal for %s.', company.display_name))
            if company.aff_expense_account_id.deprecated:
                raise UserError(_('Select an active expense account for affiliate payouts.'))
            bill = self.env['account.move'].sudo().with_company(company).create({
                'move_type': 'in_invoice',
                'company_id': company.id,
                'journal_id': company.aff_purchase_journal_id.id,
                'partner_id': payout.affiliate_id.partner_id.id,
                'currency_id': payout.currency_id.id,
                'invoice_date': fields.Date.context_today(payout),
                'ref': payout.reference,
                'invoice_line_ids': [Command.create({
                    'name': _('Affiliate settlement %s', payout.reference),
                    'price_unit': payout.amount,
                    'account_id': company.aff_expense_account_id.id,
                    'tax_ids': [Command.clear()],
                })],
            })
            payout.write({'bill_id': bill.id, 'state': 'billed'})

    def _check_manager(self):
        self.check_access_rights('read')
        self.check_access_rule('read')
        self.affiliate_id._require_manager()

    def action_create_bill(self):
        self._check_manager()
        self._create_bills()

    def action_cancel(self):
        self._check_manager()
        self.sudo().affiliate_id._lock()
        self.invalidate_recordset()
        for payout in self.sudo().filtered(lambda r: r.state in ('requested', 'billed')):
            if payout.bill_id and payout.bill_id.state != 'cancel':
                raise UserError(_('Cancel the linked vendor bill in Accounting before releasing the payout.'))
            payout._release_entries()

    def _release_entries(self):
        self.entry_ids.write({'state': 'payable', 'payout_id': False})
        self.write({'state': 'cancelled'})

    def _sync_bills(self):
        self.sudo().affiliate_id._lock()
        self.invalidate_recordset()
        for payout in self.sudo().filtered(lambda r: r.state in ('billed', 'paid')):
            paid = payout.bill_id.state == 'posted' and payout.bill_id.payment_state == 'paid'
            if paid and payout.state == 'billed':
                payout.entry_ids.write({'state': 'paid'})
                payout.write({'state': 'paid', 'paid_date': fields.Date.today()})
            elif not paid and payout.state == 'paid':
                payout.entry_ids.write({'state': 'reserved'})
                payout.write({'state': 'billed', 'paid_date': False})

    @api.model
    def _cron_sync_payouts(self):
        batches = self.sudo()
        paid_bill = [('bill_id.state', '=', 'posted'), ('bill_id.payment_state', '=', 'paid')]
        domain = expression.OR([
            [('state', '=', 'billed')] + paid_bill,
            [('state', '=', 'paid'), '|', ('bill_id.state', '!=', 'posted'), ('bill_id.payment_state', '!=', 'paid')],
        ])
        batches.search(domain, order='id', limit=1000)._sync_bills()
