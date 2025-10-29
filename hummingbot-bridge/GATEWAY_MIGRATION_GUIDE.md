# Gateway Migration Guide for Multi-Network Trading Setup

## Overview
This document provides all information needed to migrate your custom multi-network trading setup when upgrading Gateway versions.

**Created**: 2025-10-09  
**Gateway Version**: 2.9.0  
**Hummingbot Version**: dev-2.10.0

---

## 1. Custom Core Code Added

### File: `hummingbot/connector/gateway/core/gateway_network_adapter.py`

**Purpose**: Enables a single Gateway connector to trade on multiple EVM networks dynamically by temporarily overriding the network property during trade execution.

**Key Features**:
- Wraps any `GatewaySwap` connector
- Temporarily overrides `connector._network` during trades
- Thread-safe with try/finally guarantee
- Transparent proxy for all other connector methods

**Dependencies**:
- `hummingbot.connector.gateway.gateway_swap.GatewaySwap`
- `hummingbot.core.data_type.common.OrderType`
- Standard library: `decimal.Decimal`, `typing`

**Full Source Code**: See `/home/todd/PycharmProjects/hummingbot/hummingbot/connector/gateway/core/gateway_network_adapter.py`

### File: `hummingbot/connector/gateway/core/__init__.py`

**Purpose**: Makes the `core` directory a Python module.

**Content**: Empty file (just enables imports)

---

## 2. Strategy Integration Points

### File: `scripts/mqtt_webhook_strategy_w_cex.py`

**Modified Method**: `_get_dex_connector()`

**Before** (Gateway 2.9.0):
```python
def _get_dex_connector(self, exchange: str, pool_type: str = None, network: str = None) -> Optional[Any]:
    # ... pool type detection ...
    connector_key = f"{exchange.lower()}/{pool_type}"
    
    if connector_key in self.connectors:
        self.logger().debug(f"✅ Found connector: {connector_key} (network: {network})")
        return self.connectors[connector_key]  # ← Direct return
```

**After** (With Multi-Network Support):
```python
from hummingbot.connector.gateway.core.gateway_network_adapter import GatewayNetworkAdapter

def _get_dex_connector(self, exchange: str, pool_type: str = None, network: str = None) -> Optional[Any]:
    # ... pool type detection ...
    connector_key = f"{exchange.lower()}/{pool_type}"
    
    if connector_key in self.connectors:
        base_connector = self.connectors[connector_key]
        
        # Wrap with network adapter to enable dynamic network switching
        adapter = GatewayNetworkAdapter(base_connector, network)
        
        self.logger().debug(
            f"✅ Found connector: {connector_key} "
            f"(network: {network or 'default'}, adapter: {adapter.network})"
        )
        
        return adapter  # ← Return wrapped adapter
```

**Key Changes**:
1. Import `GatewayNetworkAdapter`
2. Wrap connector with adapter before returning
3. Pass `network` parameter to adapter
4. Return adapter instead of raw connector

---

## 3. How It Works

### Network Switching Mechanism

```python
# When MQTT signal specifies network="base":
connector = self._get_dex_connector("uniswap", "clmm", "base")

# The adapter wraps the connector:
# - adapter._connector = uniswap/clmm connector (default network: arbitrum)
# - adapter._network_override = "base"

# When placing an order:
order_id = connector.place_order(is_buy=False, trading_pair="WBTC-USDC", ...)

# The adapter temporarily overrides the network:
def _execute_with_network_override(self, operation):
    if self._network_override:
        original = self._connector._network  # Save "arbitrum"
        try:
            self._connector._network = "base"  # Set to "base"
            result = operation()  # Execute trade on "base"
            return result
        finally:
            self._connector._network = original  # Restore "arbitrum"
```

### Trade Flow

1. **Signal arrives**: `{"action":"BUY", "network":"base", "exchange":"uniswap"}`
2. **Get adapter**: `_get_dex_connector("uniswap", "clmm", "base")` → Returns adapter with network="base"
3. **Execute trade**: Adapter temporarily sets `connector._network = "base"`
4. **Gateway call**: Connector calls Gateway with `network: "base"` parameter
5. **Restore**: Adapter restores original network
6. **Database tracking**: MarketsRecorder saves trade with exchange="uniswap/clmm"

