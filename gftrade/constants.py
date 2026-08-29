"""Chain-level constants that never change at runtime."""

CHAIN_ID = "solana"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# The classic SPL Token program. Mints owned by any other program (notably
# Token-2022, TokenzQd...) can carry extensions — transfer hooks, transfer
# fees, permanent delegates — that create sell-traps invisible to the
# freeze/mint-authority checks, so the safety screen treats non-standard
# token programs as known-bad.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

LAMPORTS_PER_SOL = 1_000_000_000

DEXSCREENER_PAIR_URL = "https://dexscreener.com/{chain}/{pair}"
SOLSCAN_TX_URL = "https://solscan.io/tx/{sig}"
SOLSCAN_TOKEN_URL = "https://solscan.io/token/{mint}"
