# Website Affiliate Studio for Odoo 16

This module manages affiliate referrals for the Odoo website shop. The website enrollment page is `/affiliate`. Existing affiliates open `/my/affiliate` from their portal account.

## Installation

1. Put the extracted `website_affiliate_studio` directory under an existing custom addons path on Odoo 16. For Odoo.sh, put the directory in the project repository.
2. Restart the instance, update the Apps list, then install **Website Affiliate Studio**. Odoo resolves the standard Website/eCommerce, Discount & Loyalty, Delivery, Accounting and Discuss dependencies.
   If the app is already installed, replace its directory on the custom addons path, restart Odoo, then choose **Upgrade** for the app.
3. In **Affiliate Studio → Configuration → Program settings**, switch to the intended company, enable the program, set the commission trigger, and choose the purchase journal and expense account for payouts. Accounting maintains the affiliate partner's payable account and processes the vendor bills.
4. Add commission rules under **Affiliate Studio → Configuration → Commission rules**. Assign **Affiliate Reviewer** or **Affiliate Manager** under Settings → Users.
5. Link your site's navigation to `/affiliate`. Approve an application and use **Generate discount code** to create an Odoo Discount & Loyalty program. Affiliates can share their referral link as soon as they are active.

The portal offers downloadable promotional banners with ready-to-copy HTML that includes each affiliate's referral link.

## Commission and attribution behavior

- A discount reward from an active affiliate's linked program takes precedence over the referral cookie. An eligible repeat attribution applies when neither of those sources matches. Cookies are signed and scoped to the website. Repeat attribution is scoped to the customer's commercial contact and company.
- Percentage rules use untaxed sales after line discounts and applicable loyalty discount rewards. Shipping can be excluded. Monetary thresholds and fixed rates use company currency.
- Rule priority is affiliate-specific, product, category, then all products. Within the same priority, the highest eligible monthly threshold wins, then sequence and record ID. Monthly volume includes the current order and earlier credited orders from the same month. Earlier commissions keep their original rates. A fixed-per-order rule contributes once and is apportioned across the matching lines.
- The full-payment trigger recognizes completed linked website transactions or fully paid customer invoices that cover the website order. The payment schedule checks paid invoices every 15 minutes. Commission release runs hourly, and vendor bill status is checked every 15 minutes.
- Payout requests reserve the available balance. A manager can create a draft vendor bill for a request or run **Settlements → Create batch vendor bills**. Post and pay the bill from Accounting. The minimum payout is per company and affiliate. Refunds reduce an open payout and its generated draft vendor bill automatically. A full reversal cancels an unbilled request or blocks posting its zero-net draft bill. For posted vendor bills, refund adjustments apply to the next eligible payout.
- Issue customer credit notes from the original invoice so Odoo keeps the link to its sales lines. Credit notes with ambiguous product-to-order-line links require accounting review. Cancelled orders reverse any remaining commission. When a credit note already has affiliate adjustments, the module blocks resetting or cancelling it.
- Attribution is tied to Odoo website orders. Imported marketplace, standard manual sales and POS orders need a separate integration.

## Verification

The addon includes Odoo model and HTTP tests. Use a test database with `l10n_generic_coa` installed. Run the suite with `odoo-bin -d TEST_DB -u website_affiliate_studio --test-enable --test-tags /website_affiliate_studio --stop-after-init`. The HTTP cases use Odoo's local HTTP server.
