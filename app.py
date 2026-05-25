from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
import json
import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Plaid 配置 ──────────────────────────────────────────────
PLAID_CLIENT_ID = "6a139ca06fec6d000d3d83a3"
PLAID_SECRET    = os.environ.get("PLAID_SECRET", "21d24cef5f1f77e0f83049aaffba65")

configuration = plaid.Configuration(
    host=plaid.Environment.Development,
    api_key={
        'clientId': PLAID_CLIENT_ID,
        'secret':   PLAID_SECRET,
    }
)
api_client = plaid.ApiClient(configuration)
client = plaid_api.PlaidApi(api_client)

# ── SQLite 数据库 ────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(DATA_DIR, "finance.db")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                item_id      TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                name         TEXT NOT NULL,
                cursor       TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id             TEXT PRIMARY KEY,
                date           TEXT NOT NULL,
                name           TEXT NOT NULL,
                amount         REAL NOT NULL,
                account        TEXT NOT NULL,
                plaid_category TEXT,      -- JSON array stored as string
                auto_category  TEXT,
                category       TEXT,
                split          TEXT,      -- 'mine' | 'shared'
                categorized    INTEGER DEFAULT 0
            );
        """)

# ── Plaid 类别 → 自定义类别 自动映射 ──────────────────────────
PLAID_CATEGORY_MAP = {
    'restaurants':                  'food',
    'fast food':                    'food',
    'food and drink':               'food',
    'dining':                       'food',
    'coffee shop':                  'coffee',
    'coffee':                       'coffee',
    'groceries':                    'grocery',
    'supermarkets and groceries':   'grocery',
    'grocery':                      'grocery',
    'veterinarians':                'cat',
    'pets':                         'cat',
    'pet supplies':                 'cat',
    'transportation':               'transport',
    'taxi':                         'transport',
    'ride share':                   'transport',
    'car service':                  'transport',
    'public transportation':        'transport',
    'gas stations':                 'transport',
    'parking':                      'transport',
    'airlines and aviation':        'travel',
    'travel':                       'travel',
    'hotels and motels':            'travel',
    'lodging':                      'travel',
    'shops':                        'shopping',
    'shopping':                     'shopping',
    'clothing and accessories':     'shopping',
    'electronics':                  'shopping',
    'department stores':            'shopping',
    'arts and entertainment':       'entertainment',
    'recreation':                   'entertainment',
    'gyms and fitness centers':     'entertainment',
    'sport':                        'entertainment',
    'games':                        'entertainment',
    'movies and dvds':              'entertainment',
    'healthcare':                   'health',
    'pharmacies':                   'health',
    'hospitals':                    'health',
    'dentists':                     'health',
    'doctors':                      'health',
    'home improvement':             'home',
    'furniture':                    'home',
    'utilities':                    'home',
    'subscription':                 'subscription',
    'digital purchase':             'subscription',
    'software':                     'subscription',
    'cable':                        'subscription',
    'internet services':            'subscription',
    'photography':                  'photo',
    'camera':                       'photo',
}

def auto_classify(plaid_categories: list) -> str | None:
    for raw in reversed(plaid_categories):
        key = raw.lower().strip()
        if key in PLAID_CATEGORY_MAP:
            return PLAID_CATEGORY_MAP[key]
        for pattern, cat in PLAID_CATEGORY_MAP.items():
            if pattern in key or key in pattern:
                return cat
    return None

def row_to_dict(row):
    d = dict(row)
    d['plaid_category'] = json.loads(d['plaid_category'] or '[]')
    d['categorized']    = bool(d['categorized'])
    return d

# ── 页面 ───────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# ── Plaid Link：创建 link token ────────────────────────────
@app.route('/api/create_link_token', methods=['POST'])
def create_link_token():
    try:
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            client_name="我的记账本",
            country_codes=[CountryCode('US')],
            language='en',
            user=LinkTokenCreateRequestUser(client_user_id='user-1')
        )
        response = client.link_token_create(req)
        return jsonify({'link_token': response['link_token']})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400

# ── Plaid Link：交换 access token ─────────────────────────
@app.route('/api/exchange_token', methods=['POST'])
def exchange_token():
    public_token = request.json['public_token']
    account_name = request.json.get('account_name', '我的账户')
    try:
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO accounts (item_id, access_token, name, cursor) VALUES (?,?,?,?)",
                (resp['item_id'], resp['access_token'], account_name, None)
            )
        return jsonify({'success': True})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400

# ── 查看已连接账户 ─────────────────────────────────────────
@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    with get_db() as db:
        rows = db.execute("SELECT item_id, name FROM accounts").fetchall()
    return jsonify([dict(r) for r in rows])

# ── 同步新交易 ─────────────────────────────────────────────
@app.route('/api/sync', methods=['POST'])
def sync_transactions():
    new_count = 0
    errors    = []

    with get_db() as db:
        accounts = db.execute("SELECT * FROM accounts").fetchall()

        for account in accounts:
            try:
                cursor   = account['cursor']
                has_more = True

                while has_more:
                    kwargs = {'access_token': account['access_token']}
                    if cursor:
                        kwargs['cursor'] = cursor
                    resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))

                    for txn in resp['added']:
                        amount = txn['amount']
                        if amount <= 0:
                            continue
                        plaid_cats = txn.get('category') or []
                        existing = db.execute(
                            "SELECT id FROM transactions WHERE id=?", (txn['transaction_id'],)
                        ).fetchone()
                        if not existing:
                            db.execute(
                                """INSERT INTO transactions
                                   (id, date, name, amount, account, plaid_category, auto_category,
                                    category, split, categorized)
                                   VALUES (?,?,?,?,?,?,?,NULL,NULL,0)""",
                                (
                                    txn['transaction_id'],
                                    str(txn['date']),
                                    txn['name'],
                                    amount,
                                    account['name'],
                                    json.dumps(plaid_cats),
                                    auto_classify(plaid_cats),
                                )
                            )
                            new_count += 1

                    has_more = resp['has_more']
                    cursor   = resp['next_cursor']

                db.execute(
                    "UPDATE accounts SET cursor=? WHERE item_id=?",
                    (cursor, account['item_id'])
                )

            except plaid.ApiException as e:
                errors.append(str(e))

    return jsonify({'new_transactions': new_count, 'errors': errors})

# ── 获取待分类交易 ─────────────────────────────────────────
@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    show_all = request.args.get('all', 'false') == 'true'
    with get_db() as db:
        if show_all:
            rows = db.execute(
                "SELECT * FROM transactions ORDER BY date DESC"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM transactions WHERE categorized=0 ORDER BY date DESC"
            ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])

# ── 保存分类结果 ───────────────────────────────────────────
@app.route('/api/categorize', methods=['POST'])
def categorize():
    data = request.json
    with get_db() as db:
        updated = db.execute(
            "UPDATE transactions SET split=?, category=?, categorized=1 WHERE id=?",
            (data.get('split'), data.get('category'), data['id'])
        ).rowcount
    if updated:
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

# ── 月度报告 ───────────────────────────────────────────────
@app.route('/api/report', methods=['GET'])
def report():
    month       = request.args.get('month', datetime.now().strftime('%Y-%m'))
    split_ratio = float(request.args.get('ratio', 0.5))

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM transactions WHERE categorized=1 AND date LIKE ?",
            (month + '%',)
        ).fetchall()

    txns = [row_to_dict(r) for r in rows]

    mine_total   = sum(t['amount'] for t in txns if t['split'] == 'mine')
    shared_total = sum(t['amount'] for t in txns if t['split'] == 'shared')
    my_share     = shared_total * split_ratio

    by_category: dict = {}
    for t in txns:
        cat = t.get('category') or 'other'
        if cat not in by_category:
            by_category[cat] = {'mine': 0.0, 'shared': 0.0}
        if t['split'] == 'mine':
            by_category[cat]['mine']   += t['amount']
        else:
            by_category[cat]['shared'] += t['amount']

    return jsonify({
        'month':              month,
        'mine_total':         round(mine_total, 2),
        'shared_total':       round(shared_total, 2),
        'my_share_of_shared': round(my_share, 2),
        'grand_total':        round(mine_total + my_share, 2),
        'by_category':        by_category,
        'transaction_count':  len(txns),
    })

# ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀  记账本已启动！请在浏览器打开 http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
