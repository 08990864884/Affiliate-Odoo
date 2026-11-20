import secrets
from urllib.parse import quote

from odoo import _, api, Command, fields, models
from odoo.exceptions import UserError, ValidationError


class AffiliateSettings(models.Model):
    _inherit = 'res.company'

    aff_enabled = fields.Boolean(string='Enable website affiliates')
    aff_auto_approve = fields.Boolean(string='Approve applications automatically')
    aff_cookie_days = fields.Integer(default=30, string='Referral cookie days')
    aff_attribution = fields.Selection(
        [('first', 'First click'), ('last', 'Last click')], default='last', required=True)
    aff_repeat_months = fields.Integer(string='Repeat purchase months')
    aff_discount = fields.Float(default=10, string='New affiliate coupon discount (%)')
    aff_trigger = fields.Selection(
        [('confirmed', 'Order confirmation'), ('paid', 'Full payment')],
        default='paid', required=True, string='Create commission on')
    aff_hold_days = fields.Integer(default=14, string='Holding days')
    aff_exclude_shipping = fields.Boolean(default=True, string='Exclude shipping')
    aff_block_self = fields.Boolean(default=True, string='Block self-referral')
    aff_review_ip = fields.Boolean(default=True, string='Review matching IPs')
    aff_min_payout = fields.Monetary(string='Minimum payout')
    aff_expense_account_id = fields.Many2one(
        'account.account', check_company=True, string='Commission expense account',
        domain="[('company_id', '=', id), ('deprecated', '=', False), ('account_type', 'in', ('expense', 'expense_direct_cost', 'expense_depreciation'))]")
    aff_purchase_journal_id = fields.Many2one(
        'account.journal', check_company=True, string='Payout purchase journal',
        domain="[('company_id', '=', id), ('type', '=', 'purchase')]")
    aff_terms = fields.Text(string='Program terms')

    @api.constrains('aff_cookie_days', 'aff_repeat_months', 'aff_hold_days', 'aff_discount', 'aff_min_payout')
    def _check_affiliate_config(self):
        for company in self:
            if (not 1 <= company.aff_cookie_days <= 365 or company.aff_repeat_months < 0
                    or company.aff_hold_days < 0 or not 0 <= company.aff_discount <= 100
                    or company.aff_min_payout < 0):
                raise ValidationError(_('Use 1–365 cookie days, 0–100% discount and non-negative periods and amounts.'))


