TradingView Webhook Multi-Venue Trading Strategy
Complete Documentation

Table of Contents

Overview
Goal
Hypothesis
System Description
Architecture
Strategy Design
Logic Flow
Events and Handlers
Installation Guide
Configuration
Performance Metrics
Monitoring and Operations
Troubleshooting
Advanced Features
Support and Resources


1. Overview
This strategy enables seamless execution of TradingView alerts across multiple trading venues, intelligently routing orders between Coinbase (CEX) and various DEX protocols (Uniswap, Raydium, Meteora, Jupiter) to optimize execution and capture opportunities across different liquidity pools.
Key Features

Multi-Venue Execution: Trade across CEX and DEX from a single strategy
Intelligent Routing: Automatic selection of optimal execution venue
Predictive Selling: Reduces CEX latency with anticipatory balance tracking
High Reliability: 97%+ success rate with robust error handling
Real-time Processing: Sub-second latency for signal processing
Risk Management: Built-in daily limits and position tracking
Gateway Integration: Seamless DEX access via Gateway v2.9


2. Goal
The overall goal of this strategy is to create a unified, multi-venue automated trading system that executes trades across both centralized (CEX) and decentralized exchanges (DEX) based on signals received from TradingView alerts. The strategy aims to:

Maximize trading opportunities by accessing liquidity across multiple venues (Coinbase, Uniswap, Raydium, Meteora)
Minimize latency through intelligent routing and predictive order execution
Optimize execution costs by selecting the most appropriate venue based on order size and token type
Maintain high reliability with 97%+ success rate through robust error handling and retry mechanisms
Provide seamless integration between technical analysis (TradingView) and execution (Hummingbot)


3. Hypothesis
For successful implementation, the following assumptions are necessary:
Technical Infrastructure

Gateway v2.9.0 is properly configured with JSON pool files for DEX connectivity
MQTT broker is accessible and stable for signal relay (mosquitto or similar)
TradingView can send webhook alerts in the expected JSON format
Network connectivity is stable with low latency to both CEX APIs and blockchain RPCs

Market Assumptions

Liquidity exists in the configured pools/pairs for the tokens being traded
Slippage tolerance of 1% is sufficient for most market conditions
Gas prices on EVM networks remain within reasonable bounds
CEX order books maintain sufficient depth for market orders

Operational Assumptions

Wallet funding is maintained with sufficient balances for trading
API credentials for CEX are valid with appropriate permissions
Private keys for DEX wallets are secure and properly configured
Daily limits ($1000 default) are appropriate for the trading strategy


4. System Description
The strategy consists of three main components working in concert:
TradingView Alert System

Generates trading signals based on the T3 Trading Strategy V36Q
Supports multiple signal types: Slope, Crossover, Pattern-based triggers
Sends JSON-formatted webhooks with trade direction and symbol information

Webhook Bridge Server

Python Flask server that receives TradingView webhooks
Validates API keys for security
Transforms signals into MQTT messages
Publishes to topic-based MQTT channels for routing

Hummingbot Execution Engine

Subscribes to MQTT topics for real-time signal processing
Routes orders intelligently between CEX and DEX venues
Handles complex DEX interactions through Gateway v2.9
Implements predictive selling for reduced CEX latency
Tracks positions and manages risk through daily limits


