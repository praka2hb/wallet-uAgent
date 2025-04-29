![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:domain/solana](https://img.shields.io/badge/domain-solana-14F195)

**Description**: This Solana Wallet Analytics Agent helps users track and understand their on-chain activity on the Solana blockchain. It provides detailed insights about token transfers, NFT movements, and DeFi swaps across various platforms like Jupiter, Raydium, and Orca. Users can query their wallet activity over custom time periods and get organized transaction summaries.

**Features**:
- Track general wallet activity (transfers, swaps, NFT movements)
- Filter for specific transaction types (NFT, DeFi)
- Save wallet addresses for easy future reference
- View activity over custom time periods (e.g., 24h, 7d, 30d)
- Get transaction details with USD value estimates when available

**Usage Examples**:
- `use wallet <address>` - Save a wallet address for future queries
- `What's my wallet activity in the last 7 days?` - View recent transactions
- `Show my NFT activity from the last 30 days` - Filter for NFT transactions
- `What DeFi swaps in my wallet over 24h?` - View recent DeFi activity

**Technical Details**:
The agent uses Helius API for Solana transaction data and leverages large language models to provide user-friendly summaries of complex blockchain data.

**Input**: Natural language queries about wallet activity
**Output**: Formatted transaction summaries with detailed insights