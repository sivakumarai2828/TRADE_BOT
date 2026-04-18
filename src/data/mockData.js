export const metrics = [
  {
    label: "Account Balance",
    value: "$24,582.40",
    detail: "+$312.80 today",
    tone: "positive",
  },
  {
    label: "Total PnL",
    value: "+$1,248.20",
    detail: "+5.34% all time",
    tone: "positive",
  },
  {
    label: "Active Trades",
    value: "3",
    detail: "1 pending exit",
    tone: "neutral",
  },
  {
    label: "Risk Exposure",
    value: "$148.00",
    detail: "0.60% of balance",
    tone: "warning",
  },
];

export const candles = [
  { x: 18, open: 128, close: 112, high: 139, low: 104, signal: "sell" },
  { x: 52, open: 113, close: 121, high: 129, low: 108 },
  { x: 86, open: 122, close: 116, high: 132, low: 111 },
  { x: 120, open: 116, close: 134, high: 140, low: 109, signal: "buy" },
  { x: 154, open: 134, close: 147, high: 153, low: 127 },
  { x: 188, open: 146, close: 139, high: 158, low: 132 },
  { x: 222, open: 139, close: 151, high: 164, low: 137 },
  { x: 256, open: 150, close: 143, high: 157, low: 136 },
  { x: 290, open: 144, close: 158, high: 166, low: 141, signal: "buy" },
  { x: 324, open: 158, close: 169, high: 176, low: 152 },
  { x: 358, open: 169, close: 161, high: 181, low: 154 },
  { x: 392, open: 162, close: 176, high: 185, low: 158 },
  { x: 426, open: 176, close: 171, high: 188, low: 165 },
  { x: 460, open: 170, close: 186, high: 194, low: 168 },
  { x: 494, open: 186, close: 178, high: 198, low: 172, signal: "sell" },
  { x: 528, open: 179, close: 190, high: 202, low: 175 },
];

export const signal = {
  symbol: "BTC/USDT",
  action: "BUY",
  confidence: 87,
  rsi: 28.4,
  trend: "Uptrend",
  explanation:
    "Rule engine and Claude agree on a long entry. RSI is recovering from oversold while price remains above the 50 SMA.",
};

export const positions = [
  {
    symbol: "BTC/USDT",
    entry: "$66,240.00",
    current: "$67,120.00",
    pnl: "+$18.40",
    stopLoss: "$64,915.20",
    takeProfit: "$69,552.00",
    positive: true,
  },
  {
    symbol: "ETH/USDT",
    entry: "$3,140.00",
    current: "$3,104.50",
    pnl: "-$5.66",
    stopLoss: "$3,077.20",
    takeProfit: "$3,297.00",
    positive: false,
  },
  {
    symbol: "SOL/USDT",
    entry: "$148.20",
    current: "$151.10",
    pnl: "+$2.90",
    stopLoss: "$145.24",
    takeProfit: "$155.61",
    positive: true,
  },
];

export const logs = [
  {
    time: "14:32:08",
    type: "Trade executed",
    message: "Market buy BTC/USDT for $50.00",
    tone: "positive",
  },
  {
    time: "14:31:59",
    type: "Signal generated",
    message: "BUY confirmed by rules and Claude",
    tone: "neutral",
  },
  {
    time: "14:30:00",
    type: "Risk check",
    message: "Stop-loss and take-profit verified",
    tone: "warning",
  },
  {
    time: "14:28:44",
    type: "Error",
    message: "Exchange latency recovered after retry",
    tone: "negative",
  },
];
