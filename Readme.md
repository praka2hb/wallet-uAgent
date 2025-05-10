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

1. **Heroku Setup**
   ```bash
   # Install Heroku CLI
   npm install -g heroku

   # Login to Heroku
   heroku login

   # Create a new Heroku app
   heroku create your-app-name

   # Add Heroku Redis addon
   heroku addons:create heroku-redis:hobby-dev

   # Set environment variables
   heroku config:set HELIUS_API_KEY=your_helius_key
   heroku config:set ASI_API_KEY=your_asi_key
   heroku config:set AGENT_SEED=your_agent_seed
   heroku config:set REDIS_PASSWORD=your_redis_password
   ```

2. **Deploy to Heroku**
   ```bash
   # Initialize git if not already done
   git init
   git add .
   git commit -m "Initial commit"

   # Add Heroku remote
   heroku git:remote -a your-app-name

   # Deploy
   git push heroku main
   ```

3. **Verify Deployment**
   ```bash
   # Check logs
   heroku logs --tail

   # Open the app
   heroku open
   ```

**Environment Variables Required**:
- `HELIUS_API_KEY`: Your Helius API key
- `ASI_API_KEY`: Your ASI API key
- `AGENT_SEED`: Seed for agent generation
- `REDIS_PASSWORD`: Redis password (if using custom Redis)
- `REDIS_URL`: Heroku Redis URL (automatically set by Heroku Redis addon)
- `PORT`: Port number (automatically set by Heroku)



