module.exports = {
  WALLET_ADDRESS: process.env.HL_WALLET || '0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE',
  PRIVATE_KEY:    process.env.HL_PRIVATE_KEY,
  LEVERAGE:       10,
  MAX_POSITIONS:  20,
  MAX_DIR:        10,
  CIRCUIT_DD:     0.65,
  SL_ROE:        -10.0,
  LIVE_TRADING:   process.env.LIVE_TRADING === 'true',

  TICKERS: [
    'BTC','ETH','SOL','BNB','XRP','ADA','AVAX','DOGE','LINK','DOT',
    'ATOM','NEAR','APT','SUI','ARB','OP','INJ','TIA','SEI','TRX',
    'LTC','BCH','AAVE','MKR','UNI','CRV','SNX','COMP','RUNE','TAO',
    'WIF','ENA','JUP','PYTH','JTO','STRK','ONDO','FET','LDO','SAND',
    'AXS','GALA','IMX','BLUR','GMX','PENDLE','STX','MINA','DYDX','EIGEN',
    'ETHFI','IO','LAYER','MON','TON','VIRTUAL','WLD','HYPE','RNDR','GRT',
    'SUSHI','ZRX','ENS','YGG','ALGO','GALA','NEAR','FIL','ICP','NEO',
    'XLM','XMR','ZEC','HBAR','FLOW','ONE','ROSE','KAVA','FTM','MAGIC'
  ],

  CORR_GROUPS: {
    LARGE:   ['BTC','ETH','BNB'],
    LAYER1:  ['SOL','AVAX','NEAR','APT','SUI','ATOM','TIA','SEI','ALGO','TON'],
    DEFI:    ['UNI','ARB','OP','INJ','PENDLE','GMX','LDO','COMP','SNX','AAVE','CRV','DYDX'],
    MEME:    ['DOGE','XRP','ADA','WIF','ENA'],
    INFRA:   ['SAND','AXS','GALA','IMX','BLUR','FET','RNDR','GRT'],
    ALT:     ['LTC','BCH','XLM','XMR','ZEC','HBAR','FIL','ICP'],
  },
};
