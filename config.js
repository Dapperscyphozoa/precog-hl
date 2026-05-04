// PRECOG v2 — config (env vars take precedence; this is a fallback shim)
'use strict';
module.exports = {
  WALLET_ADDRESS: process.env.HYPERLIQUID_ACCOUNT || '',
  PRIVATE_KEY:    process.env.HL_PRIVATE_KEY || process.env.HYPERLIQUID_API_KEY || '',
};