---

## 4. Gateway Version Upgrade Checklist

### Before Upgrading Gateway:

- [ ] **Backup current setup**:
  ```bash
  cp -r gateway gateway.backup.$(date +%Y%m%d)
  cp -r hummingbot hummingbot.backup.$(date +%Y%m%d)
  ```

- [ ] **Document current Gateway version**:
  ```bash
  cd gateway && git describe --tags
  ```

- [ ] **Export current configuration**:
  ```bash
  cp -r gateway/conf gateway/conf.backup.$(date +%Y%m%d)
  ```

### During Gateway Upgrade:

- [ ] **Check if Gateway connector architecture changed**
  - Look for changes to `GatewaySwap` class
  - Check if `_network` property still exists
  - Verify `place_order()` method signature

- [ ] **Check if network parameter handling changed**
  - Verify Gateway API still accepts `network` parameter in trade endpoints
  - Check endpoint format: `/connectors/{exchange}/{type}/execute-swap`

- [ ] **Test core adapter compatibility**:
  ```python
  # Test if adapter can still access _network property
  from hummingbot.connector.gateway.gateway_swap import GatewaySwap
  from hummingbot.connector.gateway.core.gateway_network_adapter import GatewayNetworkAdapter
  
  # Check if connector has _network attribute
  connector = GatewaySwap(...)
  print(hasattr(connector, '_network'))  # Should be True
  
  # Test wrapping
  adapter = GatewayNetworkAdapter(connector, "base")
  print(adapter.network)  # Should print "base"
  ```

### After Gateway Upgrade:

- [ ] **Run test trades on each network**:
  ```bash
  # Test Arbitrum
  mosquitto_pub -h localhost -t hbot/signals/test/1 -m '{"action":"BUY","symbol":"WBTC-USDC","network":"arbitrum","exchange":"uniswap","pool_type":"clmm","amount":1}'
  
  # Test Base
  mosquitto_pub -h localhost -t hbot/signals/test/2 -m '{"action":"BUY","symbol":"WBTC-USDC","network":"base","exchange":"uniswap","pool_type":"clmm","amount":1}'
  ```

- [ ] **Verify database tracking**:
  ```bash
  sqlite3 data/mqtt_webhook_strategy_w_cex.sqlite "SELECT datetime(timestamp/1000, 'unixepoch'), market, symbol, exchange_trade_id FROM TradeFill ORDER BY timestamp DESC LIMIT 5;"
  ```

- [ ] **Check logs for errors**:
  ```bash
  tail -f logs/logs_mqtt_webhook_strategy_w_cex.log | grep -E "ERROR|WARNING"
  ```

---

## 5. Critical Gateway Bug Fix (2025-10-11)

### Bug: "fractional component exceeds decimals" Error on All EVM Trades

**Symptoms**:
- All EVM network trades (Arbitrum, Base, Optimism, etc.) fail with error:
  ```
  fractional component exceeds decimals [ See: https://links.ethers.org/v5-errors-NUMERIC_FAULT ]
  (fault="underflow", operation="parseFixed", code=NUMERIC_FAULT, version=bignumber/5.8.0)
  ```
- Error occurs after allowance check passes
- Affects both BUY and SELL operations
- Works fine for Solana trades (Jupiter, Meteora, Raydium)

**Root Cause**:
JavaScript floating point precision issue in gas price calculation in `gateway/src/chains/ethereum/ethereum.ts`:

```typescript
// Line 158: Multiply creates excessive decimal places
const bufferedGasPriceInGwei = gasPriceInGwei * 1.1;

// Example: 0.013242 * 1.1 = 0.014566200000000001 (18 decimal places!)

// Line 162: parseUnits fails because Gwei only supports 9 decimal places
gasOptions.gasPrice = utils.parseUnits(bufferedGasPriceInGwei.toString(), 'gwei');
// ❌ Error: "fractional component exceeds decimals"
```

