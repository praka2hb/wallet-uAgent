import os
import httpx
import asyncio
import requests
import re 
import redis
from storage_manager import StorageManager
from llm import get_completion
from datetime import datetime, timedelta
from dotenv import load_dotenv
from uagents import Agent, Context, Protocol, Model
from uagents_core.contrib.protocols.chat import (
    ChatMessage, ChatAcknowledgement, TextContent, chat_protocol_spec, StartSessionContent, EndSessionContent
)
from tenacity import retry, stop_after_attempt, wait_exponential
from uuid import uuid4



# Load environment variables
load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
ASI_API_KEY = os.getenv("ASI_API_KEY")
AGENT_SEED = os.getenv("AGENT_SEED")
ALMANAC_TIMEOUT = 10  # Timeout in seconds for Almanac registration
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")


r = redis.Redis(
  host='evolved-arachnid-10421.upstash.io',
  port=6379,
  password= REDIS_PASSWORD,
  ssl=True
)

agent = Agent(
    name="solana_wallet_agent",
    seed=AGENT_SEED,
    port=8000,
    mailbox=True,
    publish_agent_details=True,
)

storage = StorageManager(r, ttl_days=30)

# Chat protocol
chat_proto = Protocol(
    name="AgentChatProtocol",  # Match the name from the specification
    version="0.3.0",  # Match the version from the specification
    spec= chat_protocol_spec,  # Use the specification defined above
)


def is_valid_solana_address(address: str) -> bool:
    """Validate a Solana address without external dependencies."""
    # Solana addresses are base58 encoded and typically 32-44 characters
    if not address or not isinstance(address, str):
        return False
        
    # Check length
    if not (32 <= len(address) <= 44):
        return False
        
    # Check characters (base58 alphabet)
    base58_chars = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    return all(c in base58_chars for c in address)

async def get_token_symbol_helius(mint_address: str, ctx: Context = None) -> str:
    """Fetch token symbol using Helius API with caching via StorageManager."""
    if not mint_address or mint_address == "Unknown":
        return "Unknown"

    cached_symbol = storage.get_token(mint_address)
    if cached_symbol:
        return cached_symbol

    common_tokens = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        "So11111111111111111111111111111111111111112": "SOL",
        "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
        "kinXdEcpDQeHPEuQnqmUgtYykqKGVFq6CeVX5iAHJq6": "KIN",
        "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
        "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU": "SAMO",
        "BUTTLEgBYQmjQGqZfu1sMGWGbB98qchmWpWQwQfSJvbe": "BUTT",
        "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
        "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE": "ORCA",
        "PsyFiqqjiv41G7o5SMRzDJCu4psptThNR2GtfeGHfSq": "PSY",
        "EchesyfXePKdLtoiZSL8pBe8Myagyy8ZRqsACNCFGnvp": "FIDA",
        "Be8jYsVxdXYZ9yJDYrPFYcWy8tfG3TRoi8SwWb5Vpump": "RUGSCANAI"
    }

    if mint_address in common_tokens:
        symbol = common_tokens[mint_address]
        storage.set_token(mint_address, symbol)
        return symbol

    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {
            "jsonrpc": "2.0",
            "id": "token-lookup",
            "method": "getAsset",
            "params": {"id": mint_address}
        }
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                abbreviated = f"{mint_address[:4]}...{mint_address[-4:]}"
                storage.set_token(mint_address, abbreviated) # Use storage
                return abbreviated
            data = resp.json()
            if "result" in data and "content" in data["result"] and "metadata" in data["result"]["content"]:
                symbol = data["result"]["content"]["metadata"].get("symbol")
                if symbol:
                    storage.set_token(mint_address, symbol) # Use storage
                    if ctx:
                        ctx.logger.info(f"Found token via Helius: {mint_address} = {symbol}")
                    return symbol
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://cdn.jsdelivr.net/gh/solana-labs/token-list@main/src/tokens/solana.tokenlist.json")
            if resp.status_code == 200:
                token_list = resp.json()
                for token in token_list.get("tokens", []):
                    if token.get("address") == mint_address:
                        symbol = token.get("symbol", "Unknown")
                        storage.set_token(mint_address, symbol) # Use storage
                        if ctx:
                            ctx.logger.info(f"Found token via Solana token list: {mint_address} = {symbol}")
                        return symbol
    except Exception as e:
        if ctx:
            ctx.logger.warning(f"Token lookup error for {mint_address}: {e}")
    abbreviated = f"{mint_address[:4]}...{mint_address[-4:]}"
    storage.set_token(mint_address, abbreviated) # Use storage
    return abbreviated


async def generate_defi_summary(transactions, ctx: Context, days: int = 7):
    """Create a structured DeFi activity summary with platforms, stats, and recent actions."""
    # Filter only defi_swap transactions
    defi_txs = [tx for tx in transactions if tx.get("details", {}).get("type") == "defi_swap"]
    
    if not defi_txs:
        return f"No DeFi transactions found in the last {days} days."
    
    platforms_in_txs = {tx.get("details", {}).get("platform", "Unknown") for tx in defi_txs}
    single_platform = next(iter(platforms_in_txs)) if len(platforms_in_txs) == 1 else None
    # Sort by timestamp, newest first
    defi_txs.sort(key=lambda tx: tx.get("timestamp", 0), reverse=True)
    
    current_date = datetime.now()
    
    if single_platform and single_platform not in ["Unknown Platform", "DEX"]:
        summary = f"# Your {single_platform} Swaps (Last {days} Days)\n\n"
    else:
        summary = f"# Your DeFi Activity (Last {days} Days)\n\n"
    # Collect platform stats
    platforms = {}
    total_fees = 0
    sol_to_dollar = await get_sol_usd_price() or 0
    
    for tx in defi_txs:
        details = tx.get("details", {})
        platform = details.get("platform", "Unknown")
        if platform == "Unknown Platform":
            platform = "Other DEX"
        
        if platform not in platforms:
            platforms[platform] = {
                "count": 0,
                "tokens_swapped": set(),
                "volume": 0,
                "transactions": []
            }
        
        platforms[platform]["count"] += 1
        platforms[platform]["transactions"].append(tx)
        
        # Track unique tokens
        from_token = details.get("from_token", "Unknown")
        to_token = details.get("to_token", "Unknown")
        if from_token != "Unknown Token" and from_token != "Unknown":
            platforms[platform]["tokens_swapped"].add(from_token)
        if to_token != "Unknown Token" and to_token != "Unknown":
            platforms[platform]["tokens_swapped"].add(to_token)
        
        # Estimate fees (simplified approximation)
        fee = 0.000005  # Base transaction fee in SOL
        if details.get("platform") == "JUPITER":
            fee += 0.0002  # Jupiter typical fee
        elif details.get("platform") in ["RAYDIUM", "ORCA"]:
            fee += 0.00015  # Typical DEX fee
        total_fees += fee
    
    # Format the summary
    current_date = datetime.now()
    
    summary = f"# Your DeFi Activity (Last {days} Days)\n\n"
    
    # Overall stats section
    summary += f"## Overview\n"
    summary += f"- **Total DeFi Transactions:** {len(defi_txs)}\n"
    if sol_to_dollar:
        summary += f"- **Estimated Fees Paid:** {total_fees:.6f} SOL (≈${total_fees * sol_to_dollar:.2f})\n"
    else:
        summary += f"- **Estimated Fees Paid:** {total_fees:.6f} SOL\n"
    
    # Platforms section
    if platforms:
        summary += f"- **Platforms Used:**\n"
        for platform, data in sorted(platforms.items(), key=lambda x: x[1]["count"], reverse=True):
            # Determine action type based on platform
            action_type = "swaps"
            if platform == "SOLEND":
                action_type = "borrow/lend actions"
            elif platform == "MARINADE":
                action_type = "staking actions"
            elif platform == "ORCA" and any("pool" in tx.get("details", {}).get("description", "").lower() for tx in data["transactions"]):
                action_type = "liquidity interactions"
            
            summary += f"  • **{platform}:** {data['count']} {action_type}\n"
    
    # Recent actions section
    summary += f"\n## Recent Actions\n"
    for i, tx in enumerate(defi_txs[:5], 1):  # Show up to 5 most recent
        details = tx.get("details", {})
        date_str = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%b %d")
        
        from_token = details.get("from_token", "Token")
        to_token = details.get("to_token", "Token")
        from_amount = details.get("from_amount", 0)
        to_amount = details.get("to_amount", 0)
        platform = details.get("platform", "DEX")
        
        # Format amounts nicely
        from_amount_str = f"{from_amount:.4f}".rstrip('0').rstrip('.') if from_amount != 0 else "Some"
        to_amount_str = f"{to_amount:.4f}".rstrip('0').rstrip('.') if to_amount != 0 else "Some"
        
        action_text = f"Swapped {from_amount_str} {from_token} for {to_amount_str} {to_token} via {platform}"
        
        # Special handling for specific platforms
        if platform == "SOLEND":
            if from_token == "SOL" or from_token in ["USDC", "USDT"]:
                action_text = f"Deposited {from_amount_str} {from_token} as collateral on Solend"
            else:
                action_text = f"Borrowed {to_amount_str} {to_token} using {from_token} collateral on Solend"
        elif platform == "MARINADE":
            if to_token == "mSOL":
                action_text = f"Staked {from_amount_str} {from_token} for {to_amount_str} mSOL on Marinade"
            else:
                action_text = f"Unstaked {from_amount_str} {from_token} for {to_amount_str} {to_token} on Marinade"
        
        summary += f"{i}. **{date_str}:** {action_text}\n"
    
    # Add Explorer links
    summary += f"\n## Transaction Links\n"
    for i, tx in enumerate(defi_txs[:3], 1):  # Show links to first 3 transactions
        signature = tx.get("signature", "")
        if signature:
            summary += f"- [View Transaction {i}](https://explorer.solana.com/tx/{signature})\n"
    
    # Add a tips section
    summary += f"\n## 💡 Tips\n"
    summary += f"- Try 'Show my DeFi swaps from last month' for a longer history\n"
    summary += f"- Ask about a specific platform with 'Show my *Platform* swaps'\n"
    
    return summary


