import re
import os
import time
import json
import logging
import base64
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Set
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup

# پیش‌فرض فرض می‌کنیم این فایل‌ها در کنار اسکریپت هستند
try:
    from config import ProxyConfig, ChannelConfig
    from config_validator import ConfigValidator
except ImportError:
    print("Error: config.py or config_validator.py not found!")
    exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('proxy_fetcher.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class ConfigFetcher:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.validator = ConfigValidator()
        self.protocol_counts: Dict[str, int] = {p: 0 for p in config.SUPPORTED_PROTOCOLS}
        self.seen_configs: Set[str] = set()
        self.session = requests.Session()
        self.session.headers.update(config.HEADERS)
        # الگوی Regex برای شناسایی پروتکل‌ها
        self.protocol_pattern = r'(vless|vmess|ss|trojan|hy2|shadowsocks):\/\/[^\s<>"\'|]+'

    def fetch_with_retry(self, url: str) -> Optional[requests.Response]:
        backoff = 1
        for attempt in range(self.config.MAX_RETRIES):
            try:
                response = self.session.get(url, timeout=self.config.REQUEST_TIMEOUT)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                wait_time = min(self.config.RETRY_DELAY * backoff, 30)
                time.sleep(wait_time)
                backoff *= 2
        return None

    def extract_configs_from_text(self, text: str) -> List[str]:
        """استخراج تمام کانفیگ‌ها با استفاده از Regex"""
        # ابتدا بررسی Base64 بودن کل متن
        if self.validator.is_base64(text.strip()):
            decoded = self.validator.decode_base64_text(text.strip())
            if decoded: text = decoded

        found = re.findall(self.protocol_pattern, text, re.IGNORECASE)
        return list(set(found))

    def process_single_config(self, raw_config: str, channel: ChannelConfig) -> Optional[str]:
        """پردازش و ولیدیشن یک کانفیگ واحد"""
        try:
            config = raw_config.strip()
            # نرمال‌سازی Hysteria2
            if config.startswith('hy2://'):
                config = self.validator.normalize_hysteria2_protocol(config)

            for protocol, info in self.config.SUPPORTED_PROTOCOLS.items():
                if any(config.startswith(alias) for alias in [protocol] + info.get('aliases', [])):
                    if not self.config.is_protocol_enabled(protocol): return None
                    
                    if protocol == "vmess://":
                        config = self.validator.clean_vmess_config(config)
                    
                    clean_config = self.validator.clean_config(config)
                    if self.validator.validate_protocol_config(clean_config, protocol):
                        if clean_config not in self.seen_configs:
                            return clean_config
            return None
        except:
            return None

    def fetch_configs_from_source(self, channel: ChannelConfig) -> List[str]:
        """متد اصلی استخراج برای هر کانال (قابل اجرا در Thread)"""
        if not channel.enabled: return []
        
        configs: List[str] = []
        start_time = time.time()
        
        response = self.fetch_with_retry(channel.url)
        if not response:
            self.config.update_channel_stats(channel, False)
            return []

        response_time = time.time() - start_time
        
        # استخراج متن بر اساس نوع منبع
        content = ""
        if channel.is_telegram:
            soup = BeautifulSoup(response.text, 'html.parser')
            messages = soup.find_all('div', class_='tgme_widget_message_text')
            content = "\n".join([m.text for m in messages if m.text])
        else:
            content = response.text

        raw_found = self.extract_configs_from_text(content)
        channel.metrics.total_configs = len(raw_found)

        for raw in raw_found:
            processed = self.process_single_config(raw, channel)
            if processed:
                configs.append(processed)
                self.seen_configs.add(processed)
                # آپدیت آمار پروتکل
                p_type = processed.split('://')[0].lower() + '://'
                channel.metrics.protocol_counts[p_type] = channel.metrics.protocol_counts.get(p_type, 0) + 1

        self.config.update_channel_stats(channel, True, response_time)
        logger.info(f"Fetched {len(configs)} configs from {channel.url}")
        return configs

    def fetch_all_configs(self) -> List[str]:
        enabled_channels = self.config.get_enabled_channels()
        all_configs = []

        logger.info(f"Starting concurrent fetch from {len(enabled_channels)} channels...")
        
        # استفاده از Multi-threading برای سرعت حداکثری
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(self.fetch_configs_from_source, enabled_channels))

        for res in results:
            all_configs.extend(res)

        if all_configs:
            balanced = self.balance_protocols(all_configs)
            # آپدیت شمارنده نهایی برای لاگ
            for c in balanced:
                p = c.split('://')[0].lower() + '://'
                if p in self.protocol_counts: self.protocol_counts[p] += 1
            return balanced
        return []

def balance_protocols(self, configs: List[str]) -> List[str]:
        """انتخاب ۱۵۰ کانفیگ برتر بر اساس اولویت پروتکل"""
        # دسته‌بندی کانفیگ‌ها
        proto_map = {p: [] for p in self.config.SUPPORTED_PROTOCOLS}
        for c in configs:
            for p in proto_map:
                if c.startswith(p):
                    proto_map[p].append(c)
                    break
        
        # مرتب‌سازی پروتکل‌ها بر اساس اولویت (Priority) که در config.py تعریف کردی
        sorted_protocols = sorted(
            self.config.SUPPORTED_PROTOCOLS.items(),
            key=lambda x: x[1].get("priority", 0),
            reverse=True
        )

        final_selection = []
        # جمع‌آوری از پروتکل‌های با اولویت بالا تا رسیدن به عدد ۱۵۰
        for proto, info in sorted_protocols:
            configs_of_this_proto = proto_map.get(proto, [])
            final_selection.extend(configs_of_this_proto)
            
            if len(final_selection) >= 150:
                break

        # برگرداندن دقیقاً ۱۵۰ مورد اول (یا کمتر اگر کل موجودی کمتر بود)
        return final_selection[:150]

def save_configs(configs: List[str], config: ProxyConfig):
    try:
        os.makedirs(os.path.dirname(config.OUTPUT_FILE), exist_ok=True)
        with open(config.OUTPUT_FILE, 'w', encoding='utf-8') as f:
            header = f"//profile-title: base64:{base64.b64encode('Anonymous'.encode()).decode()}\n" \
                     f"//profile-update-interval: 1\n" \
                     f"//subscription-userinfo: upload=0; download=0; total=10737418240000000; expire=2546249531\n" \
                     f"//support-url: https://t.me/BXAMbot\n\n"
            f.write(header)
            f.write("\n\n".join(configs))
        logger.info(f"Saved {len(configs)} configs to {config.OUTPUT_FILE}")
    except Exception as e:
        logger.error(f"Save error: {e}")

def main():
    try:
        cfg = ProxyConfig()
        fetcher = ConfigFetcher(cfg)
        final_configs = fetcher.fetch_all_configs()
        
        if final_configs:
            save_configs(final_configs, cfg)
            # چاپ آمار نهایی در کنسول
            print("\n" + "="*30)
            for p, count in fetcher.protocol_counts.items():
                if count > 0: print(f"{p}: {count}")
            print("="*30)
        else:
            logger.warning("No configs found.")
            
    except Exception as e:
        logger.error(f"Critical error: {e}")

if __name__ == '__main__':
    main()
