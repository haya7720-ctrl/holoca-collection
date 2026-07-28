import requests
from bs4 import BeautifulSoup
import time
import re

def scrape_yuyutei(sell_url):
    # CSVにURLが登録されていない場合はスキップ
    if not isinstance(sell_url, str) or not sell_url.startswith("http"):
        return "URL未登録", "URL未登録"

    time.sleep(1)
    headers = {"User-Agent": "Mozilla/5.0"}
    sell_price = "販売停止中"
    buy_price = "買取停止中"
    
    try:
        # === 販売価格の取得 ===
        sell_res = requests.get(sell_url, headers=headers, timeout=10)
        if sell_res.status_code == 200:
            sell_soup = BeautifulSoup(sell_res.text, 'html.parser')
            
            # 価格が含まれていそうな要素をいくつか候補として挙げる（通常デザインと限定デザインの両方に対応）
            candidates = [
                sell_soup.select_one('.price'),
                sell_soup.select_one('h4.fw-bold.d-inline-block'),
                sell_soup.find(lambda tag: tag.name in ['h3', 'h4'] and '円' in tag.text)
            ]
            
            for elem in candidates:
                if elem:
                    # テキストの中から「数字＋円」のパターンをすべて抽出する
                    matches = re.findall(r'([0-9,]+)\s*円', elem.text)
                    if matches:
                        # 複数ある場合（割引など）は一番最後の金額が最新価格になるので [-1] を取得
                        sell_price = f"{matches[-1]} 円"  
                        break # 見つかったら探すのをやめる

        # === 買取価格の取得 ===
        buy_url = sell_url.replace("/sell/", "/buy/")
        buy_res = requests.get(buy_url, headers=headers, timeout=10)
        if buy_res.status_code == 200:
            buy_soup = BeautifulSoup(buy_res.text, 'html.parser')
            
            # 買取価格用の候補（PRICE UPなどの紫文字にも対応）
            candidates = [
                buy_soup.select_one('.price'),
                buy_soup.select_one('h4.fw-bold.text-purple'),
                buy_soup.find(lambda tag: tag.name in ['h3', 'h4'] and '円' in tag.text)
            ]
            
            for elem in candidates:
                if elem:
                    matches = re.findall(r'([0-9,]+)\s*円', elem.text)
                    if matches:
                        # 画像3枚目のように "5,000円 7,500円" のテキストだった場合、最後の "7,500" が取得される
                        buy_price = f"{matches[-1]} 円"  
                        break
                        
    except Exception:
        pass # エラー時は初期値の「停止中」のまま返す
        
    return sell_price, buy_price

def scrape_torecolo(card_id, rarity):
    if not isinstance(rarity, str) or not rarity:
        return "URL未登録", "URL未登録"

    headers = {"User-Agent": "Mozilla/5.0"}
    sell_price, buy_price = "販売停止中", "買取停止中"
    try:
        time.sleep(1) 
        target_id = f"HL-{card_id}{rarity}"
        
        sell_url = f"https://www.torecolo.jp/shop/g/g{target_id}/"
        sell_res = requests.get(sell_url, headers=headers, timeout=10)
        if sell_res.status_code == 200:
            sell_soup = BeautifulSoup(sell_res.text, 'html.parser')
            sell_elem = sell_soup.select_one('div.block-goods-price--price.price.js-enhanced-ecommerce-goods-price')
            if sell_elem:
                match = re.search(r'([0-9,]+)\s*円', sell_elem.text)
                if match: sell_price = f"{match.group(1)} 円"

        time.sleep(1)
        buy_url = f"https://www.torecolo.jp/shop/g/g{target_id}-S/"
        buy_res = requests.get(buy_url, headers=headers, timeout=10)
        if buy_res.status_code == 200:
            buy_soup = BeautifulSoup(buy_res.text, 'html.parser')
            buy_elem = buy_soup.select_one('div.block-goods-price--price.price.js-enhanced-ecommerce-goods-price')
            if buy_elem:
                match = re.search(r'([0-9,]+)\s*円', buy_elem.text)
                if match: buy_price = f"{match.group(1)} 円"
        return sell_price, buy_price
    except Exception:
        return "エラー", "エラー"

def scrape_fullahead(target_url):
    if not isinstance(target_url, str) or not target_url.startswith("http"):
        return "URL未登録", "個別ページなし"

    headers = {"User-Agent": "Mozilla/5.0"}
    sell_price, buy_price = "販売停止中", "個別ページなし"
    try:
        time.sleep(1)
        sell_res = requests.get(target_url, headers=headers, timeout=10)
        if sell_res.status_code == 200:
            sell_soup = BeautifulSoup(sell_res.text, 'html.parser')
            sell_elem = sell_soup.select_one('span[data-id="makeshop-item-price"]')
            if sell_elem:
                match = re.search(r'([0-9,]+)', sell_elem.text)
                if match: sell_price = f"{match.group(1)} 円"
        return sell_price, buy_price
    except Exception:
        return "エラー", buy_price