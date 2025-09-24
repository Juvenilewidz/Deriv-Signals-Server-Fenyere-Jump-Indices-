#!/usr/bin/env python3
"""
Real-time DSR Trading Bot with Persistent WebSocket Connection
- Maintains continuous connection to Deriv WebSocket
- Processes live tick/candle streams without reconnecting
- Designed for 6-hour GitHub Actions runs with auto-restart
- Real-time signal detection and analysis
"""

import os, json, time, threading, traceback
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
import websocket
import tempfile
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Telegram helpers
try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text): print("[TEXT]", text); return True, "local"
    def send_telegram_photo(token, chat_id, caption, photo): print("[PHOTO]", caption, photo); return True, "local"

# -------------------------
# Configuration
# -------------------------
DERIV_API_KEY = os.getenv("DERIV_API_KEY", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089").strip()
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DEBUG = os.getenv("DEBUG", "0") == "1"
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

# Bot configuration
MAX_RUN_TIME = 6 * 60 * 60 - 300  # 6 hours minus 5 minutes buffer
CANDLE_TIMEFRAME = 300  # 5 minutes
MAX_CANDLES_MEMORY = 500
SIGNAL_COOLDOWN = 900  # 15 minutes between signals per symbol
TMPDIR = tempfile.gettempdir()

# Jump indices only
SYMBOLS = {
    "Jump10": "JD10",
    "Jump25": "JD25", 
    "Jump50": "JD50",
    "Jump75": "JD75",
    "Jump100": "JD100"
}

# -------------------------
# Moving Averages
# -------------------------
def smma(values, period):
    """Smoothed Moving Average calculation"""
    if len(values) < period:
        return None
    
    if len(values) == period:
        return sum(values) / period
    
    # SMMA formula: (prev_smma * (period-1) + current_value) / period
    prev_smma = sum(values[:period]) / period
    for i in range(period, len(values)):
        prev_smma = (prev_smma * (period - 1) + values[i]) / period
    
    return prev_smma

def sma(values, period):
    """Simple Moving Average calculation"""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def calculate_mas(candle_data):
    """Calculate MA1, MA2, MA3 for given candle data"""
    if len(candle_data) < 25:  # Need enough data for all MAs
        return None, None, None
    
    closes = [c['close'] for c in candle_data]
    hlc3_values = [(c['high'] + c['low'] + c['close']) / 3.0 for c in candle_data]
    
    # MA1: SMMA of HLC/3, period 6
    ma1 = smma(hlc3_values, 6)
    
    # MA2: SMMA of Close, period 15
    ma2 = smma(closes, 15)
    
    # MA3: SMA of MA2 values, period 20
    # We need to calculate MA2 for the last 20 candles to get MA3
    ma2_values = []
    for i in range(len(candle_data)):
        if i >= 14:  # MA2 needs at least 15 values
            ma2_val = smma([c['close'] for c in candle_data[max(0, i-14):i+1]], 15)
            if ma2_val is not None:
                ma2_values.append(ma2_val)
    
    ma3 = sma(ma2_values, 20) if len(ma2_values) >= 20 else None
    
    return ma1, ma2, ma3

# -------------------------
# Pattern Detection
# -------------------------
def is_pin_bar(candle):
    """Pin Bar: wick >= 1.2 times body"""
    o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
    body_size = abs(c - o)
    
    if body_size == 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    
    if upper_wick >= body_size * 1.2:
        return True, "BEARISH_PIN_BAR"
    elif lower_wick >= body_size * 1.2:
        return True, "BULLISH_PIN_BAR"
    
    return False, "NONE"

def is_doji(candle):
    """Doji: body < 10% of total range"""
    o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range == 0:
        return False, "NONE"
    
    if body_size <= total_range * 0.1:
        return True, "DOJI"
    
    return False, "NONE"

def is_engulfing(prev_candle, curr_candle):
    """Engulfing pattern detection"""
    prev_body_top = max(prev_candle['open'], prev_candle['close'])
    prev_body_bottom = min(prev_candle['open'], prev_candle['close'])
    curr_body_top = max(curr_candle['open'], curr_candle['close'])
    curr_body_bottom = min(curr_candle['open'], curr_candle['close'])
    
    # Bullish engulfing
    if (curr_candle['close'] > curr_candle['open'] and
        prev_candle['close'] < prev_candle['open'] and
        curr_body_bottom < prev_body_bottom and
        curr_body_top > prev_body_top):
        return True, "BULLISH_ENGULFING"
    
    # Bearish engulfing
    elif (curr_candle['close'] < curr_candle['open'] and
          prev_candle['close'] > prev_candle['open'] and
          curr_body_top > prev_body_top and
          curr_body_bottom < prev_body_bottom):
        return True, "BEARISH_ENGULFING"
    
    return False, "NONE"

def detect_rejection_pattern(candles):
    """Detect rejection patterns in the latest candle"""
    if len(candles) < 2:
        return False, "NONE"
    
    current = candles[-1]
    previous = candles[-2]
    
    # Check Pin Bar
    is_pin, pin_type = is_pin_bar(current)
    if is_pin:
        return True, pin_type
    
    # Check Doji
    is_doji_pattern, doji_type = is_doji(current)
    if is_doji_pattern:
        return True, doji_type
    
    # Check Engulfing
    is_eng, eng_type = is_engulfing(previous, current)
    if is_eng:
        return True, eng_type
    
    return False, "NONE"

# -------------------------
# Signal Analysis
# -------------------------
def analyze_signal(symbol, candle_data):
    """Analyze candle data for DSR signals"""
    if len(candle_data) < 30:
        return None
    
    # Calculate MAs
    ma1, ma2, ma3 = calculate_mas(candle_data)
    if not all(v is not None for v in [ma1, ma2, ma3]):
        return None
    
    # Determine trend
    if ma1 > ma2 > ma3:
        trend = "UPTREND"
    elif ma1 < ma2 < ma3:
        trend = "DOWNTREND"
    else:
        trend = "RANGING"
    
    # Skip ranging markets
    if trend == "RANGING":
        return None
    
    # Check for rejection patterns
    has_rejection, pattern = detect_rejection_pattern(candle_data)
    if not has_rejection:
        return None
    
    current_candle = candle_data[-1]
    current_price = current_candle['close']
    
    # Check proximity to MA levels (0.5% tolerance)
    ma1_tolerance = abs(ma1) * 0.005
    ma2_tolerance = abs(ma2) * 0.005
    
    near_ma1 = abs(current_price - ma1) <= ma1_tolerance
    near_ma2 = abs(current_price - ma2) <= ma2_tolerance
    
    if not (near_ma1 or near_ma2):
        return None
    
    # Determine signal
    signal_side = None
    ma_level = "MA1" if near_ma1 else "MA2"
    
    if trend == "UPTREND" and pattern in ["BULLISH_PIN_BAR", "DOJI", "BULLISH_ENGULFING"]:
        if current_price >= (ma1 if near_ma1 else ma2):
            signal_side = "BUY"
    elif trend == "DOWNTREND" and pattern in ["BEARISH_PIN_BAR", "DOJI", "BEARISH_ENGULFING"]:
        if current_price <= (ma1 if near_ma1 else ma2):
            signal_side = "SELL"
    
    if not signal_side:
        return None
    
    return {
        'symbol': symbol,
        'side': signal_side,
        'pattern': pattern,
        'ma_level': ma_level,
        'trend': trend,
        'price': current_price,
        'ma1': ma1,
        'ma2': ma2,
        'ma3': ma3,
        'timestamp': current_candle['epoch'],
        'candle_data': candle_data[-100:]  # Keep last 100 candles for chart
    }

# -------------------------
# Chart Generation
# -------------------------
def create_signal_chart(signal_data):
    """Create chart for the signal"""
    candles = signal_data['candle_data']
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, 8))
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    
    # Plot candlesticks
    for i, candle in enumerate(candles):
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        
        color = "#00FF00" if c >= o else "#FF0000"
        edge_color = "#00AA00" if c >= o else "#AA0000"
        
        # Candle body
        ax.add_patch(Rectangle(
            (i - 0.3, min(o, c)), 0.6, abs(c - o) if abs(c - o) > 0 else 0.0001,
            facecolor=color, edgecolor=edge_color, alpha=0.9
        ))
        
        # Wicks
        ax.plot([i, i], [l, h], color=edge_color, linewidth=1.2, alpha=0.8)
    
    # Calculate and plot MAs for the chart period
    ma1_values, ma2_values, ma3_values = [], [], []
    for i in range(len(candles)):
        if i >= 5:  # MA1 needs 6 values
            ma1_val = smma([((c['high'] + c['low'] + c['close']) / 3) for c in candles[max(0, i-5):i+1]], 6)
            ma1_values.append(ma1_val)
        else:
            ma1_values.append(None)
        
        if i >= 14:  # MA2 needs 15 values
            ma2_val = smma([c['close'] for c in candles[max(0, i-14):i+1]], 15)
            ma2_values.append(ma2_val)
        else:
            ma2_values.append(None)
    
    # MA3 calculation for chart
    ma2_for_ma3 = [v for v in ma2_values if v is not None]
    for i in range(len(candles)):
        if i >= 34 and len(ma2_for_ma3) >= 20:  # Need 20 MA2 values for MA3
            ma3_val = sma(ma2_for_ma3[max(0, len(ma2_for_ma3)-20):], 20)
            ma3_values.append(ma3_val)
        else:
            ma3_values.append(None)
    
    # Plot MAs
    ax.plot(range(len(candles)), ma1_values, color='#FFFFFF', linewidth=2, label='MA1 (SMMA HLC/3-6)', alpha=0.9)
    ax.plot(range(len(candles)), ma2_values, color='#00BFFF', linewidth=2, label='MA2 (SMMA Close-15)', alpha=0.9)
    ax.plot(range(len(candles)), ma3_values, color='#FF6347', linewidth=2, label='MA3 (SMA MA2-20)', alpha=0.9)
    
    # Mark signal point
    signal_idx = len(candles) - 1
    signal_price = signal_data['price']
    marker_color = "#00FF00" if signal_data['side'] == "BUY" else "#FF0000"
    marker_symbol = "^" if signal_data['side'] == "BUY" else "v"
    
    ax.scatter([signal_idx], [signal_price], color=marker_color, marker=marker_symbol, 
              s=400, edgecolor='#FFFFFF', linewidth=3, zorder=10)
    
    # Title and formatting
    trend_emoji = "📈" if signal_data['trend'] == "UPTREND" else "📉"
    ax.set_title(f"{signal_data['symbol']} M5 - {signal_data['side']} Signal {trend_emoji} | {signal_data['pattern']} @ {signal_data['ma_level']}", 
                fontsize=14, color='white', fontweight='bold', pad=20)
    
    legend = ax.legend(loc="upper left", frameon=True, facecolor='black', edgecolor='white', fontsize=10)
    legend.get_frame().set_alpha(0.8)
    
    ax.grid(True, alpha=0.3, color='gray', linestyle='--', linewidth=0.5)
    ax.tick_params(colors='white', labelsize=9)
    
    for spine in ax.spines.values():
        spine.set_color('white')
    
    plt.tight_layout()
    
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, dpi=150, bbox_inches="tight", facecolor='black')
    plt.close()
    plt.style.use('default')
    
    return chart_file.name