**The Fix**:

File: `gateway/src/chains/ethereum/ethereum.ts` (lines 162-164)

```typescript
// BEFORE (Broken):
gasOptions.gasPrice = utils.parseUnits(bufferedGasPriceInGwei.toString(), 'gwei');

// AFTER (Fixed):
// Round to 9 decimal places (max for Gwei) to avoid floating point precision issues
const roundedGasPrice = bufferedGasPriceInGwei.toFixed(9);
gasOptions.gasPrice = utils.parseUnits(roundedGasPrice, 'gwei');
```

**How to Apply the Fix**:

1. **Edit the file**:
   ```bash
   cd gateway
   # Edit src/chains/ethereum/ethereum.ts at line 162-164
   # Add the rounding step before parseUnits
   ```

2. **Build Gateway** (with workaround for unrelated TypeScript errors):
   ```bash
   # Temporarily rename problematic files
   mv src/chains/ethereum/infura-service.ts src/chains/ethereum/infura-service.ts.skip
   mv src/chains/solana/helius-service.ts src/chains/solana/helius-service.ts.skip
   mv src/chains/solana/solana-priority-fees.ts src/chains/solana/solana-priority-fees.ts.skip

   # Build
   pnpm build

   # Restore files
   mv src/chains/ethereum/infura-service.ts.skip src/chains/ethereum/infura-service.ts
   mv src/chains/solana/helius-service.ts.skip src/chains/solana/helius-service.ts
   mv src/chains/solana/solana-priority-fees.ts.skip src/chains/solana/solana-priority-fees.ts
   ```

3. **Restart Gateway**:
   ```bash
   # Your usual Gateway start command
   pnpm start --passphrase=<PASSPHRASE>
   ```

4. **Verify the fix**:
   ```bash
   # Test a BUY trade on any EVM network
   mosquitto_pub -h localhost -t hbot/signals/test/fix -m '{
     "action":"BUY",
     "symbol":"WBTC-USDC",
     "network":"arbitrum",
     "exchange":"uniswap",
     "pool_type":"clmm",
     "amount":1
   }'

   # Check Gateway logs - should see successful transaction
   tail -f gateway/logs/logs_gateway_app.log.$(date +%Y-%m-%d)
   ```

**Why This Happened**:
- This bug was introduced by commit `36d4e49f` (Sep 17, 2025) which modified gas pricing logic
- The commit aimed to fix other issues but inadvertently introduced floating point precision problems
- JavaScript's floating point arithmetic creates numbers with more decimal places than ethers.js `parseUnits` can handle

**Impact**:
- **Critical**: Breaks ALL EVM network trading (Uniswap, 0x on Ethereum, Arbitrum, Base, Optimism, Polygon, etc.)
- Does not affect Solana trading
- Affects both small ($10) and large trades
- Your previous working API codebase likely used an older Gateway version without this bug

**Verification**:
After applying the fix, you should see in Gateway logs:
```
Using legacy gas pricing: 0.014566 GWEI (estimated: 0.013242 GWEI + 10% buffer) with gasLimit: 350000
Transaction sent: 0x...
Transaction confirmed: 0x...
```

Instead of:
```
Swap execution error: fractional component exceeds decimals
```

**Date Discovered**: 2025-10-11
**Affected Gateway Versions**: dev-2.9.0 (commits after Sep 17, 2025)
**Status**: **FIXED** - Critical for production use

---

## 6. EVM Gas Fee Capture Fix (2025-10-13)

### Bug: EVM Gas Fees Not Captured in Reports

**Symptoms**:
- Solana DEX trades (Jupiter, Raydium, Meteora) show gas fees correctly
- EVM DEX trades (Uniswap, 0x on Ethereum, Arbitrum, Base, etc.) show 0.0 gas fees
- Trades execute successfully, but reporting/analysis missing fee data
- Database `TradeFill` table shows `flat_fees: [{"token": "WBTC", "amount": "0"}]` for EVM trades

