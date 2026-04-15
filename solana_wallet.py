#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║          SOLANA SMART MONEY TRACKER — Copy-Trade Bot         ║
║  Analyzes wallets for consistent profit & copy-trade signals ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install requests pandas

HOW TO GET A FREE RPC & API KEY:
  - Helius RPC (recommended): https://helius.dev  → free tier = 100k req/day
  - Set HELIUS_API_KEY below with your key
  - Or use any public Solana RPC (rate-limited, slower)

USAGE:
  python solana_wallet_analyzer.py

WHAT IT DOES:
  1. Loads a list of seed wallet addresses to analyze
  2. Fetches their transaction history via Solana RPC
  3. Calculates PnL, win-rate, avg profit per trade
  4. Ranks wallets by profitability score
  5. Saves results to CSV + prints top wallets
  6. Monitors top wallets live for new trades to copy
"""

import requests
import json
import time
import csv
import os
import sys
import logging
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional
import pandas as pd

# ─────────────────────────────────────────────
#  CONFIGURATION  ← Edit these settings
# ─────────────────────────────────────────────

# Get a free key at https://helius.dev
HELIUS_API_KEY = "9a15a595-ce42-4420-b97f-a1b356466c4f"

# RPC endpoint — Helius gives best rate limits for free tier
# Fallback public RPC (very slow/rate-limited):
#   "https://api.mainnet-beta.solana.com"
RPC_ENDPOINT = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# How many wallets to analyze (1 to 10000)
MAX_WALLETS = 100  # Start with 100, scale up once working

# Number of recent transactions to analyze per wallet
TXNS_PER_WALLET = 50

# Minimum win-rate to be considered a "smart money" wallet (0.0 to 1.0)
MIN_WIN_RATE = 0.55  # 55%

# Minimum number of trades (filters out lucky one-off wallets)
MIN_TRADES = 10

# How many top wallets to monitor for copy-trading
TOP_WALLETS_TO_MONITOR = 10

# Delay between RPC calls to avoid rate limiting (seconds)
RPC_DELAY = 0.1

# Output files
OUTPUT_CSV = "wallet_analysis_results.csv"
COPY_TRADE_LOG = "copy_trade_signals.csv"

# ─────────────────────────────────────────────
#  SEED WALLETS  ← Add known active traders here
#  These are starting points; the bot will also
#  discover new wallets from their interactions.
# ─────────────────────────────────────────────

SEED_WALLETS = [
    # Add Solana wallet addresses here (base58 strings, 32–44 chars)
    # Example known active DeFi addresses (replace with real ones):
    "4ZJhPQAgUseCsWhKvJLTmmRRUV74fdoTpQLNfKoekbPY",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49",
    "H4yiPhdSsmSMJTznXzmZvdqWuhxDRzzkoQMEWXZ6agFZ",
    "zvYPtfpDXwEE46C3NeZrKV5SHA416BiK2YabQTceQ8X",
    "G62LeCBehaarj5iVh58s7QTC61upEJiJhuK3BCQ2GqW6"# SOL mint (example)
    # Add more addresses...
    # TIP: Find active wallets on https://solscan.io/leaderboard
    #      or https://birdeye.so/find-gems
]

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("analyzer.log"),
    ]
)
log = logging.getLogger("SolanaBot")

# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class Trade:
    signature: str
    timestamp: int
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    pnl_sol: float          # Profit/loss in SOL
    pnl_usd: float          # Profit/loss in USD
    is_profitable: bool

@dataclass
class WalletStats:
    address: str
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl_sol: float = 0.0
    total_pnl_usd: float = 0.0
    avg_pnl_per_trade_usd: float = 0.0
    win_rate: float = 0.0
    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0
    consistency_score: float = 0.0   # Our composite ranking score
    first_seen: str = ""
    last_active: str = ""
    trades: list = field(default_factory=list, repr=False)

# ─────────────────────────────────────────────
#  SOLANA RPC CLIENT
# ─────────────────────────────────────────────

class SolanaRPC:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _call(self, method: str, params: list, retries: int = 3) -> Optional[dict]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        for attempt in range(retries):
            try:
                resp = self.session.post(self.endpoint, json=payload, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    log.warning(f"RPC error for {method}: {data['error']}")
                    return None
                return data.get("result")
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                log.warning(f"RPC call failed (attempt {attempt+1}/{retries}): {e}. Retrying in {wait}s...")
                time.sleep(wait)
        return None

    def get_signatures_for_address(self, address: str, limit: int = 50) -> list:
        """Get recent transaction signatures for a wallet."""
        result = self._call("getSignaturesForAddress", [
            address,
            {"limit": limit, "commitment": "confirmed"}
        ])
        return result or []

    def get_transaction(self, signature: str) -> Optional[dict]:
        """Get full transaction details."""
        result = self._call("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ])
        return result

    def get_account_balance(self, address: str) -> float:
        """Get SOL balance of an address (in SOL)."""
        result = self._call("getBalance", [address])
        if result and "value" in result:
            return result["value"] / 1e9  # lamports → SOL
        return 0.0

    def get_token_accounts(self, address: str) -> list:
        """Get all SPL token accounts for a wallet."""
        result = self._call("getTokenAccountsByOwner", [
            address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ])
        if result and "value" in result:
            return result["value"]
        return []

# ─────────────────────────────────────────────
#  PRICE FEED (CoinGecko free API)
# ─────────────────────────────────────────────

class PriceFeed:
    def __init__(self):
        self._cache = {}
        self._sol_price = None
        self._last_fetch = 0

    def get_sol_price_usd(self) -> float:
        """Fetch current SOL price in USD."""
        now = time.time()
        if self._sol_price and now - self._last_fetch < 60:
            return self._sol_price
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=10
            )
            data = resp.json()
            self._sol_price = data["solana"]["usd"]
            self._last_fetch = now
            return self._sol_price
        except Exception as e:
            log.warning(f"Could not fetch SOL price: {e}")
            return self._sol_price or 150.0  # Fallback price

# ─────────────────────────────────────────────
#  TRANSACTION PARSER
# ─────────────────────────────────────────────

class TxParser:
    """Parses Solana transactions to extract swap/trade data."""

    # Known DEX program IDs
    DEX_PROGRAMS = {
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3sFjno":  "Orca Whirlpool",
        "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium CPMM",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    }

    def is_dex_transaction(self, tx: dict) -> bool:
        """Check if transaction involves a known DEX."""
        try:
            accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            for account in accounts:
                key = account if isinstance(account, str) else account.get("pubkey", "")
                if key in self.DEX_PROGRAMS:
                    return True
        except Exception:
            pass
        return False

    def extract_sol_change(self, tx: dict, wallet: str) -> float:
        """
        Extract net SOL change for a wallet from a transaction.
        Returns change in SOL (positive = received, negative = sent).
        """
        try:
            pre_balances = tx.get("meta", {}).get("preBalances", [])
            post_balances = tx.get("meta", {}).get("postBalances", [])
            accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])

            for i, account in enumerate(accounts):
                key = account if isinstance(account, str) else account.get("pubkey", "")
                if key == wallet and i < len(pre_balances) and i < len(post_balances):
                    # Net change excluding fees
                    fee = tx.get("meta", {}).get("fee", 0) if i == 0 else 0
                    change = (post_balances[i] - pre_balances[i] + fee) / 1e9
                    return change
        except Exception as e:
            log.debug(f"Error extracting SOL change: {e}")
        return 0.0

    def parse_trade(self, tx: dict, wallet: str, signature: str) -> Optional[Trade]:
        """Parse a transaction into a Trade object."""
        if not tx or not self.is_dex_transaction(tx):
            return None

        block_time = tx.get("blockTime", 0)
        sol_change = self.extract_sol_change(tx, wallet)

        # Skip dust transactions
        if abs(sol_change) < 0.001:
            return None

        return Trade(
            signature=signature,
            timestamp=block_time,
            token_in="SOL" if sol_change < 0 else "TOKEN",
            token_out="TOKEN" if sol_change < 0 else "SOL",
            amount_in=abs(sol_change) if sol_change < 0 else 0,
            amount_out=abs(sol_change) if sol_change > 0 else 0,
            pnl_sol=sol_change,
            pnl_usd=0.0,  # Will be calculated with price
            is_profitable=sol_change > 0
        )

# ─────────────────────────────────────────────
#  WALLET ANALYZER
# ─────────────────────────────────────────────

class WalletAnalyzer:
    def __init__(self):
        self.rpc = SolanaRPC(RPC_ENDPOINT)
        self.price = PriceFeed()
        self.parser = TxParser()

    def analyze_wallet(self, address: str) -> WalletStats:
        """Fully analyze a single wallet — fetch txns, parse trades, compute stats."""
        stats = WalletStats(address=address)
        log.info(f"Analyzing wallet: {address[:8]}...{address[-4:]}")

        # Get recent transactions
        signatures = self.rpc.get_signatures_for_address(address, limit=TXNS_PER_WALLET)
        if not signatures:
            log.debug(f"No transactions found for {address}")
            return stats

        sol_price = self.price.get_sol_price_usd()
        trades = []

        for sig_info in signatures:
            sig = sig_info.get("signature", "")
            if not sig:
                continue

            # Skip failed transactions
            if sig_info.get("err"):
                continue

            tx = self.rpc.get_transaction(sig)
            time.sleep(RPC_DELAY)

            trade = self.parser.parse_trade(tx, address, sig)
            if trade:
                trade.pnl_usd = trade.pnl_sol * sol_price
                trades.append(trade)

        if not trades:
            return stats

        # Compute stats
        stats.trades = trades
        stats.total_trades = len(trades)
        stats.winning_trades = sum(1 for t in trades if t.is_profitable)
        stats.win_rate = stats.winning_trades / stats.total_trades
        stats.total_pnl_sol = sum(t.pnl_sol for t in trades)
        stats.total_pnl_usd = sum(t.pnl_usd for t in trades)
        stats.avg_pnl_per_trade_usd = stats.total_pnl_usd / stats.total_trades
        stats.best_trade_usd = max(t.pnl_usd for t in trades)
        stats.worst_trade_usd = min(t.pnl_usd for t in trades)

        # Consistency score: weighted blend of win-rate, avg pnl, total pnl
        # Higher = better candidate for copy-trading
        if stats.total_trades >= MIN_TRADES:
            stats.consistency_score = (
                (stats.win_rate * 40) +
                (min(stats.avg_pnl_per_trade_usd / 10, 30)) +
                (min(stats.total_pnl_usd / 100, 30))
            )

        # Timestamps
        times = sorted([t.timestamp for t in trades])
        if times:
            stats.first_seen = datetime.datetime.utcfromtimestamp(times[0]).strftime("%Y-%m-%d")
            stats.last_active = datetime.datetime.utcfromtimestamp(times[-1]).strftime("%Y-%m-%d")

        return stats

    def analyze_batch(self, addresses: list) -> list:
        """Analyze multiple wallets concurrently (4 threads to respect rate limits)."""
        results = []
        total = len(addresses)
        log.info(f"Starting analysis of {total} wallets...")

        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_addr = {executor.submit(self.analyze_wallet, addr): addr for addr in addresses}
            done = 0
            for future in as_completed(future_to_addr):
                addr = future_to_addr[future]
                done += 1
                try:
                    stats = future.result()
                    results.append(stats)
                    pct = (done / total) * 100
                    print(f"\r  Progress: {done}/{total} ({pct:.0f}%)  |  "
                          f"Current: {addr[:8]}...  |  "
                          f"Trades found: {stats.total_trades}  |  "
                          f"Win-rate: {stats.win_rate:.0%}  ",
                          end="", flush=True)
                except Exception as e:
                    log.error(f"Error analyzing {addr}: {e}")

        print()  # newline after progress
        return results

# ─────────────────────────────────────────────
#  WALLET DISCOVERY
# ─────────────────────────────────────────────

class WalletDiscovery:
    """Discovers new active wallets by crawling transaction interactions."""

    def __init__(self, rpc: SolanaRPC):
        self.rpc = rpc

    def discover_from_seed(self, seed_wallets: list, target_count: int = 500) -> list:
        """
        Starting from seed wallets, find wallets they've interacted with.
        This gives you a broader pool of active DeFi traders to analyze.
        """
        discovered = set(seed_wallets)
        queue = list(seed_wallets)
        log.info(f"Discovering wallets from {len(seed_wallets)} seeds, target: {target_count}")

        while queue and len(discovered) < target_count:
            wallet = queue.pop(0)
            signatures = self.rpc.get_signatures_for_address(wallet, limit=20)
            time.sleep(RPC_DELAY)

            for sig_info in signatures:
                sig = sig_info.get("signature", "")
                if not sig:
                    continue
                tx = self.rpc.get_transaction(sig)
                time.sleep(RPC_DELAY)

                if not tx:
                    continue

                # Extract all signers from the transaction
                try:
                    accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    for account in accounts[:5]:  # Check first 5 accounts
                        key = account if isinstance(account, str) else account.get("pubkey", "")
                        # Basic Solana address validation
                        if len(key) >= 32 and key not in discovered:
                            discovered.add(key)
                            queue.append(key)
                except Exception:
                    pass

            log.info(f"  Discovered {len(discovered)} wallets so far...")

        result = list(discovered)[:target_count]
        log.info(f"Discovery complete: {len(result)} wallets found")
        return result

# ─────────────────────────────────────────────
#  COPY-TRADE MONITOR
# ─────────────────────────────────────────────

class CopyTradeMonitor:
    """
    Monitors top wallets in real-time and alerts when they make a trade.
    You can then manually (or programmatically) mirror that trade.
    """

    def __init__(self, rpc: SolanaRPC, price: PriceFeed):
        self.rpc = rpc
        self.price = price
        self.seen_signatures: dict[str, set] = {}  # wallet → set of seen sig hashes

    def _load_last_sigs(self, wallet: str) -> set:
        if wallet not in self.seen_signatures:
            sigs = self.rpc.get_signatures_for_address(wallet, limit=5)
            self.seen_signatures[wallet] = {s["signature"] for s in sigs if s.get("signature")}
        return self.seen_signatures[wallet]

    def check_wallet(self, wallet: str) -> list[dict]:
        """Check if a wallet has made any new trades since last check."""
        known = self._load_last_sigs(wallet)
        latest = self.rpc.get_signatures_for_address(wallet, limit=5)
        new_trades = []

        for sig_info in latest:
            sig = sig_info.get("signature", "")
            if sig and sig not in known and not sig_info.get("err"):
                known.add(sig)
                sol_price = self.price.get_sol_price_usd()
                new_trades.append({
                    "wallet": wallet,
                    "signature": sig,
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "slot": sig_info.get("slot", 0),
                    "sol_price": sol_price,
                    "explorer_url": f"https://solscan.io/tx/{sig}",
                    "copy_action": "REVIEW AND COPY MANUALLY",
                })

        self.seen_signatures[wallet] = known
        return new_trades

    def monitor_loop(self, wallets: list[str], poll_interval: int = 15):
        """Main monitoring loop — polls top wallets every N seconds."""
        print("\n" + "═"*60)
        print("  🔴 LIVE COPY-TRADE MONITOR STARTED")
        print(f"  Watching {len(wallets)} wallets | Polling every {poll_interval}s")
        print("  Press Ctrl+C to stop")
        print("═"*60 + "\n")

        # Initialize baseline signatures
        for w in wallets:
            self._load_last_sigs(w)
            time.sleep(RPC_DELAY)

        # Open CSV log for signals
        with open(COPY_TRADE_LOG, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "wallet", "signature", "timestamp", "slot",
                "sol_price", "explorer_url", "copy_action"
            ])
            writer.writeheader()

            while True:
                try:
                    for wallet in wallets:
                        new_trades = self.check_wallet(wallet)
                        time.sleep(RPC_DELAY)

                        for trade in new_trades:
                            print(f"\n{'='*60}")
                            print(f"  🚨 NEW TRADE DETECTED!")
                            print(f"  Wallet  : {trade['wallet'][:8]}...{trade['wallet'][-4:]}")
                            print(f"  Time    : {trade['timestamp']}")
                            print(f"  SOL $   : ${trade['sol_price']:.2f}")
                            print(f"  View TX : {trade['explorer_url']}")
                            print(f"  Action  : {trade['copy_action']}")
                            print(f"{'='*60}")
                            writer.writerow(trade)
                            f.flush()

                    time.sleep(poll_interval)

                except KeyboardInterrupt:
                    print("\n\n  Monitor stopped by user.")
                    break

# ─────────────────────────────────────────────
#  RESULTS REPORTER
# ─────────────────────────────────────────────

class Reporter:
    def print_summary(self, all_stats: list[WalletStats], top_wallets: list[WalletStats]):
        print("\n" + "═"*70)
        print("  ANALYSIS COMPLETE — RESULTS SUMMARY")
        print("═"*70)
        print(f"  Total wallets analyzed  : {len(all_stats)}")
        print(f"  Wallets with trades     : {sum(1 for s in all_stats if s.total_trades > 0)}")
        print(f"  Smart money wallets     : {len(top_wallets)}")
        print()

        if not top_wallets:
            print("  ⚠️  No wallets met the profitability criteria.")
            print(f"  Try lowering MIN_WIN_RATE ({MIN_WIN_RATE}) or MIN_TRADES ({MIN_TRADES})")
            return

        print(f"  {'RANK':<5} {'ADDRESS':<20} {'TRADES':<8} {'WIN%':<8} {'PNL (USD)':<12} {'SCORE':<8} {'LAST ACTIVE'}")
        print("  " + "-"*70)
        for i, s in enumerate(top_wallets, 1):
            short_addr = f"{s.address[:6]}...{s.address[-4:]}"
            print(f"  {i:<5} {short_addr:<20} {s.total_trades:<8} "
                  f"{s.win_rate:<8.0%} ${s.total_pnl_usd:<11.2f} "
                  f"{s.consistency_score:<8.1f} {s.last_active}")

        print()
        print(f"  💾 Full results saved to: {OUTPUT_CSV}")
        print(f"  📡 Copy-trade signals will log to: {COPY_TRADE_LOG}")
        print("═"*70 + "\n")

    def save_csv(self, stats: list[WalletStats]):
        rows = []
        for s in stats:
            rows.append({
                "address": s.address,
                "total_trades": s.total_trades,
                "winning_trades": s.winning_trades,
                "win_rate": f"{s.win_rate:.2%}",
                "total_pnl_sol": round(s.total_pnl_sol, 4),
                "total_pnl_usd": round(s.total_pnl_usd, 2),
                "avg_pnl_per_trade_usd": round(s.avg_pnl_per_trade_usd, 2),
                "best_trade_usd": round(s.best_trade_usd, 2),
                "worst_trade_usd": round(s.worst_trade_usd, 2),
                "consistency_score": round(s.consistency_score, 2),
                "first_seen": s.first_seen,
                "last_active": s.last_active,
                "solscan_url": f"https://solscan.io/account/{s.address}",
            })

        df = pd.DataFrame(rows)
        df = df.sort_values("consistency_score", ascending=False)
        df.to_csv(OUTPUT_CSV, index=False)
        log.info(f"Results saved to {OUTPUT_CSV}")

# ─────────────────────────────────────────────
#  MAIN ENTRYPOINT
# ─────────────────────────────────────────────

def print_banner():
    print("""
