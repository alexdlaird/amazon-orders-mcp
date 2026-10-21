# Changelog

All notable changes to this project will be documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.4.2...HEAD)

### Added

- A `search_orders` tool, to find orders by product title, order number, address or recipient across every year, as the search box on Your Orders does. It returns a batch of matching items at a time with a `cursor`, like `get_order_history`.

## [1.4.2](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.4.1...1.4.2) - 2026-10-21

### Fixed

- Bug fixes and stability improvements.

## [1.4.1](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.4.0...1.4.1) - 2026-10-21

### Added

- `--transport streamable-http`, to serve remote MCP clients over HTTP (a bearer token is required off loopback).
- `site` on every tool (e.g. `amazon.de`), to read several Amazon sites from one server.
- A `login` target in the `Dockerfile`, to sign in from a container.

### Changed

- The Docker image runs as a non-root user, with its config in `/home/app/.config/amazonorders`.

### Fixed

- Bug fixes and stability improvements.

## [1.4.0](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.3.1...1.4.0) - 2026-10-21

### Added

- A `get_invoice_pdfs` tool, for an order's invoice and credit note PDFs, e.g. the VAT invoices on EU Amazon sites.
- A `get_prime_payments` tool, for the Prime membership payment history.

### Fixed

- Bug fixes and stability improvements.

## [1.3.1](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.3.0...1.3.1) - 2026-10-19

### Added

- A `get_family_members` tool, and `family_member_id` on `get_order_history`, for the orders of Amazon Household members, like linked Teen accounts.

## [1.3.0](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.2.0...1.3.0) - 2026-10-18

### Added

- A `get_tracking` tool, for a shipment's carrier and tracking number.
- A `get_gift_card_balance` tool, for the current Gift Card balance.
- `order_filter` on `get_order_history`, e.g. `digital-orders` for Kindle and other digital purchases.

## [1.2.0](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.1.0...1.2.0) - 2026-10-15

- Bug fixes and stability improvements.

## [1.1.0](https://github.com/alexdlaird/amazon-orders-mcp/compare/1.0.0...1.1.0) - 2026-10-14

### Added

- A `sign_in` tool, to sign in to Amazon from your assistant without a terminal.
- `get_config` and `update_config` tools, to change settings like the Amazon site from your assistant.
- Order history progress shows the overall total when known.

## [1.0.0](https://github.com/alexdlaird/amazon-orders-mcp/releases/tag/1.0.0) - 2026-10-14

- First release of `amazon-orders-mcp`.