**Root Cause**:
Gateway's Ethereum poll route returns `fee: null` instead of calculating gas fees from transaction receipts. The poll route at `gateway/src/chains/ethereum/routes/poll.ts:113` was hardcoded to return `null`:

```typescript
// BROKEN CODE:
return {
  currentBlock,
  signature,
  txBlock,
  txStatus,
  txData: toEthereumTransactionResponse(txData),
  fee: null, // ← Hardcoded to null!
};
```

**The Fix (Part 1): Calculate EVM Gas Fees**

File: `gateway/src/chains/ethereum/routes/poll.ts` (after line 105, before the return statement)

```typescript
// Calculate gas fee from receipt if available
let fee: number | null = null;
if (txReceipt && txReceipt.gasUsed && txReceipt.effectiveGasPrice) {
  // fee = gasUsed * effectiveGasPrice (in wei)
  const feeWei = txReceipt.gasUsed.mul(txReceipt.effectiveGasPrice);
  // Convert to native token units (ETH, MATIC, AVAX, etc.)
  const feeEther = ethers.utils.formatEther(feeWei);
  fee = parseFloat(feeEther);
  logger.info(`Calculated transaction fee: ${fee} (gasUsed: ${txReceipt.gasUsed.toString()}, effectiveGasPrice: ${txReceipt.effectiveGasPrice.toString()})`);
}

return {
  currentBlock,
  signature,
  txBlock,
  txStatus,
  txData: toEthereumTransactionResponse(txData),
  fee,  // ← Now returns actual calculated fee
};
```

**The Fix (Part 2): Add Missing TypeScript Type Definitions**

The Gateway build was failing due to missing optional properties in config interfaces. These need to be added:

File: `gateway/src/chains/ethereum/ethereum.config.ts`

```typescript
export interface EthereumNetworkConfig {
  chainID: number;
  nodeURL: string;
  nativeCurrencySymbol: string;
  minGasPrice?: number;
  infuraAPIKey?: string;           // ← Add this
  useInfuraWebSocket?: boolean;    // ← Add this
}
```

File: `gateway/src/chains/solana/solana.config.ts`

```typescript
export interface SolanaNetworkConfig {
  nodeURL: string;
  nativeCurrencySymbol: string;
  defaultComputeUnits: number;
  confirmRetryInterval: number;
  confirmRetryCount: number;
  basePriorityFeePct: number;
  minPriorityFeePerCU: number;
  heliusAPIKey?: string;           // ← Add this
  heliusRegionCode?: string;       // ← Add this
  useHeliusRestRPC?: boolean;      // ← Add this
  useHeliusWebSocketRPC?: boolean; // ← Add this
  useHeliusSender?: boolean;       // ← Add this
}
```

**How to Apply the Fix**:

1. **Edit the Ethereum poll route**:
   ```bash
   cd gateway
   # Edit src/chains/ethereum/routes/poll.ts
   # Add the gas fee calculation code before the return statement (around line 107-116)
   ```

2. **Add missing type definitions**:
   ```bash
   # Edit src/chains/ethereum/ethereum.config.ts
   # Add infuraAPIKey and useInfuraWebSocket to EthereumNetworkConfig interface

   # Edit src/chains/solana/solana.config.ts
   # Add heliusAPIKey, heliusRegionCode, useHeliusRestRPC, useHeliusWebSocketRPC,
   # and useHeliusSender to SolanaNetworkConfig interface
   ```

3. **Build Gateway**:
   ```bash
   pnpm build
   # Should complete successfully with no TypeScript errors
   ```

4. **Restart Gateway**:
   ```bash
   pnpm start --passphrase=<PASSPHRASE>
   ```

5. **Verify the fix**:
   ```bash
   # Test trades on both Solana and EVM
   # Then check reports
   cd ../hummingbot
   python reporting/examples/export_reports.py

   # Check fees in output
   tail -10 reports/output/all_trades_*.csv
   ```

**Expected Results After Fix**:

