# Finance with Fiancée — backend
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
try:
    from plaid.model.link_token_create_request_transactions import LinkTokenCreateRequestTransactions
    HAS_TRANSACTIONS_CONFIG = True
except ImportError:
    HAS_TRANSACTIONS_CONFIG = False

# ── 启动诊断日志 ──────────────────────────────────────────
import pkg_resources as _pkg
try:
    _plaid_ver = _pkg.get_distribution("plaid-python").version
except Exception:
    _plaid_ver = "unknown"
print(f"[DIAG] plaid-python version  : {_plaid_ver}")
print(f"[DIAG] HAS_TRANSACTIONS_CONFIG: {HAS_TRANSACTIONS_CONFIG}")
print(f"[DIAG] days_requested         : {'730' if HAS_TRANSACTIONS_CONFIG else 'NOT ACTIVE - only 90 days'}")
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
# DATA_DIR 优先使用环境变量（Railway Volume 挂载路径），保证 redeploy 不丢数据
# 在 Railway 上：Settings → Volumes → Mount Path 设为 /data
# 然后设环境变量 DATA_DIR=/data
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
                owner        TEXT NOT NULL DEFAULT 'me',
                cursor       TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id             TEXT PRIMARY KEY,
                date           TEXT NOT NULL,
                name           TEXT NOT NULL,
                amount         REAL NOT NULL,
                account        TEXT NOT NULL,
                payer          TEXT NOT NULL DEFAULT 'me',
                plaid_category TEXT,
                auto_category  TEXT,
                category       TEXT,
                split          TEXT,
                categorized    INTEGER DEFAULT 0
            );
        """)
        for col, tbl, default in [
            ('owner', 'accounts', "'me'"),
            ('payer', 'transactions', "'me'"),
        ]:
            try:
                db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            except Exception:
                pass

# ── 商家名 → 分类（长优先匹配，比 Plaid 类别更准） ─────────
MERCHANT_NAME_MAP = [
    ('amazon prime video', 'subscription'),
    ('amazon prime',       'subscription'),
    ('amazon music',       'subscription'),
    ('amazon web service', 'subscription'),
    ('amazon.com',         'amazon'),
    ('amazon',             'amazon'),
    ('amzn mktp',          'amazon'),
    ('amzn',               'amazon'),
    ('uber eats',          'food'),
    ('ubereats',           'food'),
    ('doordash',           'food'),
    ('grubhub',            'food'),
    ('postmates',          'food'),
    ('instacart',          'grocery'),
    ('whole foods',        'grocery'),
    ('trader joe',         'grocery'),
    ('safeway',            'grocery'),
    ('albertsons',         'grocery'),
    ('kroger',             'grocery'),
    ('publix',             'grocery'),
    ('sprouts',            'grocery'),
    ('aldi',               'grocery'),
    ('ralphs',             'grocery'),
    ('vons',               'grocery'),
    ('wegmans',            'grocery'),
    ('stop & shop',        'grocery'),
    ('fred meyer',         'grocery'),
    ('heb',                'grocery'),
    ('costco',             'grocery'),
    ('walmart',            'grocery'),
    ('target',             'grocery'),
    ('starbucks',          'food'),
    ('dunkin',             'food'),
    ('dutch bros',         'food'),
    ("peet's",             'food'),
    ('mcdonald',           'food'),
    ('burger king',        'food'),
    ('wendy',              'food'),
    ('subway',             'food'),
    ('chipotle',           'food'),
    ('taco bell',          'food'),
    ('chick-fil-a',        'food'),
    ('chick fil a',        'food'),
    ('chickfila',          'food'),
    ('domino',             'food'),
    ('pizza hut',          'food'),
    ('panera',             'food'),
    ('five guys',          'food'),
    ('shake shack',        'food'),
    ('in-n-out',           'food'),
    ('in n out',           'food'),
    ('popeyes',            'food'),
    ('kfc',                'food'),
    ('panda express',      'food'),
    ('olive garden',       'food'),
    ('applebee',           'food'),
    ("denny's",            'food'),
    ('dennys',             'food'),
    ('ihop',               'food'),
    ('cheesecake factory', 'food'),
    ('sweetgreen',         'food'),
    ('cava',               'food'),
    ('jersey mike',        'food'),
    ('jimmy john',         'food'),
    ('uber',               'transport'),
    ('lyft',               'transport'),
    ('didi',               'transport'),
    ('bird scooter',       'transport'),
    ('lime',               'transport'),
    ('caltrain',           'transport'),
    ('zipcar',             'transport'),
    ('enterprise rent',    'transport'),
    ('hertz',              'transport'),
    ('avis',               'transport'),
    ('budget car',         'transport'),
    ('united airlines',    'travel'),
    ('american airlines',  'travel'),
    ('alaska airlines',    'travel'),
    ('spirit airlines',    'travel'),
    ('frontier airlines',  'travel'),
    ('southwest airlines', 'travel'),
    ('jetblue',            'travel'),
    ('delta air',          'travel'),
    ('southwest',          'travel'),
    ('airbnb',             'travel'),
    ('marriott',           'travel'),
    ('hilton',             'travel'),
    ('hyatt',              'travel'),
    ('sheraton',           'travel'),
    ('westin',             'travel'),
    ('holiday inn',        'travel'),
    ('expedia',            'travel'),
    ('booking.com',        'travel'),
    ('hotels.com',         'travel'),
    ('amtrak',             'travel'),
    ('home depot',         'home'),
    ("lowe's",             'home'),
    ('lowes',              'home'),
    ('ikea',               'home'),
    ('wayfair',            'home'),
    ('bed bath',           'home'),
    ('williams-sonoma',    'home'),
    ('crate and barrel',   'home'),
    ('west elm',           'home'),
    ('apple store',        'shopping'),
    ('apple.com',          'shopping'),
    ('best buy',           'shopping'),
    ('nordstrom',          'shopping'),
    ("macy's",             'shopping'),
    ('macys',              'shopping'),
    ('zara',               'shopping'),
    ('h&m',                'shopping'),
    ('old navy',           'shopping'),
    ('banana republic',    'shopping'),
    ('tj maxx',            'shopping'),
    ('marshalls',          'shopping'),
    ('nike',               'shopping'),
    ('adidas',             'shopping'),
    ('uniqlo',             'shopping'),
    ('netflix',            'subscription'),
    ('spotify',            'subscription'),
    ('hulu',               'subscription'),
    ('disney+',            'subscription'),
    ('disney plus',        'subscription'),
    ('disneyplus',         'subscription'),
    ('apple music',        'subscription'),
    ('youtube premium',    'subscription'),
    ('hbo max',            'subscription'),
    ('hbo',                'subscription'),
    ('peacock',            'subscription'),
    ('paramount+',         'subscription'),
    ('twitch',             'subscription'),
    ('adobe',              'subscription'),
    ('microsoft 365',      'subscription'),
    ('google one',         'subscription'),
    ('icloud',             'subscription'),
    ('dropbox',            'subscription'),
    ('at&t',               'subscription'),
    ('verizon',            'subscription'),
    ('t-mobile',           'subscription'),
    ('comcast',            'subscription'),
    ('xfinity',            'subscription'),
    ('spectrum',           'subscription'),
    ('cvs',                'health'),
    ('walgreens',          'health'),
    ('rite aid',           'health'),
    ('one medical',        'health'),
    ('kaiser',             'health'),
    ('adorama',            'photo'),
    ('b&h photo',          'photo'),
    ('bhphotovideo',       'photo'),
]

PLAID_CATEGORY_MAP = {
    'restaurants':                'food',
    'fast food':                  'food',
    'food and drink':             'food',
    'dining':                     'food',
    'coffee shop':                'food',
    'coffee':                     'food',
    'groceries':                  'grocery',
    'supermarkets and groceries': 'grocery',
    'grocery':                    'grocery',
    'veterinarians':              'cat',
    'pets':                       'cat',
    'pet supplies':               'cat',
    'transportation':             'transport',
    'taxi':                       'transport',
    'ride share':                 'transport',
    'car service':                'transport',
    'public transportation':      'transport',
    'gas stations':               'transport',
    'parking':                    'transport',
    'airlines and aviation':      'travel',
    'travel':                     'travel',
    'hotels and motels':          'travel',
    'lodging':                    'travel',
    'shops':                      'shopping',
    'shopping':                   'shopping',
    'clothing and accessories':   'shopping',
    'electronics':                'shopping',
    'department stores':          'shopping',
    'arts and entertainment':     'entertainment',
    'recreation':                 'entertainment',
    'gyms and fitness centers':   'entertainment',
    'sport':                      'entertainment',
    'games':                      'entertainment',
    'movies and dvds':            'entertainment',
    'healthcare':                 'health',
    'pharmacies':                 'health',
    'hospitals':                  'health',
    'dentists':                   'health',
    'doctors':                    'health',
    'home improvement':           'home',
    'furniture':                  'home',
    'utilities':                  'home',
    'subscription':               'subscription',
    'digital purchase':           'subscription',
    'software':                   'subscription',
    'cable':                      'subscription',
    'internet services':          'subscription',
    'photography':                'photo',
    'camera':                     'photo',
}

def auto_classify(plaid_categories: List[str], merchant_name: str = '') -> Optional[str]:
    name_lower = merchant_name.lower()
    for pattern, cat in MERCHANT_NAME_MAP:
        if pattern in name_lower:
            return cat
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
        req_kwargs = dict(
            products=[Products("transactions")],
            client_name="Finance with Fiancée",
            country_codes=[CountryCode('US')],
            language='en',
            user=LinkTokenCreateRequestUser(client_user_id='user-1'),
            redirect_uri="https://my-finance-app-production-39aa.up.railway.app",
        )
        if HAS_TRANSACTIONS_CONFIG:
            req_kwargs['transactions'] = LinkTokenCreateRequestTransactions(days_requested=730)
        req = LinkTokenCreateRequest(**req_kwargs)
        token = client.link_token_create(req)['link_token']
        with open(os.path.join(DATA_DIR, 'link_token.txt'), 'w') as f:
            f.write(token)
        return jsonify({'link_token': token})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400

@app.route('/api/get_link_token', methods=['GET'])
def get_link_token():
    try:
        with open(os.path.join(DATA_DIR, 'link_token.txt'), 'r') as f:
            token = f.read().strip()
        return jsonify({'link_token': token})
    except Exception:
        return jsonify({'error': 'No token found'}), 404

@app.route('/api/exchange_token', methods=['POST'])
def exchange_token():
    public_token = request.json['public_token']
    account_name = request.json.get('account_name', '账户')
    owner        = request.json.get('owner', 'me')
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
                        plaid_cats    = txn.get('category') or []
                        # Skip credit card bill payments & internal bank transfers
                        # Plaid labels these ["Transfer","Credit Card"] or ["Payment","Credit Card"]
                        if 'credit card' in [c.lower() for c in plaid_cats]:
                            continue
                        merchant_name = txn['name']
                        if not db.execute("SELECT id FROM transactions WHERE id=?",
                                          (txn['transaction_id'],)).fetchone():
                            db.execute(
                                """INSERT INTO transactions
                                   (id,date,name,amount,account,payer,plaid_category,auto_category,
                                    category,split,categorized)
                                   VALUES (?,?,?,?,?,?,?,?,NULL,NULL,0)""",
                                (txn['transaction_id'], str(txn['date']), merchant_name,
                                 amount, account['name'], account['owner'],
                                 json.dumps(plaid_cats),
                                 auto_classify(plaid_cats, merchant_name))
                            )
                            new_count += 1
                    has_more = resp['has_more']
                    cursor   = resp['next_cursor']
                db.execute("UPDATE accounts SET cursor=? WHERE item_id=?",
                           (cursor, account['item_id']))
            except plaid.ApiException as e:
                errors.append(str(e))
    return jsonify({'new_transactions': new_count, 'errors': errors})

@app.route('/api/delete_account', methods=['POST'])
def delete_account():
    """删除指定账户（及其所有未分类交易），以便重新连接获取完整历史。
    已分类的交易保留不受影响。"""
    item_id = request.json.get('item_id')
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400
    with get_db() as db:
        account = db.execute("SELECT name FROM accounts WHERE item_id=?", (item_id,)).fetchone()
        if not account:
            return jsonify({'error': 'Account not found'}), 404
        account_name = account['name']
        deleted = db.execute(
            "DELETE FROM transactions WHERE account=? AND categorized=0", (account_name,)
        ).rowcount
        db.execute("DELETE FROM accounts WHERE item_id=?", (item_id,))
    return jsonify({'success': True, 'account': account_name, 'deleted_pending': deleted})

@app.route('/api/reset_cursors', methods=['POST'])
def reset_cursors():
    """重置所有账户的同步游标，下次同步将重新拉取全部历史交易。
    如果发现交易数量异常少（只有100多条），执行此操作后再同步即可。"""
    with get_db() as db:
        db.execute("UPDATE accounts SET cursor=NULL")
    return jsonify({'success': True, 'message': '游标已重置，请重新同步以获取完整历史记录'})

@app.route('/api/reclassify', methods=['POST'])
def reclassify_all():
    """对所有未分类交易重新运行自动分类（升级规则后调用）。"""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, plaid_category FROM transactions WHERE categorized=0"
        ).fetchall()
        count = 0
        for row in rows:
            plaid_cats = json.loads(row['plaid_category'] or '[]')
            new_cat = auto_classify(plaid_cats, row['name'])
            db.execute("UPDATE transactions SET auto_category=? WHERE id=?",
                       (new_cat, row['id']))
            if new_cat:
                count += 1
    return jsonify({'updated': count})

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    show_all = request.args.get('all', 'false') == 'true'
    month    = request.args.get('month')
    category = request.args.get('category')
    with get_db() as db:
        conditions, params = [], []
        if not show_all:
            conditions.append("categorized=0")
        if month:
            conditions.append("date LIKE ?")
            params.append(month + '%')
        if category:
            conditions.append("category=?")
            params.append(category)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = db.execute(
            f"SELECT * FROM transactions {where} ORDER BY date DESC", params
        ).fetchall()
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

@app.route('/api/uncategorize', methods=['POST'])
def uncategorize():
    """撤回上一笔：把已分类的交易恢复为未分类状态。"""
    txn_id = request.json.get('id')
    with get_db() as db:
        db.execute(
            "UPDATE transactions SET split=NULL, category=NULL, categorized=0 WHERE id=?",
            (txn_id,)
        )
    return jsonify({'success': True})

@app.route('/api/months', methods=['GET'])
def get_months():
    """返回有交易记录的月份列表，包含每月的完成/待办统计。"""
    with get_db() as db:
        rows = db.execute("""
            SELECT
                substr(date,1,7)    AS month,
                COUNT(*)            AS total,
                SUM(categorized)    AS done,
                COUNT(*) - SUM(categorized) AS pending
            FROM transactions
            GROUP BY month
            ORDER BY month DESC
        """).fetchall()
        # 全部待分类数量（用于 recents 行）
        total_pending = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE categorized=0"
        ).fetchone()[0]
    months = [dict(r) for r in rows]
    return jsonify({'months': months, 'total_pending': total_pending})

@app.route('/api/progress', methods=['GET'])
def progress():
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    with get_db() as db:
        rows = db.execute(
            """SELECT payer, categorized, COUNT(*) as cnt
               FROM transactions WHERE date LIKE ?
               GROUP BY payer, categorized""",
            (month + '%',)
        ).fetchall()
    me_done = me_pending = partner_done = partner_pending = 0
    for row in rows:
        if row['payer'] == 'me':
            if row['categorized']: me_done    += row['cnt']
            else:                  me_pending += row['cnt']
        else:
            if row['categorized']: partner_done    += row['cnt']
            else:                  partner_pending += row['cnt']
    me_total      = me_done + me_pending
    partner_total = partner_done + partner_pending
    return jsonify({
        'month': month,
        'me':      {'done': me_done,      'pending': me_pending,      'total': me_total},
        'partner': {'done': partner_done, 'pending': partner_pending, 'total': partner_total},
        'both_cleared': (me_pending == 0 and partner_pending == 0
                         and (me_total + partner_total) > 0),
    })

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
    # split values: 'mine' = payer's own cost, 'shared' = split equally, 'theirs' = other person's cost
    me_own      = sum(t['amount'] for t in txns if
                      (t['payer']=='me'      and t['split']=='mine') or
                      (t['payer']=='partner' and t['split']=='theirs'))
    partner_own = sum(t['amount'] for t in txns if
                      (t['payer']=='partner' and t['split']=='mine') or
                      (t['payer']=='me'      and t['split']=='theirs'))
    me_shared_paid      = sum(t['amount'] for t in txns if t['payer']=='me'      and t['split']=='shared')
    partner_shared_paid = sum(t['amount'] for t in txns if t['payer']=='partner' and t['split']=='shared')
    total_shared        = me_shared_paid + partner_shared_paid
    # Cross-payments: I paid for partner's expense (or vice versa) → affects net balance
    me_paid_for_partner = sum(t['amount'] for t in txns if t['payer']=='me'      and t['split']=='theirs')
    partner_paid_for_me = sum(t['amount'] for t in txns if t['payer']=='partner' and t['split']=='theirs')
    net_balance = ((me_shared_paid - partner_shared_paid) * split_ratio
                   + me_paid_for_partner - partner_paid_for_me)
    by_category: dict = {}
    for t in txns:
        cat = t.get('category') or 'other'
        if cat not in by_category:
            by_category[cat] = {'me': 0.0, 'partner': 0.0, 'shared': 0.0}
        s, p, amt = t['split'], t['payer'], t['amount']
        if   s == 'mine'   and p == 'me':      by_category[cat]['me']      += amt
        elif s == 'mine'   and p == 'partner': by_category[cat]['partner'] += amt
        elif s == 'theirs' and p == 'me':      by_category[cat]['partner'] += amt
        elif s == 'theirs' and p == 'partner': by_category[cat]['me']      += amt
        else:                                  by_category[cat]['shared']  += amt
    return jsonify({
        'month': month, 'me_own': round(me_own,2), 'partner_own': round(partner_own,2),
        'me_shared_paid': round(me_shared_paid,2), 'partner_shared_paid': round(partner_shared_paid,2),
        'total_shared': round(total_shared,2), 'net_balance': round(net_balance,2),
        'combined_total': round(me_own+partner_own+total_shared,2),
        'by_category': by_category, 'transaction_count': len(txns),
    })

@app.route('/api/debug', methods=['GET'])
def debug():
    import pkg_resources
    try:
        plaid_version = pkg_resources.get_distribution("plaid-python").version
    except Exception:
        plaid_version = "unknown"
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        earliest = db.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
        latest   = db.execute("SELECT MAX(date) FROM transactions").fetchone()[0]
    return jsonify({
        'plaid_version': plaid_version,
        'has_transactions_config': HAS_TRANSACTIONS_CONFIG,
        'days_requested': 730 if HAS_TRANSACTIONS_CONFIG else '未启用（只有90天）',
        'transaction_count': count,
        'earliest_transaction': earliest,
        'latest_transaction': latest,
    })

@app.route('/api/account_stats', methods=['GET'])
def account_stats():
    """每个账户每月的交易笔数，用于账户页面的月度统计。"""
    with get_db() as db:
        rows = db.execute("""
            SELECT account, substr(date,1,7) AS month,
                   COUNT(*) AS total, SUM(categorized) AS done
            FROM transactions
            GROUP BY account, month
            ORDER BY account, month DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n💑  Finance with Fiancée — http://localhost:{port}\n")
    print(f"    数据库位置: {DB_PATH}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