5. Architecture
System Architecture Diagram
┌─────────────────────────────────────────────────────────────────────────┐
│                         SIGNAL GENERATION LAYER                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [TradingView]                                                          │
│       │                                                                 │
│       │ Strategy Alerts                                                 │
│       │                                                                 │
│       ▼                                                                 │
│  {JSON Webhook}                                                         │
│   - action: "BUY/SELL"                                                  │
│   - symbol: "ETHUSDC"                                                   │
│   - exchange: "uniswap"                                                 │
│   - network: "arbitrum"                                                 │
│   - pool_type: "clmm"                                                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ HTTPS POST
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          WEBHOOK BRIDGE LAYER                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [Flask Server :3002]                                                   │
│       │                                                                 │
│       ├──► API Key Validation                                           │
│       │                                                                 │
│       ├──► Signal Transformation                                        │
│       │                                                                 │
│       └──► MQTT Publishing                                              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ MQTT Publish
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           MESSAGE QUEUE LAYER                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [MQTT Broker :1883]                                                    │
│       │                                                                 │
│       └──► Topic: hbot/signals/{network}/{exchange}                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Subscribe
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         STRATEGY ENGINE LAYER                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [Hummingbot Core]                                                      │
│       │                                                                 │
│       ├──► Signal Validation                                            │
│       ├──► Daily Limit Check                                            │
│       ├──► Route Decision                                               │
│       │                                                                 │
│       └──────────┬──────────────────────┬─────────────────              │
│                  ▼                      ▼                               │
│           CEX Route              DEX Route                              │
│                  │                      │                               │
└─────────────────────────────────────────────────────────────────────────┘
                   │                      │
                   ▼                      ▼
┌──────────────────────────┐  ┌──────────────────────────────────────────┐
│     CEX EXECUTION        │  │         DEX EXECUTION                    │
├──────────────────────────┤  ├──────────────────────────────────────────┤
│                          │  │                                          │
│  [Coinbase API]          │  │  [Gateway v2.9]                          │
│     │                    │  │     │                                    │
│     ├─► Market Order     │  │     ├─► Pool Discovery                   │
│     ├─► Balance Check    │  │     ├─► Slippage Calc                    │
│     └─► Predictive Sell  │  │     └─► Swap Execution                   │
│                          │  │                                          │
│  Result: ~1-2 seconds    │  │  Result: ~2-5 seconds                    │
│                          │  │                                          │
└──────────────────────────┘  └──────────────────────────────────────────┘
Project Structure
tradingview-webhook-strategy/
│
├── hummingbot/
│   ├── scripts/mqtt_webhook_strategy_w_cex.py  # Main strategy file
│   └── conf/                            # Hummingbot configuration
│   ├── gateway/
│       └── conf/pools/                           # DEX pool configurations    
├── webhook-automation/hummingbot-bridge/
│   ├── webhook_bridge.py                # Flask webhook server
│   └── requirements.txt                 # Python dependencies
│
│
├── .env.example                         # Environment variables template
├── docker-compose.yml                   # Docker setup (optional)
└── README.md                           # Project readme

6. Strategy Design
6.1 Input Variables (Environment Configuration)
bash# Core Configuration
HBOT_TRADE_AMOUNT=1.0                    # Base trade size in USD
HBOT_SELL_PERCENTAGE=99.999              # Percentage of balance to sell
HBOT_SLIPPAGE_TOLERANCE=1.0              # DEX slippage tolerance
HBOT_MAX_DAILY_TRADES=50                 # Daily trade limit
HBOT_MAX_DAILY_VOLUME=1000.0            # Daily volume limit in USD

# MQTT Configuration
HBOT_MQTT_HOST=localhost                 # MQTT broker host
HBOT_MQTT_PORT=1883                      # MQTT broker port
HBOT_WEBHOOK_MQTT_NAMESPACE=hbot        # MQTT topic namespace
HBOT_WEBHOOK_API_KEY=your-secret-key    # Webhook authentication key

# CEX Configuration
HBOT_CEX_ENABLED=true                    # Enable CEX trading
HBOT_CEX_DEFAULT_EXCHANGE=coinbase_advanced_trade
HBOT_CEX_TRADING_PAIRS=ETH-USD,BTC-USD,SOL-USD,...
HBOT_CEX_PREDICTIVE_SELL=true           # Enable predictive selling
HBOT_CEX_PREDICTIVE_WINDOW=60           # Seconds to wait for balance
HBOT_CEX_DAILY_LIMIT=1000.0             # CEX daily volume limit

# DEX/Gateway Configuration  
HBOT_GATEWAY_HOST=localhost              # Gateway host
HBOT_GATEWAY_PORT=15888                  # Gateway port
HBOT_GATEWAY_HTTPS=true                  # Use HTTPS for Gateway
HBOT_GATEWAY_CERT_PATH=/path/to/cert    # Gateway certificate
HBOT_GATEWAY_CONF_PATH=/path/to/gateway/conf

