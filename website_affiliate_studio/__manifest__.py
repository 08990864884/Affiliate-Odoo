{
    "name": "Website Affiliate Studio",
    "summary": "Website referrals, commissions and affiliate settlements",
    "version": "16.0.1.1.0",
    "category": "Website/eCommerce",
    "license": "LGPL-3",
    "depends": ["website_sale_loyalty", "delivery", "account", "mail"],
    "data": [
        "security/affiliate_security.xml",
        "security/ir.model.access.csv",
        "data/affiliate_data.xml",
        "views/affiliate_views.xml",
        "views/portal_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": ["website_affiliate_studio/static/src/css/affiliate.css"],
    },
    "application": True,
    "installable": True,
}