async def generate_nft_summary(transactions, ctx: Context, days: int = 7):
    """Create a structured NFT activity summary with collections, stats, and recent actions."""
    # Filter only NFT transactions
    nft_txs = [tx for tx in transactions if tx.get("details", {}).get("type") in ["nft_mint", "nft_transfer"]]
    
    if not nft_txs:
        return f"No NFT transactions found in the last {days} days."
    
    # Sort by timestamp, newest first
    nft_txs.sort(key=lambda tx: tx.get("timestamp", 0), reverse=True)
    
    # Collect stats
    collections = {}
    mints = 0
    transfers_in = 0
    transfers_out = 0
    
    user_wallet = None
    # Find user wallet from first transaction fee payer (fallback)
    if nft_txs and "feePayer" in nft_txs[0]:
        user_wallet = nft_txs[0]["feePayer"]
    
    for tx in nft_txs:
        details = tx.get("details", {})
        nft_name = details.get("token", "Unknown NFT")
        tx_type = details.get("type")
        
        # Count by type and direction
        if tx_type == "nft_mint":
            mints += 1
        elif tx_type == "nft_transfer":
            # If to_address is user, it's incoming
            if details.get("to_address") == "Your wallet":
                transfers_in += 1
            else:
                transfers_out += 1
        
        # Track collections (simplified, would be better with actual collection data)
        collection = "Unknown Collection"
        # Extract collection from name if possible
        if " #" in nft_name:
            collection = nft_name.split(" #")[0]
        
        if collection not in collections:
            collections[collection] = {"count": 0, "transactions": []}
        
        collections[collection]["count"] += 1
        collections[collection]["transactions"].append(tx)
    
    # Format the summary
    summary = f"# Your NFT Activity (Last {days} Days)\n\n"
    
    # Overall stats section
    summary += f"## Overview\n"
    summary += f"- **Total NFT Transactions:** {len(nft_txs)}\n"
    if mints > 0:
        summary += f"- **NFTs Minted:** {mints}\n"
    summary += f"- **NFTs Received:** {transfers_in}\n"
    summary += f"- **NFTs Sent:** {transfers_out}\n"
    
    # Collections section
    if collections:
        summary += f"- **Collections Involved:** {len(collections)}\n"
        top_collections = sorted(collections.items(), key=lambda x: x[1]["count"], reverse=True)[:5]
        if top_collections:
            summary += f"\n## Top Collections\n"
            for coll_name, data in top_collections:
                summary += f"- **{coll_name}:** {data['count']} transactions\n"
    
    # Recent actions section
    summary += f"\n## Recent NFT Activity\n"
    for i, tx in enumerate(nft_txs[:10], 1):  # Show up to 10 most recent
        details = tx.get("details", {})
        date_str = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%b %d")
        
        nft_name = details.get("token", "Unknown NFT")
        tx_type = details.get("type")
        from_addr = details.get("from_address", "Unknown")
        to_addr = details.get("to_address", "Unknown")
        
        if tx_type == "nft_mint":
            action_text = f"Minted {nft_name}"
        elif tx_type == "nft_transfer":
            if to_addr == "Your wallet":
                action_text = f"Received {nft_name} from {from_addr}"
            else:
                action_text = f"Sent {nft_name} to {to_addr}"
        else:
            action_text = f"Interacted with {nft_name}"
        
        summary += f"{i}. **{date_str}:** {action_text}\n"
    
    # Add Explorer links
    summary += f"\n## Transaction Links\n"
    for i, tx in enumerate(nft_txs[:3], 1):  # Show links to first 3 transactions
        signature = tx.get("signature", "")
        if signature:
            summary += f"- [View Transaction {i}](https://explorer.solana.com/tx/{signature})\n"
    
    # Add a tips section
    summary += f"\n## 💡 Tips\n"
    summary += f"- Try 'Show my NFT activity from last month' for a longer history\n"
    summary += f"- You can also search for specific collections with 'Show my DeGods NFTs'\n"
    
    return summary

async def get_sol_usd_price():
    """Fetch the current SOL/USD price from CoinGecko with caching via StorageManager."""
    token_id = "solana"
    cached_price = storage.get_price(token_id)
    if cached_price is not None:
        return cached_price

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": token_id, "vs_currencies": "usd"},
            timeout=5
        )
        resp.raise_for_status()
        price = float(resp.json()[token_id]["usd"])
        storage.set_price(token_id, price) # Cache the price
        return price
    except Exception as e:
        # Consider logging the error here
        print(f"Error fetching SOL price: {e}")
        return None

def parse_query(text):
    """Extract activity type and time period from user query with enhanced time period parsing."""
    text = text.lower()
    
    # Get activity type
    if any(term in text for term in ["nft", "collectible", "digital art", "pfp", "collection"]):
        activity_type = "nft"
    elif "defi" in text or "swap" in text:
        activity_type = "defi"
    else:
        activity_type = "general"
        
    # Enhanced time period parsing
    days = 7  # Default to 7 days if not specified
    
    # Check for hours (24h, 48h, etc.)
    hour_match = re.search(r'(\d+)\s*h(our)?s?', text)
    if hour_match:
        hours = int(hour_match.group(1))
        days = max(1, round(hours / 24, 1))  # Convert hours to days, minimum 1 day
        
    # Check for days (7d, 30d, etc.)
    day_match = re.search(r'(\d+)\s*d(ay)?s?', text)
    if day_match:
        days = int(day_match.group(1))
        
    # Check for weeks (1w, 2w, etc.)
    week_match = re.search(r'(\d+)\s*w(eek)?s?', text)
    if week_match:
        days = int(week_match.group(1)) * 7
        
    # Check for months (1m, 2m, etc.)
    month_match = re.search(r'(\d+)\s*m(onth)?s?', text)
    if month_match and 'minute' not in text:  # Avoid confusion with minutes
        days = int(month_match.group(1)) * 30
        
    # Check for years (1y, 2y, etc.)
    year_match = re.search(r'(\d+)\s*y(ear)?s?', text)
    if year_match:
        days = int(year_match.group(1)) * 365
        
    # Check for specific day numbers
    day_number_match = re.search(r'(\d+)\s*days?', text)
    if day_number_match:
        days = int(day_number_match.group(1))
        
    # Look for "last X days" pattern
    last_days_match = re.search(r'last\s+(\d+)', text)
    if last_days_match:
        days = int(last_days_match.group(1))
    
    # Special case for "24h", "48h" etc.
    if "24h" in text or "24 h" in text or "24 hour" in text:
        days = 1
    if "48h" in text or "48 h" in text or "48 hour" in text:
        days = 2
    if "72h" in text or "72 h" in text or "72 hour" in text:
        days = 3
        
    # Special case for common time periods
    if "today" in text or "24 hours" in text:
        days = 1
    if "yesterday" in text:
        days = 2
    if "week" in text and not week_match:
        days = 7
    if "month" in text and not month_match:
        days = 30
    if "year" in text and not year_match:
        days = 365
        
    # Cap at 365 days for API limitations
    days = min(days, 365)
    
    platform = None
    if "jupiter" in text:
        platform = "JUPITER"
    elif "raydium" in text:
        platform = "RAYDIUM"
    elif "orca" in text:
        platform = "ORCA"
    elif "pump.fun" in text or "pumpfun" or "PUMP_FUN" in text:
        platform = "PUMP_FUN"
    
    return activity_type, days, platform