# Wallet Configuration
HBOT_ARBITRUM_WALLET=0x...              # EVM wallet address
HBOT_SOLANA_WALLET=...                  # Solana wallet address
6.2 Status Command Output Format
╔══════════════════════════════════════════════════════════════════╗
║                    MQTT WEBHOOK STRATEGY STATUS                   ║
╚══════════════════════════════════════════════════════════════════╝

Connection Status:
  MQTT:     [OK] Connected (localhost:1883)
  Gateway:  [OK] Connected (v2.9.0)
  CEX:      [OK] Connected (Coinbase - 12 pairs active)

Trading Configuration:
  Mode:           Multi-Venue (CEX + DEX)
  Trade Amount:   $1.00 USD
  Sell %:         99.999%
  Slippage:       1.0%
  
Daily Statistics:
  Trades Today:   15/50
  Volume Today:   $450.00/$1000.00
  Success Rate:   96.7%
  
Predictive Selling Stats:
  Attempts:       12
  Successes:      11 (91.7%)
  Avg Wait Time:  8.3s

Active Networks:
  ├─ Arbitrum:    44 pools (Uniswap)
  ├─ Solana:      40 pools (Raydium)
  └─ Coinbase:    12 pairs

Signal Queue:
  Pending:        0 signals
  Last Signal:    BUY ETHUSDC (2 min ago)
  Last Trade:     [SUCCESS] (Arbitrum)

Active Positions:
  ETH:    0.00031 ($1.02) - Pool: 0xc473...
  SOL:    0.0052 ($0.98) - Raydium AMM

7. Logic Flow
Trading Flow Diagram
START
  │
  ▼
┌─────────────────────┐
│ TradingView Alert   │
│ Triggered           │
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ Send Webhook to     │
│ Bridge Server       │
└─────────────────────┘
  │
  ▼
┌─────────────────────┐     ┌─────────────────────┐
│ API Key Valid?      │────►│ Return 401 Error    │
│                     │ NO  │ Unauthorized        │
└─────────────────────┘     └─────────────────────┘
  │ YES                               │
  ▼                                   │
┌─────────────────────┐               │
│ Publish to MQTT     │               │
│ Topic               │               │
└─────────────────────┘               │
  │                                   │
  ▼                                   │
┌─────────────────────┐               │
│ Hummingbot Receives │               │
│ Signal              │               │
└─────────────────────┘               │
  │                                   │
  ▼                                   │
┌─────────────────────┐     ┌─────────┴──────────┐
│ Daily Limits OK?    │────►│ Skip Trade         │
│                     │ NO  │ Log Event          │
└─────────────────────┘     └────────────────────┘
  │ YES                               │
  ▼                                   │
┌─────────────────────┐               │
│ Route Decision:     │               │
│ CEX or DEX?         │               │
└─────────────────────┘               │
  │         │                         │
  ▼         ▼                         │
CEX       DEX                         │
  │         │                         │
  ▼         ▼                         │
┌─────┐   ┌─────┐                     │
│ BUY │   │ BUY │                     │
│ via │   │ via │                     │
│ API │   │ GW  │                     │
└─────┘   └─────┘                     │
  │         │                         │
  ▼         ▼                         │
┌─────────────────────┐               │
│ Update Position     │               │
│ Tracking            │               │
└─────────────────────┘               │
  │                                   │
  ▼                                   │
┌─────────────────────┐               │
│ Log Transaction     │               │
│ Update Stats        │               │
└─────────────────────┘               │
  │                                   │
  ▼                                   ▼
┌─────────────────────────────────────────────────┐
│              READY FOR NEXT SIGNAL              │
└─────────────────────────────────────────────────┘
Event Flow Sequence
1. Signal Generation
   └─► TradingView Strategy detects entry/exit
   
2. Alert Transmission
   └─► Webhook POST to bridge server
   
3. Authentication
   ├─► Valid API key → Continue
   └─► Invalid key → 401 Unauthorized
   
4. Message Routing
   └─► MQTT publish to topic namespace
   
