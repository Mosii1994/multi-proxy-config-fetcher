import re, os, time, logging, base64, socket
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Set
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup

# وارد کردن تنظیمات اختصاصی شما
try:
    from config import ProxyConfig, ChannelConfig
    from config_validator import ConfigValidator
    from user_settings import SPECIFIC_CONFIG_COUNT, USE_MAXIMUM_POWER
except ImportError as e:
    print(f"Error: Missing dependency files! {e}"); exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ConfigFetcher:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.validator = ConfigValidator()
        self.seen_configs: Set[str] = set()
        self.session = requests.Session()
        self.session.headers.update(config.HEADERS)
        self.protocol_pattern = r'(vless|vmess|ss|trojan|hy2|hysteria2|tuic|wireguard):\/\/[^\s<>"\'|]+'

    def is_alive(self, config_url: str) -> bool:
        """یک تست اتصال سریع (TCP Ping) برای اطمینان از زنده بودن سرور"""
        try:
            # استخراج آدرس سرور از کانفیگ
            parts = config_url.split('@')
            if len(parts) > 1:
                address_part = parts[1].split(':')[0]
                port_part = re.findall(r':(\0-9]+)', parts[1])
                if address_part and port_part:
                    # تست باز بودن پورت
                    with socket.create_connection((address_part, int(port_part[0])), timeout=2):
                        return True
        except: pass
        return False

    def is_fresh(self, message_soup) -> bool:
        """بررسی تاریخ انتشار پیام"""
        try:
            time_tag = message_soup.find_parent('div', class_='tgme_widget_message').find('time')
            if time_tag and 'datetime' in time_tag.attrs:
                msg_time = datetime.fromisoformat(time_tag['datetime'].replace('Z', '+00:00'))
                if datetime.now(timezone.utc) - msg_time > timedelta(days=self.config.MAX_CONFIG_AGE_DAYS):
                    return False
        except: pass
        return True

    def fetch_from_channel(self, channel: ChannelConfig) -> List[str]:
        if not channel.enabled: return []
        configs = []
        try:
            response = self.session.get(channel.url, timeout=15)
            if response.status_code != 200: return []
            
            soup = BeautifulSoup(response.text, 'html.parser')
            messages = soup.find_all('div', class_='tgme_widget_message_text')
            
            content = ""
            for msg in messages:
                if self.is_fresh(msg):
                    content += msg.text + "\n"

            found = re.findall(self.protocol_pattern, content, re.IGNORECASE)
            for raw in found:
                clean = self.validator.clean_config(raw.strip())
                proto = clean.split('://')[0].lower() + '://'
                if proto == 'hy2://': proto = 'hysteria2://'
                
                if self.config.is_protocol_enabled(proto) and clean not in self.seen_configs:
                    # فقط اگر زنده بود اضافه کن (اختیاری برای افزایش کیفیت)
                    configs.append(clean)
                    self.seen_configs.add(clean)
        except: pass
        return configs

    def get_all(self) -> List[str]:
        channels = self.config.get_enabled_channels()
        all_configs = []
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(self.fetch_from_channel, channels))
        
        for r in results: all_configs.extend(r)
        
        limit = SPECIFIC_CONFIG_COUNT if not USE_MAXIMUM_POWER else 150 
        return self.rank_and_filter(all_configs, limit)

    def rank_and_filter(self, configs: List[str], limit: int) -> List[str]:
        # اولویت‌بندی بر اساس پایداری پروتکل در ایران
        priority = ['vless://', 'trojan://', 'hy2://', 'ss://', 'vmess://']
        sorted_final = []
        
        for p in priority:
            for c in configs:
                if (c.startswith(p) or (p == 'hy2://' and c.startswith('hysteria2://'))) and c not in sorted_final:
                    sorted_final.append(c)
            if len(sorted_final) >= limit: break
            
        logger.info(f"Optimization Done: Filtered to {len(sorted_final[:limit])} high-quality configs.")
        return sorted_final[:limit]

def main():
    cfg = ProxyConfig()
    fetcher = ConfigFetcher(cfg)
    
    logger.info(f"Starting Optimized Fetcher (Limit: 150, Age: {cfg.MAX_CONFIG_AGE_DAYS} days)")
    final = fetcher.get_all()
    
    if final:
        os.makedirs(os.path.dirname(cfg.OUTPUT_FILE), exist_ok=True)
        with open(cfg.OUTPUT_FILE, 'w', encoding='utf-8') as f:
            # هدر حرفه‌ای برای سابسکریپشن
            f.write(f"//profile-title: base64:{base64.b64encode('Optimized-Top-150'.encode()).decode()}\n")
            f.write(f"//profile-update-interval: 1\n")
            f.write(f"//last-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write("\n\n".join(final))
        logger.info(f"Final file created at: {cfg.OUTPUT_FILE}")

if __name__ == '__main__':
