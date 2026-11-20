from odoo import fields, models, tools


class AffiliateLeaderboard(models.Model):
    _name = 'wa.leaderboard'
    _description = 'Affiliate Leaderboard'
    _rec_name = 'affiliate_id'
    _auto = False
    _order = 'earnings desc, order_count desc, id'
    _depends = {
        'wa.affiliate': ['state', 'company_id'],
        'wa.click': ['affiliate_id'],
        'wa.entry': ['affiliate_id', 'state', 'amount'],
        'sale.order': ['wa_affiliate_id', 'wa_self_blocked', 'state'],
        'res.company': ['currency_id'],
    }

    affiliate_id = fields.Many2one('wa.affiliate', readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    state = fields.Selection(related='affiliate_id.state')
    click_count = fields.Integer(readonly=True)
    order_count = fields.Integer(readonly=True)
    conversion_rate = fields.Float(readonly=True, group_operator='avg')
    earnings = fields.Monetary(readonly=True)
    available = fields.Monetary(readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, 'wa_leaderboard')
        self.env.cr.execute("""
            CREATE VIEW wa_leaderboard AS
            SELECT
                affiliate.id,
                affiliate.id AS affiliate_id,
                affiliate.company_id,
                company.currency_id,
                COALESCE(clicks.total, 0) AS click_count,
                COALESCE(orders.total, 0) AS order_count,
                CASE WHEN clicks.total > 0
                     THEN COALESCE(orders.total, 0) * 100.0 / clicks.total
                     ELSE 0 END AS conversion_rate,
                COALESCE(commissions.earnings, 0) AS earnings,
                COALESCE(commissions.available, 0) AS available
            FROM wa_affiliate affiliate
            JOIN res_company company ON company.id = affiliate.company_id
            LEFT JOIN (
                SELECT affiliate_id, COUNT(*) AS total
                FROM wa_click GROUP BY affiliate_id
            ) clicks ON clicks.affiliate_id = affiliate.id
            LEFT JOIN (
                SELECT wa_affiliate_id, COUNT(*) AS total
                FROM sale_order
                WHERE wa_affiliate_id IS NOT NULL AND NOT wa_self_blocked AND state IN ('sale', 'done')
                GROUP BY wa_affiliate_id
            ) orders ON orders.wa_affiliate_id = affiliate.id
            LEFT JOIN (
                SELECT affiliate_id,
                       SUM(amount) FILTER (WHERE state IN ('holding', 'payable', 'reserved', 'paid')) AS earnings,
                       SUM(amount) FILTER (WHERE state = 'payable') AS available
                FROM wa_entry
                GROUP BY affiliate_id
            ) commissions ON commissions.affiliate_id = affiliate.id
        """)