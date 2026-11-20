from lxml import html

from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user

from ..models.website import safe_target


@tagged('post_install', '-at_install')
class TestAffiliatePortal(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env['website'].search([], order='id', limit=1)
        cls.company = cls.website.company_id
        cls.company.write({'aff_enabled': True, 'aff_attribution': 'first', 'aff_auto_approve': True})
        cls.portal_user = new_test_user(cls.env, login='was_portal_test', password='was-portal-qa', groups='base.group_portal')
        cls.affiliate_a = cls.env['wa.affiliate'].create({
            'partner_id': cls.env['res.partner'].create({'name': 'Referral A'}).id,
            'company_id': cls.company.id, 'state': 'active',
        })
        cls.affiliate_b = cls.env['wa.affiliate'].create({
            'partner_id': cls.env['res.partner'].create({'name': 'Referral B'}).id,
            'company_id': cls.company.id, 'state': 'active',
        })

    def test_referral_redirect_cookie_and_click(self):
        self.authenticate(None, None)
        name = self.website._wa_cookie_name()
        response = self.url_open(self.affiliate_a._share_path('/shop'), allow_redirects=False, timeout=120)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['Location'].endswith('/shop'))
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertTrue(response.cookies.get(name))
        second = self.url_open(self.affiliate_b._share_path('/%5Cexample.test'), allow_redirects=False, timeout=120)
        self.assertEqual(second.status_code, 303)
        self.assertTrue(second.headers['Location'].endswith('/shop'))
        self.assertFalse(second.cookies.get(name))
        self.assertEqual(self.env['wa.click'].search_count([('affiliate_id', 'in', (self.affiliate_a | self.affiliate_b).ids)]), 2)
        self.assertEqual(safe_target('//outside.test'), '/shop')
        self.assertEqual(safe_target('/shop/category?x=1'), '/shop/category?x=1')
        product = self.env['product.product'].create({
            'name': 'Referral cart product', 'type': 'service', 'list_price': 100, 'is_published': True,
        })
        self.cr.flush()
        cart = self.opener.post(self.base_url() + '/shop/cart/update_json', json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 11,
            'params': {'product_id': product.id, 'add_qty': 1, 'display': False},
        }, timeout=120).json()
        self.assertNotIn('error', cart, cart)
        self.assertEqual(cart['result']['cart_quantity'], 1)
        order = self.env['sale.order'].search([('order_line.product_id', '=', product.id)], limit=1)
        self.assertEqual(order.wa_referral_id, self.affiliate_a)
        self.company.aff_attribution = 'last'
        self.cr.flush()
        newer = self.url_open(self.affiliate_b._share_path(), allow_redirects=False, timeout=120)
        self.assertTrue(newer.cookies.get(name))
        self.opener.post(self.base_url() + '/shop/cart/update_json', json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 12,
            'params': {'product_id': product.id, 'add_qty': 1, 'display': False},
        }, timeout=120)
        order.invalidate_recordset(['wa_referral_id'])
        self.assertEqual(order.wa_referral_id, self.affiliate_b)

    def test_signup_dashboard_and_portal_permissions(self):
        self.authenticate('was_portal_test', 'was-portal-qa')
        response = self.url_open('/affiliate', timeout=120)
        self.assertEqual(response.status_code, 200)
        enrollment_page = html.fromstring(response.content)
        enrollment_image = enrollment_page.xpath('//img[contains(@src,"affiliate-enrollment.jpg")]/@src')[0]
        self.assertEqual(self.url_open(enrollment_image, timeout=120).headers['Content-Type'], 'image/jpeg')
        token = enrollment_page.xpath('//form[@action="/affiliate/apply"]/input[@name="csrf_token"]/@value')[0]
        application = self.url_open('/affiliate/apply', data={'csrf_token': token, 'application': 'Test channel'},
                                    allow_redirects=False, timeout=120)
        self.assertEqual(application.status_code, 303)
        profile = self.env['wa.affiliate'].search([('partner_id', '=', self.portal_user.partner_id.id)])
        self.assertEqual(profile.state, 'active')
        self.env['wa.click'].create({'affiliate_id': profile.id, 'website_id': self.website.id, 'target': '/shop'})
        self.env['wa.click'].flush_model()
        dashboard = self.url_open('/my/affiliate', timeout=120)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(profile.code, dashboard.text)
        self.assertNotIn(self.affiliate_a.code, dashboard.text)
        dashboard_page = html.fromstring(dashboard.content)
        downloads = dashboard_page.xpath('//article[@class="was-banner"]/a[@download]')
        self.assertEqual([link.get('download') for link in downloads], ['referral-banner.png', 'storefront-parcels.jpg'])
        for index, (link, content_type) in enumerate(zip(downloads, ('image/png', 'image/jpeg'))):
            image_response = self.url_open(link.get('href'), timeout=120)
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response.headers['Content-Type'], content_type)
            snippet = dashboard_page.xpath('//textarea[@id="was-snippet-%s"]/text()' % index)[0]
            self.assertIn(profile.code, snippet)
            self.assertIn(link.get('href'), snippet)
        self.assertIn('Affiliate Studio', self.url_open('/my/home', timeout=120).text)
        menu = self.env.ref('website_affiliate_studio.menu_affiliate')
        self.assertTrue(menu.web_icon_data)
        self.assertEqual(self.url_open('/website_affiliate_studio/static/description/icon.png', timeout=120).status_code, 200)
        denied = self.url_open('/my/affiliate/payout', data={'affiliate_id': self.affiliate_a.id}, allow_redirects=False, timeout=120)
        self.assertEqual(denied.status_code, 400)
        payout_token = html.fromstring(dashboard.content).xpath(
            '//form[@action="/my/affiliate/payout"]/input[@name="csrf_token"]/@value')[0]
        response = self.url_open('/my/affiliate/payout',
                                 data={'csrf_token': payout_token, 'affiliate_id': self.affiliate_a.id},
                                 allow_redirects=False, timeout=120)
        self.assertIn('status=unavailable', response.headers['Location'])
        self.assertFalse(self.env['wa.payout'].search([('affiliate_id', 'in', (profile | self.affiliate_a).ids)]))
