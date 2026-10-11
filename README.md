# Flipkart Discount Tracker — Railway-ready

A best-effort tracker for publicly visible Flipkart listing prices with Discord webhook notifications and SQLite history.

## Deploy to Railway

1. Create a private GitHub repository and upload these project files. Do **not** commit `.env` or a webhook URL.
2. In Railway, choose **New Project → Deploy from GitHub Repo** and select the repository.
3. Railway should detect the included `Dockerfile` and `railway.json`.
4. In the Railway service's **Variables** tab, add:
   - `DISCORD_WEBHOOK_URL` = your Discord channel webhook URL
   - `CHECK_INTERVAL_SECONDS` = `900`
   - `TRACKER_DATA_DIR` = `/data`
   - `PRODUCT_URLS` = your Flipkart URLs, one per line, or separated by semicolons
5. In the service's **Volumes** section, create a persistent volume and mount it at `/data`.
6. Redeploy the service. Open **Deployments → Logs** and confirm it says `Tracker started` and reports the product count.

### Example `PRODUCT_URLS`

Paste full public product URLs into the variable, separated by newlines or semicolons:

```text
https://www.flipkart.com/product-one/p/itm123456
https://www.flipkart.com/product-two/p/itm987654
```

The app also reads `/data/products.txt`, but using the `PRODUCT_URLS` variable is easiest on Railway. It imports new links automatically during subsequent cycles. The maximum is 100 unique products.

## Discord webhook

Create a webhook from the target Discord channel's **Edit Channel → Integrations → Webhooks** settings. Store the URL only in Railway Variables. Treat it as a secret; regenerate it if exposed.

## Persistence and cadence

- Mount the Railway volume at exactly `/data`. The SQLite database is `/data/tracker.sqlite3`.
- Checks run every 15 minutes by default. The program enforces a minimum interval of 15 minutes.
- First successful check saves a baseline without alerting. Later detected price drops and displayed discount changes generate Discord messages.
- Back up the Railway volume/database periodically if you need long-term history.

## Safe-access behavior and limitations

- This is not an official Flipkart API. Page markup and price extraction can change; some products may return `price_unavailable`.
- It reports only a reliably parsed listing price and displayed MRP. Bank offers, coupons, exchange values, account-specific offers, and cart-only prices may not be detectable.
- The bot does not log in, bypass CAPTCHAs, evade rate limits, rotate IP addresses, or defeat access controls.
- HTTP 401/403/429/503 or detected challenge pages cause a six-hour pause for that product. If restrictions persist, stop the service and use an authorized data source rather than trying to bypass them.
- Use only public product pages and comply with applicable Flipkart terms.

## Troubleshooting

- **No alerts on initial deployment:** expected; the first check records a baseline.
- **0 products:** verify `PRODUCT_URLS` in Railway Variables and redeploy/restart the service.
- **Blocked or no price:** inspect deployment logs; the site may restrict requests or have changed its markup.
- **Database resets after redeploy:** verify the Railway volume is mounted at `/data`.
- **Discord alert errors:** check that the webhook is valid and has not been deleted.