# Direct check for SWAP transactions
async def check_for_swap_transactions(wallet_address, days=7, ctx=None):
    """Directly query for SWAP transactions."""
    before_time = int((datetime.now() - timedelta(days=days)).timestamp())
    ctx.logger.info(f"Directly checking for SWAP transactions for {wallet_address}")

    url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/transactions?api-key={HELIUS_API_KEY}&limit=100&type=SWAP"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            ctx.logger.error(f"Error fetching SWAP transactions: {resp.status_code}")
            return []

        data = resp.json()
        # Filter by timestamp
        swaps = [tx for tx in data if tx.get("timestamp", 0) >= before_time]
        ctx.logger.info(f"Found {len(swaps)} SWAP transactions in last {days} days")

        # Mark these transactions as swaps for parsing
        for tx in swaps:
            tx["type"] = "SWAP"
            tx["_helius_marked_swap"] = True

        return swaps

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def fetch_wallet_activity(wallet_address: str, days: int = 7, ctx: Context = None):
    # Initialize all key variables early in the function
    before_time = int((datetime.now() - timedelta(days=days)).timestamp())
    txs = []
    all_txs = []
    swap_txs = []
    swap_signatures = set()
    data = []

    if not HELIUS_API_KEY:
        ctx.logger.error("HELIUS_API_KEY not found or empty")
        return []

    try:
        ctx.logger.info(f"Fetching txs for {wallet_address} since {datetime.fromtimestamp(before_time)}")

        # First get all swap transactions directly
        swap_txs = await check_for_swap_transactions(wallet_address, days, ctx) or []
        swap_signatures = {tx.get("signature") for tx in swap_txs}
        ctx.logger.info(f"Found {len(swap_signatures)} unique SWAP signatures")
        # Then get all transactions
        url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/transactions?api-key={HELIUS_API_KEY}&limit=100"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 401:
                ctx.logger.error("Helius API 401 Unauthorized: Check HELIUS_API_KEY")
                raise Exception("Invalid Helius API key")
            if resp.status_code == 429:
                ctx.logger.error("Helius API 429 Too Many Requests: Rate limit exceeded")
                raise Exception("Rate limit exceeded")
            if resp.status_code != 200:
                ctx.logger.error(f"Helius API error: {resp.status_code} - {resp.text}")
                return []
            try:
                data = resp.json()
            except Exception as e:
                ctx.logger.error(f"JSON parse error: {e}")
                return []
            if not isinstance(data, list):
                ctx.logger.error(f"Unexpected response type: {type(data)}")
                return []
    except Exception as e:
        ctx.logger.error(f"Error in fetch_wallet_activity: {str(e)}", exc_info=True)
        # Only retry on network errors, not programming errors
        if isinstance(e, (httpx.RequestError, httpx.HTTPStatusError, TimeoutError)):
            raise  # Let retry mechanism handle this
        return []  # For other errors, just return empty list

    # Merge swap transactions with regular transactions, prioritizing swaps
    # Start with swap transactions
    all_txs = list(swap_txs)

    # Add regular transactions that aren't swaps
    for tx in data:
        if tx.get("signature") not in swap_signatures:
            all_txs.append(tx)

    # Log all transaction types for debugging
    tx_types = {}
    for tx in all_txs:
        tx_type = tx.get("type", "UNKNOWN")
        if tx_type in tx_types:
            tx_types[tx_type] += 1
        else:
            tx_types[tx_type] = 1

    ctx.logger.info(f"Transaction types in response: {tx_types}")

    # Process transactions with more details
    txs = []
    parse_tasks = []

    for tx in all_txs:
        ts = tx.get("timestamp")
        if not isinstance(ts, (int, float)) or ts < before_time:
            continue

        # Check for swap keywords in description
        tx_description = tx.get("description", "").lower()
        if "swap" in tx_description:
            ctx.logger.info(f"Potential swap from description: {tx.get('signature')[:8]} - {tx_description}")
            tx["_potential_swap"] = True

        # Look for event markers of swaps
        if "events" in tx and "swap" in tx.get("events", {}):
            ctx.logger.info(f"Swap event detected: {tx.get('signature')[:8]}")
            tx["_has_swap_event"] = True

        # Mark all SWAP type transactions
        if tx.get("type") == "SWAP":
            ctx.logger.info(f"SWAP transaction type: {tx.get('signature')[:8]}")
            tx["_helius_marked_swap"] = True

        parse_tasks.append(parse_transaction(tx, ctx))

    details_list = await asyncio.gather(*parse_tasks)

    for i, tx in enumerate([tx for tx in all_txs if tx.get("timestamp", 0) >= before_time]):
        if i >= len(details_list):
            ctx.logger.warning(f"Index mismatch: {i} >= {len(details_list)}")
            continue

        details = details_list[i]

        # Ensure SWAP transactions are properly marked
        if tx.get("_helius_marked_swap") or tx.get("_has_swap_event") or tx.get("_potential_swap"):
            details["type"] = "defi_swap"

            # Ensure token names are set for swaps
            if not details["from_token"]:
                # Try to extract from description
                desc = tx.get("description", "").lower()
                swap_match = re.search(r'(?:swap|swapped|exchange|exchanged|trade|traded)\s+(\d+\.?\d*)\s+(\w+)\s+(?:for|to)\s+(\d+\.?\d*)\s+(\w+)', desc)
                if swap_match:
                    details.update(
                        from_amount=float(swap_match.group(1)),
                        from_token=swap_match.group(2).upper(),
                        to_amount=float(swap_match.group(3)),
                        to_token=swap_match.group(4).upper(),
                        amount=float(swap_match.group(3))
                    )
                else:
                    details["from_token"] = "Token"

            if not details["to_token"]:
                details["to_token"] = "Token"

            # Skip fee check for swaps
            details["is_fee"] = False

        # Skip fee or unknown transactions
        if details.get("is_fee", False) or details["type"] == "unknown":
            ctx.logger.info(f"Skipping fee/irrelevant tx: {tx.get('signature')[:8]}, type={details['type']}")
            continue

        txs.append({
            "signature": tx.get("signature", "unknown"),
            "timestamp": tx["timestamp"],
            "details": details
        })

    ctx.logger.info(f"Found {len(txs)} meaningful txs in last {days} days (after filtering)")

    # Log swap transactions specifically for debugging
    swap_count = len([tx for tx in txs if tx["details"]["type"] == "defi_swap"])
    ctx.logger.info(f"Detected {swap_count} SWAP transactions")

    return txs