```
Trade_ID | Exchange | Network  | Symbol      | Fee
---------|----------|----------|-------------|-------------
0        | Raydium  | Solana   | RAY-USDC    | 0.000035
1        | Uniswap  | Ethereum | WBTC-USDC   | 0.000003609  ← Now shows gas fee!
2        | Uniswap  | Arbitrum | WBTC-USDC   | 0.000001234  ← Now shows gas fee!
```

**How It Works**:

1. **Hummingbot Strategy**: After trade fills, `mqtt_webhook_strategy_w_cex.py` calls `_fetch_and_update_gas_fee()` (line 426)
2. **Gateway Query**: Strategy queries Gateway's poll endpoint for transaction status
3. **Fee Calculation**: Gateway now calculates `gasUsed × effectiveGasPrice` and returns it
4. **Database Update**: Strategy updates `TradeFill.trade_fee` JSON with actual gas cost
5. **Reporting**: Export reports now show correct gas fees for both Solana and EVM trades

**Why This Matters**:
- Accurate PnL calculations require gas fee data
- Tax reporting needs complete fee information
- Performance analysis must account for transaction costs
- Different networks have vastly different gas costs (Solana: ~$0.0001, Ethereum L1: ~$5-50, Arbitrum: ~$0.01)

**Date Discovered**: 2025-10-13
**Affected Gateway Versions**: dev-2.9.0 (all versions prior to fix)
**Status**: **FIXED** - Required for accurate reporting

---

## 6. Potential Breaking Changes to Watch For

### High Risk (Likely to require adaptation):

1. **Gateway Connector Architecture Changes**
   - **What to check**: `GatewaySwap` class structure in `hummingbot/connector/gateway/gateway_swap.py`
   - **Risk**: If `_network` property is renamed or moved
   - **Fix**: Update `GatewayNetworkAdapter._execute_with_network_override()` to use new property name

2. **Network Parameter Handling**
   - **What to check**: Gateway API documentation for trade endpoints
   - **Risk**: If network parameter is no longer accepted or renamed
   - **Fix**: Update how connector passes network to Gateway

3. **Connector Registration System**
   - **What to check**: How connectors are registered in `connector_manager.py`
   - **Risk**: If connector keys change format (e.g., from "uniswap/clmm" to something else)
   - **Fix**: Update `_get_dex_connector()` connector key building

### Medium Risk:

4. **MarketsRecorder Integration**
   - **What to check**: If event handling for trades changes
   - **Risk**: Database tracking might break
   - **Fix**: Verify `did_fill_order()` event handler still receives events

5. **OrderType Enum Changes**
   - **What to check**: `hummingbot.core.data_type.common.OrderType`
   - **Risk**: If order type constants change
   - **Fix**: Update import and usage in adapter

### Low Risk:

6. **Import Path Changes**
   - **What to check**: If module paths are reorganized
   - **Risk**: Import statements might fail
   - **Fix**: Update import paths in strategy and adapter

---

## 7. Information Needed for Migration Support

### Essential Information to Provide:

1. **Gateway Version Details**:
   ```bash
   cd gateway && git describe --tags
   cd gateway && git log -1 --oneline
   ```

2. **Hummingbot Version Details**:
   ```bash
   cat hummingbot/VERSION
   git log -1 --oneline
   ```

3. **Error Messages** (if any):
   - Full stack traces from logs
   - Gateway error responses
   - Connector initialization errors

4. **Connector Structure Verification**:
   ```python
   # Run this in Hummingbot console
   from hummingbot.connector.gateway.gateway_swap import GatewaySwap
   connector = connectors.get("uniswap/clmm")
   print(dir(connector))  # List all attributes
   print(hasattr(connector, '_network'))  # Check if _network exists
   print(type(connector))  # Verify it's still GatewaySwap
   ```

5. **Gateway API Response Format**:
   - Sample successful trade response
   - Network parameter in request/response
   - Any new required fields

6. **Configuration Files**:
   - `gateway/conf/chains/ethereum.yml`
   - Gateway connector configs
   - Any new configuration requirements

### Nice to Have:

