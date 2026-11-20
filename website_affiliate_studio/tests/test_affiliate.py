from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestWebsiteAffiliate(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(su=True)
        cls.company = cls.company_data['company']
        cls.company.write({
            'aff_enabled': True, 'aff_trigger': 'confirmed', 'aff_hold_days': 0,
            'aff_min_payout': 1, 'aff_repeat_months': 3, 'aff_discount': 10,
            'aff_expense_account_id': cls.company_data['default_account_expense'].id,
            'aff_purchase_journal_id': cls.company_data['default_journal_purchase'].id,
        })
        cls.website = cls.env['website'].create({'name': 'Affiliate test shop', 'company_id': cls.company.id})
        cls.general = cls.env['wa.rule'].create({
            'name': 'Base rate', 'percent': 10, 'company_id': cls.company.id,
        })
        cls.customer = cls.env['res.partner'].create({'name': 'Buyer'})
        cls.product_a.write({'invoice_policy': 'order', 'type': 'service', 'taxes_id': [Command.clear()]})
        cls.product_b.write({'invoice_policy': 'order', 'type': 'service', 'taxes_id': [Command.clear()]})
        cls.affiliate = cls.env['wa.affiliate'].create({
            'partner_id': cls.env['res.partner'].create({'name': 'Partner A'}).id,
            'company_id': cls.company.id, 'state': 'active',
        })
        cls.affiliate_b = cls.env['wa.affiliate'].create({
            'partner_id': cls.env['res.partner'].create({'name': 'Partner B'}).id,
            'company_id': cls.company.id, 'state': 'active',
        })
        cls.entries = cls.env['wa.entry']

    def _order(self, affiliate=None, customer=None, price=100, count=1, **extra):
        return self.env['sale.order'].with_company(self.company).create(dict({
            'partner_id': (customer or self.customer).id,
            'website_id': self.website.id,
            'company_id': self.company.id,
            'wa_referral_id': affiliate.id if affiliate else False,
            'order_line': [Command.create({
                'product_id': self.product_a.id, 'price_unit': price, 'product_uom_qty': count,
                'tax_id': [Command.clear()],
            })],
        }, **extra))

    def _entries(self, order):
        return self.entries.search([('order_id', '=', order.id)])

    def _pay(self, document):
        journal = self.company_data['default_journal_bank']
        pay_lines = journal.inbound_payment_method_line_ids | journal.outbound_payment_method_line_ids
        pay_lines.payment_account_id = journal.default_account_id
        self.env['account.payment.register'].with_company(self.company).with_context(
            active_model='account.move', active_ids=document.ids,
        ).create({'journal_id': journal.id})._create_payments()
        self.assertEqual(document.payment_state, 'paid')

    def test_coupon_overrides_referral_and_adjusts_base(self):
        self.affiliate_b.action_create_coupon()
        self.assertTrue(self.affiliate_b.coupon_code)
        order = self._order(self.affiliate)
        reward_result = order._try_apply_code(self.affiliate_b.coupon_code)
        self.assertNotIn('error', reward_result)
        for card, rewards in reward_result.items():
            self.assertFalse(order._apply_program_reward(rewards[:1], card).get('error'))
        self.assertEqual(order.amount_untaxed, 90)
        order.action_confirm()
        entry = self._entries(order)
        self.assertTrue(entry, (order.state, order.wa_processed, order.wa_affiliate_id.id,
                                order.wa_volume, order._wa_line_bases()[1]))
        self.assertEqual(order.wa_source, 'coupon')
        self.assertEqual(entry.affiliate_id, self.affiliate_b)
        self.assertEqual(entry.base_amount, 90)
        self.assertEqual(entry.amount, 9)
        board = self.env['wa.leaderboard'].browse(self.affiliate_b.id)
        self.assertEqual(board.earnings, 9)
        self.assertEqual(board.order_count, 1)
        self.assertEqual(self.affiliate_b.available, 9)
        order._wa_create_commissions()
        self.assertEqual(self._entries(order), entry)

    def test_holding_release_and_returning_customer(self):
        self.company.aff_hold_days = 14
        first = self._order(self.affiliate)
        first.action_confirm()
        entry = self._entries(first)
        self.assertEqual(entry.state, 'holding')
        entry.write({'ready_date': fields.Date.today() - timedelta(days=1)})
        self.entries._release_due()
        self.assertEqual(entry.state, 'payable')
        second = self._order()
        second.action_confirm()
        self.assertEqual(second.wa_source, 'repeat')
        self.assertEqual(self._entries(second).affiliate_id, self.affiliate)

    def test_self_referral_and_same_ip_review(self):
        self.affiliate.application_ip = 'isolation-test-ip'
        self_referral = self._order(self.affiliate, self.affiliate.partner_id)
        self_referral.action_confirm()
        self.assertTrue(self_referral.wa_self_blocked)
        self.assertFalse(self._entries(self_referral))
        same_ip = self._order(self.affiliate, wa_visitor_ip='isolation-test-ip')
        same_ip.action_confirm()
        self.assertEqual(self._entries(same_ip).state, 'review')
        self._entries(same_ip).action_accept_review()
        self.assertEqual(self._entries(same_ip).state, 'payable')

    def test_monthly_tier_and_fixed_order_rule(self):
        self.env['wa.rule'].create({
            'name': 'Volume rate', 'percent': 15, 'monthly_minimum': 150, 'company_id': self.company.id,
        })
        for expected in (10, 15):
            order = self._order(self.affiliate)
            order.action_confirm()
            self.assertEqual(self._entries(order).amount, expected)
        self.env['wa.rule'].create({
            'name': 'Partner B flat rate', 'rate_type': 'order', 'fixed_amount': 25.01,
            'company_id': self.company.id, 'affiliate_ids': [Command.link(self.affiliate_b.id)],
        })
        order = self._order(self.affiliate_b, order_line=[Command.create({
            'product_id': product.id, 'price_unit': 100, 'product_uom_qty': 1, 'tax_id': [Command.clear()],
        }) for product in (self.product_a, self.product_b)])
        order.action_confirm()
        self.assertEqual(len(self._entries(order)), 2)
        self.assertAlmostEqual(sum(self._entries(order).mapped('amount')), 25.01, places=2)

    def test_paid_trigger_batch_payout_and_partial_refund(self):
        self.company.aff_trigger = 'paid'
        order = self._order(self.affiliate)
        order.action_confirm()
        self.assertFalse(self._entries(order))
        invoice = order._create_invoices()
        invoice.action_post()
        self._pay(invoice)
        order._cron_affiliate_payments()
        original = self._entries(order)
        self.assertEqual(original.amount, 10)
        payout = self.env['wa.payout']._batch(self.affiliate)
        self.assertEqual(payout.state, 'billed')
        self.assertEqual(payout.bill_id.amount_total, 10)
        self.assertFalse(self.env['wa.payout']._reserve(self.affiliate, 'portal'))
        payout.bill_id.action_post()
        self._pay(payout.bill_id)
        payout._cron_sync_payouts()
        self.assertEqual(payout.state, 'paid')
        refund = invoice._reverse_moves(cancel=False)
        refund.invoice_line_ids.price_unit = 40
        refund.action_post()
        adjustment = original.adjustment_ids
        self.assertEqual(adjustment.amount, -4)
        self.assertEqual(adjustment.state, 'payable')
        refund._wa_apply_credit()
        self.assertEqual(original.adjustment_ids, adjustment)
        self.company.aff_trigger = 'confirmed'
        next_order = self._order(self.affiliate)
        next_order.action_confirm()
        next_payout = self.env['wa.payout']._reserve(self.affiliate, 'portal')
        self.assertEqual(next_payout.amount, 6)
        order._action_cancel()
        self.assertEqual(sum(original.adjustment_ids.mapped('amount')), -10)
        order._wa_reverse_orders()
        self.assertEqual(len(original.adjustment_ids), 2)

    def _refund_sale(self, order, amount):
        invoice = order._create_invoices()
        invoice.action_post()
        refund = invoice._reverse_moves(cancel=False)
        refund.invoice_line_ids.price_unit = amount
        return refund

    def test_refund_reduces_draft_settlement_and_preserves_ledger(self):
        order = self._order(self.affiliate)
        order.action_confirm()
        payout = self.env['wa.payout']._batch(self.affiliate)
        refund = self._refund_sale(order, 40)
        refund.action_post()
        self.assertEqual(payout.amount, 6)
        self.assertEqual(payout.bill_id.amount_total, 6)
        self.assertEqual(self.affiliate.available, 0)
        adjustments = payout.entry_ids.filtered('original_id')
        self.assertEqual(adjustments.amount, -4)
        self.assertEqual(adjustments.state, 'reserved')
        refund._wa_apply_credit()
        self.assertEqual(payout.entry_ids.filtered('original_id'), adjustments)
        payout.bill_id.action_post()
        self._pay(payout.bill_id)
        payout._cron_sync_payouts()
        self.assertEqual(payout.state, 'paid')
        self.assertEqual(set(payout.entry_ids.mapped('state')), {'paid'})

    def test_full_reversal_cancels_unbilled_request(self):
        order = self._order(self.affiliate)
        order.action_confirm()
        payout = self.env['wa.payout']._reserve(self.affiliate, 'portal')
        self._refund_sale(order, 100).action_post()
        self.assertEqual(payout.state, 'cancelled')
        self.assertEqual(payout.amount, 0)
        self.assertFalse(payout.entry_ids)
        self.assertEqual(self.affiliate.available, 0)
        payout.action_create_bill()
        self.assertFalse(payout.bill_id)

    def test_zero_net_or_edited_draft_cannot_be_posted_for_wrong_amount(self):
        order = self._order(self.affiliate)
        order.action_confirm()
        payout = self.env['wa.payout']._batch(self.affiliate)
        payout.bill_id.invoice_line_ids.price_unit = 12
        self._refund_sale(order, 100).action_post()
        self.assertEqual(payout.amount, 0)
        self.assertEqual(payout.bill_id.amount_total, 12)
        with self.assertRaisesRegex(UserError, 'net amount'):
            payout.bill_id.action_post()
        self.assertEqual(payout.bill_id.state, 'draft')
        payout.bill_id.button_cancel()
        payout.action_cancel()
        self.assertEqual(self.affiliate.available, 0)

    def test_existing_pending_refund_is_included_when_posting_draft(self):
        order = self._order(self.affiliate)
        order.action_confirm()
        payout = self.env['wa.payout']._batch(self.affiliate)
        refund = self._refund_sale(order, 40)
        with patch.object(type(payout), '_apply_pending_refunds'):
            refund.action_post()
        self.assertEqual(payout.bill_id.amount_total, 10)
        self.assertEqual(self.affiliate.available, -4)
        payout.bill_id.action_post()
        self.assertEqual(payout.amount, 6)
        self.assertEqual(payout.bill_id.amount_total, 6)
        self.assertEqual(payout.bill_id.state, 'posted')
        self.assertEqual(self.affiliate.available, 0)

    def test_portal_payout_reservation_and_reviewer_permissions(self):
        order = self._order(self.affiliate)
        order.action_confirm()
        payout = self.env['wa.payout']._reserve(self.affiliate, 'portal')
        self.assertEqual(payout.amount, 10)
        self.assertEqual(self._entries(order).state, 'reserved')
        payout.action_cancel()
        self.assertEqual(self._entries(order).state, 'payable')
        reviewer = new_test_user(self.env, login='affiliate_reviewer_test',
                                 groups='website_affiliate_studio.group_affiliate_user',
                                 company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        with self.assertRaises(AccessError):
            self._entries(order).with_user(reviewer).write({'amount': 999})
        other_company = self.company_data_2['company']
        foreign_profile = self.env['wa.affiliate'].create({
            'partner_id': self.affiliate.partner_id.id, 'company_id': other_company.id, 'state': 'active',
        })
        self.assertNotIn(foreign_profile.id, self.env['wa.affiliate'].with_user(reviewer).with_context(
            allowed_company_ids=self.company.ids).search([]).ids)
