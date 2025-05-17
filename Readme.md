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

**Key Details**
- Name : solana_wallet_agent
- Address: agent1qdz8a9569fu9dagkattke6ax0j2ew34ha0k6dh990ktz086yr4ga6h7y07l

**Work Flow**

![image](https://github.com/user-attachments/assets/a0ba33e3-f1b6-4444-846e-3137f5d9ef02)

**Showcasing Video:**

https://github.com/user-attachments/assets/927057e2-6d12-4a7f-9ed2-de2d420b630e

**Deployment Instructions**

**Agent Setup**
   ```bash
   # Install Dependencies

   git clone https://github.com/praka2hb/wallet-uAgent.git
   cd wallet-uAgent

   pip install -r requirements.txt

   #Setup .env
   - HELIUS_API_KEY = Your Helius API key
   - ASI_API_KEY = Your_ASI_API_key
   - AGENT_SEED = Seed_for_agent_generation
   - REDIS_HOST = Redis_Host
   - REDIS_PASSWORD = Redis_Password 
   - REDIS_PORT = Redis_Port

   #Run The Agent
   python src/ai_agent.py


   ```





