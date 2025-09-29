"""
Enhanced MQTT Webhook Strategy for Hummingbot with Gateway 2.9.0

GATEWAY 2.9.0
- JSON pool files instead of YAML
- Simplified flat structure for pools
- Updated API endpoints with /chains/ prefix
- Pool type explicitly specified in routes

Author: Todd Griggs
Date: Sept 19, 2025

"""
import time
import datetime
import traceback
import pandas as pd
import json
import os
import ssl
import asyncio
import aiohttp
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Union, Dict, List, Optional, Tuple, Any
from urllib.parse import urlencode

# Try importing MQTT with error handling
try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    mqtt = None
    MQTT_AVAILABLE = False

from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.data_type.common import OrderType
from hummingbot.client.hummingbot_application import HummingbotApplication

class ConfigurationError(Exception):
    """Specific exception for Gateway configuration errors - Phase 9.5 Single Source of Truth"""
    pass

class EnhancedMQTTWebhookStrategy(ScriptStrategyBase):
    """Enhanced MQTT webhook strategy with Gateway 2.9 configuration integration"""

    # Required framework attributes
    markets = {
        os.getenv("HBOT_CEX_DEFAULT_EXCHANGE", "coinbase_advanced_trade"): os.getenv(
            "HBOT_CEX_TRADING_PAIRS",
            "ETH-USD,BTC-USD,SOL-USD,ADA-USD,MATIC-USD,LINK-USD,UNI-USD,AVAX-USD,DOT-USD,ATOM-USD,PEPE-USD,XRP-USD,CRO-USD"
        ).split(",")
    }

    def __init__(self, connectors: Optional[Dict] = None):
        """Initialize strategy with Gateway-first architecture"""
        super().__init__(connectors or {})
        self._pending_balances = {}
        self._initialized: bool = False
        self._initializing: bool = False

        # MQTT Configuration
        self.mqtt_host: str = os.getenv("HBOT_MQTT_HOST", "localhost")
        self.mqtt_port: int = int(os.getenv("HBOT_MQTT_PORT", "1883"))
        self.mqtt_namespace: str = os.getenv("HBOT_WEBHOOK_MQTT_NAMESPACE", "hbot")
        self.mqtt_topics: List[str] = [f"{self.mqtt_namespace}/signals/+/+"]
        self.mqtt_client: Optional[mqtt.Client] = None
        self.mqtt_connected: bool = False

        # Trading Configuration with Environment Variables
        self.trade_amount: Decimal = Decimal(str(os.getenv("HBOT_TRADE_AMOUNT", "1.0")))
        self.max_daily_trades: int = int(os.getenv("HBOT_MAX_DAILY_TRADES", "50"))
        self.max_daily_volume: Decimal = Decimal(str(os.getenv("HBOT_MAX_DAILY_VOLUME", "100.0")))

        # sell configuration using environment variables
        self.sell_percentage: float = float(os.getenv("HBOT_SELL_PERCENTAGE", "99.999"))
        self.min_sell_amount: Decimal = Decimal(str(os.getenv("HBOT_MIN_SELL_AMOUNT", "0.001")))
        self.balance_cache_ttl: int = int(os.getenv("HBOT_BALANCE_CACHE_TTL", "10"))

        # SOL minimum balance protection (for gas fees)
        self.min_sol_balance: float = float(os.getenv("HBOT_MIN_SOL_BALANCE", "0.02"))

        # CEX Configuration
        self.cex_enabled: bool = os.getenv("HBOT_CEX_ENABLED", "false").lower() == "true"
        self.cex_exchange_name: str = os.getenv("HBOT_CEX_DEFAULT_EXCHANGE", "coinbase_advanced_trade")
        self.cex_order_type: str = os.getenv("HBOT_CEX_ORDER_TYPE", "market").lower()
        self.cex_max_order_size: float = float(os.getenv("HBOT_CEX_MAX_ORDER_SIZE", "100.0"))
        self.cex_min_order_size: float = float(os.getenv("HBOT_CEX_MIN_ORDER_SIZE", "1.2"))
        self.cex_daily_limit: float = float(os.getenv("HBOT_CEX_DAILY_LIMIT", "1000.0"))
        self.cex_connector = None
        self.cex_ready: bool = False
        self.cex_daily_volume: float = 0.0
        self.cex_supported_pairs: List[str] = []
        self._cex_init_completed=False

        # CEX routing configuration
        self.cex_preferred_tokens: List[str] = os.getenv("HBOT_CEX_PREFERRED_TOKENS", "ETH,BTC").split(",")
        self.use_cex_for_large_orders: bool = os.getenv("HBOT_USE_CEX_FOR_LARGE_ORDERS", "true").lower() == "true"
        self.cex_threshold_amount: float = float(os.getenv("HBOT_CEX_THRESHOLD_AMOUNT", "50.0"))

        # Gateway Configuration
        self.gateway_host: str = os.getenv("HBOT_GATEWAY_HOST", "localhost")
        self.gateway_port: int = int(os.getenv("HBOT_GATEWAY_PORT", "15888"))
        self.gateway_https: bool = os.getenv("HBOT_GATEWAY_HTTPS", "true").lower() == "true"
        self.gateway_cert_path: str = os.getenv("HBOT_GATEWAY_CERT_PATH")
        self.gateway_key_path: str = os.getenv("HBOT_GATEWAY_KEY_PATH")
        self.gateway_conf_path: str = os.getenv("HBOT_GATEWAY_CONF_PATH")
        # Validate certificate paths are configured
        if not self.gateway_cert_path:
            raise ValueError("Gateway certificate path not configured. Set HBOT_GATEWAY_CERT_PATH in .env.hummingbot")
        if not self.gateway_key_path:
            raise ValueError("Gateway key path not configured. Set HBOT_GATEWAY_KEY_PATH in .env.hummingbot")

        # Log certificate configuration (without exposing full paths)
        self.logger().info(f"🔐 Gateway certificates configured: cert={os.path.basename(self.gateway_cert_path)}")

        # Wallet Configuration
        # Used to ensure we have a wallet to trade with
        self.arbitrum_wallet: str = os.getenv("HBOT_ARBITRUM_WALLET", "")
        self.solana_wallet: str = os.getenv("HBOT_SOLANA_WALLET", "")

        # Gateway-based configuration caches
        self.gateway_config_cache: Dict = {}
        self.supported_networks: Dict[str, Dict] = {}
        self.supported_tokens: Dict[str, List[str]] = {}
        self.pool_configurations: Dict[str, Dict] = {}
        self.connector_sources = {}  # Track which connector provided which pools

        # Gateway 2.9 simplified configuration caches
        self.pool_configurations: Dict[str, Dict] = {}  # Simplified structure for 2.9
        self.supported_networks: Dict[str, Dict] = {}
        self.supported_tokens: Dict[str, List[str]] = {}

        # Trading state tracking
        self.signal_queue: List[Tuple[Dict[str, Any], str]] = []
        self.daily_trade_count: int = 0
        self.daily_volume: Decimal = Decimal("0")
        self.last_reset_date = datetime.now(timezone.utc).date()
        self.request_timeout: int = 30

        # Cache configuration
        self.gateway_config_cache_ttl: int = 300  # 5 minutes
        self._gateway_config_cache: Dict = {}
        self._balance_cache: Dict = {}
        self._last_gateway_refresh = 0
        self._last_balance_refresh = 0

        # Trading Configuration Attributes
        self.slippage_tolerance: float = float(os.getenv("HBOT_SLIPPAGE_TOLERANCE", "1.0"))  # 1% default
        self.sell_percentage: float = float(os.getenv("HBOT_SELL_PERCENTAGE", "99.999"))  # Sell 99.999% by default

        # Transaction history tracking
        self.transaction_history: List[Dict[str, Any]] = []
        self.balance_cache: Dict[str, Tuple[float, float]] = {}  # {token: (balance, timestamp)}

        # transaction hash response
        self._last_trade_response: Optional[Dict] = None

        # Specific token addresses
        self.token_details_cache = {}

        # active positions
        self.active_positions = {}

        # Performance tracking attributes
        self.successful_trades: int = 0
        self.failed_trades: int = 0
        self.avg_execution_time: float = 0.0
        self.last_signal_time: Optional[datetime] = None

        # Store app reference for CEX connector access
        self.app = HummingbotApplication.main_application()

        """Add these configuration options to __init__"""
        # Predictive selling configuration.  This is experimental and is used for very quick unintended buy/sell cycles
        self.cex_predictive_enabled = os.getenv("HBOT_CEX_PREDICTIVE_SELL", "true").lower() == "true"
        self.cex_predictive_window = int(os.getenv("HBOT_CEX_PREDICTIVE_WINDOW", "60"))  # seconds
        self.cex_fee_estimate = float(os.getenv("HBOT_CEX_FEE_ESTIMATE", "1.5"))  # 0.6% default

        # Track predictive sell results for monitoring
        self.predictive_stats = {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'fallback_success': 0  # When 99% retry works
        }

        # Gas Configuration - Adaptive Strategy
        self.gas_strategy = os.getenv("HBOT_GAS_STRATEGY", "adaptive").lower()  # "adaptive" or "fixed"
        self.gas_buffer = float(os.getenv("HBOT_GAS_BUFFER", "1.10"))  # 10% buffer by default
        self.gas_urgency_multiplier = float(os.getenv("HBOT_GAS_URGENCY", "1.25"))  # 25% for urgent trades
        self.gas_max_price_gwei = float(os.getenv("HBOT_GAS_MAX_GWEI", "0"))  # 0 = no limit
        self.gas_retry_multiplier = float(os.getenv("HBOT_GAS_RETRY_MULTIPLIER", "0.15"))

        # Log gas strategy on startup
        if self.gas_strategy == "adaptive":
            self.logger().info(
                f"⛽ Gas Strategy: ADAPTIVE (always execute at market price + {(self.gas_buffer - 1) * 100:.0f}% buffer)")
            if self.gas_max_price_gwei > 0:
                self.logger().info(f"⛽ Max gas price: {self.gas_max_price_gwei} Gwei")
            else:
                self.logger().info(f"⛽ Max gas price: UNLIMITED (execution guaranteed)")
        else:
            self.logger().info(f"⛽ Gas Strategy: FIXED (buffer={self.gas_buffer}x)")

    @classmethod
    def init_markets(cls, config=None):
        """Framework compliance method - Gateway handles market initialization"""
        return {}  # Return empty dict for Gateway-based strategies

    def on_tick(self):
        """
        Main strategy event loop - FIXED to not spam ready checks
        """
        try:
            # Skip everything during initialization phase
            # This prevents the framework from checking CEX readiness repeatedly
            if not self._initialized:
                if not self._initializing:
                    self._initializing = True
                    safe_ensure_future(self._initialize_strategy())
                # Always return during initialization to prevent any checks
                return

            # Step 2: Reset daily counters if needed
            self._reset_daily_counters_if_needed()

            # Step 3: Process queued trading signals
            while self.signal_queue:
                signal_data, topic = self.signal_queue.pop(0)
                action = signal_data.get("action", "UNKNOWN")
                symbol = signal_data.get("symbol", "UNKNOWN")
                network = signal_data.get("network", "UNKNOWN")

                self.logger().debug(f"🔄 Processing signal: {action} {symbol} on {network}")
                safe_ensure_future(self._process_trading_signal(signal_data, topic))

            # Step 4: Periodic maintenance (only after initialization)
            self._perform_periodic_maintenance()

        except Exception as tick_error:
            self.logger().error(f"❌ Strategy tick error: {tick_error}")

    def _perform_periodic_maintenance(self):
        """
        Perform periodic maintenance tasks during tick cycles
        Keeps configuration fresh without blocking main loop
        """
        try:
            # Only perform maintenance occasionally to avoid overhead
            current_time = time.time()

            # Clean up old pending balances (older than 60 seconds)
            if self._pending_balances:  # Now safe to check since it's always a dict
                tokens_to_remove = []
                for token, pending in self._pending_balances.items():
                    if current_time - pending['timestamp'] > 60:
                        tokens_to_remove.append(token)

                for token in tokens_to_remove:
                    del self._pending_balances[token]

                if tokens_to_remove:
                    self.logger().debug(f"🧹 Cleaned up {len(tokens_to_remove)} old pending balances")

            # Refresh configuration every 5 minutes
            if not hasattr(self, '_last_config_refresh'):
                self._last_config_refresh = current_time

            if not hasattr(self, '_last_predictive_stats_log'):
                self._last_predictive_stats_log = current_time

            if current_time - self._last_predictive_stats_log > 300:  # 5 minutes
                if self.predictive_stats['attempts'] > 0:
                    self.log_predictive_stats()
                self._last_predictive_stats_log = current_time

            time_since_refresh = current_time - self._last_config_refresh
            if time_since_refresh > 300:  # 5 minutes
                self.logger().debug("🔄 Performing periodic configuration refresh...")
                safe_ensure_future(self._refresh_configuration())
                self._last_config_refresh = current_time



        except Exception as maintenance_error:
            self.logger().debug(f"⚠️ Maintenance error (non-critical): {maintenance_error}")

    async def _refresh_configuration(self):
        """Refresh Gateway configuration periodically"""
        try:
            await self._refresh_gateway_configuration()
            self.logger().debug("✅ Periodic configuration refresh completed")
        except Exception as refresh_error:
            self.logger().warning(f"⚠️ Configuration refresh error: {refresh_error}")


    async def _initialize_strategy(self) -> None:
        """Initialize strategy with Gateway 2.9 configuration"""
        try:
            if self._initialized:
                self._initializing = False
                return

            self.logger().info("🚀 Initializing MQTT Webhook Strategy with Gateway 2.9 architecture")

            if self.cex_enabled:
                num_pairs = len(self.markets.get(self.cex_exchange_name, []))
                self.logger().info(f"📊 CEX Mode: Subscribing to {num_pairs} trading pairs on {self.cex_exchange_name}")

            # Step 1: Initialize supported networks
            self.supported_networks = self._get_supported_networks()
            self.logger().info(f"📋 Target networks: {', '.join(self.supported_networks)}")

            # Step 2: Load Gateway 2.9 pool configurations
            self.logger().info("🏊 Loading Gateway 2.9 pool configurations...")
            await self._refresh_gateway_configuration()

            # Step 3: Dynamic token discovery
            self.logger().info("🔍 Starting dynamic token discovery...")
            await self._initialize_dynamic_token_discovery()

            self.logger().info("🔢 Loading token decimals...")
            await self._load_token_decimals()

            # Step 4: Initialize CEX connector if enabled
            if self.cex_enabled:
                self.logger().info("📈 Initializing CEX connector...")
                await self._initialize_cex_connector()

            # Step 5: Setup MQTT if available
            if MQTT_AVAILABLE:
                self.logger().info("📡 Setting up MQTT connection...")
                self._setup_mqtt()
            else:
                self.logger().warning("⚠️ MQTT not available - webhook-only mode")

            # Step 6: Log configuration summary
            self._log_configuration_summary()

            # Step 7: Mark as initialized
            self._initialized = True
            self._initializing = False
            self.logger().info("✅ Strategy initialization complete with Gateway 2.9")

        except Exception as init_error:
            self.logger().error(f"❌ Strategy initialization error: {init_error}")
            self._initializing = False
            self._initialized = False

    async def _refresh_gateway_configuration(self) -> None:
        """
        Gateway 2.9 Configuration Refresh
        SIMPLIFIED: Uses JSON pool files directly
        """
        try:
            current_time = datetime.now().timestamp()

            # Check cache validity
            if hasattr(self, '_last_gateway_refresh') and (
                    current_time - self._last_gateway_refresh) < self.gateway_config_cache_ttl:
                self.logger().debug("🔄 Using cached Gateway 2.9 configuration")
                return

            self.logger().info("🔄 Loading Gateway 2.9 configuration from JSON files...")

            # Clear existing configuration
            self.pool_configurations = {}
            self.supported_networks = {}

            # Read Gateway 2.9 JSON configuration
            config_response = await self._read_gateway_config_files()

            # Parse Gateway 2.9 pools
            if "pools" in config_response:
                self._parse_pool_configurations(config_response["pools"])
            else:
                raise ConfigurationError("No pools found in Gateway 2.9 configuration")

            # Validate configuration
            total_networks = len(self.supported_networks)
            total_pools = sum(
                len(pools)
                for connector_pools in self.pool_configurations.values()
                for connector_config in connector_pools.values()
                for pools in connector_config.values()
            )

            if total_networks == 0:
                raise ConfigurationError("No networks with pools found")

            # Update cache timestamp
            self._last_gateway_refresh = current_time

            self.logger().info(f"✅ Gateway 2.9 configuration loaded: {total_networks} networks, {total_pools} pools")

        except Exception as e:
            self.logger().error(f"❌ Gateway 2.9 configuration error: {e}")


    async def _read_gateway_config_files(self) -> Dict:
        """
        Read Gateway 2.9.0 JSON pool configurations
        SIMPLIFIED: Direct JSON loading instead of YAML parsing
        """
        try:
            gateway_conf_path = self.gateway_conf_path
            pools_path = os.path.join(gateway_conf_path, "pools")

            if not os.path.exists(pools_path):
                raise ConfigurationError(f"Pools directory not found: {pools_path}")

            config_response = {
                "pools": {},
                "configUpdate": datetime.now().isoformat(),
                "source": "gateway_2.9.0_json_files"
            }

            # Read JSON pool files (Gateway 2.9 format)
            # Note: Jupiter router doesn't use pool files - it finds routes dynamically
            pool_files = [
                ("uniswap", "uniswap.json"),
                ("raydium", "raydium.json"),
                ("meteora", "meteora.json")
            ]

            for connector_name, filename in pool_files:
                pool_file = os.path.join(pools_path, filename)

                if not os.path.exists(pool_file):
                    self.logger().debug(f"Pool file not found: {filename}")
                    continue

                try:
                    with open(pool_file, 'r') as file:
                        pools = json.load(file)

                    config_response["pools"][connector_name] = pools
                    self.logger().info(f"✅ Loaded {len(pools)} pools from {filename}")

                except json.JSONDecodeError as e:
                    self.logger().error(f"❌ JSON error in {filename}: {e}")
                    continue

            return config_response

        except Exception as e:
            self.logger().error(f"❌ Error reading pool files: {e}")
            raise ConfigurationError(f"Failed to read Gateway 2.9.0 pool files: {e}")

    def _parse_pool_configurations(self, pools_config: Dict) -> None:
        """
        Parse Gateway 2.9.0 flat JSON pool structure
        """
        try:
            self.pool_configurations = {}
            self.supported_networks = {}

            for connector_name, pools in pools_config.items():
                if not isinstance(pools, list):
                    continue

                for pool in pools:
                    network = pool.get("network")
                    pool_type = pool.get("type")
                    base_symbol = pool.get("baseSymbol")
                    quote_symbol = pool.get("quoteSymbol")
                    address = pool.get("address")

                    if not all([network, pool_type, base_symbol, quote_symbol, address]):
                        continue

                    # Initialize network if needed
                    if network not in self.pool_configurations:
                        self.pool_configurations[network] = {}
                        self.supported_networks[network] = {"pools": 0}

                    # Initialize connector if needed
                    if connector_name not in self.pool_configurations[network]:
                        self.pool_configurations[network][connector_name] = {"amm": {}, "clmm": {}}

                    # Store pool with hyphenated key
                    pool_key = f"{base_symbol}-{quote_symbol}"
                    self.pool_configurations[network][connector_name][pool_type][pool_key] = address
                    self.supported_networks[network]["pools"] += 1

            self.logger().info(f"✅ Parsed {len(self.supported_networks)} networks from pools")

        except Exception as e:
            self.logger().error(f"❌ Error parsing pool configurations: {e}")

    def _validate_trading_signal(self, signal_data: Dict[str, Any]) -> bool:
        """
        Validate incoming trading signals - Gateway 2.9 simplified json
        """
        try:
            # Required fields validation
            required_fields = ["action", "symbol", "network", "exchange"]
            for field in required_fields:
                if field not in signal_data or not signal_data[field]:
                    self.logger().warning(f"⚠️ Missing required field: {field}")
                    return False

            action = signal_data.get("action", "").upper()
            network = signal_data.get("network", "")
            exchange = signal_data.get("exchange", "").lower()
            symbol = signal_data.get("symbol", "")

            # Action validation
            if action not in ["BUY", "SELL"]:
                self.logger().warning(f"⚠️ Invalid action: {action}")
                return False

            # Network validation
            if network not in self.supported_networks:
                self.logger().warning(f"⚠️ Unsupported network: {network}")
                self.logger().info(f"📋 Supported: {', '.join(self.supported_networks)}")
                return False

            # Exchange validation
            if self._is_cex_exchange(exchange):
                # CEX validation - simple symbol check
                if not symbol or len(symbol) < 2:
                    self.logger().warning(f"⚠️ Invalid CEX symbol: {symbol}")
                    return False
            else:
                # DEX validation
                exchange = signal_data.get("exchange", "").lower()

                # Jupiter router doesn't need pool configuration - it finds routes dynamically
                if exchange == "jupiter":
                    # Basic symbol format check for Jupiter
                    if not symbol or len(symbol) < 3:
                        self.logger().warning(f"⚠️ Invalid DEX symbol: {symbol}")
                        return False
                    self.logger().debug(f"✅ Jupiter router validation passed: {symbol}")
                else:
                    # Other DEXs need pool configuration
                    if network not in self.pool_configurations:
                        self.logger().warning(f"⚠️ No pools configured for {network}")
                        return False

                    # Basic symbol format check
                    if not symbol or len(symbol) < 3:
                        self.logger().warning(f"⚠️ Invalid DEX symbol: {symbol}")
                        return False

                    # For traditional DEXs, verify pool exists (simplified check)
                    if not self._has_pool_configured(signal_data):
                        self.logger().warning(f"⚠️ No pool available for {symbol} on {network}")
                        return False

            # Daily limits validation
            if self.daily_trade_count >= self.max_daily_trades:
                self.logger().warning(f"⚠️ Daily trade limit reached: {self.daily_trade_count}/{self.max_daily_trades}")
                return False

            if self.daily_volume >= self.max_daily_volume:
                self.logger().warning(f"⚠️ Daily volume limit reached: ${self.daily_volume}/${self.max_daily_volume}")
                return False

            self.logger().debug(f"✅ Valid signal: {action} {symbol} on {network}/{exchange}")
            return True

        except Exception as e:
            self.logger().error(f"❌ Validation error: {e}")
            return False

    @staticmethod
    def _is_cex_exchange(exchange: str) -> bool:
        """Check if exchange is CEX"""
        return exchange in ["coinbase", "coinbase_advanced_trade", "cex"]

    def _has_pool_configured(self, signal_data: Dict[str, Any]) -> bool:
        """
        Check if pool exists in configuration (synchronous)
        Gateway 2.9 - checks loaded configuration only
        """
        try:
            network = signal_data.get("network")
            exchange = signal_data.get("exchange", "").lower()
            symbol = signal_data.get("symbol")

            # Parse symbol to get pool key
            base_token, quote_token = self._parse_symbol_tokens(symbol, network)
            pool_key = f"{base_token}-{quote_token}"

            # Check configuration directly (no API call)
            if network in self.pool_configurations:
                network_pools = self.pool_configurations[network]

                # Check specific exchange if provided
                if exchange and exchange in network_pools:
                    exchange_pools = network_pools[exchange]
                    for pool_type in ['clmm', 'amm']:
                        if pool_type in exchange_pools and pool_key in exchange_pools[pool_type]:
                            return True

                # Check all exchanges
                for connector, pools in network_pools.items():
                    for pool_type in ['clmm', 'amm']:
                        if pool_type in pools and pool_key in pools[pool_type]:
                            return True

            return False

        except Exception:
            return False  # Let Gateway handle unknown cases

    def _validate_symbol_format(self, symbol: str) -> bool:
        """
        Validate trading symbol format
        Accepts various formats: ETHUSDC, ETH-USDC, ETH/USDC
        """
        try:
            if not symbol or len(symbol) < 3:
                return False

            # Allow alphanumeric characters, hyphens, slashes, and underscores

            if not re.match(r'^[A-Za-z0-9\-/_]+$', symbol):
                return False

            # Try to parse the symbol to see if it makes sense
            if "-" in symbol or "/" in symbol or "_" in symbol:
                # Already has separator, should have exactly 2 parts
                for separator in ["-", "/", "_"]:
                    if separator in symbol:
                        parts = symbol.split(separator)
                        if len(parts) == 2 and all(len(part) >= 2 for part in parts):
                            return True
            else:
                # Combined format like ETHUSDC - should be at least 6 characters
                if len(symbol) >= 6:
                    return True

            return False

        except Exception as format_error:
            self.logger().error(f"❌ Symbol format validation error: {format_error}")
            return False

    class ConfigurationError(Exception):
        """
        Specific exception for Gateway configuration errors
        Provides detailed context about configuration failures
        """

        def __init__(self, message: str, error_code: str = "CONFIG_ERROR", details: Dict = None):
            """
            Initialize configuration error with context

            Args:
                message: Error message
                error_code: Error code for categorization (default: CONFIG_ERROR)
                details: Additional error details as dictionary
            """
            super().__init__(message)
            self.message = message
            self.error_code = error_code
            self.details = details or {}
            self.timestamp = datetime.now().isoformat()

        def __str__(self):
            """String representation with full context"""
            base_msg = f"[{self.error_code}] {self.message}"

            if self.details:
                details_str = "\nDetails:"
                for key, value in self.details.items():
                    details_str += f"\n  - {key}: {value}"
                base_msg += details_str

            return base_msg

    def _setup_mqtt(self) -> None:
        """Set up MQTT client connection"""
        try:
            if not MQTT_AVAILABLE:
                self.logger().error("❌ MQTT not available - install paho-mqtt")
                return

            # Try modern paho-mqtt first, fallback for older versions
            try:
                self.mqtt_client = mqtt.Client()
            except (TypeError, AttributeError):
                self.mqtt_client = mqtt.Client()

            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_message = self._on_mqtt_message

            self.logger().info(f"🔗 Connecting to MQTT broker at {self.mqtt_host}:{self.mqtt_port}")
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()

        except Exception as mqtt_error:
            self.logger().error(f"❌ MQTT setup failed: {mqtt_error}")

    def _on_mqtt_connect(self, _client, _userdata, _flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.mqtt_connected = True
            self.logger().info("✅ MQTT client connected successfully")

            for topic in self.mqtt_topics:
                self.mqtt_client.subscribe(topic)
                self.logger().info(f"📡 Subscribing to topic: {topic}")
        else:
            self.logger().error(f"❌ MQTT connection failed with code: {rc}")

    def _on_mqtt_disconnect(self, _client, _userdata, rc):
        """MQTT disconnection callback"""
        self.mqtt_connected = False
        self.logger().warning(f"⚠️ MQTT broker disconnected with code: {rc}")

    def _on_mqtt_message(self, _client, _userdata, msg):
        """MQTT message callback - updated to use consolidated method"""
        try:
            _topic = msg.topic
            payload = msg.payload.decode('utf-8')

            self.logger().info(f"📨 Received MQTT message on {_topic}")

            signal_data = json.loads(payload)
            self.logger().info(f"📊 Signal data: {signal_data}")

            # Enhanced validation with Gateway configuration
            if self._validate_signal_with_gateway(signal_data):
                self.signal_queue.append((signal_data, _topic))
                self.logger().info(
                    f"✅ Valid trading signal queued: {signal_data.get('action')} {signal_data.get('symbol')}")
            else:
                self.logger().warning(f"⚠️ Invalid signal format: {signal_data}")

        except json.JSONDecodeError:
            self.logger().error(f"❌ Invalid JSON in MQTT message: {msg.payload.decode('utf-8', errors='ignore')}")
        except Exception as msg_error:
            self.logger().error(f"❌ MQTT message processing error: {msg_error}")

    def _validate_signal_with_gateway(self, signal_data: Dict[str, Any]) -> bool:
        """Enhanced signal validation using Gateway configuration"""
        try:
            # Basic field validation
            required_fields = ["action", "symbol", "exchange", "network"]
            for field in required_fields:
                if field not in signal_data:
                    self.logger().error(f"❌ Missing required field: {field} in signal: {signal_data}")
                    return False

            # Action validation
            action = signal_data.get("action", "").upper()
            if action not in ["BUY", "SELL"]:
                self.logger().error(f"❌ Invalid action: {action}")
                return False

            # Network validation
            network = signal_data.get("network", "")
            if network not in self.supported_networks:
                self.logger().error(f"❌ Unsupported network: {network}")
                return False

            # symbol validation for CEX vs DEX
            symbol = signal_data.get("symbol", "")
            exchange = signal_data.get("exchange", "").lower()

            if not symbol:
                self.logger().error("❌ Empty symbol in signal")
                return False

            # CEX-specific validation (more lenient)
            if exchange in ["coinbase", "coinbase_advanced_trade", "cex"]:
                # For CEX, allow simple symbols like "ETH" or "BTC"
                if len(symbol) >= 2 and symbol.isalpha():
                    self.logger().debug(f"✅ CEX symbol validation passed: {symbol}")
                    return True
                else:
                    self.logger().error(f"❌ Invalid CEX symbol format: {symbol}")
                    return False
            else:
                # DEX validation (existing logic)
                if not self._validate_symbol_format(symbol):
                    self.logger().error(f"❌ Invalid DEX symbol format: {symbol}")
                    return False

            self.logger().info(f"✅ Signal validation passed: {action} {symbol} on {network}")
            return True

        except Exception as validation_error:
            self.logger().error(f"❌ Signal validation error: {validation_error}")
            return False



    async def _initialize_cex_connector(self):
        """
        CEX initialization that just works without connector spam
        """
        try:
            # Skip if already initialized
            if hasattr(self, '_cex_init_completed') and self._cex_init_completed:
                return

            self.logger().info(f"🔍 Looking for CEX connector: {self.cex_exchange_name}")

            # Find the connector
            if self.cex_exchange_name in self.connectors:
                self.cex_connector = self.connectors[self.cex_exchange_name]
                self.logger().info(f"✅ Found {self.cex_exchange_name} in connectors")
            elif self.app and hasattr(self.app, '_markets') and self.app._markets:
                if self.cex_exchange_name in self.app._markets:
                    self.cex_connector = self.app._markets[self.cex_exchange_name]
                    self.connectors[self.cex_exchange_name] = self.cex_connector
                    self.logger().info(f"✅ Found {self.cex_exchange_name} in app._markets")

            if not self.cex_connector:
                self.logger().warning("⚠️ CEX connector not found - CEX trading disabled")
                self.cex_enabled = False
                self._cex_init_completed = True
                return

            # Wait a fixed time for order book subscriptions
            self.logger().info("⏳ Waiting for CEX order book subscriptions...")
            await asyncio.sleep(1)

            # Mark as ready
            self.cex_ready = True
            self.cex_enabled = True
            self._cex_init_completed = True
            self.logger().info(f"✅ CEX connector ready for trading: {self.cex_exchange_name}")

            # Test basic functionality
            await self._test_cex_functionality()

        except Exception as cex_error:
            self.logger().error(f"❌ CEX initialization error: {cex_error}")
            self.cex_enabled = False
            self._cex_init_completed = True

    async def _ensure_cex_ready(self, timeout: float = 10.0) -> bool:
        """
        Ensure CEX connector is fully ready before trading

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if ready, False if timeout
        """
        if not self.cex_connector:
            self.logger().error("❌ CEX connector not available")
            return False

        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check basic readiness flag
            if not self.cex_ready:
                await asyncio.sleep(0.5)
                continue

            # Try to get balances as a readiness test
            try:
                balances = self.cex_connector.get_all_balances()
                if asyncio.iscoroutine(balances):
                    balances = await balances

                # If we get a valid response (even empty dict), we're ready
                if balances is not None and isinstance(balances, dict):
                    self.logger().debug("✅ CEX connector verified ready")
                    return True

            except Exception as e:
                self.logger().debug(f"CEX readiness check failed: {e}")

            await asyncio.sleep(0.5)

        self.logger().warning(f"⚠️ CEX connector not ready after {timeout}s")
        return False

    async def _test_cex_functionality(self):
        """Quick test to confirm CEX is working"""
        try:
            # Test balances
            balances = self.cex_connector.get_all_balances()
            if asyncio.iscoroutine(balances):
                balances = await balances

            if balances:
                non_zero_balances = {k: v for k, v in balances.items() if v > 0}
                if non_zero_balances:
                    balance_tokens = list(non_zero_balances.keys())[:5]
                    self.logger().info(f"💰 CEX Balances available: {balance_tokens}")
                else:
                    self.logger().info("💰 CEX connected (ready for funding)")

            # Test order book
            order_book = self.cex_connector.get_order_book("ETH-USD")
            if order_book:
                bid = order_book.get_price(False)
                ask = order_book.get_price(True)
                if bid > 0 and ask > 0:
                    self.logger().info(f"📊 Order book test: ETH ${float(bid):.2f} / ${float(ask):.2f}")
                    return True

            self.logger().info("📊 CEX connected successfully")
            return True

        except Exception as test_error:
            self.logger().info(f"💰 CEX connected (test deferred: {str(test_error)[:30]}...)")
            return True  # Don't fail on test errors

    def _count_active_order_books(self) -> int:
        """Count active order book subscriptions"""
        try:
            if not self.cex_connector:
                return 0

            if hasattr(self.cex_connector, 'order_book_tracker'):
                tracker = getattr(self.cex_connector, 'order_book_tracker', None)
                if tracker and hasattr(tracker, '_order_books'):
                    order_books = getattr(tracker, '_order_books', {})
                    return len(order_books)

            return 0
        except Exception:
            return 0

    def _should_use_cex(self, signal_data: Dict[str, Any]) -> bool:
        """
        Determine whether to route order to CEX or DEX
        Returns True if CEX should be used, False for DEX
        """
        # If CEX is not enabled or not ready, always use DEX
        if not self.cex_enabled or not self.cex_ready or not self.cex_connector:
            return False

        # Check if signal explicitly specifies CEX
        if signal_data.get("use_cex", False):
            return True

        # Check if exchange is explicitly set to CEX
        exchange = signal_data.get("exchange", "").lower()
        if "coinbase" in exchange or "cex" in exchange:
            return True

        # Get symbol and check if it's a CEX preferred token
        symbol = signal_data.get("symbol", "").upper()
        base_token = symbol.replace("USDC", "").replace("USD", "").replace("-", "")

        # Route to CEX for preferred tokens
        if base_token in self.cex_preferred_tokens:
            self.logger().info(f"📊 Routing {base_token} to CEX (preferred token)")
            return True

        # Route large orders to CEX if configured
        if self.use_cex_for_large_orders:
            amount = float(signal_data.get("amount", self.trade_amount))
            if amount >= self.cex_threshold_amount:
                self.logger().info(f"📊 Routing ${amount} order to CEX (above threshold)")
                return True

        # Check daily CEX limit
        if self.cex_daily_volume >= self.cex_daily_limit:
            self.logger().warning(f"⚠️ CEX daily limit reached (${self.cex_daily_limit}), using DEX")
            return False

        # Default to DEX
        return False

    def _get_supported_networks(self) -> List[str]:
        """
        Get supported networks from environment or use intelligent defaults
        """
        # Try environment variable first
        env_networks = os.getenv("HBOT_SUPPORTED_NETWORKS", "")
        if env_networks:
            networks = [network.strip() for network in env_networks.split(",")]
            self.logger().info(f"📋 Using networks from environment: {networks}")
            return networks

        # Default networks for multi-chain support
        default_networks = ["arbitrum", "polygon", "base", "mainnet", "mainnet-beta"]
        self.logger().info(f"📋 Using default networks: {default_networks}")
        return default_networks

    def _log_configuration_summary(self) -> None:
        """
        Log comprehensive configuration summary for debugging and monitoring
        Enhanced with CEX trading status and Gateway 2.9 pool details
        """
        try:
            self.logger().info("📊 === CONFIGURATION SUMMARY (Gateway 2.9) ===")
            # Gas Strategy Summary
            self.logger().info("⛽ Gas Strategy Configuration:")
            if self.gas_strategy == "adaptive":
                self.logger().info(f"  📈 Strategy: ADAPTIVE (always execute at market price)")
                self.logger().info(f"  📊 Buffer: +{(self.gas_buffer - 1) * 100:.0f}% above current gas")
                self.logger().info(f"  🚨 Urgency: +{(self.gas_urgency_multiplier - 1) * 100:.0f}% for critical retries")
                if self.gas_max_price_gwei > 0:
                    self.logger().info(f"  💰 Max Price: {self.gas_max_price_gwei} Gwei")
                else:
                    self.logger().info(f"  💰 Max Price: UNLIMITED (guaranteed execution)")
                self.logger().info(f"  💡 Trades will ALWAYS execute regardless of gas price")
            else:
                self.logger().info(f"  📈 Strategy: FIXED")
                self.logger().info(f"  📊 Buffer: {self.gas_buffer}x")
                self.logger().info(f"  📈 Retry: +{self.gas_retry_multiplier}x per attempt")

            # Token discovery summary (DEX)
            self.logger().info("💎 DEX Token Discovery Results:")
            total_tokens = 0
            for network in self.supported_networks:
                token_count = len(self.supported_tokens.get(network, []))
                total_tokens += token_count

                if token_count > 0:
                    sample_tokens = self.supported_tokens[network][:5]
                    self.logger().info(f"  🌐 {network}: {token_count} tokens (sample: {', '.join(sample_tokens)})")
                else:
                    self.logger().warning(f"  ⚠️ {network}: No tokens discovered")

            # Pool configuration summary (DEX) - Gateway 2.9 structure
            self.logger().info("🏊 DEX Pool Configuration Results (Gateway 2.9):")
            pool_total = 0
            for network in self.pool_configurations:
                network_pools = 0
                for connector_name, connector_config in self.pool_configurations[network].items():
                    if isinstance(connector_config, dict):
                        for pool_type in ["amm", "clmm"]:
                            if pool_type in connector_config:
                                pools = connector_config[pool_type]
                                if isinstance(pools, dict):
                                    network_pools += len(pools)

                if network_pools > 0:
                    self.logger().info(f"  🌐 {network}: {network_pools} pools configured")
                    pool_total += network_pools
                else:
                    self.logger().debug(f"  📋 {network}: No pools configured")

            # CEX Configuration Summary
            self.logger().info("📈 CEX Trading Configuration:")
            if self.cex_enabled:
                # Basic CEX config
                self.logger().info(f"  🏛️ Exchange: {self.cex_exchange_name}")

                # Trading pairs from environment variable
                cex_pairs = self.markets.get(self.cex_exchange_name, [])
                self.logger().info(f"  💱 Trading Pairs: {len(cex_pairs)} configured")
                if len(cex_pairs) <= 5:
                    self.logger().info(f"  💱 Pairs: {cex_pairs}")
                else:
                    self.logger().info(f"  💱 Pairs: {cex_pairs[:5]} (and {len(cex_pairs) - 5} more)")

                # Routing configuration
                self.logger().info(f"  🎯 Preferred Tokens: {self.cex_preferred_tokens}")
                self.logger().info(f"  💰 Daily Limit: ${self.cex_daily_limit}")
                self.logger().info(f"  📊 Large Order Threshold: ${self.cex_threshold_amount} -> CEX")

                # Predictive selling configuration
                self.logger().info("🎯 CEX Predictive Selling:")
                if self.cex_predictive_enabled:
                    self.logger().info(f"  ✅ Enabled")
                    self.logger().info(f"  ⏱️ Window: {self.cex_predictive_window} seconds")
                    self.logger().info(f"  💰 Fee Estimate: {self.cex_fee_estimate}%")
                else:
                    self.logger().info(f"  ❌ Disabled")

                # Connection status
                if self.cex_connector:
                    connection_status = self._get_cex_connection_status()
                    ready_status = "✅ Ready" if self.cex_ready else "⏳ Initializing"
                    self.logger().info(f"  🔌 Connection: {connection_status} ({ready_status})")

                    # Order book status
                    order_book_count = 0
                    if hasattr(self.cex_connector, 'order_book_tracker'):
                        tracker = getattr(self.cex_connector, 'order_book_tracker', None)
                        if tracker and hasattr(tracker, '_order_books'):
                            order_books = getattr(tracker, '_order_books', {})
                            order_book_count = len(order_books)

                    if order_book_count > 0:
                        self.logger().info(f"  📊 Order Books: {order_book_count} active subscriptions")
                    else:
                        self.logger().info(f"  📊 Order Books: Initializing...")

                    # Environment variables source
                    self.logger().info(f"  📋 Config Source: HBOT_CEX_TRADING_PAIRS environment variable")

                else:
                    self.logger().info(f"  🔌 Connection: Connector not found")
            else:
                cex_disabled_reason = "HBOT_CEX_ENABLED=false" if not self.cex_enabled else "Connector unavailable"
                self.logger().info(f"  ❌ CEX Trading Disabled ({cex_disabled_reason})")

            # Trading configuration summary (Universal)
            self.logger().info("💰 Universal Trading Configuration:")
            self.logger().info(f"  📈 BUY Amount: ${self.trade_amount} (from HBOT_TRADE_AMOUNT)")
            self.logger().info(f"  📉 SELL Percentage: {self.sell_percentage}% (from HBOT_SELL_PERCENTAGE)")
            self.logger().info(f"  🛡️ SOL Minimum Balance: {self.min_sol_balance} SOL (from HBOT_MIN_SOL_BALANCE)")
            self.logger().info(f"  🚦 Daily Limits: {self.max_daily_trades} trades, ${self.max_daily_volume} volume")
            self.logger().info(f"  ⚡ Slippage Tolerance: {self.slippage_tolerance}% (DEX only)")

            # Routing Logic Summary
            self.logger().info("🔄 Trade Routing Logic:")
            if self.cex_enabled:
                self.logger().info(f"  📊 CEX for preferred tokens: {self.cex_preferred_tokens}")
                if self.use_cex_for_large_orders:
                    self.logger().info(f"  📊 CEX for orders ≥ ${self.cex_threshold_amount}")
                self.logger().info(f"  🌊 DEX for all other trades")
            else:
                self.logger().info(f"  🌊 DEX only (CEX disabled)")

            # Overall capability summary
            cex_capability = f"{len(self.markets.get(self.cex_exchange_name, []))} CEX pairs" if self.cex_enabled else "CEX disabled"
            self.logger().info(
                f"🎯 TOTAL CAPABILITY: {total_tokens} DEX tokens across {len(self.supported_networks)} networks "
                f"with {pool_total} pools + {cex_capability}")

            # Daily activity summary
            if hasattr(self, 'daily_trade_count') and hasattr(self, 'daily_volume'):
                self.logger().info(f"📈 Today's Activity: {self.daily_trade_count} trades, ${self.daily_volume} volume")

            self.logger().info("📊 === END SUMMARY ===")

        except Exception as summary_error:
            self.logger().warning(f"⚠️ Error logging configuration summary: {summary_error}")

    def _get_cex_connection_status(self) -> str:
        """Enhanced connection status"""
        if not self.cex_connector:
            return "Not Found"

        # Count order books
        order_book_count = self._count_active_order_books()

        # Check network status
        status = "Connected"
        if hasattr(self.cex_connector, '_network_status'):
            network_status = getattr(self.cex_connector, '_network_status', None)
            if network_status:
                status = str(network_status).replace('NetworkStatus.', '')

        if order_book_count > 0:
            return f"{status} ({order_book_count} order books)"
        else:
            return status

    async def _process_trading_signal(self, signal_data: Dict[str, Any], topic: str = None) -> None:
        """
        Process trading signal - Gateway 2.9 OPTIMIZED
        Routes to CEX or DEX based on configuration
        """
        try:
            # Extract signal details
            action = signal_data.get("action", "").upper()
            symbol = signal_data.get("symbol", "")
            network = signal_data.get("network", "arbitrum")
            exchange = signal_data.get("exchange", "uniswap")

            self.logger().info(f"🎯 Processing: {action} {symbol} on {network}/{exchange}")

            # Core validations
            if not self._initialized:
                self.logger().warning("⚠️ Strategy not initialized")
                return

            if not self._validate_trading_signal(signal_data):
                self.logger().warning("⚠️ Invalid signal")
                return

            if not self._check_daily_limits():
                self.logger().warning("⚠️ Daily limits reached")
                return

            # Route to CEX or DEX
            if self._should_use_cex(signal_data):
                success = await self._route_to_cex(action, symbol, signal_data)
            else:
                success = await self._route_to_dex(action, symbol, network, exchange, signal_data)

            # Update statistics
            if success:
                self._update_daily_statistics(action)
                self.successful_trades += 1
                self.logger().debug(f"✅ Trade success recorded. Total successful: {self.successful_trades}")
            else:
                self.failed_trades += 1
                self.logger().debug(f"❌ Trade failure recorded. Total failed: {self.failed_trades}")

        except Exception as e:
            self.logger().error(f"❌ Signal processing error: {e}")

            self.logger().error(traceback.format_exc())

    async def _route_to_cex(self, action: str, symbol: str, signal_data: Dict) -> bool:
        """Helper: Route trade to CEX"""
        self.logger().info(f"📈 CEX routing: {self.cex_exchange_name}")

        if action == "BUY":
            return await self._execute_cex_buy(
                symbol,
                float(signal_data.get("amount", self.trade_amount))
            )
        elif action == "SELL":
            return await self._execute_cex_sell(
                symbol,
                float(signal_data.get("percentage", self.sell_percentage))
            )
        else:
            self.logger().error(f"❌ Unsupported CEX action: {action}")
            return False

    async def _route_to_dex(self, action: str, symbol: str, network: str,
                            exchange: str, signal_data: Dict) -> bool:
        """Helper: Route trade to DEX with Gateway 2.9 pool resolution"""
        self.logger().info(f"🌊 DEX routing: {network}/{exchange}")

        # Parse tokens
        base_token, quote_token = self._parse_symbol_tokens(symbol, network)
        pool_key = f"{base_token}-{quote_token}"

        # Extract pool_type from signal if provided
        pool_type = signal_data.get("pool_type")  # Get pool type from webhook

        # Gateway 2.9: Get pool info with type
        pool_info = await self._get_pool_info(
            network,
            exchange,
            pool_key,
            signal_data.get("pool"),  # Use provided pool if any
            pool_type  # Pass pool type from webhook
        )

        pool_address = pool_info['address'] if pool_info else None

        if pool_address:
            self.logger().info(f"✅ Pool found: {pool_address[:10]}... ({pool_info['type']})")
        else:
            self.logger().info(f"🔄 No pool configured, Gateway will auto-select")

        # Execute trade
        if action == "BUY":
            return await self._execute_buy_trade(
                symbol=symbol,
                amount=signal_data.get("amount", self.trade_amount),
                network=network,
                exchange=exchange,
                pool=pool_address
            )
        elif action == "SELL":
            return await self._execute_sell_trade(
                symbol=symbol,
                percentage=signal_data.get("percentage", self.sell_percentage),
                network=network,
                exchange=exchange,
                pool=pool_address,
                pool_type=pool_type  # Pass pool_type from webhook
            )
        else:
            self.logger().error(f"❌ Unsupported action: {action}")
            return False

    def _update_daily_statistics(self, action: str):
        """Helper: Update daily trade statistics"""
        self.daily_trade_count += 1
        if action == "BUY":
            self.daily_volume += self.trade_amount
        self.logger().info(f"📊 Daily: {self.daily_trade_count} trades, ${self.daily_volume} volume")

    async def _execute_cex_buy(self, symbol: str, usd_amount: float) -> bool:
        """
        Execute buy order on CEX with enhanced tracking for predictive selling
        """
        try:
            if not await self._ensure_cex_ready():
                self.logger().error("❌ CEX connector not ready for trading")
                return False

            base_token = self._extract_base_token_from_symbol(symbol)
            trading_pair = f"{base_token}-USD"

            self.logger().info(f"📈 CEX BUY: ${usd_amount:.2f} worth of {trading_pair}")

            # Ensure minimum order size
            if usd_amount < self.cex_min_order_size:
                usd_amount = self.cex_min_order_size
                self.logger().info(f"📊 Adjusted to minimum order size: ${usd_amount:.2f}")

            # Get order book
            order_book = self.cex_connector.get_order_book(trading_pair)
            if not order_book:
                self.logger().error(f"❌ No order book available for {trading_pair}")
                return False

            ask_price_raw = order_book.get_price(True)
            ask_price_decimal = Decimal(str(ask_price_raw)) if not isinstance(ask_price_raw, Decimal) else ask_price_raw

            # Calculate expected amount
            usd_amount_decimal = Decimal(str(usd_amount))
            expected_amount = usd_amount_decimal / ask_price_decimal

            # Account for fees (Coinbase typically 0.5-0.6% for market orders)
            fee_multiplier = Decimal("1.0")  # 1.5% safety margin
            expected_amount_after_fees = expected_amount * fee_multiplier
            expected_amount_after_fees = expected_amount_after_fees.quantize(Decimal('0.00000001'))

            # Place the buy order
            order_id = self.cex_connector.buy(
                trading_pair=trading_pair,
                amount=expected_amount,  # Order the full amount (fees taken from USD)
                order_type=OrderType.MARKET,
                price=ask_price_decimal
            )

            self.logger().info(f"✅ CEX BUY order placed: {order_id}")

            # Enhanced tracking for predictive selling
            self._pending_balances[base_token] = {
                'order_id': order_id,
                'timestamp': time.time(),
                'expected_amount': float(expected_amount),
                'expected_after_fees': float(expected_amount_after_fees),
                'usd_amount': usd_amount,
                'price': float(ask_price_decimal),
                'trading_pair': trading_pair
            }

            self.logger().info(
                f"📊 Expected to receive: {float(expected_amount_after_fees):.8f} {base_token} "
                f"(after ~0.6% fees)"
            )

            self.cex_daily_volume += usd_amount
            return True

        except Exception as e:
            self.logger().error(f"❌ CEX BUY order failed: {e}")
            return False

    async def _execute_cex_sell(self, symbol: str, percentage: float) -> bool:
        """
        Execute CEX sell
        """
        try:
            if not self.cex_connector or not self.cex_ready:
                self.logger().error("❌ CEX connector not ready")
                return False

            base_token = self._extract_base_token_from_symbol(symbol)
            trading_pair = f"{base_token}-USD"

            # CHECK: Is there a recent buy we can use for predictive selling?
            use_predictive = False
            predictive_amount = Decimal("0")

            if base_token in self._pending_balances:
                pending = self._pending_balances[base_token]
                time_since_buy = time.time() - pending['timestamp']

                # If buy was less than predictive window, use PREDICTIVE SELLING
                if time_since_buy < self.cex_predictive_window:
                    use_predictive = True
                    # Use the conservative expected amount
                    expected = Decimal(str(pending['expected_after_fees']))

                    # EXTRA SAFETY: Take 99.5% of expected to account for rounding
                    safety_factor = Decimal("0.995")
                    safe_expected = expected * safety_factor

                    predictive_amount = safe_expected * Decimal(str(percentage / 100.0))
                    predictive_amount = predictive_amount.quantize(Decimal('0.00000001'))

                    self.logger().info(
                        f"🎯 PREDICTIVE SELL: Using conservative estimate from buy {time_since_buy:.1f}s ago"
                    )
                    self.logger().info(
                        f"📊 Conservative estimate: {float(safe_expected):.8f} {base_token}, "
                        f"selling {percentage}% = {float(predictive_amount):.8f}"
                    )

                    # INCREMENT PREDICTIVE ATTEMPTS
                    self.predictive_stats['attempts'] += 1

            # Determine sell amount
            if use_predictive:
                sell_amount = predictive_amount
                self.logger().info(f"⚡ Using predictive amount (conservative estimate)")

            else:
                # Normal balance check for older trades
                self.logger().info(f"📊 Buy is older than {self.cex_predictive_window}s, checking actual balance...")

                try:
                    balances = self.cex_connector.get_all_balances()
                    if asyncio.iscoroutine(balances):
                        balances = await balances

                    if not balances or not isinstance(balances, dict):
                        self.logger().error("❌ Could not get balances")
                        return False

                    base_balance = balances.get(base_token, Decimal("0"))

                    if base_balance <= 0:
                        self.logger().warning(f"⚠️ No {base_token} balance to sell")
                        return False

                    sell_amount = base_balance * Decimal(str(percentage / 100.0))
                    sell_amount = sell_amount.quantize(Decimal('0.00000001'))

                    self.logger().info(
                        f"📊 Actual balance: {float(base_balance):.8f} {base_token}, "
                        f"selling {percentage}% = {float(sell_amount):.8f}"
                    )

                except Exception as e:
                    self.logger().error(f"❌ Balance check failed: {e}")
                    return False

            # Get current price
            order_book = self.cex_connector.get_order_book(trading_pair)
            if not order_book:
                self.logger().error(f"❌ No order book available")
                return False

            bid_price = order_book.get_price(False)
            if not isinstance(bid_price, Decimal):
                bid_price = Decimal(str(bid_price))

            # Validate minimum order size
            usd_value = sell_amount * bid_price
            min_order_size = Decimal(str(self.cex_min_order_size))

            if usd_value < min_order_size:
                if use_predictive:
                    self.logger().warning(
                        f"⚠️ Predictive amount too small (${float(usd_value):.2f} < ${self.cex_min_order_size})"
                    )
                else:
                    self.logger().warning(f"⚠️ Order too small for minimum size")
                return False

            # EXECUTE THE SELL ORDER
            self.logger().info(
                f"📉 CEX SELL: {float(sell_amount):.8f} {base_token} "
                f"(${float(usd_value):.2f}) {'[PREDICTIVE]' if use_predictive else '[CONFIRMED]'}"
            )

            try:
                order_id = self.cex_connector.sell(
                    trading_pair=trading_pair,
                    amount=sell_amount,
                    order_type=OrderType.MARKET,
                    price=bid_price
                )

                self.logger().info(f"✅ CEX SELL order placed: {order_id}")

                # TRACK SUCCESS
                if use_predictive:
                    self.predictive_stats['successes'] += 1

                    # Log stats every 10 predictive trades
                    if self.predictive_stats['attempts'] % 10 == 0:
                        self.log_predictive_stats()

                # Clean up
                if base_token in self._pending_balances:
                    del self._pending_balances[base_token]

                self.cex_daily_volume += float(usd_value)
                return True

            except Exception as order_error:
                if use_predictive:
                    self.predictive_stats['failures'] += 1

                    self.logger().error(
                        f"❌ Predictive sell failed: {order_error}"
                    )

                    # Log what we tried vs what might be available
                    self.logger().info(f"📊 Debug: Tried to sell {float(sell_amount):.8f}")
                    self.logger().info(f"📊 Debug: Check actual balance to see discrepancy")

                    # Log stats after failure
                    self.log_predictive_stats()

                else:
                    self.logger().error(f"❌ CEX SELL order failed: {order_error}")

                return False

        except Exception as e:
            self.logger().error(f"❌ CEX SELL error: {e}")
            return False

    def log_predictive_stats(self):
        """Log statistics about predictive selling performance"""
        if self.predictive_stats['attempts'] > 0:
            success_rate = (self.predictive_stats['successes'] / self.predictive_stats['attempts']) * 100

            self.logger().info(
                f"📊 === PREDICTIVE SELLING STATS ==="
            )
            self.logger().info(
                f"   Total Attempts: {self.predictive_stats['attempts']}"
            )
            self.logger().info(
                f"   Successes: {self.predictive_stats['successes']} ({success_rate:.1f}%)"
            )
            self.logger().info(
                f"   Failures: {self.predictive_stats['failures']}"
            )

            if self.predictive_stats['fallback_success'] > 0:
                fallback_rate = (self.predictive_stats['fallback_success'] /
                                 (self.predictive_stats['failures'] + self.predictive_stats['fallback_success'])) * 100
                self.logger().info(
                    f"   99% Fallback Success: {self.predictive_stats['fallback_success']} "
                    f"({fallback_rate:.1f}% recovery rate)"
                )

            # Performance assessment
            if success_rate >= 95:
                self.logger().info(f"   🎯 Performance: EXCELLENT")
            elif success_rate >= 90:
                self.logger().info(f"   ✅ Performance: GOOD")
            elif success_rate >= 80:
                self.logger().info(f"   ⚠️ Performance: NEEDS TUNING (consider adjusting fee estimate)")
            else:
                self.logger().info(f"   ❌ Performance: POOR (check fee settings)")

            self.logger().info(f"   =========================")

    def _extract_base_token_from_symbol(self, symbol: str) -> str:
        """
        Extract base token from any symbol format - OPTIMIZED for all Coinbase tokens
        Supports any token symbol, not just ETH/BTC
        """
        try:
            # Remove common suffixes to get the base token
            symbol_upper = symbol.upper()

            # Handle different symbol formats
            if "-" in symbol_upper:
                # Format: ETH-USD, BTC-USD, SOL-USD, etc.
                return symbol_upper.split("-")[0]
            elif "/" in symbol_upper:
                # Format: ETH/USD, BTC/USD, SOL/USD, etc.
                return symbol_upper.split("/")[0]
            elif symbol_upper.endswith("USD"):
                # Format: ETHUSD, BTCUSD, SOLUSD, etc.
                return symbol_upper[:-3]
            elif symbol_upper.endswith("USDC"):
                # Format: ETHUSDC, BTCUSDC, SOLUSDC, etc.
                return symbol_upper[:-4]
            else:
                # Assume it's already the base token (ETH, BTC, SOL, ADA, etc.)
                return symbol_upper

        except Exception as e:
            self.logger().error(f"❌ Error extracting base token from {symbol}: {e}")
            # Fallback to original symbol
            return symbol.upper()

    def _update_trade_statistics(self, trade_request: Dict):
        """Update daily trading statistics"""
        try:
            # Update volume based on trade direction
            amount = Decimal(str(trade_request.get("amount", 0)))
            base_token = trade_request.get("baseToken", "")

            # For USDC/USDT trades, amount is already in USD
            if base_token in ["USDC", "USDT", "DAI"]:
                self.daily_volume += amount
            else:
                # For other tokens, we'd need price data for accurate volume
                # For now, just log that we executed a trade
                self.logger().debug(f"📊 Trade executed: {amount} {base_token}")

            # Increment trade counter
            self.daily_trade_count += 1

        except Exception as e:
            self.logger().debug(f"Could not update statistics: {e}")

    def _reset_daily_counters_if_needed(self):
        """
        Reset daily trading counters if new day started
        ENHANCED to log and reset predictive stats
        """
        try:
            current_date = datetime.now(timezone.utc).date()

            if not hasattr(self, '_current_date'):
                self._current_date = current_date

            if current_date != self._current_date:
                self.logger().info(f"📅 New day started: {current_date}")
                self.logger().info(
                    f"📊 Previous day stats: {self.daily_trade_count} trades, ${self.daily_volume} volume")

                # Log final predictive stats for the day
                if self.predictive_stats['attempts'] > 0:
                    self.logger().info("📊 Daily Predictive Selling Summary:")
                    self.log_predictive_stats()

                    # Reset predictive stats for new day
                    self.predictive_stats = {
                        'attempts': 0,
                        'successes': 0,
                        'failures': 0,
                        'fallback_success': 0
                    }

                # Reset counters
                self.daily_trade_count = 0
                self.daily_volume = Decimal("0")
                self._current_date = current_date

                self.logger().info("🔄 Daily counters reset")

        except Exception as reset_error:
            self.logger().error(f"❌ Error resetting daily counters: {reset_error}")

    async def _execute_buy_trade(self, symbol: str, amount: Decimal, network: str, exchange: str,
                                 pool: str = None) -> bool:
        """
        Execute BUY trade - Gateway 2.9 SIMPLIFIED version
        Leverages flat pool structure and explicit pool types
        """
        try:
            # Parse symbol ONCE using existing method
            base_token, quote_token = self._parse_symbol_tokens(symbol, network)
            pool_key = f"{base_token}-{quote_token}"

            self.logger().info(f"📊 BUY: {pool_key} on {network}/{exchange}")

            # Determine trade amount in quote currency
            original_usd_amount = float(amount)

            if quote_token in ["USDC", "USDT", "DAI"]:
                # Already in USD equivalent
                trade_amount = float(amount)
            else:
                # Need to convert USD to quote token
                self.logger().info(f"🔄 Converting ${original_usd_amount} to {quote_token}")

                # Get quote token price
                quote_price = await self._get_token_price(quote_token, network, exchange)
                if not quote_price or quote_price <= 0:
                    self.logger().error(f"❌ Could not get {quote_token} price")
                    return False

                trade_amount = original_usd_amount / float(quote_price)
                self.logger().info(f"💱 ${original_usd_amount} = {trade_amount:.6f} {quote_token}")

            # Gateway 2.9: Simple pool lookup from flat structure
            pool_info = await self._get_pool_info(network, exchange, pool_key, pool)

            if not pool_info:
                self.logger().error(f"❌ No pool found for {pool_key}")
                return False

            pool_address = pool_info.get('address')
            pool_type = pool_info.get('type', 'amm')  # Safe access with fallback

            # Log pool info safely
            if pool_address:
                self.logger().info(f"✅ Using {pool_type.upper()} pool: {pool_address[:10]}...")
            else:
                self.logger().info(f"✅ Using {pool_type.upper()} routing (no specific pool address)")

            # Execute trade based on network
            if self._is_solana_network(network):
                success = await self._execute_solana_buy_trade(
                    base_token=base_token,
                    quote_token=quote_token,
                    network=network,
                    exchange=exchange,
                    amount=trade_amount,
                    pool_address=pool_address,
                    pool_type=pool_type,
                    original_usd_amount=original_usd_amount
                )
            else:
                success = await self._execute_evm_buy_trade(
                    base_token, network, exchange, trade_amount, pool_address
                )

            # ⭐ Track position for sell trade
            if success:
                self._track_buy_position(
                    base_token=base_token,
                    quote_token=quote_token,
                    amount_spent=trade_amount,
                    usd_value=original_usd_amount,
                    pool=pool_address,
                    pool_type=pool_type,  # Store Gateway 2.9 pool type
                    network=network,
                    exchange=exchange
                )
                self.logger().info(f"✅ BUY trade successful!")

            return success

        except Exception as e:
            self.logger().error(f"❌ Error executing BUY trade: {e}")
            return False

    def _track_buy_position(self, base_token: str, quote_token: str, amount_spent: float,
                            usd_value: float, pool: str, pool_type: str,
                            network: str, exchange: str):
        """
        Helper: Track buy position for intelligent sell trades
        Stores Gateway 2.9 pool type for reuse in sells
        """
        self.active_positions[base_token] = {
            'quote_token': quote_token,
            'amount_spent': amount_spent,
            'usd_value': usd_value,
            'pool': pool,
            'pool_type': pool_type,  # Gateway 2.9: Explicit pool type
            'network': network,
            'exchange': exchange,
            'timestamp': time.time(),
            'tx_signature': self._last_trade_response.get('signature') if self._last_trade_response else None
        }
        self.logger().debug(f"📝 Position tracked: {base_token} with {pool_type} pool")

    async def _get_pool_info(self, network: str, exchange: str, pool_key: str = None,
                             pool_address: str = None, pool_type: str = None) -> Optional[Dict]:
        """
        Gateway 2.9 HELPER: Get pool info from local configuration
        Accept pool_type from webhook to eliminate guessing

        Args:
            network: Network name (e.g., 'mainnet-beta', 'arbitrum')
            exchange: Exchange name (e.g., 'raydium', 'uniswap')
            pool_key: Trading pair key (e.g., 'WETH-USDC')
            pool_address: Direct pool address if known
            pool_type: Pool type from webhook ('amm' or 'clmm') - NEW PARAMETER

        Returns:
            {'address': str, 'type': 'amm'|'clmm', 'base_token': str, 'quote_token': str}
        """

        # Case 1: Pool address provided directly - just return it with type
        if pool_address:
            # If pool_type provided from webhook, use it directly
            if pool_type and pool_type in ['amm', 'clmm']:
                tokens = pool_key.split("-") if pool_key else [None, None]
                self.logger().debug(f"📊 Using provided pool {pool_address[:10]}... as {pool_type}")
                return {
                    'address': pool_address,
                    'type': pool_type,
                    'base_token': tokens[0] if len(tokens) >= 2 else None,
                    'quote_token': tokens[1] if len(tokens) >= 2 else None,
                    'pair_key': pool_key,
                    'connector': exchange
                }

            # Fallback: Search configuration if no type provided (backward compatibility)
            if network in self.pool_configurations:
                for connector, pools in self.pool_configurations[network].items():
                    if exchange and connector.lower() != exchange.lower():
                        continue

                    for p_type in ['amm', 'clmm']:
                        if p_type not in pools:
                            continue

                        for pair_key, address in pools[p_type].items():
                            if address == pool_address:
                                tokens = pair_key.split("-")
                                self.logger().debug(f"📊 Found pool {pool_address[:10]}... as {pair_key} ({p_type})")
                                return {
                                    'address': pool_address,
                                    'type': p_type,
                                    'base_token': tokens[0] if len(tokens) >= 2 else None,
                                    'quote_token': tokens[1] if len(tokens) >= 2 else None,
                                    'pair_key': pair_key,
                                    'connector': connector
                                }

            # Pool not in config - use provided or default type
            return {
                'address': pool_address,
                'type': pool_type if pool_type else 'amm',
                'base_token': None,
                'quote_token': None
            }

        # Case 2: Pool key provided - look up by trading pair with type hint
        if pool_key and network in self.pool_configurations:
            network_pools = self.pool_configurations[network]

            tokens = pool_key.split("-")
            if len(tokens) < 2:
                self.logger().warning(f"⚠️ Invalid pool key format: {pool_key}")
                return None

            # SIMPLIFIED: Trust webhook pool_type directly - no complex fallback logic needed
            if pool_type and pool_type in ['amm', 'clmm', 'router']:
                self.logger().info(f"🎯 Webhook specified pool_type: {pool_type} for {pool_key}")

                # For router types (Jupiter, 0x), don't need pool lookup
                if pool_type == 'router':
                    self.logger().info(f"📡 Router type - no pool address needed for {exchange}")
                    return {
                        'address': None,  # Router doesn't use specific pool addresses
                        'type': pool_type,
                        'base_token': tokens[0],
                        'quote_token': tokens[1],
                        'pair_key': pool_key,
                        'connector': exchange.lower() if exchange else 'unknown'
                    }

                # For AMM/CLMM, look up the exact pool address
                # Check specific exchange first
                if exchange:
                    exchange_lower = exchange.lower()
                    if exchange_lower in network_pools:
                        exchange_pools = network_pools[exchange_lower]

                        if pool_type in exchange_pools and pool_key in exchange_pools[pool_type]:
                            pool_address = exchange_pools[pool_type][pool_key]
                            self.logger().info(
                                f"✅ Found {pool_key} pool: {pool_address[:10]}... ({pool_type} on {exchange_lower})")
                            return {
                                'address': pool_address,
                                'type': pool_type,
                                'base_token': tokens[0],
                                'quote_token': tokens[1],
                                'pair_key': pool_key,
                                'connector': exchange_lower
                            }
                        else:
                            self.logger().warning(
                                f"⚠️ Pool {pool_key} not found in {exchange_lower} {pool_type} configuration")

                # If not found in specific exchange, search all exchanges
                for connector, pools in network_pools.items():
                    if pool_type in pools and pool_key in pools[pool_type]:
                        pool_address = pools[pool_type][pool_key]
                        self.logger().info(
                            f"✅ Found {pool_key} pool: {pool_address[:10]}... ({pool_type} on {connector})")
                        return {
                            'address': pool_address,
                            'type': pool_type,
                            'base_token': tokens[0],
                            'quote_token': tokens[1],
                            'pair_key': pool_key,
                            'connector': connector
                        }

                # Pool type specified but not found
                self.logger().error(f"❌ Pool {pool_key} with type {pool_type} not found in configuration")
                return None

            # MINIMAL FALLBACK: Only when no pool_type provided (legacy support)
            self.logger().warning(f"⚠️ No pool_type specified, using fallback logic for {pool_key}")

            # AUTO-DETECT ROUTER TYPES: Jupiter, 0x don't need pool lookups
            if exchange and exchange.lower() in ['jupiter', '0x']:
                self.logger().info(f"🔍 Auto-detected router type for {exchange} - no pool address needed")
                return {
                    'address': None,  # Router doesn't use specific pool addresses
                    'type': 'router',
                    'base_token': tokens[0],
                    'quote_token': tokens[1],
                    'pair_key': pool_key,
                    'connector': exchange.lower()
                }

            # Simple fallback - check both types, AMM first for most cases
            for p_type in ['amm', 'clmm']:
                if exchange:
                    exchange_lower = exchange.lower()
                    if (exchange_lower in network_pools and
                        p_type in network_pools[exchange_lower] and
                        pool_key in network_pools[exchange_lower][p_type]):
                        pool_address = network_pools[exchange_lower][p_type][pool_key]
                        self.logger().info(f"✅ Fallback found {pool_key}: {pool_address[:10]}... ({p_type})")
                        return {
                            'address': pool_address,
                            'type': p_type,
                            'base_token': tokens[0],
                            'quote_token': tokens[1],
                            'pair_key': pool_key,
                            'connector': exchange_lower
                        }

            self.logger().error(f"❌ Pool {pool_key} not found in any configuration")

        return None

    async def _get_token_price(self, token: str, network: str, exchange: str) -> Optional[float]:
        """
        Unified token price getter - simplifies price fetching
        """
        if self._is_solana_network(network):
            price = await self._get_solana_token_price(token, network)
        else:
            price = await self._get_token_price_in_usd(token, network, exchange)

        return float(price) if price else None

    async def _load_token_decimals(self) -> None:
        """
        Load token decimals from Gateway configuration files
        Single source of truth for token decimal precision
        """
        try:
            self.token_decimals = {}  # {network: {symbol: decimals}}

            gateway_conf_path = self.gateway_conf_path
            tokens_path = os.path.join(gateway_conf_path, "tokens")

            # Map network names to token files
            token_files = {
                "arbitrum": "arbitrum.json",
                "mainnet": "ethereum.json",
                "polygon": "polygon.json",
                "base": "base.json",
                "optimism": "optimism.json",
                # Add other networks as needed
            }

            for network, filename in token_files.items():
                token_file = os.path.join(tokens_path, filename)

                if os.path.exists(token_file):
                    try:
                        with open(token_file, 'r') as file:
                            tokens = json.load(file)

                        self.token_decimals[network] = {}
                        for token in tokens:
                            symbol = token.get("symbol")
                            decimals = token.get("decimals")
                            if symbol and decimals is not None:
                                self.token_decimals[network][symbol] = decimals

                        self.logger().info(
                            f"✅ Loaded decimals for {len(self.token_decimals[network])} tokens on {network}")

                    except json.JSONDecodeError as e:
                        self.logger().error(f"❌ JSON error in {filename}: {e}")

        except Exception as e:
            self.logger().error(f"❌ Error loading token decimals: {e}")

    async def _execute_solana_buy_trade(self, base_token: str, quote_token: str,
                                        network: str, exchange: str, amount: float,
                                        pool_address: str, pool_type: str,
                                        original_usd_amount: float) -> bool:
        """
        Execute Solana BUY trade - Correctly handles USD value intent
        For BUY: We want to acquire $X worth of base_token using quote_token
        """
        try:
            self.logger().info(f"🎯 BUY ${original_usd_amount} worth of {base_token} using {quote_token}")

            # Get base token price to calculate how much we want to receive
            base_price = await self._get_solana_token_price(base_token, network)
            if not base_price:
                self.logger().error(f"❌ Could not get {base_token} price")
                return False

            # Calculate how much base token $1.50 gets us
            base_amount_to_receive = original_usd_amount / float(base_price)

            self.logger().info(
                f"💱 ${original_usd_amount} = {base_amount_to_receive:.6f} {base_token} @ ${base_price:.2f}")

            # For logging: calculate approximate quote token needed
            if quote_token not in ["USDC", "USDT"]:
                quote_price = await self._get_solana_token_price(quote_token, network)
                if quote_price:
                    approx_quote_needed = original_usd_amount / float(quote_price)
                    self.logger().info(f"📊 Will spend approximately {approx_quote_needed:.6f} {quote_token}")

            # Build request - amount is the base token amount we want to receive
            trade_request = {
                "network": network,
                "walletAddress": self.solana_wallet,
                "baseToken": base_token,  # RAY
                "quoteToken": quote_token,  # SOL
                "amount": base_amount_to_receive,  # How much RAY we want
                "side": "BUY",
                "slippagePct": self.slippage_tolerance
            }

            # Add pool based on type and exchange
            if exchange.lower() == "jupiter":
                # Jupiter router doesn't use pool addresses - it finds optimal routes
                endpoint = "/connectors/jupiter/router/execute-swap"
                self.logger().info(f"📡 Jupiter Routing: {base_amount_to_receive:.6f} {base_token} with {quote_token}")
            else:
                # Raydium, Meteora use pool addresses
                if pool_type == "clmm":
                    trade_request["pool"] = pool_address
                else:
                    trade_request["poolAddress"] = pool_address

                endpoint = f"/connectors/{exchange.lower()}/{pool_type}/execute-swap"
                self.logger().info(f"📡 Buying {base_amount_to_receive:.6f} {base_token} with {quote_token}")

            response = await self.gateway_request("POST", endpoint, trade_request)

            # Check for success (including timeout with signature)
            if self._is_successful_response(response):
                signature = response.get("signature")
                if signature:
                    self.logger().info(f"✅ Trade successful: {signature}")
                    self.logger().info(f"🔗 https://solscan.io/tx/{signature}")

                    # Log actual amounts if available
                    if 'totalInputSwapped' in response and 'totalOutputSwapped' in response:
                        self.logger().info(
                            f"💰 Actual: {response['totalInputSwapped']} {quote_token} → "
                            f"{response['totalOutputSwapped']} {base_token}"
                        )

                    # Track position
                    self.active_positions[base_token] = {
                        'quote_token': quote_token,
                        'amount_bought': base_amount_to_receive,
                        'usd_value': original_usd_amount,
                        'pool': pool_address,
                        'pool_type': pool_type,
                        'network': network,
                        'exchange': exchange,
                        'timestamp': time.time(),
                        'tx_signature': signature
                    }

                    self._last_trade_response = response
                    return True

            self.logger().error(f"❌ Trade failed: {response}")
            return False

        except Exception as e:
            self.logger().error(f"❌ Solana BUY error: {e}")
            return False

    async def _execute_sell_trade(self, symbol: str, percentage: Union[float, Decimal],
                                  network: str, exchange: str = "uniswap",
                                  pool: str = None, pool_type: str = None) -> bool:
        """
        Execute SELL trade - Gateway 2.9 OPTIMIZED version
        Sells percentage of token balance back to original quote token
        """
        try:
            # Parse symbol once
            base_token, quote_token = self._parse_symbol_tokens(symbol, network)
            percentage_float = float(percentage)

            # Determine actual quote token (smart position tracking)
            actual_quote_token = self._determine_quote_token(base_token, quote_token, network)

            self.logger().info(
                f"📉 SELL: {percentage_float}% of {base_token} → {actual_quote_token} "
                f"on {network}/{exchange}"
            )

            # Gateway 2.9: Get pool info if not provided
            pool_info = None
            if not pool and network in self.pool_configurations:
                # Check tracked position first for pool info
                position = self.active_positions.get(base_token, {})
                if position and position.get('pool'):
                    pool = position['pool']
                    pool_type = position.get('pool_type', 'amm')
                    pool_info = {'address': pool, 'type': pool_type}
                else:
                    # Look up pool from configuration
                    pool_key = f"{base_token}-{actual_quote_token}"
                    pool_info = await self._get_pool_info(network, exchange, pool_key, pool, pool_type)

            # Execute network-specific trade
            if self._is_solana_network(network):
                success = await self._execute_solana_sell_trade(
                    base_token, network, exchange, percentage_float,
                    pool_info['address'] if pool_info and 'address' in pool_info else pool
                )
            else:
                success = await self._execute_evm_sell_trade(
                    base_token, network, exchange, percentage_float,
                    pool_info['address'] if pool_info and 'address' in pool_info else pool
                )

            # Handle success (simplified)
            if success:
                await self._handle_sell_success(base_token, actual_quote_token, network)

            return success

        except Exception as e:
            self.logger().error(f"❌ SELL trade failed: {e}")
            return False

    def _determine_quote_token(self, base_token: str, parsed_quote: str, network: str) -> str:
        """
        Helper: Determine the actual quote token to use
        Prioritizes tracked position info over parsed symbol
        """
        # Check for tracked position (smart!)
        position = self.active_positions.get(base_token, {})
        if position and 'quote_token' in position:
            quote_token = position['quote_token']
            self.logger().debug(f"📝 Using tracked quote token: {quote_token}")
            return quote_token

        # Network-specific defaults
        if self._is_solana_network(network):
            return parsed_quote  # Use what was parsed
        else:
            return "USDC"  # EVM default

    async def _handle_sell_success(self, base_token: str, quote_token: str, network: str):
        """
        Helper: Handle successful sell completion
        Consolidates logging and cleanup
        """
        # Extract and log transaction
        if hasattr(self, '_last_trade_response') and self._last_trade_response:
            tx_hash = self._extract_transaction_hash(self._last_trade_response, network)
            if tx_hash:
                explorer_url = self._get_blockchain_explorer_url(tx_hash, network)
                self.logger().info(f"✅ SELL successful: {explorer_url}")

        # Clean up position tracking
        if base_token in self.active_positions:
            del self.active_positions[base_token]
            self.logger().debug(f"📝 Position closed: {base_token}")

        # Update statistics
        self._update_trade_statistics({
            "baseToken": base_token,
            "quoteToken": quote_token,
            "side": "SELL",
            "network": network
        })

    def _get_token_decimals(self, token: str, network: str) -> int:
        """Get token decimals from loaded configuration"""
        if network in self.token_decimals and token in self.token_decimals[network]:
            return self.token_decimals[network][token]
        # Fallback defaults
        if token in ["USDC", "USDT"]:
            return 6
        elif token in ["WBTC", "cbBTC"]:
            return 8
        return 18  # Default for most ERC20 tokens

    # Fix for WBTC buy - handles both USDC and WBTC decimal precision

    async def _execute_evm_buy_trade(self, base_token: str, network: str, exchange: str,
                                     amount: float, pool: str = None) -> bool:
        """
        Execute BUY trade on EVM networks - Gateway 2.9.0 compatible
        PROVEN APPROACH: Selling USDC to acquire base_token
        ADAPTIVE GAS: Always executes at current market gas price with max 3 retries
        FIXED: Proper decimal handling for both input (USDC) and output (WBTC) tokens
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                wallet_address = self._get_wallet_for_network(network)
                if not wallet_address:
                    self.logger().error(f"❌ No wallet configured for network: {network}")
                    return False

                # Determine DEX type: router vs pool-based
                is_router_dex = exchange.lower() in ["0x", "jupiter"]

                if is_router_dex:
                    # Router DEXs like 0x don't use pools - they aggregate across multiple sources
                    pool_type = "router"
                    self.logger().info(f"🌐 Using {exchange} router for optimal routing")
                else:
                    # Pool-based DEXs like Uniswap, Raydium
                    pool_type = "clmm"

                    if pool and network in self.pool_configurations:
                        if exchange.lower() in self.pool_configurations[network]:
                            connector_pools = self.pool_configurations[network][exchange.lower()]
                            for p_type in ["clmm", "amm"]:
                                if p_type in connector_pools and pool in connector_pools[p_type].values():
                                    pool_type = p_type
                                    break

                # Get USDC decimals
                usdc_decimals = self._get_token_decimals("USDC", network)

                # Get target token decimals (e.g., WBTC = 8)
                target_token_decimals = self._get_token_decimals(base_token, network)
                self.logger().debug(f"📊 Target token {base_token} has {target_token_decimals} decimals")

                # IMPROVED: Better decimal handling for USDC amount


                # Convert to Decimal for precise handling
                trade_amount_decimal = Decimal(str(amount))

                # Round to USDC precision
                if usdc_decimals == 0:
                    # No decimals - must be an integer
                    trade_amount_decimal = trade_amount_decimal.quantize(Decimal('1'), rounding=ROUND_DOWN)
                    trade_amount_str = str(int(trade_amount_decimal))
                    trade_amount = float(trade_amount_str)
                else:
                    # Create precision with proper number of decimal places
                    precision_str = '0.' + '0' * (usdc_decimals - 1) + '1'
                    precision = Decimal(precision_str)
                    trade_amount_decimal = trade_amount_decimal.quantize(precision, rounding=ROUND_DOWN)

                    # CRITICAL FIX: Format to ensure no excess decimals
                    # Use string formatting to control decimal places exactly
                    trade_amount = round(float(trade_amount_decimal), usdc_decimals)

                    # Ensure the float doesn't have floating point errors
                    # Format with exact decimal places needed
                    trade_amount_str = f"{trade_amount:.{usdc_decimals}f}"

                    # Parse back to float to ensure clean value
                    trade_amount = float(trade_amount_str)

                # Ensure we have a valid amount
                if trade_amount <= 0:
                    self.logger().error(f"❌ Trade amount rounds to zero")
                    return False

                self.logger().debug(f"📊 Formatted USDC amount: {trade_amount} (decimals: {usdc_decimals})")

                # CRITICAL FIX: Get a quote first to ensure proper decimal handling
                if is_router_dex:
                    # Router DEXs use /router/ prefix in endpoint structure
                    quote_endpoint = f"/connectors/{exchange.lower()}/router/quote-swap"
                else:
                    # Pool-based DEXs use pool_type in path
                    quote_endpoint = f"/connectors/{exchange.lower()}/{pool_type}/quote-swap"

                # Initialize target_base_amount for all paths
                target_base_amount = 0.0

                # For 0x router, BUY operations need different logic than pool-based DEXs
                # Experimental.  initial tests are failing
                if is_router_dex:
                    # 0x router: BUY WBTC means we want to buy WBTC using USDC
                    # First get current price to calculate how much WBTC we can buy with our USD amount
                    price_request = {
                        "network": network,
                        "baseToken": base_token,  # WBTC
                        "quoteToken": "USDC",     # USDC
                        "amount": "1",            # 1 unit of base token
                        "side": "SELL",           # Price for selling 1 WBTC
                        "indicativePrice": True   # Just price discovery
                    }

                    self.logger().info(f"🔍 Getting price for 1 {base_token} in USDC")
                    price_response = await self.gateway_request("GET", quote_endpoint, params=price_request)

                    if not price_response or "price" not in price_response:
                        self.logger().error(f"❌ Could not get price for {base_token}")
                        return False

                    # Calculate how much base token we can buy with our USD amount
                    base_token_price = float(price_response["price"])  # USDC per base_token
                    target_base_amount = trade_amount / base_token_price

                    # Round to appropriate precision for target token
                    if target_token_decimals >= 8:
                        max_precision = min(target_token_decimals, 8)
                        target_base_amount = round(target_base_amount, max_precision)
                    else:
                        target_base_amount = round(target_base_amount, target_token_decimals)

                    self.logger().info(f"🎯 Calculated target: {target_base_amount} {base_token} for ${trade_amount}")

                    quote_request = {
                        "network": network,
                        "baseToken": base_token,     # What we're buying (WBTC)
                        "quoteToken": "USDC",        # What we're paying with
                        "amount": str(target_base_amount),  # Amount of base token to buy
                        "side": "BUY"                # BUY operation
                    }
                else:
                    # Pool-based DEXs (Uniswap): BUY WBTC = SELL USDC to get WBTC
                    quote_request = {
                        "network": network,
                        "baseToken": "USDC",
                        "quoteToken": base_token,
                        "amount": str(trade_amount),  # Use string for quote
                        "side": "SELL"  # We're selling USDC to get base_token
                    }

                if pool:
                    quote_request["poolAddress"] = pool

                self.logger().info(f"🔍 Getting price quote for {trade_amount} USDC → {base_token}")

                # Get the quote
                quote_response = await self.gateway_request("GET", quote_endpoint, params=quote_request)

                expected_output = None
                min_output = None

                if quote_response and "amountOut" in quote_response:
                    expected_output_raw = float(quote_response.get("amountOut", 0))

                    # CRITICAL FIX: Format the expected output based on target token decimals
                    if target_token_decimals >= 8:
                        # Example: For high decimal tokens like WBTC (8 decimals), limit precision
                        max_precision = min(target_token_decimals, 8)
                        expected_output = round(expected_output_raw, max_precision)
                    else:
                        expected_output = round(expected_output_raw, target_token_decimals)

                    # Calculate minimum output with slippage
                    min_output = expected_output * (1 - self.slippage_tolerance / 100)

                    # Format min_output to same precision
                    if target_token_decimals >= 8:
                        max_precision = min(target_token_decimals, 8)
                        min_output = round(min_output, max_precision)
                    else:
                        min_output = round(min_output, target_token_decimals)

                    self.logger().info(
                        f"📊 Expected output: {expected_output:.8f} {base_token}, min: {min_output:.8f} {base_token}")
                else:
                    self.logger().warning(f"⚠️ Could not get quote, proceeding without min output")

                # Build trade request based on DEX type
                if is_router_dex:
                    # 0x router: Use BUY logic with calculated target amount
                    # my 0x is experimental and not working yet
                    trade_request = {
                        "network": network,
                        "walletAddress": wallet_address,
                        "baseToken": base_token,     # What we're buying (WBTC)
                        "quoteToken": "USDC",        # What we're paying with
                        "amount": target_base_amount,  # Amount of base token to buy
                        "side": "BUY",               # BUY operation
                        "slippagePct": self.slippage_tolerance
                    }
                else:
                    # Pool-based DEXs: Use SELL logic (proven to work with uniswap: for a goal to BUY WBTC we Sell USDC to get WBTC)
                    trade_request = {
                        "network": network,
                        "walletAddress": wallet_address,
                        "baseToken": "USDC",  # SELLING USDC
                        "quoteToken": base_token,  # TO GET base_token
                        "amount": trade_amount,  # Amount of USDC to sell
                        "side": "SELL",  # SELL side (proven to work)
                        "slippagePct": self.slippage_tolerance
                    }

                # Add minimum output if we calculated it
                if min_output and min_output > 0:
                    # Format as string with appropriate precision
                    if target_token_decimals >= 8:
                        max_precision = min(target_token_decimals, 8)
                        trade_request["minAmountOut"] = round(min_output, max_precision)
                    else:
                        trade_request["minAmountOut"] = round(min_output, target_token_decimals)

                # ADAPTIVE GAS STRATEGY.  This address edge cases whee the gas price is to higher than expected and
                # we have a failed trade.
                if self.gas_strategy == "adaptive":
                    # Always use current gas price with appropriate buffer
                    if attempt == 0:
                        # First attempt: use standard buffer for immediate execution
                        gas_multiplier = self.gas_buffer
                        self.logger().info(f"⛽ ADAPTIVE: Using current gas + {(gas_multiplier - 1) * 100:.0f}% buffer")
                    else:
                        # Retries: progressively increase to ensure execution
                        gas_multiplier = self.gas_buffer + (self.gas_retry_multiplier * attempt)
                        # For critical trades, use urgency multiplier
                        if attempt >= 2:
                            gas_multiplier = max(gas_multiplier, self.gas_urgency_multiplier)
                        self.logger().warning(
                            f"⛽ ADAPTIVE RETRY {attempt}: Using {(gas_multiplier - 1) * 100:.0f}% above current gas")

                    trade_request["gasPriceMultiplier"] = gas_multiplier

                if pool:
                    trade_request["poolAddress"] = pool

                endpoint = f"/connectors/{exchange.lower()}/{pool_type}/execute-swap"

                #  Use the clean string for logging
                self.logger().info(
                    f"📤 EVM BUY: Swapping {trade_amount:.{usdc_decimals}f} USDC → {base_token} on {network}")

                if expected_output:
                    self.logger().info(f"💰 Expecting ~{expected_output:.8f} {base_token}")

                response = await self.gateway_request("POST", endpoint, trade_request)
                self._last_trade_response = response

                if self._is_successful_response(response):
                    tx_hash = self._extract_transaction_hash(response, network)
                    if tx_hash:
                        self.logger().info(f"✅ EVM BUY trade successful: {tx_hash}")

                        # Log actual gas used if available
                        if "gasUsed" in response and "gasPrice" in response:
                            gas_used = response["gasUsed"]
                            gas_price = response["gasPrice"]
                            gas_cost_eth = (gas_used * gas_price) / 1e18
                            gas_cost_gwei = gas_price / 1e9
                            self.logger().info(f"⛽ Gas used: {gas_cost_eth:.6f} ETH @ {gas_cost_gwei:.2f} Gwei")

                        explorer_url = self._get_blockchain_explorer_url(tx_hash, network)
                        self.logger().info(f"🔗 Transaction: {explorer_url}")

                        self.active_positions[base_token] = {
                            'quote_token': 'USDC',
                            'amount_spent': trade_amount,
                            'pool': pool,
                            'pool_type': pool_type,
                            'network': network,
                            'timestamp': time.time()
                        }
                    return True

                # ERROR HANDLING WITH RETRIES
                error_msg = str(response.get("message", response.get("error", ""))).lower()

                if "max fee per gas less than block base fee" in error_msg or "gas price" in error_msg:
                    if attempt < max_retries - 1:
                        # Extract actual required gas from error
                        import re
                        base_fee_match = re.search(r'baseFee:\s*(\d+)', error_msg)
                        max_fee_match = re.search(r'maxFeePerGas:\s*(\d+)', error_msg)

                        if base_fee_match:
                            base_fee = int(base_fee_match.group(1))
                            base_fee_gwei = base_fee / 1e9
                            self.logger().warning(f"📊 Current base fee: {base_fee_gwei:.2f} Gwei")

                            if max_fee_match:
                                our_fee = int(max_fee_match.group(1))
                                our_fee_gwei = our_fee / 1e9
                                deficit_pct = ((base_fee - our_fee) / our_fee) * 100
                                self.logger().warning(
                                    f"📊 Our offer: {our_fee_gwei:.2f} Gwei (deficit: {deficit_pct:.1f}%)")

                        self.logger().warning(f"⚠️ Gas too low - switching to ADAPTIVE mode for retry")
                        # Force adaptive strategy for retry
                        self.gas_strategy = "adaptive"
                        await asyncio.sleep(1)  # Short wait
                        continue
                    else:
                        self.logger().error(f"❌ Unable to match required gas price after {max_retries} attempts")
                        return False

                elif "fractional component exceeds decimals" in error_msg:
                    self.logger().error(f"❌ Decimal precision error")
                    self.logger().debug(f"Attempted amount: {trade_amount}, USDC decimals: {usdc_decimals}")
                    self.logger().debug(
                        f"Expected output: {expected_output}, {base_token} decimals: {target_token_decimals}")

                    # Try without minAmountOut on retry
                    if attempt < max_retries - 1:
                        self.logger().warning(f"⚠️ Retrying without minimum output constraint...")
                        min_output = None  # Disable minimum output for retry
                        await asyncio.sleep(1)
                        continue

                    return False

                elif "transaction failed" in error_msg or "call_exception" in error_msg:
                    self.logger().error(f"❌ Transaction reverted - likely slippage or liquidity issue")
                    if attempt < max_retries - 1:
                        self.logger().info(f"⏳ Waiting before retry {attempt + 2}/{max_retries}...")
                        await asyncio.sleep(3)
                        continue
                    return False

                else:

                    self.logger().error(f"❌ EVM BUY trade failed: {response}")
                    return False

            except Exception as e:
                self.logger().error(f"❌ EVM BUY trade error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False

        return False

    # Fix for WETH (and other 18-decimal tokens) sell precision error

    async def _execute_evm_sell_trade(self, base_token: str, network: str, exchange: str, percentage: float,
                                      pool: str = None) -> bool:
        """
        Execute SELL trade on EVM networks - Gateway 2.9.0 compatible
        FIXED: Proper decimal handling for high-precision tokens like WETH (18 decimals)
        ADAPTIVE GAS: Always executes at current market gas price with max 3 retries
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                balance_response = await self._get_token_balance(base_token, network)

                if balance_response is None or balance_response <= 0:
                    self.logger().warning(f"⚠️ No {base_token} balance available to sell")
                    return False

                # Get token decimals from configuration
                token_decimals = self._get_token_decimals(base_token, network)

                # Calculate sell amount
                sell_amount_raw = float(balance_response) * (percentage / 100.0)

                # Convert to Decimal for precise handling
                sell_amount_decimal = Decimal(str(sell_amount_raw))

                # SPECIAL HANDLING FOR HIGH DECIMAL TOKENS
                if token_decimals >= 8:  # For tokens with 8+ decimals
                    # Limit precision to prevent Gateway/ethers.js overflow
                    # Most DEXs don't need more than 8-10 decimal precision anyway
                    max_precision = 10  # Use at most 10 decimal places

                    # Create precision string with limited decimals
                    actual_precision = min(token_decimals, max_precision)
                    precision_str = '0.' + '0' * (actual_precision - 1) + '1'
                    precision = Decimal(precision_str)
                    sell_amount_decimal = sell_amount_decimal.quantize(precision, rounding=ROUND_DOWN)

                    # Clean conversion to float
                    sell_amount = float(sell_amount_decimal)

                    # Additional safety: ensure we don't have floating point artifacts
                    sell_amount_str = f"{sell_amount:.{actual_precision}f}"
                    # Remove trailing zeros to keep it clean
                    sell_amount_str = sell_amount_str.rstrip('0').rstrip('.')
                    sell_amount = float(sell_amount_str)

                    self.logger().debug(
                        f"📊 High-precision token {base_token}: {sell_amount} (limited to {actual_precision} decimals)")

                elif token_decimals == 0:
                    # Integer tokens (no decimals)
                    sell_amount_decimal = sell_amount_decimal.quantize(Decimal('1'), rounding=ROUND_DOWN)
                    sell_amount = float(int(sell_amount_decimal))
                else:
                    # Low decimal tokens (1-7 decimals)
                    precision_str = '0.' + '0' * (token_decimals - 1) + '1'
                    precision = Decimal(precision_str)
                    sell_amount_decimal = sell_amount_decimal.quantize(precision, rounding=ROUND_DOWN)
                    sell_amount = round(float(sell_amount_decimal), token_decimals)

                if sell_amount <= 0:
                    self.logger().error(f"❌ Calculated sell amount is zero")
                    return False

                self.logger().debug(
                    f"📊 Formatted {base_token} amount: {sell_amount} (token decimals: {token_decimals})")

                wallet_address = self._get_wallet_for_network(network)
                if not wallet_address:
                    self.logger().error(f"❌ No wallet configured for network: {network}")
                    return False

                # Determine DEX type: router vs pool-based
                is_router_dex = exchange.lower() in ["0x", "jupiter"]

                if is_router_dex:
                    # Router DEXs like 0x don't use pools - they aggregate across multiple sources
                    pool_type = "router"
                    self.logger().info(f"🌐 Using {exchange} router for optimal routing")
                else:
                    # Pool-based DEXs like Uniswap, Raydium
                    pool_type = "clmm"
                    if base_token in self.active_positions:
                        position = self.active_positions[base_token]
                        if position.get('network') == network and position.get('pool_type'):
                            pool_type = position['pool_type']
                            if not pool and position.get('pool'):
                                pool = position['pool']
                    elif pool and network in self.pool_configurations:
                        if exchange.lower() in self.pool_configurations[network]:
                            connector_pools = self.pool_configurations[network][exchange.lower()]
                            for p_type in ["clmm", "amm"]:
                                if p_type in connector_pools and pool in connector_pools[p_type].values():
                                    pool_type = p_type
                                    break

                # Get a price quote first to calculate proper minimum output
                if is_router_dex:
                    # Router DEXs use /router/ prefix in endpoint structure
                    quote_endpoint = f"/connectors/{exchange.lower()}/router/quote-swap"
                else:
                    # Pool-based DEXs use pool_type in path
                    quote_endpoint = f"/connectors/{exchange.lower()}/{pool_type}/quote-swap"

                quote_request = {
                    "network": network,
                    "baseToken": base_token,
                    "quoteToken": "USDC",
                    "amount": str(sell_amount),  # Use string for quote
                    "side": "SELL"
                }

                # Only add pool address for pool-based DEXs
                if pool and not is_router_dex:
                    quote_request["poolAddress"] = pool

                self.logger().info(f"🔍 Getting price quote for {sell_amount} {base_token}")

                # Get the quote first
                quote_response = await self.gateway_request("GET", quote_endpoint, params=quote_request)

                expected_output = None
                min_output = None

                if quote_response and "amountOut" in quote_response:
                    expected_output = float(quote_response.get("amountOut", 0))
                    # Apply slippage tolerance to get minimum acceptable output
                    min_output = expected_output * (1 - self.slippage_tolerance / 100)
                    self.logger().info(f"📊 Expected output: {expected_output:.6f} USDC, min: {min_output:.6f} USDC")
                else:
                    self.logger().warning(f"⚠️ Could not get quote, using default slippage")
                    # Fallback: Let Gateway handle it
                    min_output = 0

                # Build the actual trade request
                trade_request = {
                    "network": network,
                    "walletAddress": wallet_address,
                    "baseToken": base_token,
                    "quoteToken": "USDC",
                    "amount": sell_amount,  # Now properly formatted with limited precision
                    "side": "SELL",
                    "slippagePct": self.slippage_tolerance
                }

                # CRITICAL: Add minimum output if we calculated it
                if min_output and min_output > 0:
                    trade_request["minAmountOut"] = min_output

                # ADAPTIVE GAS STRATEGY
                if self.gas_strategy == "adaptive":
                    # Always use current gas price with appropriate buffer
                    if attempt == 0:
                        # First attempt: use standard buffer for immediate execution
                        gas_multiplier = self.gas_buffer
                        self.logger().info(f"⛽ ADAPTIVE: Using current gas + {(gas_multiplier - 1) * 100:.0f}% buffer")
                    else:
                        # Retries: progressively increase to ensure execution
                        gas_multiplier = self.gas_buffer + (self.gas_retry_multiplier * attempt)
                        # For critical trades, use urgency multiplier
                        if attempt >= 2:
                            gas_multiplier = max(gas_multiplier, self.gas_urgency_multiplier)
                        self.logger().warning(
                            f"⛽ ADAPTIVE RETRY {attempt}: Using {(gas_multiplier - 1) * 100:.0f}% above current gas")

                    trade_request["gasPriceMultiplier"] = gas_multiplier

                # Only add pool address for pool-based DEXs
                if pool and not is_router_dex:
                    trade_request["poolAddress"] = pool

                # Use appropriate endpoint based on DEX type
                if is_router_dex:
                    endpoint = f"/connectors/{exchange.lower()}/router/execute-swap"
                else:
                    endpoint = f"/connectors/{exchange.lower()}/{pool_type}/execute-swap"

                # Log with appropriate precision for display
                display_precision = min(token_decimals, 10)
                self.logger().info(
                    f"📤 EVM SELL: Selling {sell_amount:.{display_precision}f} {base_token} → USDC on {network}")

                if expected_output:
                    self.logger().info(f"💰 Expecting ~{expected_output:.2f} USDC (min: {min_output:.2f})")

                response = await self.gateway_request("POST", endpoint, trade_request)
                self._last_trade_response = response

                if self._is_successful_response(response):
                    tx_hash = self._extract_transaction_hash(response, network)
                    if tx_hash:
                        self.logger().info(f"✅ EVM SELL trade successful: {tx_hash}")

                        # Log actual gas used if available
                        if "gasUsed" in response and "gasPrice" in response:
                            gas_used = response["gasUsed"]
                            gas_price = response["gasPrice"]
                            gas_cost_eth = (gas_used * gas_price) / 1e18
                            gas_cost_gwei = gas_price / 1e9
                            self.logger().info(f"⛽ Gas used: {gas_cost_eth:.6f} ETH @ {gas_cost_gwei:.2f} Gwei")

                        explorer_url = self._get_blockchain_explorer_url(tx_hash, network)
                        self.logger().info(f"🔗 Transaction: {explorer_url}")

                    if base_token in self.active_positions:
                        del self.active_positions[base_token]

                    return True

                # ERROR HANDLING
                error_msg = str(response.get("message", response.get("error", ""))).lower()

                if "max fee per gas less than block base fee" in error_msg or "gas price" in error_msg:
                    if attempt < max_retries - 1:
                        # Extract actual required gas from error

                        base_fee_match = re.search(r'baseFee:\s*(\d+)', error_msg)
                        max_fee_match = re.search(r'maxFeePerGas:\s*(\d+)', error_msg)

                        if base_fee_match:
                            base_fee = int(base_fee_match.group(1))
                            base_fee_gwei = base_fee / 1e9
                            self.logger().warning(f"📊 Current base fee: {base_fee_gwei:.2f} Gwei")

                            if max_fee_match:
                                our_fee = int(max_fee_match.group(1))
                                our_fee_gwei = our_fee / 1e9
                                deficit_pct = ((base_fee - our_fee) / our_fee) * 100
                                self.logger().warning(
                                    f"📊 Our offer: {our_fee_gwei:.2f} Gwei (deficit: {deficit_pct:.1f}%)")

                        self.logger().warning(f"⚠️ Gas too low - switching to ADAPTIVE mode for retry")
                        # Force adaptive strategy for retry
                        self.gas_strategy = "adaptive"
                        await asyncio.sleep(1)  # Short wait
                        continue
                    else:
                        self.logger().error(f"❌ Unable to match required gas price after {max_retries} attempts")
                        return False

                elif "fractional component exceeds decimals" in error_msg:
                    self.logger().error(f"❌ Decimal precision error for {base_token}")
                    self.logger().debug(f"Attempted amount: {sell_amount}, decimals: {token_decimals}")

                    # If we still get this error, try with even less precision
                    if token_decimals >= 8 and attempt < max_retries - 1:
                        self.logger().warning(f"⚠️ Retrying with reduced precision...")
                        # Force even lower precision on retry
                        max_precision = 8  # Reduce to 8 decimals max
                        await asyncio.sleep(1)
                        continue

                    return False

                elif "transaction failed" in error_msg or "call_exception" in error_msg:
                    self.logger().error(f"❌ Transaction reverted - likely slippage or price movement")
                    if attempt < max_retries - 1:
                        self.logger().info(f"⏳ Waiting before retry {attempt + 2}/{max_retries}...")
                        await asyncio.sleep(3)
                        continue
                    return False

                else:
                    self.logger().error(f"❌ EVM SELL trade failed: {response}")
                    return False

            except Exception as e:
                self.logger().error(f"❌ EVM SELL trade error: {e}")
                self.logger().debug(f"Traceback: {traceback.format_exc()}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue
                return False

        return False

    async def _execute_solana_sell_trade(self, base_token: str, network: str, exchange: str,
                                         percentage: float, pool: str = None) -> bool:
        """
        Execute SELL trade on Solana - Gateway 2.9 simplified version
        Uses unified _get_pool_info method
        """
        try:
            # Step 1: Determine pool and quote token
            position = self.active_positions.get(base_token, {})

            if position:
                # Use tracked position info
                quote_token = position.get("quote_token", "USDC")
                pool_type = position.get("pool_type", "amm")

                if not pool and position.get("pool"):
                    pool = position["pool"]
                    self.logger().info(f"📝 Using pool from BUY position: {pool[:10]}...")

                self.logger().info(f"📝 Closing position: {base_token} → {quote_token} ({pool_type})")

            else:
                # No position tracked - determine pool info
                self.logger().info(f"⚠️ No position tracked for {base_token}, detecting pool configuration")

                # Default quote token
                quote_token = "USDC"
                pool_type = None

                # If pool address provided, get its info
                if pool:
                    pool_info = await self._get_pool_info(network, exchange, pool_address=pool)
                    if pool_info:
                        pool_type = pool_info['type']
                        # Use discovered quote token if available
                        if pool_info.get('quote_token'):
                            quote_token = pool_info['quote_token']
                        self.logger().info(f"📋 Found pool config: {pool_type} pool for {quote_token}")

                # If no pool provided, find one
                else:
                    # Special handling for Jupiter - it's a router and doesn't need pool lookup
                    if exchange and exchange.lower() == "jupiter":
                        pool_type = "router"
                        pool = None
                        self.logger().info(f"📋 Jupiter router detected - no pool address needed for {base_token}-{quote_token}")
                    else:
                        pool_key = f"{base_token}-{quote_token}"
                        pool_info = await self._get_pool_info(network, exchange, pool_key=pool_key)

                        if pool_info:
                            pool = pool_info['address']
                            pool_type = pool_info['type']
                            if pool_info.get('quote_token'):
                                quote_token = pool_info['quote_token']

                            # Handle router-based exchanges (Jupiter) that don't use pool addresses
                            if pool_type == 'router' or pool is None:
                                self.logger().info(f"📋 Found {pool_type} configuration for {base_token}-{quote_token}")
                            else:
                                self.logger().info(f"📋 Found {pool_type} pool: {pool[:10]}...")
                        else:
                            # pool_info is None - handle gracefully
                            self.logger().warning(f"⚠️ No pool configuration found for {base_token}-{quote_token} on {exchange}")
                            pool = None
                            pool_type = None

                # Final fallback for pool type
                if not pool_type:
                    # CRITICAL FIX: On Solana, default to AMM for most trading pairs
                    pool_type = "amm"
                    self.logger().info(f"📋 Using default AMM for Solana trading")

            # Step 2: Get balance with guaranteed initialization
            balance = await self._get_token_balance(base_token, network)

            # Ensure balance is always a number, never None
            if balance is None:
                balance = 0

            if not balance or balance <= 0:
                # Retry with direct Gateway call
                self.logger().info(f"⚠️ Retrying balance check with direct Gateway call")

                wallet_address = self._get_wallet_for_network(network)
                balance_request = {
                    "network": network,
                    "address": wallet_address,
                    "tokens": [base_token]
                }

                response = await self.gateway_request("POST", "/chains/solana/balances", balance_request)

                if response and "balances" in response and response["balances"] is not None:
                    balances_dict = response["balances"]
                    if isinstance(balances_dict, dict):
                        balance = float(balances_dict.get(base_token, 0))
                    else:
                        self.logger().warning(f"⚠️ Balances response is not a dict: {type(balances_dict)}")
                        balance = 0
                else:
                    # If Gateway response is None or invalid, set balance to 0
                    self.logger().warning(f"⚠️ Gateway balance response invalid for {base_token}")
                    balance = 0

                if not balance or balance <= 0:
                    self.logger().warning(f"⚠️ No {base_token} balance to sell")
                    self.active_positions.pop(base_token, None)
                    return False

            # Step 3: Calculate sell amount with SOL minimum balance protection
            sell_amount = float(balance) * (percentage / 100.0)

            # SOL minimum balance protection for gas fees
            if base_token == "SOL" and quote_token in ["USDC", "USDT"]:
                if sell_amount > (float(balance) - self.min_sol_balance):
                    original_sell_amount = sell_amount
                    sell_amount = max(0.0, float(balance) - self.min_sol_balance)
                    self.logger().info(
                        f"🛡️ SOL minimum balance protection: Reducing sell from {original_sell_amount:.6f} to {sell_amount:.6f} "
                        f"to preserve {self.min_sol_balance} SOL for gas fees"
                    )

                    if sell_amount <= 0:
                        self.logger().warning(
                            f"⚠️ Cannot sell SOL: Would leave less than minimum balance of {self.min_sol_balance} SOL for gas fees"
                        )
                        return False

            self.logger().info(f"💰 Selling {sell_amount:.6f} {base_token} ({percentage}% of {balance:.6f})")

            # Step 4: Build trade request
            wallet_address = self._get_wallet_for_network(network)

            trade_request = {
                "network": network,
                "walletAddress": wallet_address,
                "baseToken": base_token,
                "quoteToken": quote_token,
                "amount": sell_amount,
                "side": "SELL",
                "slippagePct": self.slippage_tolerance
            }

            # Add pool parameter based on type and exchange
            if exchange.lower() == "jupiter":
                # Jupiter router doesn't use pool addresses - it finds optimal routes
                endpoint = "/connectors/jupiter/router/execute-swap"
                self.logger().info(f"📡 Jupiter Routing: {sell_amount:.6f} {base_token} → {quote_token}")
            else:
                # Raydium, Meteora use pool addresses
                if pool_type == "clmm":
                    if pool:
                        trade_request["pool"] = pool
                    endpoint = f"/connectors/{exchange.lower()}/clmm/execute-swap"
                else:  # AMM
                    if pool:
                        trade_request["poolAddress"] = pool
                    endpoint = f"/connectors/{exchange.lower()}/amm/execute-swap"

            # Step 5: Execute trade
            self.logger().info(f"📤 SELL: {sell_amount:.6f} {base_token} → {quote_token}")

            # CRITICAL FIX: For CLMM pools on Solana, handle slippage differently
            if pool_type == "clmm" and self._is_solana_network(network):
                # Get quote to calculate minimum output with slippage
                quote_endpoint = f"/connectors/{exchange.lower()}/clmm/quote-swap"
                quote_params = {
                    "network": network,
                    "baseToken": base_token,
                    "quoteToken": quote_token,
                    "amount": str(sell_amount),
                    "side": "SELL"
                }
                if pool:
                    quote_params["pool"] = pool

                quote_response = await self.gateway_request("GET", quote_endpoint, params=quote_params)

                if quote_response and "amountOut" in quote_response:
                    expected_output = float(quote_response.get("amountOut", 0))
                    # Apply slippage tolerance
                    min_output = expected_output * (1 - self.slippage_tolerance / 100)
                    trade_request["minAmountOut"] = min_output
                    self.logger().info(f"📊 Expected output: {expected_output:.6f} {quote_token}, min: {min_output:.6f}")

            self.logger().debug(f"📋 Request to {endpoint}: {json.dumps(trade_request, indent=2)}")

            response = await self.gateway_request("POST", endpoint, trade_request)
            self._last_trade_response = response

            # Step 6: Handle response
            if self._is_successful_response(response) and "signature" in response:
                signature = response["signature"]

                self.logger().info(f"✅ SELL successful: {signature}")
                self.logger().info(f"🔗 https://solscan.io/tx/{signature}")

                # Log actual swap amounts
                if "totalInputSwapped" in response and "totalOutputSwapped" in response:
                    self.logger().info(
                        f"💰 Swapped: {response['totalInputSwapped']} {base_token} → "
                        f"{response['totalOutputSwapped']} {quote_token}"
                    )

                # Clean up position tracking
                self.active_positions.pop(base_token, None)
                self.logger().info(f"📝 Position closed: {base_token}")

                return True
            else:
                self.logger().error(f"❌ SELL failed: {response}")
                return False

        except Exception as e:
            self.logger().error(f"❌ SELL error: {e}")
            self.logger().debug(traceback.format_exc())
            return False

    def _parse_symbol_tokens(self, symbol: str, network: str) -> tuple:
        """Parse trading symbol into base and quote tokens - unchanged"""
        symbol_upper = symbol.upper()

        if network == "mainnet-beta":
            known_tokens = ['SOL', 'USDC', 'USDT', 'RAY', 'PEPE', 'WIF', 'POPCAT',
                           'TRUMP', 'LAYER', 'JITOSOL', 'BONK', 'FARTCOIN']

            for token in known_tokens:
                if symbol_upper.startswith(token):
                    remainder = symbol_upper[len(token):]
                    if remainder in known_tokens:
                        return token, remainder
                if symbol_upper.endswith(token):
                    prefix = symbol_upper[:-len(token)]
                    if prefix in known_tokens:
                        return prefix, token

            if 'USD' in symbol_upper:
                parts = symbol_upper.split('USD')
                if len(parts) == 2 and parts[1] in ['C', 'T']:
                    return parts[0], 'USD' + parts[1]

        for quote in ['USDC', 'USDT', 'USD', 'DAI', 'ETH', 'WETH']:
            if symbol_upper.endswith(quote):
                base = symbol_upper[:-len(quote)]
                if base:
                    return base, quote

        if '-' in symbol:
            parts = symbol.split('-')
            if len(parts) == 2:
                return parts[0].upper(), parts[1].upper()

        self.logger().warning(f"⚠️ Could not parse {symbol}, defaulting to {symbol}-USDC")
        return symbol.upper(), 'USDC'

    async def _get_token_price_in_usd(self, token: str, network: str, exchange: str = "raydium") -> Optional[Decimal]:
        """
        Get token price in USD from Gateway
        FIXED: Use quote-swap endpoint for all Raydium price queries with proper params
        """
        try:
            # For Raydium on Solana, always use quote-swap endpoint
            if network == "mainnet-beta" and exchange == "raydium":
                # Determine which pool to use for price query
                pool_address = None
                pool_type = "clmm"  # Default to CLMM for USDC pairs

                # Special case for SOL - we know it's in CLMM
                if token == "SOL":
                    pool_address = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
                    pool_type = "clmm"
                else:
                    # Try to find the token-USDC pool
                    pool_symbol = f"{token}-USDC"

                    if network in self.pool_configurations:
                        # Check CLMM first (preferred for USDC pairs)
                        if "clmm" in self.pool_configurations[network]:
                            if pool_symbol in self.pool_configurations[network]["clmm"]:
                                pool_address = self.pool_configurations[network]["clmm"][pool_symbol]
                                pool_type = "clmm"

                        # Check AMM if not found in CLMM
                        if not pool_address and "amm" in self.pool_configurations[network]:
                            if pool_symbol in self.pool_configurations[network]["amm"]:
                                pool_address = self.pool_configurations[network]["amm"][pool_symbol]
                                pool_type = "amm"

                # Use quote-swap endpoint (GET request)
                endpoint = f"/connectors/raydium/{pool_type}/quote-swap"

                # Build query parameters for GET request
                price_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "USDC",
                    "amount": "1",  # String for GET params
                    "side": "SELL"
                }

                if pool_address:
                    price_params["poolAddress"] = pool_address

                self.logger().info(f"🔍 Fetching {token} price from Raydium {pool_type.upper()} using quote-swap...")

                # CRITICAL FIX: Pass params as the params argument, not data
                response = await self.gateway_request("GET", endpoint, params=price_params)

            elif network == "mainnet-beta" and exchange == "meteora":
                # Meteora also uses quote-swap endpoint
                endpoint = "/connectors/meteora/clmm/quote-swap"

                price_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "USDC",
                    "amount": "1",  # String for GET params
                    "side": "SELL"
                }

                self.logger().info(f"🔍 Fetching {token} price from Meteora using quote-swap...")

                # CRITICAL FIX: Pass params as the params argument
                response = await self.gateway_request("GET", endpoint, params=price_params)

            elif network == "mainnet-beta" and exchange == "jupiter":
                # Jupiter router uses quote-swap endpoint
                endpoint = "/connectors/jupiter/router/quote-swap"

                price_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "USDC",
                    "amount": "1",  # String for GET params
                    "side": "SELL"
                }

                self.logger().info(f"🔍 Fetching {token} price from Jupiter Router using quote-swap...")

                # Use GET request for quote-swap endpoint
                response = await self.gateway_request("GET", endpoint, params=price_params)

            else:
                # For other networks/exchanges (EVM), check if we should use GET or POST
                if exchange in ["uniswap", "pancakeswap"]:
                    # EVM exchanges might use GET for quote-swap
                    endpoint = f"/connectors/{exchange}/quote-swap"
                    price_params = {
                        "network": network,
                        "baseToken": token,
                        "quoteToken": "USDC",
                        "amount": "1",
                        "side": "SELL"
                    }
                    self.logger().info(f"🔍 Fetching {token} price from {exchange} on {network}...")
                    response = await self.gateway_request("GET", endpoint, params=price_params)
                else:
                    # Fallback to POST for price endpoint (if it exists)
                    endpoint = f"/connectors/{exchange}/price"
                    price_request = {
                        "network": network,
                        "baseToken": token,
                        "quoteToken": "USDC",
                        "amount": 1.0
                    }
                    self.logger().info(f"🔍 Fetching {token} price from {exchange} on {network}...")
                    response = await self.gateway_request("POST", endpoint, data=price_request)

            if self._is_successful_response(response):
                # Extract price from response
                if "price" in response:
                    price = Decimal(str(response["price"]))
                    self.logger().info(f"✅ Live {token} price: ${price:.2f} USD")
                    return price
                elif "amountOut" in response:
                    # quote-swap returns amountOut for 1 token
                    amount_out = Decimal(str(response["amountOut"]))
                    price = amount_out  # This is the price in USDC for 1 token
                    self.logger().info(f"✅ Live {token} price: ${price:.2f} USD")
                    return price
                elif "expectedAmount" in response:
                    # Some endpoints return expectedAmount instead of price
                    expected = Decimal(str(response["expectedAmount"]))
                    price = expected  # Already for 1 token
                    self.logger().info(f"✅ Live {token} price: ${price:.2f} USD")
                    return price

            # If price fetch failed, return None (caller will use fallback)
            self.logger().warning(f"⚠️ Could not fetch live price for {token}")
            return None

        except Exception as e:
            self.logger().error(f"❌ Error fetching {token} price: {e}")
            fallback = self._get_fallback_price(token)
            self.logger().warning(f"⚠️ Using fallback price ${fallback:.2f} due to error")
            return fallback

    def _get_fallback_price(self, token: str) -> Decimal:
        """
        Get fallback price for token when live price unavailable
        Only used in edge situations when we see network issues
        """
        fallback_prices = {
            # Major tokens
            "BTC": Decimal("100000"),
            "WBTC": Decimal("100000"),
            "ETH": Decimal("3200"),
            "WETH": Decimal("3200"),
            "SOL": Decimal("200"),
            "RAY": Decimal("3.5"),

            # Stablecoins
            "USDC": Decimal("1"),
            "USDT": Decimal("1"),
            "DAI": Decimal("1"),

            # Meme/Alt tokens (approximate)
            "WIF": Decimal("2.5"),
            "POPCAT": Decimal("1.2"),
            "PEPE": Decimal("0.000012"),
            "TRUMP": Decimal("4.5"),
            "LAYER": Decimal("0.15"),

            # Other common tokens
            "MATIC": Decimal("0.70"),
            "LINK": Decimal("14"),
            "UNI": Decimal("7"),
            "AAVE": Decimal("95"),
            "COMP": Decimal("55"),
        }

        token_upper = token.upper()
        if token_upper in fallback_prices:
            return fallback_prices[token_upper]

        # Default fallback for unknown tokens
        self.logger().warning(f"⚠️ No fallback price for {token}, using $10 default")
        return Decimal("10")

    async def _get_solana_token_price(self, token: str, network: str) -> Optional[float]:
        """
        Get token price in USD with special handling for SOL and tokens without USDC pairs

        - SOL-USDC pool is in CLMM
        - Many tokens only have SOL pairs, not USDC pairs
        - Need to calculate USD price through SOL bridge
        - This method is kind of messy but it is reliable
        """
        try:
            # Special handling for stablecoins
            if token in ["USDC", "USDT"]:
                return 1.0

            self.logger().info(f"💰 Getting {token} price on {network}...")

            # For SOL, we KNOW it's in CLMM with a specific pool
            if token == "SOL":
                # Use the known CLMM pool directly
                pool_address = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
                pool_type = "clmm"

                self.logger().info(f"🔍 Using known SOL-USDC CLMM pool: {pool_address[:8]}...")

                # Try to get price from CLMM
                try:
                    # Method 1: Try quote-swap endpoint
                    quote_params = {
                        "network": network,
                        "baseToken": "SOL",
                        "quoteToken": "USDC",
                        "amount": "1",
                        "side": "SELL",
                        "poolAddress": pool_address
                    }

                    endpoint = f"/connectors/raydium/clmm/quote-swap"
                    response = await self.gateway_request("GET", endpoint, params=quote_params)

                    if response and "amountOut" in response:
                        price = float(response["amountOut"])
                        self.logger().info(f"✅ SOL price from CLMM quote: ${price:.2f}")
                        return price

                except Exception as e:
                    self.logger().warning(f"⚠️ CLMM quote failed: {e}")

                # Method 2: Fallback to a reasonable SOL price
                fallback_price = 196.0
                self.logger().warning(f"⚠️ Using fallback SOL price: ${fallback_price:.2f}")
                return fallback_price

            # For other tokens, first try direct USDC pair
            pool_address = None
            pool_type = "amm"  # Default to AMM for other tokens

            # Check if we have a direct USDC pair in our configuration
            if network in self.pool_configurations:
                network_config = self.pool_configurations[network]

                # Check Raydium pools for USDC pair
                if "raydium" in network_config:
                    raydium_config = network_config["raydium"]

                    # Format symbol for lookup
                    formatted_symbol = f"{token}-USDC"

                    # Check AMM first for USDC pairs
                    if "amm" in raydium_config and formatted_symbol in raydium_config["amm"]:
                        pool_address = raydium_config["amm"][formatted_symbol]
                        pool_type = "amm"
                        self.logger().info(f"✅ Found AMM pool for {formatted_symbol}: {pool_address[:8]}...")

                    # Check CLMM if no AMM pool found
                    elif "clmm" in raydium_config and formatted_symbol in raydium_config["clmm"]:
                        pool_address = raydium_config["clmm"][formatted_symbol]
                        pool_type = "clmm"
                        self.logger().info(f"✅ Found CLMM pool for {formatted_symbol}: {pool_address[:8]}...")

            # If we have a direct USDC pool, use it
            if pool_address:
                quote_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "USDC",
                    "amount": "1",
                    "side": "SELL",
                    "poolAddress": pool_address
                }

                endpoint = f"/connectors/raydium/{pool_type}/quote-swap"
                self.logger().debug(f"🔍 Getting {token} price from {endpoint}")

                response = await self.gateway_request("GET", endpoint, params=quote_params)

                if response and "amountOut" in response:
                    price = float(response["amountOut"])
                    self.logger().info(f"💰 {token} price: ${price:.6f}")
                    return price

            # If no direct USDC pair, try to get price through SOL bridge
            self.logger().info(f"🔄 No direct USDC pair for {token}, trying SOL bridge...")

            # Look for token-SOL pair
            sol_pool_address = None
            sol_pool_type = "amm"

            if network in self.pool_configurations and "raydium" in self.pool_configurations[network]:
                raydium_config = self.pool_configurations[network]["raydium"]

                # Check for token-SOL pair
                formatted_symbol = f"{token}-SOL"

                # Check AMM
                if "amm" in raydium_config and formatted_symbol in raydium_config["amm"]:
                    sol_pool_address = raydium_config["amm"][formatted_symbol]
                    sol_pool_type = "amm"
                    self.logger().info(f"✅ Found {formatted_symbol} AMM pool: {sol_pool_address[:8]}...")

                # Check CLMM
                elif "clmm" in raydium_config and formatted_symbol in raydium_config["clmm"]:
                    sol_pool_address = raydium_config["clmm"][formatted_symbol]
                    sol_pool_type = "clmm"
                    self.logger().info(f"✅ Found {formatted_symbol} CLMM pool: {sol_pool_address[:8]}...")

            if sol_pool_address:
                # Get token price in SOL
                quote_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "SOL",
                    "amount": "1",
                    "side": "SELL",
                    "poolAddress": sol_pool_address
                }

                endpoint = f"/connectors/raydium/{sol_pool_type}/quote-swap"
                self.logger().debug(f"🔍 Getting {token} price in SOL from {endpoint}")

                response = await self.gateway_request("GET", endpoint, params=quote_params)

                if response and "amountOut" in response:
                    token_price_in_sol = float(response["amountOut"])
                    self.logger().info(f"💱 {token} = {token_price_in_sol:.9f} SOL")

                    # Get SOL price in USD
                    sol_price_usd = await self._get_solana_token_price("SOL", network)

                    if sol_price_usd:
                        # Calculate USD price
                        token_price_usd = token_price_in_sol * sol_price_usd
                        self.logger().info(f"💰 {token} price: ${token_price_usd:.9f} (via SOL bridge)")
                        return token_price_usd

            # If all else fails, try without pool address (let Gateway auto-select)
            self.logger().warning(f"⚠️ Trying to get {token} price without specific pool...")

            # Try token-USDC first
            try:
                quote_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "USDC",
                    "amount": "1",
                    "side": "SELL"
                }

                response = await self.gateway_request("GET", "/connectors/raydium/amm/quote-swap", params=quote_params)

                if response and "amountOut" in response:
                    price = float(response["amountOut"])
                    self.logger().info(f"💰 {token} price (auto-selected pool): ${price:.6f}")
                    return price
            except:
                pass

            # Try token-SOL as last resort
            try:
                quote_params = {
                    "network": network,
                    "baseToken": token,
                    "quoteToken": "SOL",
                    "amount": "1",
                    "side": "SELL"
                }

                response = await self.gateway_request("GET", "/connectors/raydium/amm/quote-swap", params=quote_params)

                if response and "amountOut" in response:
                    token_price_in_sol = float(response["amountOut"])
                    sol_price_usd = await self._get_solana_token_price("SOL", network)

                    if sol_price_usd:
                        token_price_usd = token_price_in_sol * sol_price_usd
                        self.logger().info(f"💰 {token} price (via SOL, auto-pool): ${token_price_usd:.9f}")
                        return token_price_usd
            except:
                pass

            self.logger().warning(f"⚠️ Could not get price for {token}")
            return None

        except Exception as e:
            self.logger().error(f"❌ Error getting {token} price: {e}")
            return None

    def _get_wallet_for_network(self, network: str) -> str:
        """Get appropriate wallet address for network type"""
        if self._is_solana_network(network):
            return self.solana_wallet
        else:
            return self.arbitrum_wallet  # For EVM networks

    async def _get_token_balance(self, token_symbol: str, network: str) -> Optional[float]:
        """
        Query token balance - Updated for Gateway 2.9.0 endpoints
        """
        try:
            wallet_address = self._get_wallet_for_network(network)

            balance_request = {
                "network": network,
                "address": wallet_address,
                "tokens": [token_symbol]
            }

            # Gateway 2.9: Updated endpoint with /chains/ prefix
            if self._is_solana_network(network):
                endpoint = "/chains/solana/balances"
            else:
                endpoint = "/chains/ethereum/balances"

            response = await self.gateway_request("POST", endpoint, balance_request)

            if response and "balances" in response and response["balances"] is not None:
                balance = float(response["balances"].get(token_symbol, 0))
                self.logger().debug(f"💰 {token_symbol} balance on {network}: {balance}")
                return balance

            return None

        except Exception as e:
            self.logger().error(f"❌ Balance query failed: {e}")
            return None
    @staticmethod
    def _is_solana_network(network: str) -> bool:
        """
        Determine if a network is Solana-based

        SOLANA NETWORKS:
        - mainnet-beta (production)
        - devnet (testing)

        ALL OTHER NETWORKS ARE EVM:
        - mainnet, arbitrum, optimism, base, sepolia, bsc, avalanche, celo, polygon, blast, zora, worldchain
        """
        return network in ["mainnet-beta", "devnet"]

    @staticmethod
    def _get_blockchain_explorer_url(tx_hash: str, network: str) -> str:
        """
        Get blockchain explorer URL for transaction verification

        SUPPORTS ALL NETWORKS with appropriate explorers
        """
        if not tx_hash:
            return ""

        # === SOLANA NETWORKS ===
        if network in ["mainnet-beta", "devnet"]:
            if network == "mainnet-beta":
                return f"https://explorer.solana.com/tx/{tx_hash}"
            else:  # devnet
                return f"https://explorer.solana.com/tx/{tx_hash}?cluster=devnet"

        # === EVM NETWORKS ===
        explorer_urls = {
            "mainnet": f"https://etherscan.io/tx/{tx_hash}",
            "arbitrum": f"https://arbiscan.io/tx/{tx_hash}",
            "optimism": f"https://optimistic.etherscan.io/tx/{tx_hash}",
            "base": f"https://basescan.org/tx/{tx_hash}",
            "polygon": f"https://polygonscan.com/tx/{tx_hash}",
            "bsc": f"https://bscscan.com/tx/{tx_hash}",
            "avalanche": f"https://snowtrace.io/tx/{tx_hash}",
            "celo": f"https://celoscan.io/tx/{tx_hash}",
            "blast": f"https://blastscan.io/tx/{tx_hash}",
            "zora": f"https://explorer.zora.energy/tx/{tx_hash}",
            "worldchain": f"https://worldchain-mainnet.explorer.alchemy.com/tx/{tx_hash}",
            "sepolia": f"https://sepolia.etherscan.io/tx/{tx_hash}",
        }

        return explorer_urls.get(network, f"https://etherscan.io/tx/{tx_hash}")



    async def _initialize_dynamic_token_discovery(self) -> None:
        """
        Initialize dynamic token discovery for all supported networks
        Gateway 2.9: Token endpoints removed, using pool-based discovery
        """
        try:
            self.logger().info("🔄 Initializing token discovery from pool configurations...")

            # Extract tokens from pool configurations instead of API
            for network in self.supported_networks:
                tokens = set()  # Use set to avoid duplicates

                # Extract tokens from pool configurations
                if network in self.pool_configurations:
                    for connector, connector_config in self.pool_configurations[network].items():
                        for pool_type in ["amm", "clmm"]:
                            if pool_type in connector_config:
                                for pool_key in connector_config[pool_type].keys():
                                    # Parse pool key like "WETH-USDC" to get tokens
                                    if "-" in pool_key:
                                        base, quote = pool_key.split("-", 1)
                                        tokens.add(base)
                                        tokens.add(quote)

                self.supported_tokens[network] = list(tokens)

                if tokens:
                    self.logger().info(f"✅ Network {network}: {len(tokens)} tokens found in pools")
                else:
                    self.logger().debug(f"📋 Network {network}: No tokens in pool configurations")

            total_tokens = sum(len(tokens) for tokens in self.supported_tokens.values())
            self.logger().info(
                f"🎯 Token discovery complete: {total_tokens} unique tokens from pool configurations")

        except Exception as init_error:
            self.logger().error(f"❌ Token discovery initialization error: {init_error}")
            # Initialize empty lists as fallback
            for network in self.supported_networks:
                self.supported_tokens[network] = []

    def _extract_transaction_hash(
            self,
            response: Optional[Dict] = None,
            network: str = "arbitrum"
    ) -> Optional[str]:
        """
        Universal transaction hash extraction for Gateway 2.9.0

        SUPPORTS ALL NETWORKS:
        ✅ Solana: mainnet-beta, devnet
        ✅ Ethereum: mainnet, arbitrum, optimism, base, sepolia, bsc, avalanche, celo, polygon, blast, zora, worldchain

        Args:
            response: Gateway API response dictionary
            network: Network name (determines hash format validation)

        Returns:
            Transaction hash/signature string or None if not found
        """
        if not response or not isinstance(response, dict):
            return None

        try:
            # ===== Gateway 2.9.0 Response Format =====
            # Gateway uses 'signature' field for BOTH EVM and Solana (discovered through testing)
            signature = response.get("signature")
            if signature and isinstance(signature, str) and len(signature) > 10:

                # === SOLANA NETWORKS ===
                if network in ["mainnet-beta", "devnet"]:
                    # Solana signatures: base58 strings, typically 88 characters
                    if len(signature) >= 20:  # Solana signatures are much longer
                        return signature

                # === ALL EVM NETWORKS ===
                else:
                    # EVM networks (Ethereum, Arbitrum, Base, Avalanche, Polygon, BSC, etc.)
                    # All use same transaction hash format: 0x + 64 hex characters
                    if signature.startswith("0x") and len(signature) == 66:
                        return signature

            # ===== FALLBACK: Check documented fields =====
            hash_fields = ["hash", "txHash", "transactionHash", "tx_hash", "transaction_hash"]

            for field in hash_fields:
                if field in response:
                    hash_value = response[field]
                    if isinstance(hash_value, str) and len(hash_value) > 10:

                        # Validate format based on network
                        if network in ["mainnet-beta", "devnet"]:
                            # Solana: any string longer than 20 characters
                            if len(hash_value) >= 20:
                                return hash_value
                        else:
                            # EVM: must be 0x + 64 hex characters
                            if hash_value.startswith("0x") and len(hash_value) == 66:
                                return hash_value

            # ===== NESTED STRUCTURE CHECK =====
            nested_locations = ["data", "result", "transaction", "txn", "response"]

            for location in nested_locations:
                if location in response and isinstance(response[location], dict):
                    nested_response = response[location]

                    # ✅ FIXED: Recursively check nested structure using self
                    nested_hash = self._extract_transaction_hash(nested_response, network)
                    if nested_hash:
                        return nested_hash

            return None

        except (TypeError, AttributeError, KeyError, ValueError):
            return None

    def _check_daily_limits(self) -> bool:
        """Check if daily trading limits are reached"""
        return (self.daily_trade_count < self.max_daily_trades and
                self.daily_volume < self.max_daily_volume)

    async def gateway_request(self, method: str, endpoint: str, data: Optional[Dict] = None,
                              params: Optional[Dict] = None) -> Dict:
        """
        Gateway 2.9.0 compatible request method with proper query parameter handling
        Handle 500 errors with signatures (Solana timeout cases)
        """
        try:
            protocol = "https" if self.gateway_https else "http"

            # Ensure endpoint starts with /
            if not endpoint.startswith("/"):
                endpoint = f"/{endpoint}"

            base_url = f"{protocol}://{self.gateway_host}:{self.gateway_port}{endpoint}"

            # Handle query parameters for GET requests
            if method.upper() == "GET" and params:

                query_string = urlencode(params)
                url = f"{base_url}?{query_string}"
            else:
                url = base_url

            self.logger().debug(f"🔗 Gateway request: {method} {url}")

            # Prepare SSL context if using HTTPS
            ssl_context = None
            if self.gateway_https:
                ssl_context = ssl.create_default_context()
                ssl_context.load_cert_chain(self.gateway_cert_path, self.gateway_key_path)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

            # Set timeout
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)

            # Make the request
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }

                if method.upper() == "GET":
                    async with session.get(url, headers=headers, ssl=ssl_context) as response:
                        response_data = await response.json()

                        # Special handling for 500 errors with signatures
                        if response.status == 500 and "signature" in response_data:
                            self.logger().warning(f"⚠️ Gateway 500 with signature - likely confirmation timeout")
                        elif response.status >= 400:
                            self.logger().error(f"❌ Gateway error {response.status}: {response_data}")

                        return response_data
                else:
                    async with session.request(
                            method,
                            url,
                            headers=headers,
                            json=data,
                            ssl=ssl_context
                    ) as response:
                        response_data = await response.json()

                        # Special handling for 500 errors with signatures (Solana timeout case)
                        if response.status == 500 and "signature" in response_data:
                            self.logger().warning(f"⚠️ Gateway 500 with signature - likely confirmation timeout")
                        elif response.status >= 400:
                            self.logger().error(f"❌ Gateway error {response.status}: {response_data}")

                        return response_data

        except Exception as e:
            self.logger().error(f"❌ Gateway request error: {e}")
            return {"error": str(e)}

    def _is_successful_response(self, response: Optional[Dict]) -> bool:
        """
        FIXED: Enhanced response validation for Gateway 2.9.0 API responses
        Properly recognizes successful trade responses with 'signature' field

        Gateway 2.9 uses 'signature' field for BOTH EVM and Solana transaction hashes
        in successful trade responses.
        """
        try:
            if not response or not isinstance(response, dict):
                return False

            # CRITICAL: Check for explicit error field first
            if "error" in response:
                error_msg = response.get("error", "Unknown error")
                self.logger().debug(f"🔍 Response contains error: {error_msg}")
                return False

            # Check for HTTP error status codes
            status_code = response.get("statusCode", 0)
            if status_code >= 400:
                self.logger().debug(f"🔍 Response has error status code: {status_code}")
                return False

            # Check for error messages in message field
            message = response.get("message", "")
            if message and (
                    "error" in message.lower() or
                    "failed" in message.lower() or
                    "not found" in message.lower()):
                self.logger().debug(f"🔍 Response message indicates error: {message}")
                return False

            # Recognize successful TRADE responses (Gateway 2.9.0 format)
            # Gateway 2.9.0 uses 'signature' field for both EVM and Solana successful trades
            if "signature" in response:
                signature = response.get("signature")
                if signature and isinstance(signature, str) and len(signature) > 10:
                    self.logger().debug(f"✅ Trade response contains valid signature: {signature[:10]}...")
                    return True

            # Check for other success indicators in trade responses
            trade_success_fields = [
                "txHash", "hash", "transactionHash", "tx_hash",  # EVM transaction hashes
                "totalInputSwapped", "totalOutputSwapped",  # Trade amounts
                "baseTokenBalanceChange", "quoteTokenBalanceChange"  # Balance changes
            ]

            success_count = 0
            for field in trade_success_fields:
                if field in response and response[field] is not None:
                    success_count += 1

            # If we have multiple success indicators, consider it successful
            if success_count >= 2:
                self.logger().debug(f"✅ Trade response has {success_count} success indicators")
                return True

            # For Gateway configuration responses
            if "networks" in response:
                networks = response["networks"]
                if isinstance(networks, dict) and len(networks) > 0:
                    self.logger().debug(f"✅ Config response contains {len(networks)} networks")
                    return True

            # For token/connector responses
            if "tokens" in response or "connectors" in response:
                self.logger().debug("✅ Token/connector response detected")
                return True

            #  For balance responses
            if "balances" in response:
                balances = response["balances"]
                if isinstance(balances, dict):
                    self.logger().debug(f"✅ Balance response contains {len(balances)} balances")
                    return True

            # For empty but valid responses (like status checks)
            if len(response) > 0 and not any(key in response for key in ["error", "message"]):
                self.logger().debug(f"✅ Non-empty response without errors: {list(response.keys())}")
                return True

            # If we get here, log what we received for debugging
            self.logger().debug(f"🔍 Unclear response type - treating as unsuccessful: {list(response.keys())}")
            return False

        except Exception as validation_error:
            self.logger().error(f"❌ Error during response validation: {validation_error}")
            return False

    def on_start(self):
        """Start the strategy - unchanged"""
        self.logger().info("🚀 Starting MQTT Webhook Strategy with Gateway 2.9")

    async def on_stop(self):
        """Stop the strategy - Framework compliant method signature"""
        try:
            if self.mqtt_client and self.mqtt_connected:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
                self.logger().info("🔌 Disconnected from MQTT broker")

            self.logger().info("⏹️ Enhanced MQTT Webhook Strategy stopped")

        except Exception as stop_error:
            self.logger().error(f"❌ Error stopping strategy: {stop_error}")

    # =============================================
    # STATUS REPORTING METHODS FOR CLI
    # =============================================

    def format_status(self) -> str:
        """
        Enhanced status reporting for 'status --live' command
        Shows MQTT, Gateway, CEX, and trading information
        """
        # Strategy Header
        lines = [
            "📊 MQTT Webhook Strategy Status",
            "=" * 50
        ]

        # System Status Section
        lines.extend(self._format_system_status())

        # Trading Status Section
        lines.extend(self._format_trading_status())

        # Active Positions Section
        lines.extend(self._format_active_positions())

        # Configuration Section
        lines.extend(self._format_configuration_status())

        # Performance Section
        lines.extend(self._format_performance_metrics())

        # Warnings Section
        warning_lines = self._format_warnings()
        if warning_lines:
            lines.extend(["", "⚠️  WARNINGS ⚠️"] + warning_lines)

        return "\n".join(lines)

    def _format_system_status(self) -> List[str]:
        """Format system connectivity status"""
        lines = ["", "🔌 System Status:"]

        # Gateway Status
        gateway_status = "✅ Connected" if hasattr(self, '_initialized') and self._initialized else "🔄 Connecting"
        lines.append(f"  Gateway ({self.gateway_host}:{self.gateway_port}): {gateway_status}")

        # MQTT Status
        mqtt_status = "✅ Connected" if self.mqtt_connected else "❌ Disconnected"
        lines.append(f"  MQTT ({self.mqtt_host}:{self.mqtt_port}): {mqtt_status}")

        # CEX Status
        if self.cex_enabled:
            cex_status = self._get_cex_connection_status()
            cex_ready = "✅ Ready" if self.cex_ready else "🔄 Initializing"
            lines.append(f"  CEX ({self.cex_exchange_name}): {cex_status} - {cex_ready}")
        else:
            lines.append(f"  CEX: ❌ Disabled")

        return lines

    def _format_trading_status(self) -> List[str]:
        """Format current trading status and limits"""
        lines = ["", "📈 Trading Status:"]

        # Daily Limits
        trade_pct = (self.daily_trade_count / self.max_daily_trades) * 100 if self.max_daily_trades > 0 else 0
        volume_pct = (float(self.daily_volume) / float(self.max_daily_volume)) * 100 if self.max_daily_volume > 0 else 0

        lines.append(f"  Daily Trades: {self.daily_trade_count}/{self.max_daily_trades} ({trade_pct:.1f}%)")
        lines.append(f"  Daily Volume: ${self.daily_volume:,.2f}/${float(self.max_daily_volume):,.2f} ({volume_pct:.1f}%)")

        # CEX Daily Volume
        if self.cex_enabled:
            cex_pct = (self.cex_daily_volume / self.cex_daily_limit) * 100 if self.cex_daily_limit > 0 else 0
            lines.append(f"  CEX Daily Volume: ${self.cex_daily_volume:,.2f}/${self.cex_daily_limit:,.2f} ({cex_pct:.1f}%)")

        # Trade Settings
        lines.append(f"  Default Trade Amount: ${float(self.trade_amount):,.2f}")
        lines.append(f"  Slippage Tolerance: {self.slippage_tolerance:.1f}%")

        return lines

    def _format_active_positions(self) -> List[str]:
        """Format active positions information"""
        lines = ["", "💼 Active Positions:"]

        if not hasattr(self, 'active_positions') or not self.active_positions:
            lines.append("  No active positions")
            return lines

        # Position headers
        lines.append("  Token    Quote    Network     Exchange    Pool Type   USD Value")
        lines.append("  " + "-" * 65)

        total_usd_value = 0
        for token, position in self.active_positions.items():
            quote_token = position.get('quote_token', 'USDC')
            network = position.get('network', 'N/A')
            exchange = position.get('exchange', 'N/A')
            pool_type = position.get('pool_type', 'N/A')
            usd_value = position.get('usd_value', 0)

            # Truncate long values for display
            token_display = token[:8].ljust(8)
            quote_display = quote_token[:8].ljust(8)
            network_display = network[:10].ljust(10)
            exchange_display = exchange[:10].ljust(10)
            pool_display = pool_type[:9].ljust(9)

            lines.append(f"  {token_display} {quote_display} {network_display} {exchange_display} {pool_display} ${usd_value:>8.2f}")
            total_usd_value += float(usd_value)

        lines.append("  " + "-" * 65)
        lines.append(f"  Total USD Value: ${total_usd_value:,.2f}")

        return lines

    def _format_configuration_status(self) -> List[str]:
        """Format configuration status"""
        lines = ["", "⚙️  Configuration:"]

        # Network configurations
        if hasattr(self, 'supported_networks') and self.supported_networks:
            all_networks = list(self.supported_networks.keys())
            lines.append(f"  Supported Networks: {', '.join(all_networks)}")

        # DEX configurations
        supported_dexs = ["uniswap", "raydium", "meteora", "pancakeswap", "jupiter", "0x"]
        lines.append(f"  Supported DEXs: {', '.join(supported_dexs)}")

        # CEX configurations
        if self.cex_enabled:
            lines.append(f"  Supported CEXs: {self.cex_exchange_name}")
            cex_pairs = self.markets.get(self.cex_exchange_name, [])
            if cex_pairs:
                lines.append(f"  CEX Trading Pairs: {len(cex_pairs)} configured")
        else:
            lines.append("  Supported CEXs: None (CEX trading disabled)")

        # Pool configurations
        if hasattr(self, 'pool_configurations') and self.pool_configurations:
            total_pools = 0
            for network, config in self.pool_configurations.items():
                for connector, types in config.items():
                    for pool_type, pools in types.items():
                        total_pools += len(pools)
            lines.append(f"  Total Configured Pools: {total_pools}")

        # SOL Balance Protection
        if hasattr(self, 'min_sol_balance'):
            lines.append(f"  SOL Minimum Balance: {self.min_sol_balance} SOL")

        return lines

    def _format_performance_metrics(self) -> List[str]:
        """Format performance metrics"""
        lines = ["", "📊 Performance Metrics:"]

        # Calculate success rate
        total_trades = getattr(self, 'successful_trades', 0) + getattr(self, 'failed_trades', 0)
        if total_trades > 0:
            success_rate = (getattr(self, 'successful_trades', 0) / total_trades) * 100
            lines.append(f"  Success Rate: {success_rate:.1f}% ({getattr(self, 'successful_trades', 0)}/{total_trades})")
        else:
            lines.append("  Success Rate: N/A (No trades yet)")

        # Average execution time
        if hasattr(self, 'avg_execution_time') and getattr(self, 'avg_execution_time', 0) > 0:
            lines.append(f"  Avg Execution Time: {getattr(self, 'avg_execution_time', 0):.2f}s")

        # Last signal time
        if hasattr(self, 'last_signal_time') and self.last_signal_time:

            time_diff = datetime.datetime.now(datetime.timezone.utc) - self.last_signal_time
            lines.append(f"  Last Signal: {time_diff.seconds//60}m {time_diff.seconds%60}s ago")

        return lines

    def _format_warnings(self) -> List[str]:
        """Format system warnings"""
        warnings = []

        # Check daily limits
        if self.daily_trade_count >= self.max_daily_trades * 0.9:
            warnings.append(f"  ⚠️  Approaching daily trade limit ({self.daily_trade_count}/{self.max_daily_trades})")

        if float(self.daily_volume) >= float(self.max_daily_volume) * 0.9:
            warnings.append(f"  ⚠️  Approaching daily volume limit (${self.daily_volume:,.2f}/${float(self.max_daily_volume):,.2f})")

        # Check system connectivity
        if not self.mqtt_connected:
            warnings.append("  🔴 MQTT not connected - signals will not be received")

        if self.cex_enabled and not self.cex_ready:
            warnings.append("  🔴 CEX connector not ready")

        # Check for stale positions (if implemented)
        if hasattr(self, 'active_positions') and self.active_positions:
            stale_count = len([p for p in self.active_positions.values()
                             if hasattr(p, 'timestamp') and
                             (datetime.now(timezone.utc) - p.get('timestamp', datetime.now(timezone.utc))).days > 1])
            if stale_count > 0:
                warnings.append(f"  ⚠️  {stale_count} positions older than 24h")

        return warnings

    def active_orders_df(self):
        """
        Return DataFrame of active positions
        This shows current positions as 'active orders' for the status display
        """

        if not hasattr(self, 'active_positions') or not self.active_positions:
            raise ValueError("No active positions")

        # Convert positions to order-like format for display
        orders_data = []

        for token, position in self.active_positions.items():
            quote_token = position.get('quote_token', 'USDC')
            network = position.get('network', 'Unknown')
            pool_type = position.get('pool_type', 'N/A')
            usd_value = position.get('usd_value', 0)

            # Format as trading pair
            market = f"{token}-{quote_token}"

            # Use network as "exchange" for display
            exchange = f"{network}/{position.get('exchange', 'DEX')}"

            orders_data.append({
                'Exchange': exchange[:20],  # Truncate for display
                'Market': market,
                'Side': 'HOLD',  # Positions are held, not buy/sell orders
                'Pool Type': pool_type,
                'USD Value': f"${usd_value:.2f}",
                'Age': self._calculate_position_age(position)
            })

        return pd.DataFrame(orders_data)

    def _calculate_position_age(self, position) -> str:
        """Calculate how long a position has been held"""
        if 'timestamp' not in position:
            return "Unknown"

        try:

            pos_time = position.get('timestamp')
            if isinstance(pos_time, str):
                # Parse timestamp string if needed
                pos_time = datetime.fromisoformat(pos_time.replace('Z', '+00:00'))

            age_delta = datetime.now(timezone.utc) - pos_time

            if age_delta.days > 0:
                return f"{age_delta.days}d"
            elif age_delta.seconds > 3600:
                return f"{age_delta.seconds // 3600}h"
            else:
                return f"{age_delta.seconds // 60}m"

        except Exception:
            return "Unknown"

# For compatibility with direct script execution
if __name__ == "__main__":
    pass
