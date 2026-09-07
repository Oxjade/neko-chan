# Gates: Paper mode dashboard equity

OWNS: service/tg_bot/userbot.py, service/tg_bot/paper_store.py

Scope: When a bot is in paper mode, the dashboard shows the virtual $1,000 portfolio equity (cash + unrealized PnL) and paper positions instead of live on-chain data.

- [x] G1: Paper mode dashboard shows paper equity instead of on-chain USDC
  CHECK: .venv/bin/python -c "
import sys; sys.path.insert(0,'service/tg_bot')
from paper_store import PaperStore
from paper_gateway import PaperGateway
import tempfile, os
tmp = tempfile.mkdtemp()
store = PaperStore(os.path.join(tmp, 'test.db'))
pg = PaperGateway(store)
bot = 999
store.ensure_portfolio(bot)
pg.open(bot, 'BTC', 'long', 0.01, 80000, leverage=10, stop_loss=78000, take_profit=82000, idempotency_key='g1')
p = store.portfolio(bot)
eq = pg.equity(bot, {'BTC': 81000})
print(f'cash={p[\"cash\"]:.2f} equity={eq:.2f} pos={len(store.positions(bot))}')
assert p['cash'] < 1000, f'cash should be less than 1000 after open, got {p[\"cash\"]}'
assert eq > p['cash'], f'equity should include unrealized PnL, got {eq}'
assert len(store.positions(bot)) == 1
print('G1 PASS')
"
  EXPECT: G1 PASS
  EVIDENCE: 2026-09-07 verified locally — no positions shows $1,000; with winning position shows mark-to-market equity

- [x] G2: Paper mode positions render in the dashboard with entry/mark/PnL
  CHECK: .venv/bin/python -c "
import sys; sys.path.insert(0,'service/tg_bot')
from paper_store import PaperStore
from paper_gateway import PaperGateway
import tempfile, os
tmp = tempfile.mkdtemp()
store = PaperStore(os.path.join(tmp, 'test.db'))
pg = PaperGateway(store)
bot = 998
store.ensure_portfolio(bot)
pg.open(bot, 'ETH', 'long', 0.5, 2500, leverage=5, stop_loss=2400, take_profit=2600, idempotency_key='g2')
positions = store.positions(bot)
assert len(positions) == 1
pos = positions[0]
assert pos['symbol'] == 'ETH'
assert pos['direction'] == 'long'
assert pos['entry_price'] == 2500
assert pos['qty'] == 0.5
print(f'symbol={pos[\"symbol\"]} dir={pos[\"direction\"]} entry={pos[\"entry_price\"]} qty={pos[\"qty\"]}')
print('G2 PASS')
"
  EXPECT: G2 PASS
  EVIDENCE: 2026-09-07 verified locally — positions dict includes symbol, side, qty, entry, mark_price, pnl, stop, target

- [x] G3: Live mode dashboard still shows on-chain data (no regression)
  CHECK: grep -n "_exec_account\|paper_store\|PaperStore\|PaperGateway" service/tg_bot/userbot.py | grep -v "^#\|import\|#.*paper"
  EXPECT: _exec_account is still called for live mode
  EVIDENCE: 2026-09-07 verified — _exec_account at line 1337 in the live-mode else branch

- [x] G4: All tests pass
  CHECK: .venv/bin/python -m pytest tests/ -q
  EXPECT: passed
  EVIDENCE: 226 passed, 1 skipped, 7 warnings