async def parse_transaction(tx, ctx: Context = None):
    """Parse Helius API transaction with enhanced swap detection."""
    details = {
        "type": "unknown",
        "amount": 0,
        "token": "SOL", # Default token if none found
        "program": None,
        "from_token": "Unknown Token", # Default if cannot resolve
        "to_token": "Unknown Token",   # Default if cannot resolve
        "from_amount": 0,
        "to_amount": 0,
        "from_address": None,
        "to_address": None,
        "description": None,
        "is_fee": False,
        "platform": "Unknown Platform" # Default platform
    }

    fee = tx.get("fee", 0)
    fee_payer = tx.get("feePayer")
    user_wallet = fee_payer # Assume fee payer is the user for context
    abbreviated_user = f"{user_wallet[:4]}...{user_wallet[-4:]}" if user_wallet else "Your Wallet"
    signature = tx.get("signature", "unknown")[:8] # For logging

    # Set addresses early
    details["from_address"] = abbreviated_user
    details["to_address"] = abbreviated_user
    details["description"] = tx.get("description", "") # Store description

    # --- Platform Detection Logic ---
    platform_found = False
    tx_source = tx.get("source", "") # Get transaction source

    # 1. Check explicit source first (e.g., PUMP_FUN)
    if tx_source == "PUMP_FUN":
        details["platform"] = "PUMP_FUN"
        platform_found = True
        ctx.logger.info(f"Platform set to PUMP_FUN via source ({signature})")

    # 2. Check swap event source
    events = tx.get("events", {}) or {}
    swap_event = events.get("swap", {}) or {}
    if not platform_found and swap_event:
        platform_source = swap_event.get("programInfo", {}).get("source", "")
        if platform_source:
            details["platform"] = platform_source.upper()
            platform_found = True
            ctx.logger.info(f"Platform found via swap event source: {details['platform']} ({signature})")

    # 3. Check instructions for known DEX program IDs
    if not platform_found:
        instructions = tx.get("instructions", [])
        known_dex_patterns = {
            "JUPITER": "JUP",
            "RAYDIUM": "675kPX",
            "ORCA": "whirLp",
            "METEORA": "M2mx93",
            "DRIFT": "dRifT",
            "PUMP_FUN": "6EF8rr",
        }
        for instr in instructions:
            program_id = instr.get("programId", "")
            if program_id:
                for name, pattern in known_dex_patterns.items():
                    if pattern in program_id:
                        details["platform"] = name
                        platform_found = True
                        ctx.logger.info(f"Platform found via program ID: {name} ({signature})")
                        break # Found platform for this instruction
            if platform_found:
                break # Found platform in instructions

    # 4. Check description for platform keywords
    if not platform_found and details["description"]:
        desc_lower = details["description"].lower()
        # Updated regex to better capture platform names after 'on', 'via', etc.
        platform_match = re.search(r'(?:on|via|using|through|at)\s+([\w\-.]+)', desc_lower) # Allow dots in names like pump.fun
        if platform_match:
            platform_name = platform_match.group(1).upper()
            # Avoid generic terms
            if platform_name not in ["TOKEN", "SOLANA", "PROGRAM"]:
                # Specific check for pump.fun variations
                if "PUMP" in platform_name:
                    details["platform"] = "PUMP_FUN"
                else:
                    details["platform"] = platform_name
                platform_found = True
                ctx.logger.info(f"Platform found via description: {details['platform']} ({signature})")

    # 5. Default if still not found
    if not platform_found:
        # If type is SWAP or swap event exists, default platform is DEX
        if tx.get("type") == "SWAP" or swap_event:
             details["platform"] = "DEX" # Use generic DEX if specific platform unknown for swaps
             ctx.logger.info(f"Platform defaulted to DEX for swap ({signature})")
        # else: Keep "Unknown Platform" for non-swaps if not found


    # --- Swap Detection and Parsing ---
    is_swap = False
    # Prioritize explicit SWAP type or detected platform
    if tx.get("type") == "SWAP" or details["platform"] not in ["Unknown Platform", "DEX"]:
        is_swap = True
        ctx.logger.info(f"Swap detected via type/platform: {tx.get('type', 'N/A')}/{details['platform']} ({signature})")
    elif swap_event:
        is_swap = True
        ctx.logger.info(f"Swap detected via event ({signature})")
    elif details["description"] and any(keyword in details["description"].lower() for keyword in ["swap", "swapped", "exchange", "exchanged", "trade", "traded"]):
        is_swap = True
        ctx.logger.info(f"Swap detected via description keywords ({signature})")


    if is_swap:
        details["type"] = "defi_swap"
        ctx.logger.info(f"Swap Parse Start ({signature}): Platform={details['platform']}, Initial from={details['from_token']}, to={details['to_token']}")

        # --- Pump.fun Specific Logic ---
        if details["platform"] == "PUMP_FUN":
            ctx.logger.info(f"Applying PUMP_FUN parsing logic ({signature})")
            user_native_change = 0
            user_token_sent = None
            user_token_received = None

            # Find user's native balance change
            for acc_data in tx.get("accountData", []):
                if acc_data.get("account") == user_wallet:
                    user_native_change = acc_data.get("nativeBalanceChange", 0)
                    ctx.logger.info(f"PUMP_FUN ({signature}): User native change = {user_native_change} lamports")
                    break

            # Find user's token involvement
            for tt in tx.get("tokenTransfers", []):
                mint = tt.get("mint")
                amount = tt.get("tokenAmount", 0)
                if tt.get("fromUserAccount") == user_wallet and mint and amount > 0:
                    user_token_sent = {"mint": mint, "amount": amount}
                    ctx.logger.info(f"PUMP_FUN ({signature}): User sent token {mint}, amount {amount}")
                elif tt.get("toUserAccount") == user_wallet and mint and amount > 0:
                    user_token_received = {"mint": mint, "amount": amount}
                    ctx.logger.info(f"PUMP_FUN ({signature}): User received token {mint}, amount {amount}")

            # Determine Buy or Sell based on token movement and native change
            if user_token_sent and user_native_change > 0: # Sold token, received SOL
                details["from_token"] = await get_token_symbol_helius(user_token_sent["mint"], ctx)
                details["from_amount"] = user_token_sent["amount"]
                details["to_token"] = "SOL"
                details["token"] = "SOL" # Primary token is what was received
                details["to_amount"] = user_native_change / 1e9 # Convert lamports change to SOL
                ctx.logger.info(f"PUMP_FUN ({signature}): Parsed as SELL {details['from_amount']} {details['from_token']} for {details['to_amount']} SOL")
            elif user_token_received and user_native_change < 0: # Bought token, spent SOL
                details["from_token"] = "SOL"
                details["from_amount"] = abs(user_native_change) / 1e9 # Convert lamports change to SOL
                details["to_token"] = await get_token_symbol_helius(user_token_received["mint"], ctx)
                details["token"] = details["to_token"] # Primary token is what was received
                details["to_amount"] = user_token_received["amount"]
                ctx.logger.info(f"PUMP_FUN ({signature}): Parsed as BUY {details['to_amount']} {details['to_token']} for {details['from_amount']} SOL")
            else:
                ctx.logger.warning(f"PUMP_FUN ({signature}): Could not determine buy/sell from native change ({user_native_change}) and token transfers (sent: {user_token_sent}, received: {user_token_received}). Falling back.")
                # Fallback to generic swap logic might be needed here if pump.fun logic fails

        # --- Generic Swap Event Logic (if not Pump.fun or fallback needed) ---
        if details["from_token"] == "Unknown Token" or details["to_token"] == "Unknown Token": # Check if pump.fun logic failed or wasn't applicable
            ctx.logger.info(f"Applying generic swap parsing logic ({signature})")
            if swap_event:
                # Get swap details from top-level event data
                token_in_mint = swap_event.get("tokenIn")
                amount_in = swap_event.get("amountIn")
                decimals_in = swap_event.get("decimalsIn", 9)

                token_out_mint = swap_event.get("tokenOut")
                amount_out = swap_event.get("amountOut")
                decimals_out = swap_event.get("decimalsOut", 9)

                if token_in_mint and amount_in is not None:
                    resolved_from = await get_token_symbol_helius(token_in_mint, ctx)
                    # Only update if still default
                    if details["from_token"] == "Unknown Token":
                        details["from_token"] = resolved_from
                        details["from_amount"] = float(amount_in) / (10 ** decimals_in) if amount_in else 0
                        ctx.logger.info(f"Swap Event Parse ({signature}): Found input mint {token_in_mint}, resolved to {resolved_from}")
                else:
                    ctx.logger.info(f"Swap Event Parse ({signature}): No input mint found in top-level event.")

                if token_out_mint and amount_out is not None:
                    resolved_to = await get_token_symbol_helius(token_out_mint, ctx)
                    if details["to_token"] == "Unknown Token":
                        details["to_token"] = resolved_to
                        details["token"] = resolved_to
                        details["to_amount"] = float(amount_out) / (10 ** decimals_out) if amount_out else 0
                        ctx.logger.info(f"Swap Event Parse ({signature}): Found output mint {token_out_mint}, resolved to {resolved_to}")
                else:
                     ctx.logger.info(f"Swap Event Parse ({signature}): No output mint found in top-level event.")

                # Refine using inner swaps if necessary or available
                inner_swaps = swap_event.get("innerSwaps", [])
                if inner_swaps and (details["from_token"] == "Unknown Token" or details["to_token"] == "Unknown Token"):
                    ctx.logger.info(f"Processing {len(inner_swaps)} inner swaps for refinement ({signature})")
                    for inner in inner_swaps:
                        # ... (inner swap logic remains the same, ensure it updates details only if default) ...
                        token_inputs = inner.get("tokenInputs", [])
                        token_outputs = inner.get("tokenOutputs", [])

                        for ti in token_inputs:
                            if ti.get("fromUserAccount") == user_wallet:
                                mint = ti.get("mint")
                                amount = ti.get("tokenAmount")
                                if mint and amount is not None and details["from_token"] == "Unknown Token": # Only update if default
                                    resolved_inner_from = await get_token_symbol_helius(mint, ctx)
                                    details["from_token"] = resolved_inner_from
                                    details["from_amount"] = float(amount)
                                    ctx.logger.info(f"InnerSwap Input ({signature}): Found mint {mint}, resolved to {resolved_inner_from}, amount {amount}")
                                    break

                        for to in token_outputs:
                            if to.get("toUserAccount") == user_wallet:
                                mint = to.get("mint")
                                amount = to.get("tokenAmount")
                                if mint and amount is not None and details["to_token"] == "Unknown Token": # Only update if default
                                    resolved_inner_to = await get_token_symbol_helius(mint, ctx)
                                    details["to_token"] = resolved_inner_to
                                    details["token"] = resolved_inner_to
                                    details["to_amount"] = float(amount)
                                    ctx.logger.info(f"InnerSwap Output ({signature}): Found mint {mint}, resolved to {resolved_inner_to}, amount {amount}")
                                    break


            # Fallback: Infer from description if still missing data
            if (details["from_amount"] == 0 or details["to_amount"] == 0 or details["from_token"] == "Unknown Token" or details["to_token"] == "Unknown Token") and details["description"]:
                 # ... (description parsing logic remains the same) ...
                 swap_match = re.search(r'(?:swap|swapped|exchange|exchanged|trade|traded)\s+(\d+\.?\d*)\s+([\w\.\-]+)\s+(?:for|to)\s+(\d+\.?\d*)\s+([\w\.\-]+)', details["description"].lower())
                 if swap_match:
                     ctx.logger.info(f"Refining swap details from description ({signature})")
                     if details["from_amount"] == 0: details["from_amount"] = float(swap_match.group(1))
                     if details["from_token"] == "Unknown Token":
                         desc_token_symbol = swap_match.group(2).upper()
                         if not (32 <= len(desc_token_symbol) <= 44): details["from_token"] = desc_token_symbol
                     if details["to_amount"] == 0: details["to_amount"] = float(swap_match.group(3))
                     if details["to_token"] == "Unknown Token":
                         desc_token_symbol = swap_match.group(4).upper()
                         if not (32 <= len(desc_token_symbol) <= 44):
                             details["to_token"] = desc_token_symbol
                             details["token"] = details["to_token"]


            # Fallback: Infer from token transfers if still missing data
            if details["from_amount"] == 0 or details["to_amount"] == 0 or details["from_token"] == "Unknown Token" or details["to_token"] == "Unknown Token":
                # ... (token transfer inference logic remains the same) ...
                ctx.logger.info(f"Attempting to infer swap details from transfers ({signature})")
                token_transfers = tx.get("tokenTransfers", [])
                for tt in token_transfers:
                    # ... (logic to update details["from_token"], details["to_token"] etc. if default) ...
                    from_user = tt.get("fromUserAccount")
                    to_user = tt.get("toUserAccount")
                    mint = tt.get("mint")
                    amount = abs(tt.get("tokenAmount", 0))
                    token_standard = tt.get("tokenStandard", "")

                    if mint and amount > 0 and token_standard == "Fungible":
                        token_symbol = await get_token_symbol_helius(mint, ctx)
                        if from_user == user_wallet and (details["from_amount"] == 0 or details["from_token"] == "Unknown Token"):
                            details["from_token"] = token_symbol
                            details["from_amount"] = amount
                            ctx.logger.info(f"Inferred swap input from transfer ({signature}): {amount} {token_symbol}")
                        elif to_user == user_wallet and (details["to_amount"] == 0 or details["to_token"] == "Unknown Token"):
                            details["to_token"] = token_symbol
                            details["token"] = token_symbol
                            details["to_amount"] = amount
                            ctx.logger.info(f"Inferred swap output from transfer ({signature}): {amount} {token_symbol}")


        # Set overall amount (useful for sorting/display)
        details["amount"] = max(details["from_amount"], details["to_amount"])

        ctx.logger.info(f"Swap Parse End ({signature}): from={details['from_amount']} {details['from_token']}, to={details['to_amount']} {details['to_token']}, platform={details['platform']}")
        return details # Return early for swaps

    # --- Handle Non-Swap Transactions ---

    # Check if it's just a fee payment (often small SOL transfer from fee payer)
    native_transfers = tx.get("nativeTransfers", []) or []
    if len(native_transfers) == 1:
        nt = native_transfers[0]
        lamports = nt.get("amount", 0)
        from_address = nt.get("fromUserAccount")
        # Check if it's a small amount matching the fee, paid by the fee payer
        if from_address == fee_payer and abs(lamports) == fee and fee > 0:
            details["is_fee"] = True
            sol_amount = abs(lamports) / 1e9
            ctx.logger.info(f"Fee-only transaction detected: {sol_amount} SOL ({signature})")
            return details # Return early, marking as fee

    # Handle simple token transfers (NFT or Fungible) - ensure not part of a swap already processed
    token_transfers = tx.get("tokenTransfers", []) or []
    if token_transfers:
        # Prioritize NFT transfers if present
        nft_transfer = next((tt for tt in token_transfers if tt.get("tokenStandard") == "NonFungible"), None)
        if nft_transfer:
            token_mint = nft_transfer.get("mint", "")
            from_user = nft_transfer.get("fromUserAccount")
            to_user = nft_transfer.get("toUserAccount")
            details.update(
                type="nft_transfer",
                token=await get_token_symbol_helius(token_mint, ctx) if token_mint else "Unknown NFT",
                amount=1,
                from_address=f"{from_user[:4]}...{from_user[-4:]}",
                to_address=f"{to_user[:4]}...{to_user[-4:]}",
                program="Token Program"
            )
            ctx.logger.info(f"Parsed NFT Transfer: {details['token']} ({signature})")
            return details

        # Handle simple fungible token transfers
        # Check if there's exactly one fungible transfer and no significant native change for the user
        fungible_transfers = [tt for tt in token_transfers if tt.get("tokenStandard") == "Fungible"]
        user_native_change_simple = 0
        for acc_data in tx.get("accountData", []):
             if acc_data.get("account") == user_wallet:
                 user_native_change_simple = acc_data.get("nativeBalanceChange", 0)
                 break

        # If it's just one token transfer and minimal SOL movement (likely just fee)
        if len(fungible_transfers) == 1 and abs(user_native_change_simple) <= fee * 2: # Allow slight variation
            ft = fungible_transfers[0]
            token_mint = ft.get("mint", "")
            token_amount = ft.get("tokenAmount", 0)
            from_user = ft.get("fromUserAccount")
            to_user = ft.get("toUserAccount")

            if token_mint:
                token_symbol = await get_token_symbol_helius(token_mint, ctx)
                details.update(
                    type="token_transfer",
                    token=token_symbol,
                    amount=abs(token_amount),
                    program="Token Program",
                    from_address=f"{from_user[:4]}...{from_user[-4:]}",
                    to_address=f"{to_user[:4]}...{to_user[-4:]}"
                )
                ctx.logger.info(f"Parsed Simple Token Transfer: {details['amount']} {details['token']} ({signature})")
                return details

    # Handle simple SOL transfers (if not fee and not part of swap)
    if native_transfers:
        # Consider the largest SOL transfer if multiple exist, ignore tiny ones unless it's the only one
        significant_transfers = [nt for nt in native_transfers if abs(nt.get("amount", 0)) / 1e9 > 0.00001]
        transfer_to_parse = max(significant_transfers, key=lambda nt: abs(nt.get("amount",0)), default=None) \
                            if significant_transfers else (native_transfers[0] if native_transfers else None)

        if transfer_to_parse:
            lamports = transfer_to_parse.get("amount", 0)
            from_address = transfer_to_parse.get("fromUserAccount")
            to_address = transfer_to_parse.get("toUserAccount")
            sol_amount = abs(lamports) / 1e9

            # Ensure it wasn't just the fee payment already handled
            if not (from_address == fee_payer and abs(lamports) == fee):
                details.update(
                    type="token_transfer", # Use same type for consistency
                    token="SOL",
                    amount=sol_amount,
                    program="System Program",
                    from_address=f"{from_address[:4]}...{from_address[-4:]}",
                    to_address=f"{to_address[:4]}...{to_address[-4:]}"
                )
                ctx.logger.info(f"Parsed Simple SOL Transfer: {details['amount']} SOL ({signature})")
                return details

    # Handle NFT mints (often seen in accountData changes)
    # ... (NFT mint logic remains the same) ...
    account_data = tx.get("accountData", []) or []
    for account in account_data:
        token_changes = account.get("tokenBalanceChanges", []) or []
        for change in token_changes:
            if change.get("rawTokenAmount", {}).get("tokenAmount") == "1" and \
               change.get("rawTokenAmount", {}).get("decimals", -1) == 0 and \
               change.get("changeType") == "mintTo":
                mint = change.get("mint")
                user_account = change.get("userAccount")
                if mint:
                    nft_name = "Unknown NFT"
                    collection = "Unknown Collection"
                    
                    nft_events = tx.get("events", {}).get("nft", {})
                    if nft_events:
                        nft_name = nft_events.get("name", nft_name)
                        collection = nft_events.get("collectionName", collection)
                
                    details.update(
                        type="nft_mint",
                        token=nft_name,
                        collection=collection,  # Store collection information
                        amount=1,
                        program="Token Program",
                        from_address="Mint Authority",
                        to_address=f"{user_account[:4]}...{user_account[-4:]}"
                    )
                    ctx.logger.info(f"Parsed NFT Mint: {details['token']} ({signature})")
                    return details


    # If no specific type determined, log and return unknown
    ctx.logger.info(f"Transaction type UNKNOWN: {signature}")
    details["type"] = "unknown" # Explicitly mark as unknown if nothing else fits
    return details

