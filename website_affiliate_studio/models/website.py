import hashlib
import hmac
import time
from urllib.parse import unquote, urlsplit

from odoo import models
from odoo.http import request


def safe_target(value):
    if not isinstance(value, str) or len(value) > 1024:
        return '/shop'
    decoded = value
    for _attempt in range(3):
        decoded = unquote(decoded)
    if not decoded.startswith('/') or decoded.startswith('//') or any(
            ord(char) < 32 or ord(char) == 127 or char == chr(92) for char in decoded):
        return '/shop'
    path = urlsplit(decoded)
    if path.netloc or path.scheme or path.path.startswith('/affiliate/r/'):
        return '/shop'
    return value


class Website(models.Model):
    _inherit = 'website'

    def _wa_cookie_name(self):
        self.ensure_one()
        return 'was_referral_%s' % self.id

    def _wa_sign(self, value):
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _wa_ip(self):
        address = request.httprequest.remote_addr
        return self._wa_sign('ip:%s' % address) if address else False

    def _wa_pack_cookie(self, affiliate):
        self.ensure_one()
        expiry = int(time.time()) + self.sudo().company_id.aff_cookie_days * 86400
        value = '%s:%s:%s' % (self.id, affiliate.code, expiry)
        return '%s:%s' % (value, self._wa_sign(value))

    def _wa_cookie_affiliate(self):
        self.ensure_one()
        model = self.env['wa.affiliate'].sudo()
        if not self.sudo().company_id.aff_enabled:
            return model
        token = request.httprequest.cookies.get(self._wa_cookie_name(), '')
        try:
            site, code, expiry, signature = token.split(':')
            if int(site) != self.id or int(expiry) <= int(time.time()):
                return model
            if not hmac.compare_digest(signature, self._wa_sign('%s:%s:%s' % (site, code, expiry))):
                return model
        except (TypeError, ValueError):
            return model
        return model.search([
            ('code', '=', code), ('state', '=', 'active'), ('company_id', '=', self.sudo().company_id.id),
        ], limit=1)

    def sale_get_order(self, force_create=False, update_pricelist=False):
        order = super().sale_get_order(force_create=force_create, update_pricelist=update_pricelist)
        if request and order and order.state in ('draft', 'sent') and self.sudo().company_id.aff_enabled:
            affiliate = self._wa_cookie_affiliate()
            fingerprint = self._wa_ip() if self.sudo().company_id.aff_review_ip else False
            if order.sudo().wa_referral_id != affiliate or order.sudo().wa_visitor_ip != fingerprint:
                order.sudo().write({'wa_referral_id': affiliate.id, 'wa_visitor_ip': fingerprint})
        return order