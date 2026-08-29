"""
Solana keypair management for the bot's hot wallet.

⚠️ SECURITY — read before running anything live:
- wallet.json holds the PRIVATE KEY in plaintext. Anyone with this file can
  drain the wallet completely and irreversibly. It's in .gitignore — keep it
  there, never upload it anywhere, never paste its contents into a chat.
- Treat this as a hot wallet for bot capital only: money you can afford to
  lose entirely. Never reuse a wallet that holds your main funds.
- /export in Telegram prints the key in base58 (for importing into
  Phantom/Solflare). That message travels through Telegram's servers —
  use it for recovery, then consider the wallet compromised-adjacent and
  rotate when convenient.
- There is no recovery if the file is lost or the key leaks.

Run `python -m gftrade.wallet` once to generate the wallet.
"""
import json
import os

import base58
from solders.keypair import Keypair

from . import config


def generate_wallet(path: str = None) -> Keypair:
    path = path or config.WALLET_KEYFILE
    if os.path.exists(path):
        raise FileExistsError(
            f"{path} already exists — refusing to overwrite a wallet. "
            "Move it away manually first if you really want a new one."
        )
    keypair = Keypair()
    with open(path, "w") as f:
        json.dump(list(bytes(keypair)), f)  # standard 64-byte secret-key array format
    os.chmod(path, 0o600)
    return keypair


def import_wallet(base58_secret: str, path: str = None) -> Keypair:
    """Import a wallet exported from Phantom/Solflare (base58 of the 64-byte secret)."""
    path = path or config.WALLET_KEYFILE
    if os.path.exists(path):
        raise FileExistsError(f"{path} already exists — refusing to overwrite it.")
    keypair = Keypair.from_base58_string(base58_secret.strip())
    with open(path, "w") as f:
        json.dump(list(bytes(keypair)), f)
    os.chmod(path, 0o600)
    return keypair


def load_wallet(path: str = None) -> Keypair:
    path = path or config.WALLET_KEYFILE
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No wallet found at {path}. Run `python -m gftrade.wallet` to generate one."
        )
    with open(path) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


def export_base58(keypair: Keypair) -> str:
    """Base58 of the full 64-byte secret key — the format wallet apps import."""
    return base58.b58encode(bytes(keypair)).decode()


if __name__ == "__main__":
    kp = generate_wallet()
    print(f"New wallet created: {kp.pubkey()}")
    print(f"Secret key saved to {config.WALLET_KEYFILE} (mode 600).")
    print("Back it up somewhere safe and offline, then fund the address above with a")
    print("small amount of SOL. This file must never be committed or shared.")