async def summarize_activity(transactions, ctx: Context, activity_type: str = "general", days: int = 7, platform: str = None):
    """Summarize transactions based on activity type and time period with improved swap handling."""
    
    if activity_type == "defi" and platform:
        ctx.logger.info(f"Filtering DeFi activity specifically for platform: {platform}")
        # Filter transactions first by the requested platform
        platform_transactions = [
            tx for tx in transactions
            if isinstance(tx.get("details"), dict) and
               tx["details"].get("platform", "").upper() == platform.upper() and
               tx["details"].get("type") == "defi_swap" # Ensure it's still a swap
        ]
        if not platform_transactions:
             return f"No DeFi swap transactions found for the platform '{platform}' in the last {days} days."
        # Use only the platform-specific transactions for the DeFi summary
        return await generate_defi_summary(platform_transactions, ctx, days)
    elif activity_type == "defi":
         # If general DeFi query, use the existing DeFi summary
         return await generate_defi_summary(transactions, ctx, days)
    elif activity_type == "nft":
        # NFT summary doesn't typically filter by platform in this way
        return await generate_nft_summary(transactions, ctx, days)
    
    filtered = []
    seen_signatures = set()

    valid_types = {
        "general": ["token_transfer", "defi_swap", "nft_mint", "nft_transfer"],
        "nft": ["nft_mint", "nft_transfer"],
        "defi": ["defi_swap"]
    }

    # Re-check transaction types based on potentially updated details during parsing
    if (activity_type == "defi"):
        ctx.logger.info("Ensuring all DeFi transactions are marked as defi_swap if possible")
        for tx in transactions:
            if isinstance(tx.get("details"), dict):
                # If platform indicates a DEX, ensure type is defi_swap
                platform = tx["details"].get("platform", "Unknown Platform")
                if platform not in ["Unknown Platform", "DEX"] and tx["details"].get("type") != "defi_swap":
                    ctx.logger.warning(f"Correcting type to defi_swap based on platform {platform} for {tx.get('signature', 'unknown')[:8]}")
                    tx["details"]["type"] = "defi_swap"
                    # Ensure minimal swap details if missing after re-typing
                    if tx["details"].get("from_token", "Unknown Token") == "Unknown Token":
                        tx["details"]["from_token"] = "Token" # Fallback if truly unknown
                    if tx["details"].get("to_token", "Unknown Token") == "Unknown Token":
                        tx["details"]["to_token"] = "Token"
                    if tx["details"].get("from_amount", 0) == 0:
                        tx["details"]["from_amount"] = 1.0 # Placeholder amount
                    if tx["details"].get("to_amount", 0) == 0:
                        tx["details"]["to_amount"] = 1.0 # Placeholder amount


    for tx in transactions:
        # Ensure 'details' exists and is a dictionary before accessing it
        d = tx.get("details")
        if not isinstance(d, dict):
            ctx.logger.warning(f"Skipping transaction due to missing or invalid details: {tx.get('signature', 'unknown')[:8]}")
            continue

        signature = tx.get("signature", "unknown")

        if signature in seen_signatures:
            # ctx.logger.info(f"Skipping duplicate signature: {signature[:8]}") # Reduce noise
            continue

        tx_type = d.get("type", "unknown")
        if tx_type not in valid_types.get(activity_type, []):
            # ctx.logger.info(f"Skipping non-{activity_type} tx: {signature[:8]}, type={tx_type}") # Reduce noise
            continue
        
        tx_platform = d.get("platform", "").upper()
        if platform and tx_platform != platform.upper():
             # ctx.logger.info(f"Skipping tx {signature[:8]} due to platform mismatch (requested: {platform}, actual: {tx_platform})") # Optional logging
             continue
         
        # Skip transactions marked purely as fees
        if d.get("is_fee", False):
            ctx.logger.info(f"Skipping fee transaction: {signature[:8]}")
            continue

        seen_signatures.add(signature)
        filtered.append(tx)


    filtered.sort(key=lambda tx: tx.get("timestamp", 0)) # Added default for sorting safety

    if not filtered:
        context = "You are a helpful Solana wallet Tracking History agent. You analyze on-chain activity and provide clear insights about transactions including SOL transfers, token movements, NFT activities, and DeFi swaps across various platforms. You help users understand their on-chain activity."
        activity_label = {"general": "meaningful", "nft": "NFT", "defi": "DeFi"}[activity_type]
        platform_text = f" on {platform}" if platform else ""
        prompt = f"No {activity_label} transactions found in the last {days} days. Provide a friendly message suggesting the user check their wallet address or try a different time period."
        # Use max_tokens=150 for shorter messages
        return await get_completion(context, prompt, max_tokens=150)

    ctx.logger.info(f"Found {len(filtered)} valid {activity_type.upper()} transactions after filtering")

    sol_usd = await get_sol_usd_price()

    tx_details = [f"{activity_type.upper()} Transactions (Oldest First):"]
    for i, tx in enumerate(filtered, 1):
        d = tx["details"] # Already checked if d is a dict
        date_str = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%b %d %I:%M %p") # Added default
        amt = float(d.get("amount", 0)) # Added default
        sig = tx.get("signature", "unknown")[:8] # Added default
        from_addr = d.get("from_address", "Unknown")
        to_addr = d.get("to_address", "Unknown")
        platform = d.get("platform", "Unknown Platform") # Use the parsed platform

        # Handle missing or None fee_payer - FIX for the error
        fee_payer = tx.get("feePayer")
        user_abbrev = f"Unknown" # Default value
        if fee_payer:  # Only access fee_payer if it exists
            user_abbrev = f"{fee_payer[:4]}...{fee_payer[-4:]}"
            
        # Use 'Your wallet' consistently if address matches fee payer context
        if from_addr == user_abbrev or from_addr == "Your wallet": from_addr = "Your wallet"
        if to_addr == user_abbrev or to_addr == "Your wallet": to_addr = "Your wallet"

        usd_str = ""
        token_symbol = d.get("token", "Unknown Token") # Use parsed token or default
        if sol_usd and token_symbol == "SOL":
            usd_value = amt * sol_usd
            usd_str = f", ${usd_value:.2f}"
        elif token_symbol in ["USDC", "USDT"]:
            # Use the specific amount for stablecoins if available
            stable_amount = amt if d.get("type") != "defi_swap" else d.get("to_amount", amt) if d.get("to_token") == token_symbol else d.get("from_amount", amt)
            usd_str = f", ${stable_amount:.2f}"

        if d.get("type") == "defi_swap":
            # Use parsed swap details directly
            from_amount = d.get("from_amount", 0)
            to_amount = d.get("to_amount", 0)
            # Use the resolved token name (symbol or abbreviated address)
            from_token = d.get("from_token", "Unknown Token")
            to_token = d.get("to_token", "Unknown Token")

            # Add USD value if output is SOL or a known stablecoin
            swap_usd_str = ""
            if to_token == "SOL" and sol_usd and to_amount > 0:
                usd_value = to_amount * sol_usd
                swap_usd_str = f" (~${usd_value:.2f})"
            elif to_token in ["USDC", "USDT"] and to_amount > 0:
                swap_usd_str = f" (~${to_amount:.2f})"
            elif from_token in ["USDC", "USDT"] and from_amount > 0: # Show input value if output isn't stable/SOL
                 swap_usd_str = f" (~${from_amount:.2f})"


            # Format amounts nicely, avoid excessive precision for large numbers
            from_amount_str = f"{from_amount:.6f}".rstrip('0').rstrip('.') if from_amount != 0 else "Some"
            to_amount_str = f"{to_amount:.6f}".rstrip('0').rstrip('.') if to_amount != 0 else "Some"

            swap_info = f"{from_amount_str} {from_token} for {to_amount_str} {to_token}{swap_usd_str}"

            tx_details.append(f"{i}. {date_str}|SWAP|{swap_info} on {platform}|{from_addr} -> {to_addr}|{sig}")
        elif d.get("type") == "nft_mint":
            tx_details.append(f"{i}. {date_str}|NFT MINT|{token_symbol}|{from_addr} -> {to_addr}|{sig}")
        elif d.get("type") == "nft_transfer":
            tx_details.append(f"{i}. {date_str}|NFT TRANSFER|{token_symbol}|{from_addr} -> {to_addr}|{sig}")
        else: # Default to TRANSFER format
            amount_str = f"{amt:.6f}".rstrip('0').rstrip('.')
            tx_details.append(f"{i}. {date_str}|TRANSFER|{amount_str} {token_symbol}{usd_str}|{from_addr} -> {to_addr}|{sig}")
            
    time_period_text = ""
    if days < 1:
        hours = int(days * 24)
        time_period_text = f"last {hours} hours"
    elif days == 1:
        time_period_text = "last 24 hours"
    else:
        time_period_text = f"last {days} days"
        
    if not filtered:
        context = "You are a helpful Solana wallet Activity agent..."
        activity_label = {"general": "meaningful", "nft": "NFT", "defi": "DeFi"}[activity_type]
        prompt = f"No {activity_label} transactions found in the last {days} days. Provide a friendly message..."
        return await get_completion(context, prompt, max_tokens=150)

    context = "You are a specialized Solana blockchain analytics agent..."
    activity_label = {"general": "", "nft": "NFT", "defi": "DeFi"}[activity_type]
    platform_context = f" specifically on the {platform} platform" if platform else ""
    prompt = (
        f"List ALL {len(filtered)} {activity_label} transactions below EXACTLY as provided, in order (oldest first), from the {time_period_text}{platform_context}. "
        "Use the format: '<number>. <date> | <TYPE> | <Details> | <From> -> <To> | <Tx Sig>'. "
        "For SWAPs, show amounts, tokens (use symbol or abbreviated address like 'EPjF...Dt1v' if symbol unknown), platform, and approximate USD value if possible (e.g., 'SWAP 100 USDC for 1 SOL (~$150.00) on JUPITER'). "
        "For NFTs, show token name/symbol. For TRANSFERS, show amount and token. "
        "Use 'Your wallet' for the user's address. Include EVERY transaction. "
        "End with 'Ask more about your wallet!' Do NOT summarize or omit transactions.\n\n"
        + "\n".join(tx_details)
    )

    # ... existing LLM call and fallback logic ...
    try:
        ctx.logger.info(f"LLM prompt length: {len(prompt)} chars, {len(filtered)} txs")
        # Increase max_tokens slightly if needed for longer token names/platforms
        summary = await get_completion(context, prompt, max_tokens=2500)
        ctx.logger.info(f"LLM response length: {len(summary)} chars")

        # Basic check if LLM likely included most transactions
        numbered_lines = len([line for line in summary.split('\n') if re.match(r'^\d+\.', line.strip())])
        ctx.logger.info(f"LLM output included {numbered_lines}/{len(filtered)} numbered lines")
        # Use fallback if LLM output seems significantly truncated
        if numbered_lines < len(filtered) * 0.8: # Allow for some minor formatting differences
            ctx.logger.warning(f"LLM output seems truncated ({numbered_lines}/{len(filtered)}), using fallback.")
            return generate_compact_fallback(filtered, sol_usd, activity_type, days)

        # Ensure closing phrase is present
        if "ask more" not in summary.lower():
            summary += "\n\nAsk more about your wallet!"

        return summary
    except Exception as e:
        ctx.logger.error(f"Error getting LLM completion: {e}")
        return generate_compact_fallback(filtered, sol_usd, activity_type, days, platform)