7. **Gateway Changelog**:
   - Release notes for new Gateway version
   - Breaking changes documentation
   - Migration guide from Gateway team

8. **Test Trade Results**:
   - Logs from attempted test trades
   - Database entries (or lack thereof)
   - Gateway response logs

---

## 8. Rollback Procedure

If upgrade fails:

```bash
# Stop Hummingbot and Gateway
pkill -f hummingbot
cd gateway && docker-compose down

# Restore backups
cd /home/todd/PycharmProjects
rm -rf gateway hummingbot
cp -r gateway.backup.YYYYMMDD gateway
cp -r hummingbot.backup.YYYYMMDD hummingbot

# Restore Gateway configuration
cd gateway
cp -r conf.backup.YYYYMMDD/* conf/

# Restart
docker-compose up -d
cd ../hummingbot && ./start -f mqtt_webhook_strategy_w_cex.py
```

---

## 8. Testing Script

Save this as `test_multinetwork.sh`:

```bash
#!/bin/bash
# Test multi-network trading after Gateway upgrade

echo "🧪 Testing Multi-Network Trading Setup"
echo "======================================"

# Test 1: Arbitrum Trade
echo "Test 1: Arbitrum WBTC-USDC BUY..."
mosquitto_pub -h localhost -t hbot/signals/test/arb -m '{
  "action":"BUY",
  "symbol":"WBTC-USDC",
  "network":"arbitrum",
  "exchange":"uniswap",
  "pool_type":"clmm",
  "amount":1
}'
sleep 5

# Test 2: Base Trade
echo "Test 2: Base WBTC-USDC BUY..."
mosquitto_pub -h localhost -t hbot/signals/test/base -m '{
  "action":"BUY",
  "symbol":"WBTC-USDC",
  "network":"base",
  "exchange":"uniswap",
  "pool_type":"clmm",
  "amount":1
}'
sleep 5

# Test 3: Optimism Trade
echo "Test 3: Optimism WBTC-USDC BUY..."
mosquitto_pub -h localhost -t hbot/signals/test/op -m '{
  "action":"BUY",
  "symbol":"WBTC-USDC",
  "network":"optimism",
  "exchange":"uniswap",
  "pool_type":"clmm",
  "amount":1
}'
sleep 5

# Check database
echo ""
echo "📊 Checking database for trades..."
sqlite3 data/mqtt_webhook_strategy_w_cex.sqlite << SQL
SELECT 
  datetime(timestamp/1000, 'unixepoch') as time,
  market,
  symbol,
  trade_type,
  substr(exchange_trade_id, 1, 20) as tx_hash
FROM TradeFill 
WHERE datetime(timestamp/1000, 'unixepoch') > datetime('now', '-1 minute')
ORDER BY timestamp DESC;
SQL

echo ""
echo "✅ Test complete! Check logs for detailed results."
```

---

## 9. Quick Reference

### Key Files:
- **Adapter**: `hummingbot/connector/gateway/core/gateway_network_adapter.py`
- **Strategy**: `scripts/mqtt_webhook_strategy_w_cex.py`
- **Gateway Config**: `gateway/conf/chains/ethereum.yml`
- **Database**: `data/mqtt_webhook_strategy_w_cex.sqlite`

### Key Concepts:
- **One connector, many networks**: Single `uniswap/clmm` connector works for all EVM networks
- **Dynamic switching**: Network is set per-trade, not per-connector
- **Temporary override**: Adapter saves/restores network automatically
- **Framework compatible**: Works with MarketsRecorder and OrderFilledEvent

### Common Issues:
1. **Trades not executing**: Check `connector._network` property exists
2. **Wrong network**: Verify adapter is wrapping connector
3. **Database not tracking**: Ensure MarketsRecorder receives events
4. **Import errors**: Check if module paths changed in upgrade

---

## Contact & Support

When requesting migration support, provide:
1. This migration guide
2. Gateway version (old and new)
3. Error logs
4. Connector structure verification output
5. Test trade results

**Created by**: Claude AI Assistant  
**Last Updated**: 2025-10-09
