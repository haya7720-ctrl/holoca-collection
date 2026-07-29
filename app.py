import streamlit.components.v1 as components
import streamlit as st
import pandas as pd
import time
import base64
import os
import unicodedata
import json
import re
from datetime import datetime
import scraper
import extra_streamlit_components as stx

# === Supabase連携用ライブラリのインポート ===
from supabase import create_client, Client

# ==========================================
# ページ全体の設定
# ==========================================
st.set_page_config(
    page_title="ホロカ専用カードコレクション", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==========================================
# ★ Supabaseの初期化と接続設定 ★
# ==========================================
@st.cache_resource
def init_connection() -> Client:
    # .streamlit/secrets.toml から鍵を読み込む
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

try:
    supabase = init_connection()
except Exception as e:
    st.error("データベース（Supabase）の接続に失敗しました。secrets.tomlの設定を確認してください。")
    st.stop()

# ==========================================
# 🍪 クッキーマネージャーの設定
# ==========================================
cookie_manager = stx.CookieManager(key="cookie_manager")

# 🌟 ここに追加：キーボードショートカット（Cキー）の誤爆を防止する
components.html(
    """
    <script>
    const doc = window.parent.document;
    doc.addEventListener('keydown', function(e) {
        // 検索バーなどで文字入力中の場合は邪魔しない
        const active = doc.activeElement;
        const isInput = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
        
        // Cキー単独押しの時だけ無効化する（Ctrl/Cmdとの同時押し＝コピーは許可）
        if (!isInput && (e.key === 'c' || e.key === 'C') && !e.ctrlKey && !e.metaKey) {
            e.stopPropagation();
            e.preventDefault();
        }
    }, true);
    </script>
    """,
    height=0,
    width=0,
)

# ==========================================
# 🔐 認証状態の確認
# ==========================================
if "user" not in st.session_state:
    st.session_state.user = None

access_token = cookie_manager.get(cookie="supabase_token")
if access_token and st.session_state.user is None:
    try:
        res = supabase.auth.get_user(access_token)
        st.session_state.user = res.user
    except Exception:
        pass

# ==========================================
# 👤 サイドバー上部のユーザー表示切替
# ==========================================
if st.session_state.user is None:
    st.sidebar.write("👤 ゲスト さん")
    if st.sidebar.button("ログイン / 新規登録", type="primary"):
        st.session_state.current_view = "login"
        st.query_params["view"] = "login"
        st.rerun()
else:
    display_name = st.session_state.user.user_metadata.get("display_name")
    if not display_name:
        display_name = st.session_state.user.email
    st.sidebar.write(f"👤 {display_name} さん")
    if st.sidebar.button("ログアウト"):
        supabase.auth.sign_out()
        st.session_state.user = None
        cookie_manager.delete("supabase_token")
        time.sleep(1)
        st.session_state.current_view = "all_cards"
        st.rerun()

# ==========================================
# ★ データベース（Supabase）との通信関数群 ★
# ==========================================

def sync_cards_to_db(local_cards):
    """CSVのカード情報をデータベース(cardsテーブル)に登録・更新する"""
    if not local_cards: return
    try:
        for card in local_cards:
            data = {
                "id": card["id"],
                "full_name": card["full_name"],
                "img_path": card.get("img_path", ""),
                "rarity": card.get("rarity", ""),
                "yuyutei_url": card.get("yuyutei_url", ""),
                "fullahead_url": card.get("fullahead_url", "")
            }
            supabase.table("cards").upsert(data).execute()
    except Exception as e:
        st.sidebar.error(f"カード情報の同期エラー: {e}")

def fetch_collection_from_db():
    if st.session_state.user is None: return {}, []
    """データベース(collectionテーブル)からログイン中ユーザーの所持枚数とお気に入りを取得する"""
    try:
        uid = st.session_state.user.id
        response = supabase.table("collection").select("*").eq("user_id", uid).execute()
        
        collection_dict = {}
        favorites_list = []
        
        for row in response.data:
            cid = row["card_id"]
            if row.get("owned_count", 0) > 0:
                collection_dict[cid] = row["owned_count"]
            if row.get("is_favorite", False):
                favorites_list.append(cid)
                
        return collection_dict, favorites_list
    except Exception as e:
        st.error(f"データ取得エラー: {e}")
        return {}, []

def update_collection_in_db(card_id, owned_count=None, is_favorite=None):
    if st.session_state.user is None: return False
    """特定のカードの所持枚数またはお気に入り状態をデータベースに保存する"""
    try:
        uid = st.session_state.user.id
        res = supabase.table("collection").select("*").eq("card_id", card_id).eq("user_id", uid).execute()
        current_data = res.data[0] if res.data else {"card_id": card_id, "user_id": uid, "owned_count": 0, "is_favorite": False}
        
        new_data = {
            "user_id": uid,
            "card_id": card_id,
            "owned_count": owned_count if owned_count is not None else current_data.get("owned_count", 0),
            "is_favorite": is_favorite if is_favorite is not None else current_data.get("is_favorite", False),
            "updated_at": datetime.now().isoformat()
        }
        
        supabase.table("collection").upsert(new_data).execute()
        return True
    except Exception as e:
        st.error(f"保存エラー: {e}")
        return False

def fetch_prices_from_db():
    """データベース(pricesテーブル)から価格情報を取得する"""
    try:
        response = supabase.table("prices").select("*").execute()
        price_cache = {}
        for row in response.data:
            cid = row["card_id"]
            last_up = row.get("last_updated")
            if last_up:
                try:
                    dt = datetime.fromisoformat(last_up.replace('Z', '+00:00'))
                    last_up_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    last_up_str = str(last_up)
            else:
                last_up_str = "未取得"
                
            price_cache[cid] = {
                "last_updated": last_up_str,
                "遊々亭": {"sell": row.get("yuyutei_sell", "未取得"), "buy": row.get("yuyutei_buy", "未取得")},
                "CBトレコロ": {"sell": row.get("torecolo_sell", "未取得"), "buy": row.get("torecolo_buy", "未取得")},
                "フルアヘッド": {"sell": row.get("fullahead_sell", "未取得"), "buy": row.get("fullahead_buy", "未取得")}
            }
        return price_cache
    except Exception:
        return {}

def update_price_in_db(card_id, yuyu_s, yuyu_b, tore_s, tore_b, full_s, full_b):
    """特定のカードの最新価格をデータベースに保存する"""
    try:
        data = {
            "card_id": card_id,
            "last_updated": datetime.now().isoformat(),
            "yuyutei_sell": yuyu_s, "yuyutei_buy": yuyu_b,
            "torecolo_sell": tore_s, "torecolo_buy": tore_b,
            "fullahead_sell": full_s, "fullahead_buy": full_b
        }
        supabase.table("prices").upsert(data).execute()
        return True
    except Exception:
        return False

# ==========================================
# 状態管理（Session State）の初期化
# ==========================================
if "current_view" not in st.session_state:
    st.session_state.current_view = st.query_params.get("view", "all_cards")

# 🌟 追加：すでにログイン済みなのに「ログイン画面」にいる場合は、強制的にカード一覧へ移動
if st.session_state.user is not None and st.session_state.current_view == "login":
    st.session_state.current_view = "all_cards"
    st.query_params["view"] = "all_cards"

# 🌟 変更：アプリ起動時だけでなく、「ログイン・ログアウトで人が切り替わった時」にもデータを読み込み直す
current_uid = st.session_state.user.id if st.session_state.user else "guest"

if st.session_state.get("loaded_user_id") != current_uid:
    with st.spinner("コレクションデータを同期中..."):
        db_collection, db_favorites = fetch_collection_from_db()
        st.session_state.collection = db_collection
        st.session_state.favorites = db_favorites
        st.session_state.price_cache = fetch_prices_from_db()
        st.session_state.loaded_user_id = current_uid  # 誰のデータを読み込んだか記憶しておく

# ==========================================
# CSVファイルからカードデータベースを読み込む
# ==========================================
@st.cache_data
def load_card_database():
    if os.path.exists("cards.csv"):
        try:
            df = pd.read_csv("cards.csv", encoding="utf-8")
            df.columns = df.columns.str.strip().str.lower()
            required_columns = ['id', 'full_name', 'img_path']
            if not all(col in df.columns for col in required_columns):
                return []
            df = df.fillna("")
            cards = df.to_dict(orient="records")
            return cards
        except:
            return []
    return []

card_db = load_card_database()

if "cards_synced" not in st.session_state and card_db:
    sync_cards_to_db(card_db)
    st.session_state.cards_synced = True

# ==========================================
# 画像取得ヘルパー関数
# ==========================================
def get_image_b64(filename):
    if filename:
        target_path = os.path.join("images", filename)
        if os.path.exists(target_path):
            with open(target_path, "rb") as image_file:
                return f"data:image/jpeg;base64,{base64.b64encode(image_file.read()).decode()}"
        elif os.path.exists(filename):
            with open(filename, "rb") as image_file:
                return f"data:image/jpeg;base64,{base64.b64encode(image_file.read()).decode()}"
    
    svg_placeholder = '''
    <svg width="210" height="294" xmlns="http://www.w3.org/2000/svg">
        <rect width="100%" height="100%" fill="#f1f5f9" rx="6" ry="6"/>
        <text x="50%" y="50%" font-family="sans-serif" font-size="16" font-weight="bold" fill="#94a3b8" text-anchor="middle" dominant-baseline="middle">NO IMAGE</text>
    </svg>
    '''
    return f"data:image/svg+xml;base64,{base64.b64encode(svg_placeholder.encode()).decode()}"

# ==========================================
# CSS設定
# ==========================================
st.markdown(
    """
    <style>
    ::selection { background-color: #bae6fd !important; color: #000000 !important; }
    ::-moz-selection { background-color: #bae6fd !important; color: #000000 !important; }
    .stApp { background-color: #FFFFFF; color: #000000; }
    h1, h2, h3, h4, h5, h6, p, label { color: #000000 !important; }
    [data-testid="stMetricValue"], [data-testid="stMetricLabel"] { color: #000000 !important; }
    button[title="View fullscreen"], [data-testid="StyledFullScreenButton"] { display: none !important; }
    
    .stButton > button {
        background-color: #f0f2f6 !important; color: #000000 !important;
        border: 1px solid #dcdcdc !important; box-shadow: none !important;
        border-radius: 6px !important; transition: 0.2s;
    }
    .stButton > button p, .stButton > button div, .stButton > button span {
        color: #000000 !important; font-weight: bold !important;
    }
    .stButton > button:hover { background-color: #e2e8f0 !important; border-color: #94a3b8 !important; }

    .block-container, [data-testid="block-container"] {
        padding-top: 200px !important; padding-left: 30px !important; 
        padding-right: 30px !important; margin-left: 0 !important; max-width: 100% !important;  
    }
    div[data-testid="stHorizontalBlock"] { gap: 0.5rem !important; }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] { gap: 0px !important; }
    div[data-testid="column"] div.element-container { margin-bottom: 0px !important; }

    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] {
        display: flex !important; align-items: center !important; justify-content: center !important;
        gap: 8px !important; margin-top: 4px !important; margin-bottom: 0px !important;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
        flex: 0 0 32px !important; width: 32px !important; min-width: 32px !important; max-width: 32px !important;
        padding: 0 !important; display: flex !important; align-items: center !important; justify-content: center !important;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] {
        margin: 0 !important; padding: 0 !important; width: 100% !important;
        display: flex !important; align-items: center !important; justify-content: center !important;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button {
        width: 28px !important; height: 28px !important; min-height: 28px !important; max-height: 28px !important;
        padding: 0 !important; border-radius: 4px !important; font-size: 1.2rem !important;
        font-weight: bold !important; margin: 0 !important; line-height: 1 !important;
        display: flex !important; align-items: center !important; justify-content: center !important;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] div.element-container,
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] div[data-testid="stMarkdownContainer"] {
        margin: 0 !important; padding: 0 !important; width: 100% !important;
        display: flex !important; align-items: center !important; justify-content: center !important;
    }
    div[data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] p {
        margin: 0 !important; padding: 0 !important; font-size: 1.1rem !important;
        font-weight: bold !important; color: #1e293b !important; line-height: 1 !important;
    }

    [data-testid="stSidebar"] .stButton > button {
        width: 100% !important; text-align: left !important; background-color: transparent !important;
        border: none !important; padding: 10px 14px !important; font-weight: normal !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover { background-color: rgba(255, 255, 255, 0.5) !important; }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background-color: rgba(255, 255, 255, 0.75) !important; font-weight: bold !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
    }

    div[data-testid="stTable"] { width: fit-content !important; }
    div[data-testid="stTable"] > table { width: auto !important; }
    div[data-testid="stTable"] th, div[data-testid="stTable"] td {
        border-bottom: 1px solid #e2e8f0 !important; padding: 8px 24px !important; 
        text-align: center !important; white-space: nowrap !important; 
    }

    [data-testid="stSidebar"] {
        background: linear-gradient(to bottom, #ffffff 0%, #38bdf8 100%) !important;
        width: 18rem !important; min-width: 18rem !important; max-width: 18rem !important;
    }
    [data-testid="stSidebar"] ~ div, [data-testid="stResizableContainer"], section[data-testid="stSidebar"] + div { resize: none !important; }
    [data-testid="stSidebar"], [data-testid="stSidebar"] span, [data-testid="stSidebar"] div { color: #000000 !important; }
    header[data-testid="stHeader"] { background-color: transparent !important; }
    [data-testid="collapsedControl"], [data-testid="stSidebarCollapseButton"] { display: none !important; }

    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) {
        position: fixed !important; top: 12px !important; left: calc(18rem + 30px) !important; 
        width: 420px !important; z-index: 9999999 !important; 
    }
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) > div, 
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) div[data-baseweb="base-input"], 
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) div[data-baseweb="input"] {
        min-height: 54px !important; height: 54px !important;
        background-color: transparent !important; display: flex !important; align-items: center !important;
    }
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) input {
        background-color: #FFFFFF !important; border: 1px solid #cbd5e1 !important; border-radius: 6px !important;
        min-height: 54px !important; height: 54px !important; padding: 0 14px 0 44px !important;
        font-size: 1rem !important; color: #1e293b !important; box-shadow: none !important;
        line-height: normal !important; margin: 0 !important;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'%3E%3C/circle%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'%3E%3C/line%3E%3C/svg%3E") !important;
        background-repeat: no-repeat !important; background-position: 14px center !important; transition: 0.2s;
    }
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) input::placeholder { color: #64748b !important; opacity: 1 !important; }
    div[data-testid="stTextInput"]:has(input[placeholder="カード名・タレント名で検索"]) input:focus { border-color: #94a3b8 !important; }

    .sticky-header-bg { position: fixed; top: 0; left: 18rem; right: 0; height: 200px; background-color: #FFFFFF; z-index: 99998; border-bottom: 2px solid #e2e8f0; }
    .sticky-title-container { position: fixed; top: 90px; left: calc(18rem + 30px); z-index: 99999; display: flex; align-items: center; gap: 16px; }

    /* 🌟 追加：右上の「⋮」メニュー（ツールバー）を完全に非表示にする */
    [data-testid="stToolbar"] {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# ==========================================
# 左上（サイドバー）のメニュー設定
# ==========================================
with st.sidebar:
    st.markdown(
        """
        <div style="padding: 12px; text-align: center; margin-bottom: 20px;">
            <span style="color: #000000 !important; font-size: 1.1rem; font-weight: bold; line-height: 1.4; display: block;">
                ホロカ専用<br>カードコレクション
            </span>
        </div>
        """,
        unsafe_allow_html=True
    )
    st.divider()
    
    def render_menu(label, target_view, requires_login=False):
        is_selected = (st.session_state.current_view == target_view)
        btn_type = "primary" if is_selected else "secondary"
        
        if st.button(label, key=f"menu_{target_view}", type=btn_type):
            if requires_login and st.session_state.user is None:
                st.session_state.current_view = "login"
                st.query_params["view"] = "login"
            else:
                st.query_params["view"] = target_view
                st.session_state.current_view = target_view
                if "global_search" in st.session_state:
                    st.session_state.global_search = ""
            st.rerun()

    render_menu("カード一覧", "all_cards")
    render_menu("コレクション一覧", "home", requires_login=True)
    render_menu("資産総額", "assets", requires_login=True)
    render_menu("お気に入り", "favorite", requires_login=True)
    render_menu("設定", "setting", requires_login=True)
    render_menu("ヘルプ", "help")

# ==========================================
# 検索バーの表示制御
# ==========================================
def on_search_submit():
    if st.session_state.current_view not in ["all_cards", "home", "favorite"]:
        st.session_state.current_view = "all_cards"
        st.query_params["view"] = "all_cards"

if st.session_state.current_view in ["all_cards", "home", "favorite", "detail", "assets"]:
    search_query = st.text_input(
        "検索", 
        key="global_search", 
        label_visibility="collapsed", 
        placeholder="カード名・タレント名で検索",
        on_change=on_search_submit
    )
else:
    search_query = ""

# ==========================================
# 画面最上部に固定するページヘッダーの動的生成
# ==========================================
header_data = {
    "all_cards": {
        "title": "カード一覧",
        "desc": "ホロライブOCGのすべてのカード一覧です。未所持のカードも確認できます。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>'
    },
    "home": {
        "title": "コレクション一覧",
        "desc": "あなたが所持しているホロカのカード一覧です。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="13" height="15" rx="2" ry="2"></rect><path d="M19 2H8a2 2 0 0 0-2 2v1"></path><path d="M22 6v13a2 2 0 0 1-2 2h-1"></path></svg>'
    },
    "assets": {
        "title": "資産総額",
        "desc": "所持しているカードの買取相場（平均）から計算した推定資産総額です。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"></line><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path></svg>'
    },
    "favorite": {
        "title": "お気に入り",
        "desc": "お気に入り登録したカードの一覧です。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>'
    },
    "setting": {
        "title": "設定",
        "desc": "アプリの各種設定を行います。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"></path><circle cx="12" cy="12" r="3"></circle></svg>'
    },
    "detail": {
        "title": "カード詳細",
        "desc": "選択したカードの詳細情報と現在の相場です。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>'
    },
    "login": {
        "title": "ログイン / 新規登録",
        "desc": "コレクションの管理や資産計算を行うにはログインが必要です。",
        "icon": '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"></path><polyline points="10 17 15 12 10 7"></polyline><line x1="15" y1="12" x2="3" y2="12"></line></svg>'
    },
}

current_info = header_data.get(st.session_state.current_view, header_data["all_cards"])

st.markdown(
    f"""
    <div class="sticky-header-bg"></div>
    <div class="sticky-title-container">
        <div style="background-color: #f0f8ff; padding: 10px; border-radius: 10px; display: flex; align-items: center; justify-content: center; border: 1px solid #bae6fd; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
            {current_info["icon"]}
        </div>
        <div>
            <h1 style="margin: 0; font-size: 1.6rem; color: #000000 !important; font-weight: bold; letter-spacing: -0.02em;">{current_info["title"]}</h1>
            <p style="margin: 2px 0 0 0; font-size: 0.9rem; color: #4b5563 !important;">{current_info["desc"]}</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# ==========================================
# ★ カードを一覧表示する共通関数（DB連携版）
# ==========================================
def render_card_grid(cards_to_show, view_prefix, search_term=""):
    if not cards_to_show:
        return

    filtered_cards = []
    
    if search_term:
        norm_query = unicodedata.normalize('NFKC', search_term).lower()
        keywords = norm_query.replace(" ", " ").split()
        for card in cards_to_show:
            if "full_name" in card:
                norm_target = unicodedata.normalize('NFKC', str(card["full_name"])).lower()
                match = True
                for kw in keywords:
                    if kw not in norm_target:
                        match = False
                        break
                if match:
                    filtered_cards.append(card)
    else:
        filtered_cards = cards_to_show

    if not filtered_cards:
        if search_term:
            st.info(f"「{search_term}」に一致するカードは見つかりませんでした。")
        else:
            st.info("表示できるカードがありません。")
        return

    num_cols = 7 
    
    for i in range(0, len(filtered_cards), num_cols):
        cols = st.columns(num_cols, gap="small")
        for j in range(num_cols):
            if i + j < len(filtered_cards):
                card = filtered_cards[i + j]
                cid = card["id"]
                
                with cols[j]:
                    detail_url = f"?view=detail&card_id={cid}"
                    img_src = get_image_b64(card.get("img_path", ""))
                    
                    img_html = f'''
                    <div style="width: 100%; max-width: 210px; margin: 0 auto; text-align: center;">
                        <a href="{detail_url}" target="_self" style="text-decoration: none; display: block; line-height: 0;">
                            <img src="{img_src}" width="100%" style="border-radius: 6px; cursor: pointer; transition: 0.15s; box-shadow: 0 2px 4px rgba(0,0,0,0.1);" onmouseover="this.style.opacity=0.7" onmouseout="this.style.opacity=1" />
                        </a>
                        <p style="margin: 6px 0 4px 0; font-size: 0.9rem; font-weight: bold; color: #000000; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="{card['full_name']}">{card['full_name']}</p>
                    </div>
                    '''
                    st.markdown(img_html, unsafe_allow_html=True)

                    current_count = st.session_state.collection.get(cid, 0)
                    
                    c_spL, c_minus, c_num, c_plus, c_spR = st.columns([1.3, 1, 0.6, 1, 1.3], gap="small")
                    
                    with c_minus:
                        if st.button("－", key=f"minus_{view_prefix}_{cid}", use_container_width=True):
                            if st.session_state.user is None:
                                st.session_state.current_view = "login" 
                                st.rerun()
                            elif current_count > 0:
                                new_count = current_count - 1
                                st.session_state.collection[cid] = new_count
                                update_collection_in_db(cid, owned_count=new_count)
                                st.rerun()
                                
                    with c_num:
                        st.markdown(f'<div style="text-align: center; font-size: 1.2rem; font-weight: bold; line-height: 38px; color: #1e293b;">{current_count}</div>', unsafe_allow_html=True)
                        
                    with c_plus:
                        if st.button("＋", key=f"plus_{view_prefix}_{cid}", use_container_width=True):
                            if st.session_state.user is None:
                                st.session_state.current_view = "login" 
                                st.rerun()
                            else:
                                new_count = current_count + 1
                                st.session_state.collection[cid] = new_count
                                update_collection_in_db(cid, owned_count=new_count)
                                st.rerun()
                            
                    st.markdown('<div style="height: 15px;"></div>', unsafe_allow_html=True)

# ==========================================
# メイン画面の表示
# ==========================================

if st.session_state.current_view == "login":
    st.info("💡 アプリのすべての機能を利用するにはログインしてください。")
    
    email = st.text_input("メールアドレス")
    password = st.text_input("パスワード", type="password")
    clean_email = email.strip()
    
    col1, col2 = st.columns(2)
    with col1:
            if st.button("ログイン"):
                try:
                    response = supabase.auth.sign_in_with_password({"email": clean_email, "password": password})
                    st.session_state.user = response.user
                    cookie_manager.set("supabase_token", response.session.access_token, max_age=60*60*24*30)
                    time.sleep(1)
                    st.session_state.current_view = "all_cards" 
                    st.query_params["view"] = "all_cards" # 🌟 追加：URLも「カード一覧」に書き換える
                    st.rerun()
                except Exception as e:
                    st.error(f"ログインに失敗しました。(詳細: {e})")
                
    with col2:
        if st.button("新規登録"):
            try:
                response = supabase.auth.sign_up({"email": clean_email, "password": password})
                st.success("登録が完了しました！もう一度「ログイン」ボタンを押してください。")
            except Exception as e:
                st.error(f"登録に失敗しました。(詳細: {e})")

elif st.session_state.current_view == "all_cards":

    unique_rarities = set()
    for c in card_db:
        r = c.get("rarity")
        if pd.notna(r) and str(r).strip() != "":
            unique_rarities.add(str(r).strip())
    rarity_options = ["すべて"] + sorted(list(unique_rarities))

    col_head, col_filter = st.columns([0.8, 0.2], gap="large")
    with col_head:
        st.subheader("すべてのカード一覧")
        st.write("ホロライブOCGのレアリティがOSR以上のカードを表示しています。カード画像をクリックすると詳細・相場情報を確認できます。")
    with col_filter:
        selected_rarity = st.selectbox("レアリティ", rarity_options, key="all_rarity_filter")
    
    st.divider()
    
    cards_to_show = card_db
    if selected_rarity != "すべて":
        cards_to_show = [c for c in card_db if c.get("rarity") == selected_rarity]
        
    render_card_grid(cards_to_show, "all", search_term=search_query)

elif st.session_state.current_view == "home":
    if card_db:
        owned_cards = [card for card in card_db if st.session_state.collection.get(card.get("id"), 0) > 0]
        
        if not owned_cards:
            col_head, _ = st.columns([0.8, 0.2], gap="large")
            with col_head:
                st.subheader("所持カード一覧")
                st.write("あなたが所持しているカードの一覧です。")
            st.divider()
            st.info("現在、コレクションに追加されているカードはありません。「カード一覧」から ＋ ボタンで追加してください。")
        else:
            unique_rarities = set()
            for c in owned_cards:
                r = c.get("rarity")
                if pd.notna(r) and str(r).strip() != "":
                    unique_rarities.add(str(r).strip())
            rarity_options = ["すべて"] + sorted(list(unique_rarities))

            col_head, col_filter = st.columns([0.8, 0.2], gap="large")
            with col_head:
                st.subheader("所持カード一覧")
                st.write("あなたが所持しているカードの一覧です。")
            with col_filter:
                selected_rarity = st.selectbox("レアリティ", rarity_options, key="home_rarity_filter")
            
            st.divider()
            
            cards_to_show = owned_cards
            if selected_rarity != "すべて":
                cards_to_show = [c for c in owned_cards if c.get("rarity") == selected_rarity]
                
            render_card_grid(cards_to_show, "home", search_term=search_query)

elif st.session_state.current_view == "assets":
    st.subheader("資産総額（推定）")
    st.write("所持しているカードの買取価格の平均値をもとに、現在の推定資産を計算しています。")
    st.divider()

    owned_card_ids = [cid for cid, count in st.session_state.collection.items() if count > 0]
    
    col_btn, col_note = st.columns([0.3, 0.7])
    with col_btn:
        if st.button("🔄 所持カードの価格を一括取得", use_container_width=True, type="primary"):
            if not owned_card_ids:
                st.warning("所持しているカードがありません。")
            else:
                progress_text = "価格データを収集中..."
                my_bar = st.progress(0.0, text=progress_text)
                
                for i, cid in enumerate(owned_card_ids):
                    my_bar.progress(i / len(owned_card_ids), text=f"価格データを収集中... ({i+1}/{len(owned_card_ids)}) - ID: {cid}")
                    
                    card_info = next((c for c in card_db if c["id"] == cid), None)
                    if card_info:
                        yuyu_sell, yuyu_buy = scraper.scrape_yuyutei(card_info.get("yuyutei_url", ""))
                        tore_sell, tore_buy = scraper.scrape_torecolo(card_info["id"], card_info.get("rarity", ""))
                        full_sell, full_buy = scraper.scrape_fullahead(card_info.get("fullahead_url", ""))
                        
                        update_price_in_db(cid, yuyu_sell, yuyu_buy, tore_sell, tore_buy, full_sell, full_buy)
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        st.session_state.price_cache[cid] = {
                            "last_updated": now_str,
                            "遊々亭": {"sell": yuyu_sell, "buy": yuyu_buy},
                            "CBトレコロ": {"sell": tore_sell, "buy": tore_buy},
                            "フルアヘッド": {"sell": full_sell, "buy": full_buy}
                        }
                
                my_bar.progress(1.0, text="一括取得が完了しました！")
                time.sleep(1)
                st.rerun() 
                
    with col_note:
        st.caption("※所持している種類の数だけ各サイトにアクセスするため、カードの種類が多い場合は時間がかかります。")
        
    st.write("") 
    
    total_assets = 0
    asset_list = []

    for cid, count in st.session_state.collection.items():
        if count > 0:
            card_info = next((c for c in card_db if c["id"] == cid), None)
            card_name = card_info["full_name"] if card_info else cid
            
            cache = st.session_state.price_cache.get(cid, {})
            buy_prices = []
            
            for site in ["遊々亭", "CBトレコロ", "フルアヘッド"]:
                buy_str = cache.get(site, {}).get("buy", "")
                if isinstance(buy_str, str):
                    match = re.sub(r'[^\d]', '', buy_str)
                    if match:
                        buy_prices.append(int(match))
            
            if buy_prices:
                avg_buy = sum(buy_prices) // len(buy_prices)
                subtotal = avg_buy * count
                total_assets += subtotal
                
                asset_list.append({
                    "カード名": card_name,
                    "所持数": f"{count} 枚",
                    "平均買取価格": f"{avg_buy:,} 円",
                    "小計": f"{subtotal:,} 円",
                    "元データ": subtotal 
                })
            else:
                asset_list.append({
                    "カード名": card_name,
                    "所持数": f"{count} 枚",
                    "平均買取価格": "データなし",
                    "小計": "0 円",
                    "元データ": 0
                })

    st.markdown(
        f"""
        <div style="background-color: #f8fafc; padding: 20px; border-radius: 10px; border: 1px solid #e2e8f0; text-align: center; margin-bottom: 30px;">
            <p style="margin: 0; font-size: 1.1rem; color: #64748b; font-weight: bold;">現在の推定資産総額</p>
            <p style="margin: 0; font-size: 3rem; color: #0f172a; font-weight: bold; letter-spacing: 1px;">{total_assets:,} <span style="font-size: 1.5rem;">円</span></p>
        </div>
        """, unsafe_allow_html=True
    )

    if asset_list:
        asset_list.sort(key=lambda x: x["元データ"], reverse=True)
        df_assets = pd.DataFrame(asset_list)
        df_assets = df_assets.drop(columns=["元データ"])
        
        st.write("▼ 資産内訳（小計が大きい順）")
        st.dataframe(df_assets, use_container_width=True, hide_index=True)
    else:
        st.info("現在所持しているカードがありません。「カード一覧」からコレクションに追加してください。")

elif st.session_state.current_view == "favorite":
    st.subheader("お気に入り登録済みカード")
    st.write("詳細画面で「お気に入り」に登録したカードが表示されます。")
    st.divider()

    if card_db:
        fav_cards = [card for card in card_db if card.get("id") in st.session_state.favorites]
        if not fav_cards:
            st.info("現在、お気に入りに登録されているカードはありません。")
        else:
            render_card_grid(fav_cards, "fav", search_term=search_query)

elif st.session_state.current_view == "detail":
    if st.button("← カード一覧に戻る", key="back_to_home"):
        st.query_params["view"] = "all_cards"
        st.session_state.current_view = "all_cards"
        st.rerun()
        
    st.write("") 
    
    selected_card_id = st.query_params.get("card_id", "hBP08-076")
    selected_card = next((card for card in card_db if card["id"] == selected_card_id), None)
    
    if not selected_card:
        st.error("カード情報が見つかりません。")
    else:
        col_title, col_fav, _ = st.columns([3.0, 2.0, 5.0])
        
        with col_title:
            st.markdown(f'<h3 style="margin: 0; padding-top: 4px; white-space: nowrap;">{selected_card.get("full_name", "不明なカード")}</h3>', unsafe_allow_html=True)
            
        with col_fav:
            is_fav = selected_card_id in st.session_state.favorites
            fav_label = "★ お気に入り解除" if is_fav else "☆ お気に入り登録"
            
            if st.button(fav_label):
                if st.session_state.user is None:
                    st.session_state.current_view = "login"
                    st.query_params["view"] = "login"
                    st.rerun()
                else:
                    if is_fav:
                        st.session_state.favorites.remove(selected_card_id)
                        update_collection_in_db(selected_card_id, is_favorite=False)
                    else:
                        st.session_state.favorites.append(selected_card_id)
                        update_collection_in_db(selected_card_id, is_favorite=True)
                    st.rerun() 

        st.markdown(
            f"""
            <div style="margin-top: 5px; margin-bottom: 12px; font-size: 1rem; color: #1e293b;">
                <span style="font-weight: bold;">カードID:</span> {selected_card["id"]} ｜ <span style="font-weight: bold;">レアリティ:</span> {selected_card.get("rarity", "不明")}
            </div>
            <hr style="margin: 0; padding: 0; border: none; border-top: 1px solid #e2e8f0; margin-bottom: 20px;">
            """,
            unsafe_allow_html=True
        )

        col_img, col_info = st.columns([0.3, 0.7], gap="medium")

        with col_img:
            st.markdown('<h4 style="margin-top: 0; margin-bottom: 12px; font-size: 1.25rem;">カード画像</h4>', unsafe_allow_html=True)
            img_src = get_image_b64(selected_card.get("img_path", ""))
            st.markdown(
                f'<img src="{img_src}" width="100%" style="max-width: 300px; border-radius: 6px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);" />', 
                unsafe_allow_html=True
            )

        with col_info:
            st.markdown('<h4 style="margin-top: 0; margin-bottom: 12px; font-size: 1.25rem;">所持状況・相場情報</h4>', unsafe_allow_html=True)
            
            current_detail_count = st.session_state.collection.get(selected_card_id, 0)
            st.metric(label="現在の所持枚数", value=f"{current_detail_count} 枚")
            
            card_cache = st.session_state.price_cache.get(selected_card_id, {})
            last_updated = card_cache.get("last_updated", "未取得")
            
            df_prices = pd.DataFrame({
                "サイト名": ["遊々亭", "CBトレコロ", "フルアヘッド"],
                "買取価格": [
                    card_cache.get("遊々亭", {}).get("buy", "未取得"),
                    card_cache.get("CBトレコロ", {}).get("buy", "未取得"),
                    card_cache.get("フルアヘッド", {}).get("buy", "個別ページなし")
                ],
                "販売価格": [
                    card_cache.get("遊々亭", {}).get("sell", "未取得"),
                    card_cache.get("CBトレコロ", {}).get("sell", "未取得"),
                    card_cache.get("フルアヘッド", {}).get("sell", "未取得")
                ]
            }, index=[1, 2, 3])

            st.write(f"▼ 指定サイト別 価格相場（最終取得日: {last_updated}）")

            if st.button("最新の価格を取得する"):
                with st.spinner("各サイトから価格を収集中..."):
                    
                    yuyu_sell, yuyu_buy = scraper.scrape_yuyutei(selected_card.get("yuyutei_url", ""))
                    tore_sell, tore_buy = scraper.scrape_torecolo(selected_card["id"], selected_card.get("rarity", ""))
                    full_sell, full_buy = scraper.scrape_fullahead(selected_card.get("fullahead_url", ""))

                    update_price_in_db(selected_card_id, yuyu_sell, yuyu_buy, tore_sell, tore_buy, full_sell, full_buy)
                    
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    st.session_state.price_cache[selected_card_id] = {
                        "last_updated": now_str,
                        "遊々亭": {"sell": yuyu_sell, "buy": yuyu_buy},
                        "CBトレコロ": {"sell": tore_sell, "buy": tore_buy},
                        "フルアヘッド": {"sell": full_sell, "buy": full_buy}
                    }
                    
                st.success("価格の更新が完了しました！")
                time.sleep(1)
                st.rerun()

            st.table(df_prices)

elif st.session_state.current_view == "setting":
    st.subheader("設定")
    st.write("アプリの各種設定を行います。")
    st.divider()
    
    st.markdown("#### 👤 ユーザー情報の変更")
    
    current_name = st.session_state.user.user_metadata.get("display_name", "")
    
    new_name = st.text_input("表示名（ニックネーム）", value=current_name, placeholder="例：ホロカ太郎")
    
    if st.button("変更を保存", type="primary"):
        if new_name.strip() == "":
            st.warning("表示名を入力してください。")
        else:
            try:
                res = supabase.auth.update_user({
                    "data": {"display_name": new_name}
                })
                st.session_state.user = res.user
                st.success("表示名を更新しました！")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"更新に失敗しました: {e}")

elif st.session_state.current_view == "help":
    st.subheader("ヘルプ")
    st.write("使い方やよくある質問が表示されます。（準備中）")