def generate_compact_fallback(transactions, sol_usd, activity_type: str = "general", days: int = 7, platform: str = None):
    """Generate a compact summary with categorized transactions (fallback)."""
    platform_text = f" on {platform}" if platform else ""
    lines = [f"Summary of your Solana {activity_type.upper()} activity{platform_text} (last {days} days, oldest first):\n"]
    categories = {"TRANSFER": [], "SWAP": [], "NFT": []}

    for i, tx in enumerate(transactions, 1):
        # Always use get() with default values to avoid None errors
        d = tx.get("details", {})
        date_str = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%b %d %H:%M")
        amt = float(d.get("amount", 0))
        sig = tx.get("signature", "unknown")[:6]
        from_addr = d.get("from_address", "?")
        to_addr = d.get("to_address", "?")
        platform = d.get("platform", "DEX")
        token_symbol = d.get("token", "Unknown")

        # Use generic "Wallet" if fee_payer is missing
        user_abbrev = "Your wallet"
        fee_payer = tx.get("feePayer")
        if fee_payer:
            user_abbrev = f"{fee_payer[:4]}...{fee_payer[-4:]}"
            
        if from_addr == user_abbrev or from_addr == "Your wallet": from_addr = "You"
        if to_addr == user_abbrev or to_addr == "Your wallet": to_addr = "You"


        usd_str = ""
        if sol_usd and token_symbol == "SOL":
            usd_value = amt * sol_usd
            usd_str = f" (~${usd_value:.2f})"
        elif token_symbol in ["USDC", "USDT"]:
             stable_amount = amt if d.get("type") != "defi_swap" else d.get("to_amount", amt) if d.get("to_token") == token_symbol else d.get("from_amount", amt)
             usd_str = f" (~${stable_amount:.2f})"


        if d.get("type") == "defi_swap":
            from_amount = d.get("from_amount", 0)
            to_amount = d.get("to_amount", 0)
            from_token = d.get("from_token", "Unknown") # Use parsed name
            to_token = d.get("to_token", "Unknown")   # Use parsed name

            # Add USD value if output is SOL or a known stablecoin
            swap_usd_str = ""
            if to_token == "SOL" and sol_usd and to_amount > 0:
                usd_value = to_amount * sol_usd
                swap_usd_str = f" (~${usd_value:.2f})"
            elif to_token in ["USDC", "USDT"] and to_amount > 0:
                swap_usd_str = f" (~${to_amount:.2f})"
            elif from_token in ["USDC", "USDT"] and from_amount > 0:
                 swap_usd_str = f" (~${from_amount:.2f})"

            from_amount_str = f"{from_amount:.4f}".rstrip('0').rstrip('.') if from_amount != 0 else "?"
            to_amount_str = f"{to_amount:.4f}".rstrip('0').rstrip('.') if to_amount != 0 else "?"

            line = f"- {date_str}: SWAP {from_amount_str} {from_token} for {to_amount_str} {to_token}{swap_usd_str} on {platform} ({sig})"
            categories["SWAP"].append(line)
        elif d.get("type") in ["nft_mint", "nft_transfer"]:
            type_str = "MINT" if d.get("type") == "nft_mint" else f"XFER {token_symbol}"
            line = f"- {date_str}: NFT {type_str} | {from_addr} -> {to_addr} ({sig})"
            categories["NFT"].append(line)
        else: # Default to TRANSFER
            amount_str = f"{amt:.4f}".rstrip('0').rstrip('.')
            line = f"- {date_str}: XFER {amount_str} {token_symbol}{usd_str} | {from_addr} -> {to_addr} ({sig})"
            categories["TRANSFER"].append(line)

    # ... existing fallback category printing logic ...
    has_content = False
    for category, txs in categories.items():
        # Check if this category is relevant for the requested activity type
        is_relevant = (
            activity_type == "general" or
            (activity_type == "nft" and category == "NFT") or
            (activity_type == "defi" and category == "SWAP")
        )
        if txs and is_relevant:
            lines.append(f"\n{category}S:")
            lines.extend(txs)
            has_content = True

    if not has_content: # If filtering removed everything relevant
         lines = [f"No relevant {activity_type.upper()} transactions found in the last {days} days."]


    lines.append("\n\nAsk more about your wallet!")
    return "\n".join(lines)

async def register_with_almanac(ctx: Context):
    """Register agent with Almanac with retries."""
    try:
        ctx.logger.info("Attempting to register with Almanac...")
        # Simulating registration success for now
        await asyncio.sleep(0.1) # Simulate network delay
        ctx.logger.info("Successfully registered with Almanac")
    except asyncio.TimeoutError:
        ctx.logger.warning("Almanac registration timed out - continuing without registration")
    except Exception as e:
        ctx.logger.warning(f"Almanac registration failed: {e}")
        ctx.logger.info("Continuing without Almanac registration")

@agent.on_event("startup")
async def on_startup(ctx: Context):
    """Handle agent startup with safer Almanac registration checks."""
    ctx.logger.info(f"Agent started with address: {agent.address}")
    try:
        # Check if Almanac registration is available using a safer approach
        # The attribute may not exist in some versions of uagents
        try:
            almanac_disabled = getattr(agent, "almanac_contracts_disabled", False)

            # If almanac is not explicitly disabled, try to register
            if not almanac_disabled:
                try:
                    await asyncio.wait_for(register_with_almanac(ctx), timeout=ALMANAC_TIMEOUT)
                except asyncio.TimeoutError:
                    ctx.logger.warning(f"Almanac registration timed out after {ALMANAC_TIMEOUT}s - continuing without it")
        except Exception as e:
            ctx.logger.warning(f"Skipping Almanac registration due to: {str(e)}")
            ctx.logger.info("Agent will function without Almanac registration")
    except Exception as e:
        ctx.logger.warning(f"Error during startup: {str(e)}")
        ctx.logger.info("Continuing agent startup despite errors")