5. Strategy Processing
   ├─► Signal validation
   ├─► Daily limit check
   └─► Route decision
   
6. Order Execution
   ├─► CEX Path
   │   ├─► Market order via API
   │   ├─► Balance monitoring
   │   └─► Predictive selling
   │
   └─► DEX Path
       ├─► Pool discovery
       ├─► Gas estimation
       └─► Swap transaction
       
7. Post-Trade Operations
   ├─► Position tracking update
   ├─► Statistics recording
   └─► Transaction logging

8. Events and Handlers
Critical Events and Handlers
┌────────────────────┬────────────────┬──────────────────────┬────────────────────────┐
│ Event              │ Source         │ Handler              │ Action                 │
├────────────────────┼────────────────┼──────────────────────┼────────────────────────┤
│ Signal Received    │ MQTT           │ _on_mqtt_message()   │ Validate, queue        │
│ Trade Execution    │ Strategy       │ _process_trading()   │ Route to CEX/DEX       │
│ Balance Update     │ CEX/DEX        │ _get_balance()       │ Update positions       │
│ Order Fill         │ Exchange       │ _handle_sell()       │ Clean up, log          │
│ Gas Price Spike    │ Gateway        │ _execute_evm_buy()   │ Retry with 1.1x        │
│ Daily Reset        │ Timer          │ _reset_counters()    │ Reset at UTC midnight  │
│ Network Error      │ Gateway/CEX    │ Exception handlers   │ Retry with backoff     │
│ Slippage Exceeded  │ DEX            │ Trade execution      │ Transaction reverts    │
└────────────────────┴────────────────┴──────────────────────┴────────────────────────┘
Event Processing Details
Signal Reception Flow

TradingView webhook → Flask server
API key validation
MQTT publish to hbot/signals/{network}/{exchange}
Hummingbot receives via subscription
Signal added to processing queue


On BUY: Store pool address, amount, timestamp
On SELL: Use stored pool for optimal routing
Cleanup: Remove position after successful sell

Error Recovery

Gas errors: Retry 3x with increasing gas multiplier
Balance not found: Wait up to 60s with periodic checks
Pool not found: Let Gateway auto-select
Slippage exceeded: Recalculate with fresh quote


9. Installation Guide
Prerequisites

Python 3.10+
Hummingbot 2.0.0+
Gateway v2.9.0 (for DEX trading)
MQTT Broker (Mosquitto recommended)
TradingView Pro/Premium account (for webhooks)
Exchange Accounts:

Coinbase Advanced Trade API credentials
Funded wallets for DEX trading (Arbitrum/Solana)


Step 1: Clone the Repository
bashgit clone https://github.com/yourusername/tradingview-webhook-strategy.git
cd tradingview-webhook-strategy
Step 2: Install Dependencies
bash# Install Hummingbot
git clone https://github.com/hummingbot/hummingbot.git
cd hummingbot
./install

# Install Gateway
git clone https://github.com/hummingbot/gateway.git
cd gateway
yarn install
yarn build

# Install Python dependencies for webhook bridge
pip install -r webhook-bridge/requirements.txt
Step 3: Configure Environment Variables
Create a .env file in the project root with the configuration from Section 6.1.
Step 4: Start the Services
bash# Terminal 1: Start MQTT Broker
mosquitto -c /path/to/mosquitto.conf

# Terminal 2: Start Gateway
cd gateway
yarn start

# Terminal 3: Start Webhook Bridge
python webhook_bridge.py

# Terminal 4: Start Hummingbot with the strategy
cd hummingbot
bin/hummingbot_quickstart.py
>>> import mqtt_webhook_strategy_w_cex
>>> start
Step 5: Configure TradingView

Open your TradingView chart with the T3 Strategy V36Q
Create an alert with webhook URL:

   http://your-server:3002/webhook/hummingbot

Set the alert message format:

json   {
     "action": "{{strategy.order.action}}",
     "symbol": "{{ticker}}",
     "exchange": "uniswap",
     "network": "arbitrum",
     "pool_type": "clmm"
   }

