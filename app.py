# Finance with Fiancée — backend
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
import json, os, sqlite3
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, List

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Plaid ──────────────────────────────────────────────────
PLAID_CLIENT_ID = "6a139ca06fec6d000d3d83a3"
PLAID_SECRET    = os.environ.get("PLAID_SECRET", "21d24cef5f1f77e0f83049aaffba65")

configuration = plaid.Configuration(
    host="https://production.plaid.com",
    api_key={'clientId': PLAID_CLIENT_ID, 'secret': PLAID_SECRET}
)
api_client = plaid.ApiClient(configuration)
client      = plaid_api.PlaidApi(api_client)

# ── SQLite ─────────────────────────────────────────────────
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
                owner        TEXT NOT NULL DEFAULT 'me',  -- 'me' | 'partner'
                cursor       TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id             TEXT PRIMARY KEY,
                date           TEXT NOT NULL,
                name           TEXT NOT NULL,
                amount         REAL NOT NULL,
                account        TEXT NOT NULL,
                payer          TEXT NOT NULL DEFAULT 'me', -- 'me' | 'partner'
                plaid_category TEXT,
                auto_category  TEXT,
                category       TEXT,
                split          TEXT,      -- 'mine' | 'shared' | 'partner'
                categorized    INTEGER DEFAULT 0
            );
        """)
        # migrate: add owner/payer columns if upgrading from old schema
        for col, tbl, default in [
            ('owner', 'accounts', "'me'"),
            ('payer', 'transactions', "'me'"),
        ]:
            try:
                db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            except Exception:
                pass

# ── Auto-classify ──────────────────────────────────────────
PLAID_CATEGORY_MAP = {
    'restaurants':'food','fast food':'food','food and drink':'food','dining':'food',
    'coffee shop':'coffee','coffee':'coffee',
    'groceries':'grocery','supermarkets and groceries':'grocery','grocery':'grocery',
    'veterinarians':'cat','pets':'cat','pet supplies':'cat',
    'transportation':'transport','taxi':'transport','ride share':'transport',
    'car service':'transport','public transportation':'transport',
    'gas stations':'transport','parking':'transport',
    'airlines and aviation':'travel','travel':'travel',
    'hotels and motels':'travel','lodging':'travel',
    'shops':'shopping','shopping':'shopping','clothing and accessories':'shopping',
    'electronics':'shopping','department stores':'shopping',
    'arts and entertainment':'entertainment','recreation':'entertainment',
    'gyms and fitness centers':'entertainment','sport':'entertainment',
    'games':'entertainment','movies and dvds':'entertainment',
    'healthcare':'health','pharmacies':'health','hospitals':'health',
    'dentists':'health','doctors':'health',
    'home improvement':'home','furniture':'home','utilities':'home',
    'subscription':'subscription','digital purchase':'subscription',
    'software':'subscription','cable':'subscription','internet services':'subscription',
    'photography':'photo','camera':'photo',
}

def auto_classify(plaid_categories: List[str]) -> Optional[str]:
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
    d['plaid_category'] = json.loads(d.get('plaid_category') or '[]')
    d['categorized']    = bool(d['categorized'])
    return d

# ── Routes ─────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/apple-touch-icon.png')
def apple_icon():
    return send_from_directory('.', 'apple-touch-icon.png')

@app.route('/api/create_link_token', methods=['POST'])
def create_link_token():
    try:
        req = LinkTokenCreateRequest(
            products=[Products("transactions")],
            client_name="Finance with Fiancée",
            country_codes=[CountryCode('US')],
            language='en',
            user=LinkTokenCreateRequestUser(client_user_id='user-1'),
            redirect_uri="https://my-finance-app-production-39aa.up.railway.app"
        )
        return jsonify({'link_token': client.link_token_create(req)['link_token']})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400

@app.route('/api/exchange_token', methods=['POST'])
def exchange_token():
    public_token = request.json['public_token']
    account_name = request.json.get('account_name', '账户')
    owner        = request.json.get('owner', 'me')   # 'me' | 'partner'
    try:
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO accounts (item_id, access_token, name, owner, cursor) VALUES (?,?,?,?,?)",
                (resp['item_id'], resp['access_token'], account_name, owner, None)
            )
        return jsonify({'success': True})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    with get_db() as db:
        rows = db.execute("SELECT item_id, name, owner FROM accounts").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sync', methods=['POST'])
def sync_transactions():
    new_count, errors = 0, []
    with get_db() as db:
        accounts = db.execute("SELECT * FROM accounts").fetchall()
        for account in accounts:
            try:
                cursor, has_more = account['cursor'], True
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
                        if not db.execute("SELECT id FROM transactions WHERE id=?",
                                          (txn['transaction_id'],)).fetchone():
                            db.execute(
                                """INSERT INTO transactions
                                   (id,date,name,amount,account,payer,plaid_category,auto_category,
                                    category,split,categorized)
                                   VALUES (?,?,?,?,?,?,?,?,NULL,NULL,0)""",
                                (txn['transaction_id'], str(txn['date']), txn['name'],
                                 amount, account['name'], account['owner'],
                                 json.dumps(plaid_cats), auto_classify(plaid_cats))
                            )
                            new_count += 1
                    has_more = resp['has_more']
                    cursor   = resp['next_cursor']
                db.execute("UPDATE accounts SET cursor=? WHERE item_id=?",
                           (cursor, account['item_id']))
            except plaid.ApiException as e:
                errors.append(str(e))
    return jsonify({'new_transactions': new_count, 'errors': errors})

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    show_all = request.args.get('all', 'false') == 'true'
    with get_db() as db:
        q = "SELECT * FROM transactions" + ("" if show_all else " WHERE categorized=0")
        rows = db.execute(q + " ORDER BY date DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/categorize', methods=['POST'])
def categorize():
    data = request.json
    with get_db() as db:
        updated = db.execute(
            "UPDATE transactions SET split=?, category=?, categorized=1 WHERE id=?",
            (data.get('split'), data.get('category'), data['id'])
        ).rowcount
    return jsonify({'success': True}) if updated else (jsonify({'error': 'Not found'}), 404)

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

    # ── individual spending ──────────────────────────────
    me_own             = sum(t['amount'] for t in txns if t['payer']=='me'      and t['split']=='mine')
    partner_own        = sum(t['amount'] for t in txns if t['payer']=='partner' and t['split']=='mine')
    me_shared_paid     = sum(t['amount'] for t in txns if t['payer']=='me'      and t['split']=='shared')
    partner_shared_paid= sum(t['amount'] for t in txns if t['payer']=='partner' and t['split']=='shared')
    total_shared       = me_shared_paid + partner_shared_paid

    # net balance: positive → partner owes me; negative → I owe partner
    net_balance = (me_shared_paid - partner_shared_paid) * split_ratio

    # ── category breakdown ───────────────────────────────
    by_category: dict = {}
    for t in txns:
        cat = t.get('category') or 'other'
        if cat not in by_category:
            by_category[cat] = {'me': 0.0, 'partner': 0.0, 'shared': 0.0}
        if t['split'] == 'mine' and t['payer'] == 'me':
            by_category[cat]['me']      += t['amount']
        elif t['split'] == 'mine' and t['payer'] == 'partner':
            by_category[cat]['partner'] += t['amount']
        else:
            by_category[cat]['shared']  += t['amount']

    return jsonify({
        'month':               month,
        'me_own':              round(me_own, 2),
        'partner_own':         round(partner_own, 2),
        'me_shared_paid':      round(me_shared_paid, 2),
        'partner_shared_paid': round(partner_shared_paid, 2),
        'total_shared':        round(total_shared, 2),
        'net_balance':         round(net_balance, 2),  # + partner owes me / − I owe partner
        'combined_total':      round(me_own + partner_own + total_shared, 2),
        'by_category':         by_category,
        'transaction_count':   len(txns),
    })

if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n💑  Finance with Fiancée — http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