@chat_proto.on_message(model=ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    """Process incoming chat messages with wallet memory system and advanced pattern recognition."""
    # 1. Initial validation and message extraction
    if not isinstance(msg.content, list) or not msg.content:
        ctx.logger.error(f"Received invalid message content from {sender}: {msg.content}")
        return

    first_content = msg.content[0]
    if not hasattr(first_content, 'text'):
        ctx.logger.error(f"Received message content without text from {sender}: {first_content}")
        return
    
    request_id = str(uuid4())
    raw = first_content.text.strip()
    text = raw.lower()
    ctx.logger.info(f"Message from {sender}: {raw}")

    # 2. Store session info and send acknowledgement
    ctx.storage.set(str(ctx.session), sender)
    try:
        await ctx.send(
            sender,
            ChatAcknowledgement(timestamp=datetime.now(), acknowledged_msg_id=msg.msg_id)
        )
    except Exception as e:
        ctx.logger.error(f"Failed to send acknowledgement: {e}")

    # 3. Initialize response variables
    response = None
    wallet_address = None
    
    # 4. Enhanced pattern recognition with regex
    # Wallet/Activity related patterns
    wallet_pattern = r'\b(wallet|address|(?:on[-\s]?chain)|account|ledger)\b'
    activity_pattern = r'\b(activit(?:y|ies)|transaction|history|record|log|event)\b'
    asset_pattern = r'\b(nft|token|sol|usdc|swap|defi|crypto|coin)\b'
    action_pattern = r'\b(check|show|view|tell|give|look|find|what(?:[\'s]|\sis|\shappened))\b'
    collection_search_pattern = r'\b(?:my|show|check|what(?:\'s|s|\sis))\s+(?:my\s+)?([\w\s]+)\s+(?:nft|collection|collectible)s?\b'
    
    is_wallet_query = (
        re.search(wallet_pattern, text) is not None or 
        re.search(activity_pattern, text) is not None or
        (re.search(action_pattern, text) is not None and (
            re.search(wallet_pattern, text) is not None or 
            re.search(activity_pattern, text) is not None or
            re.search(asset_pattern, text) is not None)
        )
    )
    
    collection_match = re.search(collection_search_pattern, text)
    if collection_match:
        collection_name = collection_match.group(1).strip()
    
    # Possessive forms with word boundaries
    possessive_pattern = r'\b(my|mine|our|mis?|me[uios]|mein|mon|ma|notr?e|我的|私の|내|나의|moйяе)\b'
    is_my_query = re.search(possessive_pattern, text) is not None
    
    # Gratitude detection with word boundaries
    gratitude_pattern = r'\b(thank|thanks|thx|ty|grateful|appreciate|good\s+job|awesome|great)\b'
    is_gratitude = re.search(gratitude_pattern, text) is not None
    is_only_gratitude = is_gratitude and not is_wallet_query
    
    # Swap detection with better pattern matching
    swap_pattern = r'\b(?:swap|exchange|trade)s?(?:\s+(?:history|activity|transactions|records))?\b'
    is_general_swap_query = re.search(swap_pattern, text) is not None
    
    # About agent detection
    about_agent_pattern = r'\b(who\s+are\s+you|what\s+(?:do|can)\s+you\s+do|how\s+(?:do\s+you|to|can\s+I)|tell\s+(?:me\s+)?about\s+(?:you|yourself)|help(?:\s+me)?)\b'
    is_about_agent = re.search(about_agent_pattern, text) is not None
    
    # Follow-up question detection
    followup_pattern = r'\b(previous|transaction|that\s+swap|tell\s+me\s+more|explain|what\s+about|first\s+one|last\s+one|earlier)\b'
    is_followup = re.search(followup_pattern, text) is not None
    
    # Wallet commands detection
    wallet_cmd_pattern = r'\b(?:use|set|save|remember|store|update|change|switch)\s+(?:my\s+)?(?:wallet|address)\b'
    wallet_is_pattern = r'\bmy\s+wallet\s+(?:address\s+)?is\b'
    is_wallet_command = re.search(wallet_cmd_pattern, text) is not None or re.search(wallet_is_pattern, text) is not None
    
    # Forget wallet detection
    forget_pattern = r'\b(?:forget|remove|clear|delete|reset)\s+(?:my\s+)?(?:wallet|address)\b'
    is_forget_command = re.search(forget_pattern, text) is not None
    
    # NFT activity detection
    nft_activity_pattern = r'\b(?:my|show|check|what(?:\'s|s|\sis))\s+(?:my\s+)?nft\s+activity\b'
    is_nft_query = re.search(nft_activity_pattern, text) is not None
    
    # Save wallet detection for using saved wallet
    using_saved_wallet = is_my_query and (
        is_wallet_query or 
        re.search(r'\b(activity|transactions|defi|nft|collectibles?|assets?)\b', text) is not None
    )

    # 5. Process gratitude messages (simple case)
    if is_only_gratitude:
        gratitude_responses = [
            "You're welcome! I'm here whenever you need to check your Solana wallet activity.",
            "Thank you for the kind words. Happy to assist with your Solana analytics needs!",
            "Glad I could help! Let me know if you need anything else with your wallet activity.",
            "I appreciate that! Feel free to ask anytime you want to check your on-chain activity.",
            "Always a pleasure to assist with Solana transaction analysis. Let me know when you need more insights!"
        ]
        response_index = sum(ord(c) for c in raw) % len(gratitude_responses)
        response = gratitude_responses[response_index]
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(),
                msg_id=str(uuid4()),
                content=[TextContent(type="text", text=response)]
            )
        )
        return

    # 6. About Agent queries
    if is_about_agent:
        agent_info = (
            "I'm a Solana blockchain analytics assistant that helps you track and understand "
            "your on-chain activity. I can analyze:\n\n"
            "• Token transfers and balances\n"
            "• NFT movements and mints\n"
            "• DeFi swaps across Jupiter, Raydium, Orca, and other platforms\n\n"
            "To get started, you can:\n"
            "• Save your wallet: 'use wallet <address>'\n"
            "• Then simply ask: 'What's my wallet activity?'\n"
            "• Or specify a time period: 'Check my DeFi swaps from the last 24h'\n\n"
            "I support specific time periods like '24h', '7d', 'last month', etc."
        )
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(),
                msg_id=str(uuid4()),
                content=[TextContent(type="text", text=agent_info)]
            )
        )
        return

    # 7. Wallet command processing
    if is_wallet_command:
        # Extract Solana address using regex
        address_pattern = r'[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{32,44}'
        address_match = re.search(address_pattern, raw)
        
        if address_match and is_valid_solana_address(address_match.group(0)):
            wallet_address = address_match.group(0)
            current_wallet = storage.get_user_wallet(sender) # Use storage
            
            if current_wallet and current_wallet != wallet_address:
                storage.set_user_wallet(sender, wallet_address) # Use storage
                response = f"I've updated your wallet from {current_wallet[:6]}...{current_wallet[-6:]} to {wallet_address[:6]}...{wallet_address[-6:]}. You can now check your activity without providing the address each time."
            else:
                storage.set_user_wallet(sender, wallet_address) # Use storage
                response = f"I've saved {wallet_address[:6]}...{wallet_address[-6:]} as your default wallet. Now you can check your activity without providing the address each time. Try asking 'What's my wallet activity in the last 7 days?'"
        else:
            response = "I couldn't find a valid Solana address in your message. Please try again with a valid address like 'use wallet 78hxw2Hqzns...xZzZMkwtew'"
        
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(),
                msg_id=str(uuid4()),
                content=[TextContent(type="text", text=response)]
            )
        )
        return

    # 8. Forget wallet command
    if is_forget_command:
        if storage.get_user_wallet(sender): # Use storage
            storage.delete_user_wallet(sender) # Use storage
            response = "I've removed your saved wallet address. You'll need to provide your wallet address for future queries or set a new default wallet."
        else:
            response = "You don't have a saved wallet address yet. To save one, try 'use wallet <your-address>'."
        
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(),
                msg_id=str(uuid4()),
                content=[TextContent(type="text", text=response)]
            )
        )
        return

    # 9. NFT activity specific query
    if is_nft_query:
        activity_type = "nft"
        days = 30
        
        wallet_address = storage.get_user_wallet(sender) # Use storage
        if wallet_address:
            ctx.logger.info(f"Using saved wallet for NFT query: {wallet_address[:8]}...")
        else:
            help_msg = "I'd be happy to show your NFT activity, but I don't have your wallet address saved. Please either include your address in the message or save a default wallet with 'use wallet <your-address>'."
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=help_msg)]
                )
            )
            return

    # 10. General swap query processing
    if is_general_swap_query:
        user_data = storage.get_transaction_history(sender) # Use storage
        if user_data:
            last_txs = user_data.get("transactions", [])
            
            # Parse query to extract platform and time period
            _, days_from_query, platform_filter = parse_query(raw)
            ctx.logger.info(f"Swap query detected with platform: {platform_filter or 'Any'}")
            
            # Find all swaps in stored transactions
            all_swaps = []
            for tx in last_txs:
                details = tx.get("details", {})
                if details.get("type") == "defi_swap":
                    tx_platform = details.get("platform", "").upper()
                    # Only include if platform matches filter (if specified)
                    if not platform_filter or platform_filter.upper() == tx_platform:
                        all_swaps.append(tx)
            
            if all_swaps:
                # Format the swap details with nicely grouped information by platform
                swaps_by_platform = {}
                
                # If platform filter is specified, only show that platform
                if platform_filter:
                    platform_name = platform_filter.upper()
                    # Create a section just for the specified platform
                    platform_swaps = [
                        tx for tx in all_swaps 
                        if tx.get("details", {}).get("platform", "").upper() == platform_name
                    ]
                    
                    if platform_swaps:
                        swaps_by_platform[platform_name] = platform_swaps
                        swap_info = f"Here's a summary of your {platform_filter} swaps:\n\n"
                    else:
                        swap_info = f"No swaps found for platform {platform_filter} in your recent transactions."
                        await ctx.send(
                            sender,
                            ChatMessage(
                                timestamp=datetime.now(),
                                msg_id=str(uuid4()),
                                content=[TextContent(type="text", text=swap_info)]
                            )
                        )
                        return
                else:
                    # Otherwise group all swaps by platform
                    swap_info = "Here's a summary of your swaps from the last query:\n\n"
                    for swap in all_swaps:
                        details = swap.get("details", {})
                        platform = details.get("platform", "Unknown Platform").upper()
                        if platform not in swaps_by_platform:
                            swaps_by_platform[platform] = []
                        swaps_by_platform[platform].append(swap)
                
                # Go through each platform
                for platform, platform_swaps in swaps_by_platform.items():
                    swap_info += f"**{platform} SWAPS ({len(platform_swaps)}):**\n\n"
                    
                    # Sort by timestamp, newest first
                    platform_swaps.sort(key=lambda tx: tx.get("timestamp", 0), reverse=True)
                    
                    # Format each swap
                    for tx in platform_swaps:
                        details = tx.get("details", {})
                        date_str = datetime.fromtimestamp(tx.get("timestamp", 0)).strftime("%b %d %I:%M %p")
                        from_amount = details.get("from_amount", 0)
                        from_token = details.get("from_token", "Unknown")
                        to_amount = details.get("to_amount", 0)
                        to_token = details.get("to_token", "Unknown")
                        signature = tx.get("signature", "")
                        
                        # Format amounts nicely
                        from_amount_str = f"{from_amount:.6f}".rstrip('0').rstrip('.') if from_amount else "Unknown"
                        to_amount_str = f"{to_amount:.6f}".rstrip('0').rstrip('.') if to_amount else "Unknown"
                        
                        swap_info += f"{date_str}: {from_amount_str} {from_token} ➝ {to_amount_str} {to_token}"
                        if signature:
                            swap_info += f" Transaction: https://solscan.io/tx/{signature}\n"
                        else:
                            swap_info += "\n"
                    
                    swap_info += "\n"
                
                # Add a closing line with tip
                if platform_filter:
                    swap_info += f"You can ask about all swaps with 'Show my swap activity' or about other platforms like 'Show my Raydium swaps'."
                else:
                    swap_info += f"You can filter by specific platforms like 'Show my Jupiter swaps' or 'Show my Raydium swaps'."
                
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=swap_info)]
                    )
                )
                return
            else:
                no_swaps_msg = "I couldn't find any swap transactions in your recent activity. Try checking a longer time period or make sure you're checking the correct wallet."
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=no_swaps_msg)]
                    )
                )
                return
        else:
            no_history_msg = "I don't have any transaction history for you yet. Please first check your wallet activity by asking 'Show wallet activity for <your-address>' or 'What's my wallet activity?' if you've saved a wallet."
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=no_history_msg)]
                )
            )
            return

    # 11. Follow-up questions
    user_data = storage.get_transaction_history(sender) # Use storage
    if is_followup and user_data:
        last_wallet = user_data.get("last_wallet")
        last_txs = user_data.get("transactions", [])
        last_summary = user_data.get('last_summary', 'No previous summary available')

        if last_wallet and last_txs:
        # Create context for LLM with previous response and transaction data
            followup_context = (
                "You are a Solana blockchain analytics assistant responding to a follow-up question. "
                "The user previously received information about their wallet transactions, and now has a follow-up question. "
                "Review the provided previous summary and answer their specific question about the transactions. "
                "If the question asks about specific transaction details that aren't in the summary, explain that you need more specificity. "
                "If the question is about a transaction number, refer to that numbered transaction in the previous summary. "
                "If they ask about overall patterns or specific tokens/platforms in their history, use the summary to inform your answer."
            )
        
        # Extract specific aspects mentioned in the follow-up
            specific_tx_match = re.search(r'transaction(?:\s+#?)?\s*(\d+)', text)
            specific_token_match = re.search(r'(?:about|tell\s+me\s+about)\s+(\w+)\s+(?:token|coin)', text)
            specific_platform_match = re.search(r'(?:about|on)\s+(jupiter|raydium|orca|pump)', text)

            prompt = (
                f"The user asked: '{raw}'\n\n"
                f"Their previous transaction summary was:\n{last_summary}\n\n"
            )
            
            # Add contextual hints based on what they're asking about
            if specific_tx_match:
                tx_num = specific_tx_match.group(1)
                prompt += f"They seem to be asking about transaction #{tx_num}. Find and provide details for this specific transaction.\n"
            elif specific_token_match:
                token = specific_token_match.group(1).upper()
                prompt += f"They seem to be asking about {token} token transactions. Find and summarize these transactions.\n"
            elif specific_platform_match:
                platform = specific_platform_match.group(1).upper()
                prompt += f"They seem to be asking about {platform} platform activity. Provide details focusing on this platform.\n"
            
            prompt += "Provide a concise, helpful response addressing their specific follow-up question."
        
            try:
            # Call LLM for intelligent follow-up handling
                ctx.logger.info(f"Processing follow-up question via LLM: {raw}")
                response = await get_completion(followup_context, prompt, max_tokens=3000)
                
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=response)]
                    )
                )
                return
            except Exception as e:
                ctx.logger.error(f"LLM failed for follow-up: {e}")
                # Fall back to the original approach if LLM fails
                followup_response = (
                    "I noticed you're asking about the previous transactions. "
                    "Here's a summary of what I showed you earlier:\n\n"
                    f"{last_summary}\n\n"
                    "For more specific details, you can ask about a particular transaction by number or date."
                )
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=followup_response)]
                    )
                )
                return

    # 12. Using saved wallet or finding wallet in message
    if using_saved_wallet:
        wallet_address = storage.get_user_wallet(sender) # Use storage
        if not wallet_address:
            response = "You don't have a saved wallet address yet. Please provide a wallet address or save one first with 'use wallet <your-address>'."
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=response)]
                )
            )
            return
        ctx.logger.info(f"Using saved wallet address for {sender}: {wallet_address[:8]}...")
    else:
        # Try to find a wallet address in the message using regex
        address_match = re.search(r'[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{32,44}', raw)
        if address_match and is_valid_solana_address(address_match.group(0)):
            wallet_address = address_match.group(0)
            ctx.logger.info(f"Found wallet address in message: {wallet_address[:8]}...")

    # 13. Process wallet activity
    saved_wallet = storage.get_user_wallet(sender) # Use storage
    if (is_wallet_query or using_saved_wallet or is_nft_query) and (wallet_address or saved_wallet):
        if not wallet_address and saved_wallet:
            wallet_address = saved_wallet
            
        activity_type, days, platform = parse_query(raw)
        
        # Override activity type if it's an explicit NFT query
        if is_nft_query:
            activity_type = "nft"
            days = max(days, 30)  # Use at least 30 days for NFT queries
            platform = None  # No platform filtering for NFT queries
            
        if days <= 0 or days > 365:
            error_msg = f"The specified time period ({days} days) is invalid. Please use a positive number of days up to 365, e.g., 'last 7 days'."
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=error_msg)]
                )
            )
            return

        if not wallet_address:
            if saved_wallet: # Check again if we fell through without wallet_address but have a saved one
                use_existing = f"You have a saved wallet, try asking 'What's my wallet activity in the last {days} days?'"
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=use_existing)]
                    )
                )
            else:
                help_msg = "You asked about wallet activity but didn't provide a wallet address. You can either include your address in the message or save a default wallet with 'use wallet <your-address>'."
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(),
                        msg_id=str(uuid4()),
                        content=[TextContent(type="text", text=help_msg)]
                    )
                )
            return
            
        # Process wallet activity
        try:
            # Log with hours for clarity when using small time periods
            if days < 2:
                hours = int(days * 24)
                ctx.logger.info(f"Processing {activity_type.upper()} activity for {wallet_address} over {hours} hours")
            else:
                ctx.logger.info(f"Processing {activity_type.upper()} activity for {wallet_address} over {days} days")
                
            time_log = f"{int(days * 24)} hours" if days < 2 else f"{days} days"
            platform_log = f" on platform {platform}" if platform else ""
            ctx.logger.info(f"Processing {activity_type.upper()} activity for {wallet_address} over {time_log}{platform_log}")
            
            txs = await fetch_wallet_activity(wallet_address, days=days, ctx=ctx)
            summary = await summarize_activity(txs, ctx, activity_type=activity_type, days=days, platform=platform)

            # Save this query info for transaction history feature
            history_data = { # Use storage
                "last_wallet": wallet_address,
                "transactions": txs,
                "last_summary": summary,
                "timestamp": datetime.now().isoformat() # Use ISO format for JSON compatibility if needed, or keep datetime if pickle handles it
            }
            storage.set_transaction_history(sender, history_data) # Use storage

            # If this was a new wallet and not already saved, suggest saving it
            current_saved_wallet = storage.get_user_wallet(sender) # Check storage again
            if wallet_address != current_saved_wallet and not using_saved_wallet:
                summary += f"\n\nTip: You can save this wallet address for future queries by typing 'use wallet {wallet_address}'"

            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=summary)]
                )
            )
            return
        except Exception as e:
            ctx.logger.error(f"Error processing wallet: {e}", exc_info=True)
            error_msg = "There was an error retrieving or processing wallet data. Please try again later or verify the wallet address."
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(),
                    msg_id=str(uuid4()),
                    content=[TextContent(type="text", text=error_msg)]
                )
            )
            return

    # 14. Default response for unclear queries
    help_msg = (
        "I'm your Solana blockchain assistant. Here's how I can help:\n\n"
        "• Check wallet activity by providing an address: 'Show wallet activity for 78hxw2Hqzns...'\n"
        "• Save your address for easier use: 'use wallet 78hxw2Hqzns...'\n"
        "• Then simply ask: 'What's my wallet activity in the last 7 days?'\n\n"
        "You can check general activity, NFT transactions, or DeFi swaps over any time period."
    )

    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(),
            msg_id=str(uuid4()),
            content=[TextContent(type="text", text=help_msg)]
        )
    )

@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Ack from {sender} for msg {msg.acknowledged_msg_id}")
    

agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    agent.run()