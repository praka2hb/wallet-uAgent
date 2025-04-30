import json
import pickle
from datetime import datetime, timedelta
import traceback

class StorageManager:
    """Manages persistent storage needs for the Solana wallet agent using Redis."""
    
    def __init__(self, redis_client, ttl_days=30):
        """Initialize with an existing Redis client."""
        self.redis = redis_client
        self.ttl_seconds = ttl_days * 24 * 60 * 60  # Convert days to seconds
        self._verify_connection()
        
    def _verify_connection(self):
        """Verify Redis connection works."""
        try:
            self.redis.ping()
            print("✅ Connected to Redis storage")
        except Exception as e:
            print(f"⚠️ Redis connection issue: {e}")
            traceback.print_exc()
    
    # ----- Token Cache Methods -----
    
    def get_token(self, mint_address):
        """Get token symbol from cache."""
        key = f"token:{mint_address}"
        value = self.redis.get(key)
        if value:
            return value.decode('utf-8')
        return None
    
    def set_token(self, mint_address, symbol, ttl=None):
        """Cache token symbol with TTL."""
        key = f"token:{mint_address}"
        ttl = ttl or self.ttl_seconds
        self.redis.set(key, symbol, ex=ttl)
    
    def get_tokens_batch(self, mint_addresses):
        """Get multiple token symbols at once for efficiency."""
        if not mint_addresses:
            return {}
            
        pipe = self.redis.pipeline()
        for addr in mint_addresses:
            pipe.get(f"token:{addr}")
        
        results = pipe.execute()
        return {
            addr: (results[i].decode('utf-8') if results[i] else None) 
            for i, addr in enumerate(mint_addresses)
        }
    
    # ----- User Wallet Methods -----
    
    def get_user_wallet(self, user_id):
        """Get saved wallet for user."""
        key = f"wallet:{user_id}"
        value = self.redis.get(key)
        if value:
            return value.decode('utf-8')
        return None
    
    def set_user_wallet(self, user_id, wallet_address):
        """Save wallet for user (permanent)."""
        key = f"wallet:{user_id}"
        self.redis.set(key, wallet_address)  # No expiration for wallet addresses
    
    def delete_user_wallet(self, user_id):
        """Remove user's saved wallet."""
        key = f"wallet:{user_id}"
        self.redis.delete(key)
    
    def get_all_users_with_wallets(self):
        """Get all users who have saved wallets."""
        keys = self.redis.keys("wallet:*")
        result = {}
        for key in keys:
            user_id = key.decode('utf-8').split(':')[1]
            wallet = self.redis.get(key)
            if wallet:
                result[user_id] = wallet.decode('utf-8')
        return result
    
    # ----- Transaction History Methods -----
    
    def get_transaction_history(self, user_id):
        """Get stored transaction history for user."""
        key = f"txhistory:{user_id}"
        data = self.redis.get(key)
        if data:
            try:
                return pickle.loads(data)
            except Exception as e:
                print(f"Error deserializing transaction history: {e}")
        return {}
    
    def set_transaction_history(self, user_id, history_data):
        """Save transaction history with TTL."""
        key = f"txhistory:{user_id}"
        try:
            serialized = pickle.dumps(history_data)
            self.redis.set(key, serialized, ex=self.ttl_seconds)
        except Exception as e:
            print(f"Error serializing transaction history: {e}")
    
    # ----- NFT Collection Methods -----
    
    def get_nft_collection(self, collection_id):
        """Get cached NFT collection data."""
        key = f"nftcoll:{collection_id.lower()}"
        data = self.redis.get(key)
        if data:
            try:
                return json.loads(data)
            except Exception:
                return None
        return None
    
    def set_nft_collection(self, collection_id, collection_data):
        """Cache NFT collection data."""
        key = f"nftcoll:{collection_id.lower()}"
        try:
            self.redis.set(key, json.dumps(collection_data), ex=self.ttl_seconds)
        except Exception as e:
            print(f"Error caching NFT collection: {e}")
    
    # ----- Price Cache Methods -----
    
    def get_price(self, token_id):
        """Get cached price data."""
        key = f"price:{token_id.lower()}"
        data = self.redis.get(key)
        if data:
            try:
                return float(data.decode('utf-8'))
            except Exception:
                return None
        return None
    
    def set_price(self, token_id, price, ttl=3600):  # Default 1 hour TTL for prices
        """Cache price data with shorter TTL."""
        key = f"price:{token_id.lower()}"
        self.redis.set(key, str(price), ex=ttl)
    
    # ----- Metrics and Diagnostics -----
    
    def get_metrics(self):
        """Get storage usage metrics."""
        metrics = {}
        
        # Count by key type
        metrics["token_cache_size"] = len(self.redis.keys("token:*"))
        metrics["users_with_wallets"] = len(self.redis.keys("wallet:*"))
        metrics["transaction_histories"] = len(self.redis.keys("txhistory:*"))
        metrics["nft_collections"] = len(self.redis.keys("nftcoll:*"))
        metrics["price_cache"] = len(self.redis.keys("price:*"))
        
        return metrics
    
    def flush_all_data(self):
        """Development use only - remove all data (DANGER)."""
        self.redis.flushdb()
        return "All data flushed"

    def health_check(self):
        """Check Redis connection health."""
        try:
            self.redis.ping()
            return True
        except Exception:
            return False