class AffiliateProfile(models.Model):
    _name = 'wa.affiliate'
    _description = 'Website Affiliate'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(related='partner_id.name', store=True)
    partner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='restrict', tracking=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    state = fields.Selection(
        [('pending', 'Application'), ('active', 'Active'), ('rejected', 'Declined'), ('paused', 'Paused')],
        default='pending', required=True, tracking=True, index=True)
    application = fields.Text()
    application_ip = fields.Char(groups='website_affiliate_studio.group_affiliate_user')
    code = fields.Char(required=True, copy=False, readonly=True, index=True, default=lambda self: secrets.token_hex(12))
    program_id = fields.Many2one('loyalty.program', readonly=True, copy=False, check_company=True)
    coupon_code = fields.Char(compute='_compute_coupon')
    share_url = fields.Char(compute='_compute_share_url')
    click_ids = fields.One2many('wa.click', 'affiliate_id')
    entry_ids = fields.One2many('wa.entry', 'affiliate_id')
    credited_order_ids = fields.One2many('sale.order', 'wa_affiliate_id')
    click_count = fields.Integer(compute='_compute_metrics')
    order_count = fields.Integer(compute='_compute_metrics')
    conversion_rate = fields.Float(compute='_compute_metrics')
    earnings = fields.Monetary(compute='_compute_metrics')
    available = fields.Monetary(compute='_compute_metrics')

    _sql_constraints = [
        ('partner_company_unique', 'unique(partner_id, company_id)', 'This contact already has an affiliate application.'),
        ('unique_code', 'unique(code)', 'Referral code must be unique.'),
        ('unique_program', 'unique(program_id)', 'A discount program can only identify one affiliate.'),
    ]

    @api.depends('program_id.rule_ids.code')
    def _compute_coupon(self):
        for affiliate in self:
            affiliate.coupon_code = affiliate.sudo().program_id.rule_ids[:1].code

    @api.depends('click_ids', 'entry_ids.amount', 'entry_ids.state',
                 'credited_order_ids.state', 'credited_order_ids.wa_self_blocked')
    def _compute_metrics(self):
        names = ['click_count', 'order_count', 'conversion_rate', 'earnings', 'available']
        leaderboard = self.env['wa.leaderboard'].sudo()
        leaderboard.browse(self.ids).invalidate_recordset(names, flush=False)
        rows = leaderboard.search_read([('id', 'in', self.ids)], names)
        totals = {row['id']: row for row in rows}
        for affiliate in self:
            row = totals.get(affiliate.id, {})
            for name in names:
                affiliate[name] = row.get(name, 0)

    @api.depends('company_id', 'code')
    def _compute_share_url(self):
        websites = self.env['website'].sudo().search([('company_id', 'in', self.company_id.ids)], order='sequence, id')
        by_company = {}
        for website in websites:
            by_company.setdefault(website.company_id.id, website)
        for affiliate in self:
            website = by_company.get(affiliate.company_id.id)
            affiliate.share_url = website.get_base_url().rstrip('/') + affiliate._share_path() if website else False

    def _share_path(self, target='/shop'):
        self.ensure_one()
        return '/affiliate/r/%s?target=%s' % (self.code, quote(target, safe=''))

    def _lock(self):
        if self:
            self.env.cr.execute('SELECT id FROM wa_affiliate WHERE id IN %s ORDER BY id FOR UPDATE',
                                [tuple(self.ids)])
            self.invalidate_recordset()

    def _require_manager(self):
        self.check_access_rights('read')
        self.check_access_rule('read')
        if not self.env.su and not self.env.user.has_group('website_affiliate_studio.group_affiliate_manager'):
            raise UserError(_('Only affiliate managers can perform this action.'))

    def _approve(self):
        self._lock()
        for affiliate in self.filtered(lambda r: r.state != 'active'):
            affiliate.state = 'active'
            affiliate.program_id.sudo().write({'active': True})
            template = self.env.ref('website_affiliate_studio.mail_affiliate_approved')
            if affiliate.partner_id.email:
                template.sudo().send_mail(affiliate.id, force_send=False)

    def action_approve(self):
        self._require_manager()
        self._approve()

    def action_decline(self):
        self._require_manager()
        self._lock()
        self.write({'state': 'rejected'})
        self.program_id.sudo().write({'active': False})

    def action_pause(self):
        self._require_manager()
        self._lock()
        self.write({'state': 'paused'})
        self.program_id.sudo().write({'active': False})

    def action_create_coupon(self):
        self._require_manager()
        self._lock()
        for affiliate in self:
            if affiliate.state != 'active' or affiliate.program_id:
                continue
            if not affiliate.company_id.aff_discount:
                raise UserError(_('Set the coupon discount in the company settings.'))
            affiliate.program_id = self.env['loyalty.program'].sudo().with_company(affiliate.company_id).create({
                'name': _('Affiliate discount: %s', affiliate.display_name),
                'company_id': affiliate.company_id.id,
                'program_type': 'promo_code',
                'trigger': 'with_code',
                'applies_on': 'current',
                'sale_ok': False,
                'ecommerce_ok': True,
                'rule_ids': [Command.create({
                    'mode': 'with_code', 'code': 'PARTNER-' + affiliate.code[:12].upper(),
                    'minimum_qty': 1, 'reward_point_amount': 1, 'reward_point_mode': 'order',
                })],
                'reward_ids': [Command.create({
                    'reward_type': 'discount', 'discount_mode': 'percent',
                    'discount_applicability': 'order', 'discount': affiliate.company_id.aff_discount,
                    'required_points': 1,
                })],
            })

    def action_batch_payout(self):
        self._require_manager()
        self.env['wa.payout']._batch(self or self.search([
            ('state', '=', 'active'), ('company_id', '=', self.env.company.id),
        ]))
        return {'type': 'ir.actions.act_window', 'res_model': 'wa.payout', 'view_mode': 'tree,form'}


class CommissionRule(models.Model):
    _name = 'wa.rule'
    _description = 'Affiliate Commission Rule'
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    scope = fields.Selection([('all', 'All products'), ('category', 'Product category'), ('product', 'Product')],
                             required=True, default='all')
    categ_id = fields.Many2one('product.category')
    product_id = fields.Many2one('product.product', check_company=True)
    affiliate_ids = fields.Many2many('wa.affiliate', check_company=True)
    rate_type = fields.Selection([('percent', 'Percentage'), ('unit', 'Fixed per unit'), ('order', 'Fixed per order')],
                                 required=True, default='percent')
    percent = fields.Float()
    fixed_amount = fields.Monetary()
    monthly_minimum = fields.Monetary(default=0.0, string='Monthly sales threshold')

    @api.constrains('scope', 'categ_id', 'product_id', 'percent', 'fixed_amount', 'monthly_minimum')
    def _check_rule(self):
        for rule in self:
            if ((rule.scope == 'category' and not rule.categ_id)
                    or (rule.scope == 'product' and not rule.product_id)
                    or not 0 <= rule.percent <= 100 or rule.fixed_amount < 0 or rule.monthly_minimum < 0):
                raise ValidationError(_('Select the required product/category and enter valid rates and thresholds.'))

    def _matches_line(self, line):
        self.ensure_one()
        if self.scope == 'product':
            return self.product_id == line.product_id
        if self.scope == 'category':
            return str(self.categ_id.id) in (line.product_id.categ_id.parent_path or '').split('/')
        return True


class AffiliateClick(models.Model):
    _name = 'wa.click'
    _description = 'Affiliate Referral Click'
    _order = 'create_date desc'

    affiliate_id = fields.Many2one('wa.affiliate', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one(related='affiliate_id.company_id', store=True, index=True)
    website_id = fields.Many2one('website', required=True, ondelete='cascade')
    target = fields.Char(required=True)


class CustomerReferral(models.Model):
    _name = 'wa.customer'
    _description = 'Affiliate Customer Referral'
    _check_company_auto = True

    affiliate_id = fields.Many2one('wa.affiliate', required=True, check_company=True, ondelete='restrict')
    partner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, index=True)
    expires = fields.Date(required=True)

    _sql_constraints = [('referral_unique', 'unique(partner_id, company_id)', 'A customer has one referral per company.')]