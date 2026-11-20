from collections import Counter
from datetime import timedelta

from odoo import _, fields, http
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.http import request

from ..models.website import safe_target


class AffiliatePortal(CustomerPortal):
    def _company(self):
        return request.website.sudo().company_id

    def _profile(self):
        if request.env.user._is_public():
            return request.env['wa.affiliate'].sudo().browse()
        return request.env['wa.affiliate'].sudo().search([
            ('partner_id', '=', request.env.user.partner_id.id),
            ('company_id', '=', self._company().id),
        ], limit=1)

    @http.route('/affiliate/r/<string:code>', type='http', auth='public', website=True, methods=['GET'], sitemap=False)
    def referral(self, code, target='/shop', **kwargs):
        target = safe_target(target)
        website = request.website
        company = self._company()
        affiliate = request.env['wa.affiliate'].sudo().search([
            ('code', '=', code), ('company_id', '=', company.id), ('state', '=', 'active'),
        ], limit=1) if company.aff_enabled else False
        response = request.redirect(target, code=303, local=True)
        response.headers['Cache-Control'] = 'private, no-store'
        if affiliate:
            request.env['wa.click'].sudo().create({
                'affiliate_id': affiliate.id, 'website_id': website.id, 'target': target,
            })
            if company.aff_attribution == 'last' or not website._wa_cookie_affiliate():
                response.set_cookie(
                    website._wa_cookie_name(), website._wa_pack_cookie(affiliate),
                    max_age=company.aff_cookie_days * 86400, httponly=True,
                    secure=request.httprequest.is_secure, samesite='Lax',
                )
        return response

    @http.route('/affiliate', type='http', auth='public', website=True, methods=['GET'])
    def join(self, **kwargs):
        company = self._company()
        if not company.aff_enabled:
            return request.not_found()
        return request.render('website_affiliate_studio.affiliate_landing', {
            'program_terms': company.aff_terms, 'profile': self._profile(),
            'is_public_user': request.env.user._is_public(),
        })

    @http.route('/affiliate/apply', type='http', auth='user', website=True, methods=['POST'], sitemap=False)
    def apply(self, application='', **kwargs):
        company = self._company()
        if not company.aff_enabled:
            return request.not_found()
        partner = request.env.user.partner_id
        request.env.cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', [partner.id])
        profile = self._profile()
        if not profile:
            profile = request.env['wa.affiliate'].sudo().with_company(company).create({
                'company_id': company.id, 'partner_id': partner.id,
                'application': application[:2000],
                'application_ip': request.website._wa_ip() if company.aff_review_ip else False,
            })
            if company.aff_auto_approve:
                profile._approve()
        return request.redirect('/my/affiliate', code=303)

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'wa_affiliate_count' in counters:
            values['wa_affiliate_count'] = int(self._company().aff_enabled and bool(self._profile()))
        return values

    @http.route('/my/affiliate', type='http', auth='user', website=True, methods=['GET'], sitemap=False)
    def dashboard(self, target='/shop', page=1, **kwargs):
        company = self._company()
        if not company.aff_enabled:
            return request.not_found()
        profile = self._profile()
        if not profile:
            return request.redirect('/affiliate')
        try:
            page = max(1, min(5000, int(page)))
        except (TypeError, ValueError):
            page = 1
        domain = [('affiliate_id', '=', profile.id)]
        ledger = request.env['wa.entry'].sudo()
        payouts = request.env['wa.payout'].sudo().search(domain, limit=30)
        today = fields.Date.context_today(profile)
        dates = [today - timedelta(days=offset) for offset in range(13, -1, -1)]
        recent = request.env['wa.click'].sudo().read_group(
            domain + [('create_date', '>=', fields.Datetime.to_datetime(dates[0]))],
            ['create_date'], ['create_date:day'])
        counts = Counter({(item.get('__range', {}).get('create_date:day', {}).get('from') or '')[:10]:
                          item['create_date_count'] for item in recent})
        peak = max(counts.values(), default=1)
        trend = [(day.strftime('%d/%m'), counts[str(day)], (90 * counts[str(day)] / peak if peak else 0))
                 for day in dates]
        target = safe_target(target)
        origin = request.httprequest.host_url.rstrip('/')
        return request.render('website_affiliate_studio.affiliate_dashboard', {
            'profile': profile, 'company': company, 'target': target,
            'share_url': origin + profile._share_path(target),
            'entries': ledger.search(domain, offset=(page - 1) * 20, limit=20),
            'has_next': ledger.search_count(domain, limit=page * 20 + 1) > page * 20,
            'payouts': payouts, 'page': page, 'trend': trend,
            'message': {'requested': _('Payout requested.'), 'unavailable': _('No eligible balance for payout.')}.get(kwargs.get('status')),
            'banner_root': origin + '/website_affiliate_studio/static/src/img/',
            'banners': [
                (_('Explore the shop'), 'referral-banner.png', 600, 150),
                (_('Storefront parcels'), 'storefront-parcels.jpg', 1200, 400),
            ],
        })

    @http.route('/my/affiliate/payout', type='http', auth='user', website=True, methods=['POST'], sitemap=False)
    def request_payout(self, **kwargs):
        profile = self._profile()
        if not self._company().aff_enabled or not profile:
            return request.not_found()
        payout = request.env['wa.payout'].sudo()._reserve(profile, 'portal')
        return request.redirect('/my/affiliate?status=' + ('requested' if payout else 'unavailable'), code=303)
