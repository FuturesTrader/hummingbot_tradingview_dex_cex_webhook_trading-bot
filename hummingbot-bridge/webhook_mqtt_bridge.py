"""
Enhanced TradingView Webhook → MQTT Bridge Server
Updated to Remove Pool Address from MQTT Messages

CRITICAL UPDATE: Removed pool_address field from MQTT messages as Gateway
handles pool selection internally. Clean directional signals only.

Author: Todd Griggs
Date: Sept 28, 2025
Status: PRODUCTION READY - Clean Directional Signals
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, Any

from flask import Flask, request, jsonify
import paho.mqtt.client as mqtt


class EnhancedWebhookMQTTBridge:
    """Production-ready webhook to MQTT bridge with clean directional signals"""

    def __init__(self):
        """Initialize with secure environment-based configuration"""
        # Initialize all attributes first (fixes IDE warnings)
        # Webhook Server Configuration (will be loaded from environment)
        self.webhook_port: int = 0
        self.webhook_debug: bool = False
        self.log_level: str = ""

        # MQTT Configuration (will be loaded from environment)
        self.mqtt_host: str = ""
        self.mqtt_port: int = 0
        self.mqtt_namespace: str = ""

        # Security Configuration (will be loaded from environment)
        self.api_key: Optional[str] = None

        # Component instances (runtime state - correctly initialized here)
        self.mqtt_client: Optional[mqtt.Client] = None
        self.mqtt_connected: bool = False
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.app: Flask = Flask(__name__)

        # Load configuration and setup components
        self.load_configuration()
        self.setup_logging()
        self.setup_flask()
        self.setup_mqtt()
        self.validate_configuration()

    def load_configuration(self) -> None:
        """Load all configuration from environment variables"""
        # Webhook Server Configuration
        self.webhook_port = int(os.getenv("HBOT_WEBHOOK_PORT", "3002"))
        self.webhook_debug = os.getenv("HBOT_WEBHOOK_DEBUG", "false").lower() == "true"
        self.log_level = os.getenv("HBOT_WEBHOOK_LOG_LEVEL", "INFO").upper()

        # MQTT Configuration
        self.mqtt_host = os.getenv("HBOT_WEBHOOK_MQTT_HOST", "localhost")
        self.mqtt_port = int(os.getenv("HBOT_WEBHOOK_MQTT_PORT", "1883"))
        self.mqtt_namespace = os.getenv("HBOT_WEBHOOK_MQTT_NAMESPACE", "hbot")

        # Security Configuration
        self.api_key = os.getenv("HBOT_WEBHOOK_API_KEY")

    def setup_logging(self) -> None:
        """Setup secure logging with data masking"""
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def setup_flask(self) -> None:
        """Setup Flask application with routes"""
        # Main webhook endpoint
        self.app.route('/webhook/hummingbot', methods=['POST'])(self.handle_webhook)

        # Utility endpoints
        self.app.route('/health', methods=['GET'])(self.health_check)
        self.app.route('/config', methods=['GET'])(self.get_config)
        self.app.route('/test', methods=['POST'])(self.test_signal)

    def setup_mqtt(self) -> None:
        """Setup MQTT client with callbacks"""
        try:
            # Universal MQTT client pattern (lessons learned)
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_publish = self._on_mqtt_publish

            # Connect to MQTT broker
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()

        except Exception as mqtt_error:
            self.logger.error(f"❌ MQTT setup error: {mqtt_error}")

    def validate_configuration(self) -> None:
        """Validate all required configuration is present"""
        errors = []

        if not self.api_key:
            errors.append("HBOT_WEBHOOK_API_KEY environment variable is required")

        if errors:
            for error in errors:
                self.logger.error(f"❌ Configuration error: {error}")
            raise ValueError(f"Configuration errors: {errors}")

        self.logger.info("🚀 Starting Enhanced TradingView → MQTT Bridge Server")
        self.logger.info("📡 Minimal Directional Signals - Gateway-First Architecture")
        self.logger.info(f"🔐 API Key: {'✅ Configured' if self.api_key else '❌ Missing'}")
        self.logger.info(f"📍 Webhook endpoint: http://0.0.0.0:{self.webhook_port}/webhook/hummingbot")
        self.logger.info(f"⚡ Health check: http://0.0.0.0:{self.webhook_port}/health")
        self.logger.info(f"⚙️ Config endpoint: http://0.0.0.0:{self.webhook_port}/config")
        self.logger.info(f"🧪 Test endpoint: http://0.0.0.0:{self.webhook_port}/test")

    @staticmethod
    def _mask_sensitive_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """Mask sensitive data for secure logging"""
        masked_data = data.copy()

        # Mask API key
        if 'api_key' in masked_data:
            api_key = masked_data['api_key']
            if len(api_key) > 8:
                masked_data['api_key'] = f"{api_key[:4]}...{api_key[-4:]}"

        return masked_data

    def _on_mqtt_connect(self, _client, _userdata, _flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            self.mqtt_connected = True
            self.logger.info("✅ Successfully connected to MQTT broker")
        else:
            self.mqtt_connected = False
            self.logger.error(f"❌ Failed to connect to MQTT broker with code {rc}")

    def _on_mqtt_disconnect(self, _client, _userdata, _rc):
        """MQTT disconnection callback"""
        self.mqtt_connected = False
        self.logger.warning("⚠️ Disconnected from MQTT broker")

    def _on_mqtt_publish(self, _client, _userdata, mid):
        """MQTT publish callback"""
        self.logger.debug(f"📤 MQTT message published with mid: {mid}")

    def handle_webhook(self):
        """
        Handle incoming TradingView webhooks (Minimal Directional Signals)

        Expected minimal TradingView signal format:
        {
            "api_key": "your-secret-api-key",
            "action": "BUY|SELL",
            "symbol": "ETHUSDC",
            "exchange": "uniswap|raydium",
            "network": "arbitrum|solana"
        }

        NOTE: Gateway-first architecture - all configuration handled elsewhere:
        - Pool selection: Gateway manages internally
        - Slippage: Strategy environment configuration
        - Amount: Strategy environment configuration
        """
        try:
            # Verify content type
            if not request.is_json:
                self.logger.error("❌ Request is not JSON")
                return jsonify({"error": "Content-Type must be application/json"}), 400

            data = request.get_json()
            if not data:
                self.logger.error("❌ Empty request body")
                return jsonify({"error": "Request body is empty"}), 400

            # Log masked request data for security
            masked_data = self._mask_sensitive_data(data)
            self.logger.info(f"📨 Received webhook request: {masked_data}")

            # Validate API key
            if data.get('api_key') != self.api_key:
                self.logger.error("❌ Invalid or missing API key")
                return jsonify({"error": "Unauthorized"}), 401

            # Validate request format
            validation_error = self._validate_webhook_data(data)
            if validation_error:
                return validation_error

            # Create clean directional signal (no pool_address)
            signal = self._create_clean_trading_signal(data)

            # Publish to MQTT
            publish_error = self._publish_signal(signal)
            if publish_error:
                return publish_error

            return jsonify({
                "status": "success",
                "message": "Minimal directional signal published successfully",
                "signal_type": "directional_only",
                "configuration": "gateway_and_strategy_managed"
            }), 200

        except Exception as e:
            self.logger.error(f"❌ Webhook processing error: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def _validate_webhook_data(self, data: Dict[str, Any]) -> Optional[tuple]:
        """Validate webhook request data format"""
        # Required fields for directional signals
        required_fields = ['action', 'symbol', 'exchange', 'network']
        missing_fields = [field for field in required_fields if field not in data]

        if missing_fields:
            error_msg = f"Missing required fields: {missing_fields}"
            self.logger.error(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 400

        # Validate action
        if data['action'].upper() not in ['BUY', 'SELL']:
            error_msg = "Action must be 'BUY' or 'SELL'"
            self.logger.error(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 400

        # Validate exchange and network combinations
        valid_combinations = {
            'uniswap': [
                'arbitrum',  # Proven working
                'ethereum',  # Mainnet
                'base',  # Base network
                'polygon',  # Polygon network
                'mainnet',  # Ethereum mainnet
                'optimism',  # Optimism L2
                'bsc'  # Binance Smart Chain
            ],
            '0x': [
                'arbitrum',  # Proven working
                'ethereum',  # Mainnet
                'base',  # Base network
                'polygon',  # Polygon network
                'mainnet',  # Ethereum mainnet
                'optimism',  # Optimism L2
                'bsc'  # Binance Smart Chain
            ],
            'raydium': [
                'mainnet-beta',  # ✅ FIX: Solana mainnet (correct name)
                'devnet',  # Solana devnet
                'solana'  # Legacy alias for compatibility
            ],
            'jupiter': [
                'mainnet-beta',  # ✅ Solana mainnet for Jupiter aggregator
                'devnet',  # Solana devnet
                'solana'  # Legacy alias for compatibility
            ],
            'meteora': [
                'mainnet-beta',  # ✅ Solana mainnet for Meteora DEX
                'devnet',  # Solana devnet
                'solana'  # Legacy alias for compatibility
            ],
            # ============================================
            # CEX EXCHANGES (NEW) - Add these
            # ============================================
            'coinbase': [
                'mainnet',  # CEX doesn't really need network but accept it
                'mainnet-beta',
                'ethereum',  # Alternative network name
                'any'  # CEX works with any network value
            ],
            'kraken': [
                'mainnet',
                'any'
            ],
            'okx': [
                'mainnet',
                'any'
            ],
            'hyperliquid': [
                'arbitrum',  # Hyperliquid runs on Arbitrum
                'mainnet',
                'any'
            ],
            'hyperliquid_perpetual': [  # Alternative name
                'arbitrum',
                'mainnet',
                'any'
            ]
        }

        exchange = data['exchange'].lower()
        network = data['network'].lower()

        if exchange not in valid_combinations:
            error_msg = f"Unsupported exchange: {exchange}"
            self.logger.error(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 400

        # For CEX exchanges, accept any network value
        if exchange in ['coinbase', 'binance', 'kraken', 'okx', 'bybit', 'hyperliquid', 'hyperliquid_perpetual']:
            # CEX doesn't care about network, so always pass
            return None

        if network not in valid_combinations[exchange]:
            error_msg = f"Invalid network '{network}' for exchange '{exchange}'"
            self.logger.error(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 400

        return None

    @staticmethod
    def _create_clean_trading_signal(data: Dict[str, Any]) -> Dict[str, Any]:
        """Create minimal directional trading signal (Gateway-first architecture)"""
        signal = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": data['action'].upper(),
            "symbol": data['symbol'].upper(),
            "exchange": data['exchange'].lower(),
            "network": data['network'].lower(),
            "strategy": data.get('strategy', 'webhook_strategy'),
            "source": "tradingview_webhook"
        }

        # OPTIMIZED: Include pool_type if provided by TradingView
        if 'pool_type' in data and data['pool_type'] in ['amm', 'clmm','n/a','router']:
            signal['pool_type'] = data['pool_type'].lower()

        # - NO pool_address: Gateway handles pool selection internally
        # - NO slippage: Strategy manages slippage via environment configuration
        # - NO amount: Strategy controls trade amounts via environment variables

        return signal

    def _get_mqtt_topic(self, network: str, exchange: str) -> str:
        """Generate MQTT topic for the signal"""
        return f"{self.mqtt_namespace}/signals/{network}/{exchange}"

    def _publish_signal(self, signal: Dict[str, Any]) -> Optional[tuple]:
        """Publish trading signal to MQTT"""
        if not self.mqtt_connected:
            error_msg = "MQTT broker not connected"
            self.logger.error(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 503

        try:
            topic = self._get_mqtt_topic(signal["network"], signal["exchange"])
            message = json.dumps(signal)

            result = self.mqtt_client.publish(topic, message, qos=1)

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                error_msg = f"Failed to publish MQTT message: {result.rc}"
                self.logger.error(f"❌ {error_msg}")
                return jsonify({"error": "Failed to publish signal"}), 500

            self.logger.info(f"📤 Published minimal signal to '{topic}': {signal['action']} {signal['symbol']}")
            return None

        except Exception as e:
            self.logger.error(f"❌ Error publishing signal: {e}")
            return jsonify({"error": "Failed to publish signal"}), 500

    def health_check(self):
        """Health check endpoint"""
        health_status = {
            "status": "healthy" if self.mqtt_connected else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mqtt_connected": self.mqtt_connected,
            "version": "2.8.0-minimal-signals",
            "signal_type": "directional_only",
            "architecture": "gateway_first"
        }

        status_code = 200 if self.mqtt_connected else 503
        return jsonify(health_status), status_code

    def get_config(self):
        """Configuration endpoint (with sensitive data masked)"""
        config = {
            "webhook_port": self.webhook_port,
            "mqtt_host": self.mqtt_host,
            "mqtt_port": self.mqtt_port,
            "mqtt_namespace": self.mqtt_namespace,
            "api_key_configured": bool(self.api_key),
            "mqtt_connected": self.mqtt_connected,
            "log_level": self.log_level,
            "signal_processing": "directional_only",
            "pool_routing": "gateway_managed",
            "supported_exchanges": [
                "uniswap (arbitrum, ethereum)",
                "raydium (solana)"
            ],
            "required_webhook_fields": [
                "api_key", "action", "symbol", "exchange", "network"
            ],
            "optional_webhook_fields": [
                "strategy"
            ],
            "gateway_managed_fields": [
                "pool_address (Gateway handles pool selection)",
                "slippage (Strategy environment configuration)",
                "amount (Strategy environment configuration)"
            ]
        }
        return jsonify(config), 200

    def test_signal(self):
        """Test endpoint to send a clean directional signal"""
        try:
            data = request.get_json() or {}

            test_signal = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": data.get('action', 'BUY'),
                "symbol": data.get('symbol', 'ETHUSDC'),
                "exchange": data.get('exchange', 'uniswap'),
                "network": data.get('network', 'arbitrum'),
                "strategy": "test_strategy",
                "source": "manual_test"
            }
            # NOTE: Minimal signal - Gateway and strategy handle all configuration

            topic = f"{self.mqtt_namespace}/signals/test"
            message = json.dumps(test_signal)

            if not self.mqtt_connected:
                return jsonify({"error": "MQTT broker not connected"}), 503

            result = self.mqtt_client.publish(topic, message, qos=1)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.logger.info(f"🧪 Published minimal test signal: {test_signal['action']} {test_signal['symbol']}")
                return jsonify({
                    "status": "success",
                    "message": "Minimal directional signal published",
                    "signal": test_signal,
                    "topic": topic,
                    "note": "Gateway-first: No pool/slippage in MQTT"
                }), 200
            else:
                return jsonify({"error": "Failed to publish test signal"}), 500

        except Exception as e:
            self.logger.error(f"❌ Error in test endpoint: {e}")
            return jsonify({"error": "Internal server error"}), 500

    def run(self):
        """Start the webhook server"""
        try:
            self.app.run(
                host='0.0.0.0',
                port=self.webhook_port,
                debug=self.webhook_debug,
                threaded=True,
                use_reloader=False  # Prevent double startup in debug mode
            )
        except KeyboardInterrupt:
            self.logger.info("🛑 Server stopped by user")
        except Exception as e:
            self.logger.error(f"❌ Server error: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources"""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.logger.info("🧹 MQTT client disconnected")


def main():
    """Main entry point"""
    try:
        # Check for required environment variable
        if not os.getenv("HBOT_WEBHOOK_API_KEY"):
            print("❌ CRITICAL: HBOT_WEBHOOK_API_KEY environment variable not set!")
            print("💡 Load environment: source .env.hummingbot")
            sys.exit(1)

        # Create and run bridge
        bridge = EnhancedWebhookMQTTBridge()
        bridge.run()

    except Exception as e:
        print(f"❌ Failed to start webhook bridge: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()