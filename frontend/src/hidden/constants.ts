// Per-symbol lot size now travels with each option-chain response (lot_size field).
// The legacy NIFTY-only constant lived here; symbol-aware code reads response.lot_size instead.
export {};