╔══════════════════════════════════════════════════════════════╗
║        SOLANA SMART MONEY TRACKER  v1.0                      ║
║        Copy-Trade Bot for Solana Blockchain                  ║
╚══════════════════════════════════════════════════════════════╝
""")

def validate_config():
    """Check config before running."""
    if HELIUS_API_KEY == "YOUR_HELIUS_API_KEY_HERE":
        print("⚠️  WARNING: No Helius API key set!")
        print("   Using public RPC — expect rate limits and slow performance.")
        print("   Get a free key at: https://helius.dev\n")
        # Fall back to public RPC
        global RPC_ENDPOINT
        RPC_ENDPOINT = "https://api.mainnet-beta.solana.com"

    if not SEED_WALLETS or all("example" in w.lower() for w in SEED_WALLETS):
        print("⚠️  WARNING: No real seed wallets configured!")
        print("   Add active Solana wallet addresses to SEED_WALLETS in the script.")
        print("   Find active traders at: https://solscan.io/leaderboard\n")

def main():
    print_banner()
    validate_config()

    analyzer = WalletAnalyzer()
    discovery = WalletDiscovery(analyzer.rpc)
    reporter = Reporter()

    # ── Step 1: Discover wallets ──────────────────────────────
    print("┌─ STEP 1: WALLET DISCOVERY")
    target = min(MAX_WALLETS, 10000)
    wallets = discovery.discover_from_seed(SEED_WALLETS, target_count=target)
    print(f"└─ Found {len(wallets)} wallets to analyze\n")

    # ── Step 2: Analyze wallets ───────────────────────────────
    print("┌─ STEP 2: WALLET ANALYSIS")
    all_stats = analyzer.analyze_batch(wallets)
    print(f"└─ Analysis complete\n")

    # ── Step 3: Filter top wallets ────────────────────────────
    print("┌─ STEP 3: FILTERING PROFITABLE WALLETS")
    top_wallets = [
        s for s in all_stats
        if s.win_rate >= MIN_WIN_RATE
        and s.total_trades >= MIN_TRADES
        and s.total_pnl_usd > 0
    ]
    top_wallets.sort(key=lambda x: x.consistency_score, reverse=True)
    top_wallets = top_wallets[:TOP_WALLETS_TO_MONITOR]
    print(f"└─ {len(top_wallets)} smart money wallets identified\n")

    # ── Step 4: Save results ──────────────────────────────────
    print("┌─ STEP 4: SAVING RESULTS")
    reporter.save_csv(all_stats)
    reporter.print_summary(all_stats, top_wallets)

    # ── Step 5: Monitor for copy-trade signals ────────────────
    if top_wallets:
        answer = input("Start live copy-trade monitor? (y/n): ").strip().lower()
        if answer == "y":
            monitor = CopyTradeMonitor(analyzer.rpc, analyzer.price)
            top_addresses = [s.address for s in top_wallets]
            monitor.monitor_loop(top_addresses, poll_interval=15)
    else:
        print("No wallets to monitor. Adjust MIN_WIN_RATE or MIN_TRADES and re-run.")

if __name__ == "__main__":
    main()