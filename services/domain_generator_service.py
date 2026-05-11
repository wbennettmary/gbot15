import random
import string
import requests
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

class DomainGeneratorService:
    """Service for generating niche-specific longtail domains and notifying via Telegram."""
    
    NICHES = {
        'education': [
            'luminary', 'vanguard', 'pinnacle', 'prestige', 'scholar', 'academy', 
            'mentor', 'wisdom', 'erudite', 'apex', 'genius', 'mastery', 'nexus', 
            'elite', 'wisdom', 'insight', 'virtuoso', 'stellar', 'proven'
        ],
        'industry': [
            'titan', 'summit', 'matrix', 'velocity', 'dynamic', 'global', 'prime', 
            'enterprise', 'industrial', 'sector', 'vortex', 'pillar', 'fortress', 
            'omega', 'empire', 'sovran', 'axis', 'legacy', 'heavy'
        ],
        'ai': [
            'neural', 'cortex', 'intelligence', 'vision', 'quantum', 'nexus', 
            'synergy', 'logic', 'core', 'automated', 'cognitive', 'cipher', 
            'synapse', 'mind', 'brain', 'sentient', 'vector', 'binary'
        ],
        'tech': [
            'pixel', 'cloud', 'digital', 'network', 'cyber', 'matrix', 'portal', 
            'system', 'infinity', 'vertex', 'nexus', 'stack', 'bit', 'logic', 
            'code', 'dev', 'web', 'data', 'connect'
        ]
    }
    
    MODIFIERS = [
        'pro', 'hq', 'hub', 'lab', 'studio', 'base', 'flow', 'wave', 'prime', 
        'edge', 'star', 'axis', 'sphere', 'bridge', 'path', 'nexus', 'portal', 
        'zone', 'expert', 'elite', 'master', 'vanguard', 'vision'
    ]
    
    TLDS = ['.asia', '.fun', '.email', '.org', '.shop', '.quest', '.work', '.cv', '.space', '.info', '.lat', '.biz', '.cfd', '.help']

    def __init__(self, telegram_token: str):
        self.telegram_token = telegram_token
        self.telegram_api_url = f"https://api.telegram.org/bot{telegram_token}"

    def generate_domains(self, niches: List[str] = None, count: int = 20, tlds: List[str] = None) -> List[str]:
        """Generate random longtail domains based on niches with TLD rotation."""
        if not niches:
            niches = list(self.NICHES.keys())
            
        if not tlds:
            tlds = self.TLDS
            
        generated = set()
        tld_index = 0
        
        while len(generated) < count:
            niche = random.choice(niches)
            if niche not in self.NICHES:
                continue
                
            keyword = random.choice(self.NICHES[niche])
            modifier = random.choice(self.MODIFIERS)
            
            # Rotate TLDs equally
            tld = tlds[tld_index % len(tlds)]
            tld_index += 1
            
            # Variety of patterns - NO NUMBERS, NO DASHES
            pattern = random.choice([
                f"{keyword}{modifier}",
                f"{modifier}{keyword}",
                f"the{keyword}{modifier}",
                f"{keyword}experts",
                f"my{keyword}{modifier}",
                f"{keyword}global",
                f"{keyword}hq",
                f"{keyword}pro"
            ])
            
            if tld and not tld.startswith('.'):
                tld = f".{tld}"
                
            domain = f"{pattern}{tld}"
            generated.add(domain)
            
        return list(generated)

    def send_to_telegram(self, chat_id: str, message: str):
        """Send a message to the specified Telegram chat."""
        url = f"{self.telegram_api_url}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Telegram notification sent to {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False

    def get_bot_updates(self):
        """Get updates to find the last chat_id if not known."""
        url = f"{self.telegram_api_url}/getUpdates"
        try:
            response = requests.get(url, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get Telegram updates: {e}")
            return None