10. Configuration
Trading Parameters
┌─────────────────────┬─────────────────────────┬───────────┬──────────────┐
│ Parameter           │ Description             │ Default   │ Range        │
├─────────────────────┼─────────────────────────┼───────────┼──────────────┤
│ TRADE_AMOUNT        │ Base trade size (USD)   │ 1.0       │ 0.1-10000    │
│ SELL_PERCENTAGE     │ % of balance to sell    │ 99.999    │ 1-100        │
│ SLIPPAGE_TOLERANCE  │ Max DEX slippage        │ 1.0%      │ 0.1-5.0      │
│ MAX_DAILY_TRADES    │ Daily trade limit       │ 50        │ 1-1000       │
│ MAX_DAILY_VOLUME    │ Daily volume limit      │ 1000      │ 10-100000    │
└─────────────────────┴─────────────────────────┴───────────┴──────────────┘
Supported Venues
Centralized Exchanges (CEX)

Coinbase Advanced Trade (primary)
Support for 12+ trading pairs
ETH-USD, BTC-USD, SOL-USD, MATIC-USD, etc.

Decentralized Exchanges (DEX)

Ethereum V2 layer: Uniswap (Arbitrum, Base, optimism, mainnet, polygon: Uniswap V3 (44 pools)).  Testing 0x
Solana: Raydium, Meteora, Jupiter (40+ pools)

System Requirements
┌────────────────┬──────────────────────────────────────────────────────┐
│ Component      │ Minimum Requirements                                 │
├────────────────┼──────────────────────────────────────────────────────┤
│ CPU            │ 2 cores, 2.4 GHz+                                    │
│ RAM            │ 4 GB (8 GB recommended)                              │
│ Storage        │ 20 GB SSD                                            │
│ Network        │ 10 Mbps stable connection                            │
│ OS             │ Ubuntu 20.04+ / macOS 12+ / Windows WSL2             │
└────────────────┴──────────────────────────────────────────────────────┘

11. Performance Metrics
Current Performance
┌──────────────────────────┬──────────┬──────────┐
│ Metric                   │ Target   │ Current  │
├──────────────────────────┼──────────┼──────────┤
│ Success Rate             │ >95%     │ 97%      │
│ Execution Latency (CEX)  │ <2s      │ 1.2s     │
│ Execution Latency (DEX)  │ <5s      │ 3.1s     │
│ Predictive Sell Accuracy │ >90%     │ 92%      │
│ Daily Uptime             │ >99.9%   │ 99.95%   │
└──────────────────────────┴──────────┴──────────┘
Performance Tracking
The strategy tracks and reports:

Success rate: Target 95%+
Execution latency: < 2 seconds average
Predictive selling accuracy: 90%+ hit rate
Daily volume utilization: % of limits used
Network distribution: Trades per venue/network


12. Monitoring and Operations (Status)
MQTT Webhook Strategy Status                                                                                                                                                                                                          
  ==================================================                                                                                                                    
System Status:                                                                                                    
Gateway (localhost15888):                                                                                                                                                                                     
MQTT (localhost:1883): ✅ Connected                                                                               
CEX (coinbase_advanced_trade): ===                                               
Trading                                                 
Daily Trades: 13/500 (2.6%) 
Daily Volume: $60.00/$10,000.00 (0.6%)                                                                                                                                                                                                 
CEX Daily Volume: $59.74/$1,000.00 (6.0%)                                                                         
Default Trade Amount: $10.00                                                                                                                                                                                                           
Slippage Tolerance: 1.0%                                                                                           1                                                              
Active Positions:                                                                                                 
Token    Quote    Network     Exchange    Pool Type   USD Value                                                                                                                                                                        
    -----------------------------------------------------------------                                                  
    RAY      USDC     mainnet-be raydium    amm       $   10.00                                                                                                                                                                            
    -----------------------------------------------------------------                                                 
    Total USD Value: 
Metrics:                                                                                                                                                                                                                  
Success Rate: 100.0% (11/11)                          

DEBUG = True
LOG_LEVEL = "DEBUG"
Health Checks
Monitor system health with these commands:
bash# Check MQTT connection
mosquitto_sub -h localhost -t "hbot/signals/#" -v