# -------------------------
# Real-time Bot Class
# -------------------------
class RealTimeDSRBot:
    def __init__(self):
        self.ws = None
        self.candle_data = defaultdict(lambda: deque(maxlen=MAX_CANDLES_MEMORY))
        self.last_signals = defaultdict(int)
        self.subscriptions = {}
        self.running = True
        self.start_time = time.time()
        self.connected = False
        self.authorized = False
        self.subscribed_symbols = set()
        self.reconnect_count = 0
        self.max_reconnects = 50
        
    def on_open(self, ws):
        """WebSocket connection opened"""
        print(f"[{datetime.now()}] Connected to Deriv WebSocket")
        self.connected = True
        self.authorized = False
        
        # Wait a moment for connection to stabilize
        time.sleep(1)
        
        # Authorize if API key is provided
        if DERIV_API_KEY:
            auth_request = {"authorize": DERIV_API_KEY}
            ws.send(json.dumps(auth_request))
            print("Sent authorization request...")
        else:
            # No API key, proceed directly to subscriptions
            self.authorized = True
            self.setup_subscriptions(ws)
    
    def on_message(self, ws, message):
        """Process incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type', 'unknown')
            
            if DEBUG:
                print(f"[{datetime.now()}] Received: {msg_type}")
            
            # Handle authorization response
            if msg_type == 'authorize':
                if data.get('error'):
                    print(f"Authorization failed: {data['error']['message']}")
                    self.authorized = False
                else:
                    print("Successfully authorized")
                    self.authorized = True
                    # Now setup subscriptions after successful auth
                    self.setup_subscriptions(ws)
            
            # Handle initial candle history
            elif msg_type == 'history':
                self.process_initial_history(data)
            
            # Handle streaming candle data
            elif msg_type == 'candles':
                self.process_candle_data(data)
            
            # Handle subscription confirmations
            elif msg_type == 'candles_subscription' or msg_type == 'history_subscription':
                symbol = data.get('echo_req', {}).get('ticks_history', 'unknown')
                print(f"Successfully subscribed to {symbol}")
                self.subscribed_symbols.add(symbol)
            
            # Handle errors
            elif data.get('error'):
                error_msg = data['error'].get('message', 'Unknown error')
                print(f"API Error: {error_msg}")
        
        except Exception as e:
            print(f"Error processing message: {e}")
            if DEBUG:
                traceback.print_exc()
    
    def on_error(self, ws, error):
        """Handle WebSocket errors"""
        print(f"WebSocket error: {error}")
        self.connected = False
    
    def on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close"""
        print(f"[{datetime.now()}] WebSocket connection closed: {close_status_code} - {close_msg}")
        self.connected = False
        self.authorized = False
        self.subscribed_symbols.clear()
        
        # Attempt to reconnect if we're still supposed to be running
        if self.running and (time.time() - self.start_time) < MAX_RUN_TIME:
            self.reconnect_count += 1
            if self.reconnect_count < self.max_reconnects:
                reconnect_delay = min(30, 5 + self.reconnect_count * 2)  # Progressive delay
                print(f"Attempting to reconnect in {reconnect_delay} seconds... (attempt {self.reconnect_count})")
                time.sleep(reconnect_delay)
                self.connect()
            else:
                print("Max reconnection attempts reached. Stopping bot.")
                self.running = False
    
    def setup_subscriptions(self, ws):
        """Setup all symbol subscriptions after connection is stable"""
        if not (self.connected and self.authorized):
            return
            
        print("Setting up symbol subscriptions...")
        time.sleep(2)  # Wait for connection to fully stabilize
        
        for symbol_name, deriv_symbol in SYMBOLS.items():
            try:
                self.subscribe_to_candles(ws, deriv_symbol, symbol_name)
                time.sleep(1)  # Stagger subscription requests
            except Exception as e:
                print(f"Failed to subscribe to {symbol_name}: {e}")
                
        print(f"Subscription setup complete. Monitoring {len(SYMBOLS)} symbols.")
    
    def subscribe_to_candles(self, ws, deriv_symbol, symbol_name):
        """Subscribe to candle data for a symbol"""
        # Use simple, clean request IDs
        history_req_id = f"hist_{deriv_symbol}_{int(time.time())}"
        stream_req_id = f"stream_{deriv_symbol}_{int(time.time())}"
        
        # First get historical data
        history_request = {
            "ticks_history": deriv_symbol,
            "style": "candles",
            "granularity": CANDLE_TIMEFRAME,
            "count": 150,
            "end": "latest",
            "req_id": history_req_id
        }
        
        ws.send(json.dumps(history_request))
        self.subscriptions[history_req_id] = symbol_name
        
        # Wait a moment before subscribing to live stream
        time.sleep(0.5)
        
        # Then subscribe to live updates
        subscription_request = {
            "ticks_history": deriv_symbol,
            "style": "candles", 
            "granularity": CANDLE_TIMEFRAME,
            "subscribe": 1,
            "req_id": stream_req_id
        }
        
        ws.send(json.dumps(subscription_request))
        self.subscriptions[stream_req_id] = symbol_name
        
        print(f"Requested history and subscription for {symbol_name} ({deriv_symbol})")
    
    def process_initial_history(self, data):
        """Process initial historical candle data"""
        try:
            req_id = data.get('req_id', '')
            if not req_id.startswith('hist_'):
                return
                
            symbol_name = self.subscriptions.get(req_id, 'Unknown')
            
            if 'candles' not in data:
                print(f"No candles in history response for {symbol_name}")
                return
            
            print(f"Processing {len(data['candles'])} historical candles for {symbol_name}")
            
            # Clear existing data for this symbol
            self.candle_data[symbol_name].clear()
            
            # Process historical candles
            candle_count = 0
            for candle_raw in data['candles']:
                try:
                    candle = {
                        'epoch': int(candle_raw['epoch']),
                        'open': float(candle_raw['open']),
                        'high': float(candle_raw['high']),
                        'low': float(candle_raw['low']),
                        'close': float(candle_raw['close'])
                    }
                    self.candle_data[symbol_name].append(candle)
                    candle_count += 1
                except (KeyError, ValueError) as e:
                    print(f"Invalid candle data: {e}")
                    continue
            
            print(f"Successfully loaded {candle_count} candles for {symbol_name}")
            
            # Check if we have enough data for analysis
            if candle_count >= 30:
                print(f"{symbol_name} ready for signal analysis")
            else:
                print(f"Warning: {symbol_name} has only {candle_count} candles (need 30+ for analysis)")
            
        except Exception as e:
            print(f"Error processing historical data: {e}")
            if DEBUG:
                traceback.print_exc()
    
    def process_candle_data(self, data):
        """Process incoming candle data and check for signals"""
        try:
            # Identify which symbol this data is for
            req_id = data.get('req_id', '')
            symbol_name = self.subscriptions.get(req_id, 'Unknown')
            
            if 'candles' not in data:
                return
            
            # Process each candle
            for candle_raw in data['candles']:
                candle = {
                    'epoch': int(candle_raw['epoch']),
                    'open': float(candle_raw['open']),
                    'high': float(candle_raw['high']),
                    'low': float(candle_raw['low']),
                    'close': float(candle_raw['close'])
                }
                
                # Add to our candle storage
                self.candle_data[symbol_name].append(candle)
                
                # Check for signals on the latest complete candle
                if len(self.candle_data[symbol_name]) >= 30:
                    signal = analyze_signal(symbol_name, list(self.candle_data[symbol_name]))
                    if signal:
                        self.handle_signal(signal)
        
        except Exception as e:
            print(f"Error processing candle data: {e}")
            if DEBUG:
                traceback.print_exc()
    
    def handle_signal(self, signal):
        """Handle detected signals"""
        symbol = signal['symbol']
        timestamp = signal['timestamp']
        side = signal['side']
        
        # Check cooldown
        last_signal_time = self.last_signals[f"{symbol}_{side}"]
        if timestamp - last_signal_time < SIGNAL_COOLDOWN:
            return
        
        self.last_signals[f"{symbol}_{side}"] = timestamp
        
        # Create alert message
        trend_emoji = "📈" if signal['trend'] == "UPTREND" else "📉"
        alert_text = (f"🎯 {symbol} M5 - {side} SIGNAL\n"
                     f"{trend_emoji} Trend: {signal['trend']}\n"
                     f"🎨 Pattern: {signal['pattern']}\n"
                     f"📍 Level: {signal['ma_level']} Dynamic S/R\n"
                     f"💰 Price: {signal['price']:.5f}\n"
                     f"📊 MA1={signal['ma1']:.5f} MA2={signal['ma2']:.5f} MA3={signal['ma3']:.5f}")
        
        print(f"\n🚨 SIGNAL DETECTED: {symbol} {side}")
        print(alert_text)
        
        # Create and send chart
        try:
            chart_path = create_signal_chart(signal)
            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, alert_text, chart_path)
            
            if success:
                print(f"✅ Signal sent to Telegram for {symbol}")
            else:
                print(f"❌ Failed to send signal for {symbol}")
            
            # Clean up chart file
            try:
                os.unlink(chart_path)
            except:
                pass
                
        except Exception as e:
            print(f"Error creating/sending chart: {e}")
            # Send text message as fallback
            send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, alert_text)
    
    def connect(self):
        """Establish WebSocket connection with improved error handling"""
        try:
            print(f"[{datetime.now()}] Attempting WebSocket connection (attempt {self.reconnect_count + 1})...")
            
            self.ws = websocket.WebSocketApp(
                DERIV_WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            # Use run_forever with timeout to prevent hanging
            self.ws.run_forever(
                ping_interval=30,  # Send ping every 30 seconds
                ping_timeout=10,   # Wait 10 seconds for pong
                reconnect=5        # Reconnect delay
            )
            
        except Exception as e:
            print(f"Connection error: {e}")
            self.connected = False
    
    def run(self):
        """Main bot execution"""
        print(f"[{datetime.now()}] Starting Real-time DSR Bot")
        print(f"Max run time: {MAX_RUN_TIME/3600:.1f} hours")
        print(f"Monitoring symbols: {list(SYMBOLS.keys())}")
        
        # Send startup notification
        startup_msg = (f"🤖 DSR Bot Started\n"
                      f"📊 Symbols: {', '.join(SYMBOLS.keys())}\n"
                      f"⏰ Max runtime: {MAX_RUN_TIME/3600:.1f}h\n"
                      f"🔄 Timeframe: M5")
        send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, startup_msg)
        
        # Connect and run
        self.connect()
        
        # Monitor runtime in a separate thread
        def runtime_monitor():
            while self.running and (time.time() - self.start_time) < MAX_RUN_TIME:
                time.sleep(60)  # Check every minute
            
            if self.running:
                print(f"[{datetime.now()}] Max runtime reached, shutting down...")
                self.shutdown()
        
        runtime_thread = threading.Thread(target=runtime_monitor, daemon=True)
        runtime_thread.start()
        
        # Keep main thread alive
        try:
            while self.running and (time.time() - self.start_time) < MAX_RUN_TIME:
                time.sleep(10)
        except KeyboardInterrupt:
            print("Received interrupt signal")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """Graceful shutdown"""
        print(f"[{datetime.now()}] Shutting down bot...")
        self.running = False
        
        if self.ws:
            self.ws.close()
        
        # Send shutdown notification
        runtime = (time.time() - self.start_time) / 3600
        shutdown_msg = f"🔴 DSR Bot Stopped\n⏱️ Runtime: {runtime:.1f}h\n🔄 Auto-restart in progress..."
        send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, shutdown_msg)

# -------------------------
# Main Execution
# -------------------------
if __name__ == "__main__":
    bot = RealTimeDSRBot()
    bot.run()
