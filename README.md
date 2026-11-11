# ProcureFind – Procurement Finder Prototype

A simple internal procurement research prototype built with HTML, CSS, Bootstrap 5 and vanilla JavaScript.

## Run locally

1. Download and extract the project folder.
2. Open the folder in VS Code or your preferred editor.
3. Start a local web server from the project directory. For example, with Python:

   ```bash
    python3 -m http.server 3000
   ```

4. Open `http://localhost:3000/` or `http://localhost:3000/index.html` in your browser.

You can also use the VS Code **Live Server** extension.

## Included features

- Dashboard with summary cards and recent searches
- Product search across 15 sample products
- Category filtering and price/name sorting
- Product detail modal with specifications and supplier information
- Save products and compare up to four products
- Supplier directory
- Search history
- Browser localStorage persistence
- Responsive Bootstrap layout

## Important prototype notes

All product prices, supplier records, contact details and listing URLs are fictional demonstration data. The `example.com`, `example.org` and `example.net` links are placeholders, not actual supplier listings. The prototype does not currently search the live internet.

## Where to integrate APIs

- `services/productService.js`: replace mock search/detail functions with calls to your backend or approved product APIs.
- `services/supplierService.js`: connect supplier directory data.
- `assets/js/data.js`: current mock catalogue and supplier records.
- `ProductService.normalizeProduct()`: map provider-specific responses to the application's standard product structure.

This prototype has no authentication and should not be used to protect sensitive information. For production, do not place private API keys in frontend JavaScript. Create a backend service to call providers securely, integrate company-approved authentication (for example, SSO), normalize results and apply rate limits.

## Suggested next development steps

1. Decide which product data providers or supplier APIs are available.
2. Build a small backend API to protect credentials and normalize provider responses.
3. Add result freshness, currency and shipping/tax fields.
4. Add supplier verification and quotation follow-up workflow.