# Check Gateway status
curl http://localhost:15888/

# Check Hummingbot status
>>> status

13. Troubleshooting
Common Issues and Solutions
┌────────────────────────┬─────────────────────────────────────────────────┐
│ Issue                  │ Solution                                        │
├────────────────────────┼─────────────────────────────────────────────────┤
│ Webhook 401 Error      │ Verify WEBHOOK_API_KEY matches TradingView     │
│ MQTT Connection Failed │ Check broker is running: mosquitto -v          │
│ Gateway Timeout        │ Ensure Gateway v2.9+ is running                │
│ Insufficient Balance   │ Fund wallets for gas + trading                 │
│ High Slippage          │ Increase SLIPPAGE_TOLERANCE or reduce amount   │
│ No Trades Executing    │ Check daily limits haven't been exceeded       │
│ CEX Order Rejected     │ Verify API permissions and balance             │
│ DEX Transaction Fails  │ Check gas price and wallet balance             │
└────────────────────────┴─────────────────────────────────────────────────┘
Error Messages Explained

"Signal validation failed": Malformed JSON from TradingView
"Daily limit exceeded": Reset occurs at UTC midnight
"Pool not found": Token pair not configured in Gateway
"Insufficient gas": Increase gas multiplier in config
"Balance check timeout": CEX API delay, increase timeout


14. Advanced Features
Predictive Selling (this is experimental to address slow balance edge case) 
Reduces CEX round-trip latency by initiating sell orders before balance confirmation:
python# Configuration
CEX_PREDICTIVE_SELL = True
CEX_PREDICTIVE_WINDOW = 60  # seconds

# Logic flow
1. Buy order placed on CEX
2. Start monitoring balance
3. If balance not confirmed within window:
   - Place sell order anyway
   - Continue monitoring
4. If balance never arrives:
   - Cancel sell order
   - Log failure
Intelligent Routing
Automatically selects execution venue based on:

Token availability: Is token in CEX list?
Order size thresholds: Orders > $50 prefer CEX
Historical success rates: Route to venue with better performance
Gas price conditions: Avoid DEX during high gas periods

Position Tracking
Maintains state across buy/sell cycles:
pythonpositions = {
    "ETH": {
        "pool": "0xc473...",
        "amount": 0.00031,
        "entry_price": 3250.00,
        "timestamp": 1703001234,
        "network": "arbitrum",
        "exchange": "uniswap"
    }
}
Risk Management Features

Daily Limits: Hard stops on trade count and volume
Slippage Protection: Configurable tolerance per network
Gas Price Limits: Maximum gas multiplier settings
Balance Verification: Double-check before executing sells
Position Size Limits: Maximum exposure per token

Contributing
We welcome contributions! Please see our Contributing Guide for details.


Updates & Roadmap
Recent Updates (v1.2.0)

 Added Solana DEX support (Raydium, Meteora, Jupiter)
 Implemented predictive selling for CEX
 Enhanced error recovery mechanisms
 Added comprehensive position tracking

Roadmap (2025)

 Q4: Trade by trade Profit/loss tracking and reporting (backtest validation)
 Q4: Pancake swap and 0x integration
 Q4: Web dashboard for monitoring

Roadmap (2026)
 Q1: Mobile app for alerts
 Q2: Advanced order types (limit, stop-loss)

License
This project is licensed under the Apache 2.0 License - see the LICENSE file for details.
Acknowledgments

Hummingbot - Open source algo trading platform
Gateway - DEX connectivity layer
TradingView - Charting and technical analysis
MQTT - Lightweight messaging protocol
Community Contributors - Thank you to all contributors

Disclaimer
IMPORTANT: This software is for educational purposes only. Cryptocurrency trading carries substantial risk of loss. Always test with small amounts first and never trade more than you can afford to lose. The authors are not responsible for any financial losses incurred through the use of this software.
Trading involves significant risk of loss and is not suitable for all investors. Past performance is not indicative of future results. The strategies and settings described in this documentation are examples only and should not be considered investment advice.

