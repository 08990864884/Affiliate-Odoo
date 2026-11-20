from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class AffiliateSettingsWizard(models.TransientModel):
    _name = 'wa.settings'
    _description = 'Website Affiliate Settings'

    company_id = fields.Many2one('res.company', readonly=True, required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    aff_enabled = fields.Boolean(string='Enable website affiliates')
    aff_auto_approve = fields.Boolean(string='Automatic approval')
    aff_cookie_days = fields.Integer(string='Referral cookie days')
    aff_repeat_months = fields.Integer(string='Repeat purchase months')
    aff_discount = fields.Float(string='Coupon discount (%)')
    aff_hold_days = fields.Integer(string='Holding days')
    aff_exclude_shipping = fields.Boolean(string='Exclude shipping')
    aff_block_self = fields.Boolean(string='Block self-referral')
    aff_review_ip = fields.Boolean(string='Review matching IPs')
    aff_min_payout = fields.Monetary(string='Minimum payout')
    aff_terms = fields.Text(string='Program terms')
    aff_attribution = fields.Selection([('first', 'First click'), ('last', 'Last click')], required=True)
    aff_trigger = fields.Selection([('confirmed', 'Order confirmation'), ('paid', 'Full payment')],
                                   required=True, string='Create commission on')
    aff_expense_account_id = fields.Many2one(
        'account.account', string='Expense account',
        domain="[('company_id', '=', company_id), ('deprecated', '=', False), ('account_type', 'in', ('expense', 'expense_direct_cost', 'expense_depreciation'))]")
    aff_purchase_journal_id = fields.Many2one(
        'account.journal', string='Purchase journal',
        domain="[('company_id', '=', company_id), ('type', '=', 'purchase')]")

    def _setting_names(self):
        return (
            'aff_enabled',
            'aff_auto_approve',
            'aff_cookie_days',
            'aff_repeat_months',
            'aff_discount',
            'aff_hold_days',
            'aff_exclude_shipping',
            'aff_block_self',
            'aff_review_ip',
            'aff_min_payout',
            'aff_terms',
            'aff_attribution',
            'aff_trigger',
            'aff_expense_account_id',
            'aff_purchase_journal_id',
        )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        company = self.env.company.sudo()
        for name in set(fields_list).intersection(self._setting_names()):
            value = company[name]
            values[name] = value.id if company._fields[name].type == 'many2one' else value
        return values

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group('website_affiliate_studio.group_affiliate_manager'):
            raise AccessError(_('Only affiliate managers can edit affiliate settings.'))
        if self.company_id != self.env.company:
            raise AccessError(_('Switch to this company before changing its settings.'))
        vals = {name: self[name].id if self._fields[name].type == 'many2one' else self[name]
                for name in self._setting_names()}
        self.company_id.sudo().write(vals)
        return {'type': 'ir.actions.act_window_close'}