import time
import requests
import traceback
import json
import logging
import threading
import signal
import sys
import os
import shutil
import random
import tempfile
from datetime import datetime
from collections import defaultdict
from typing import List, Set, Optional, Dict, Any

logging.basicConfig(
   level=logging.INFO,
   format='%(asctime)s - %(levelname)s - %(message)s',
   handlers=[
       logging.FileHandler('ally_sender.log'),
       logging.StreamHandler()
   ]
)
logger = logging.getLogger(__name__)

class Config:
   def __init__(self, config_file='config.json'):
       with open(config_file, 'r') as f:
           self.config = json.load(f)
       self.validate()
   
   def validate(self):
       required_keys = ['cookies', 'webhook', 'group_id', 'already_added_file']
       for key in required_keys:
           if key not in self.config:
               raise ValueError(f"Missing required config key: {key}")
       
       if not isinstance(self.get('cookies'), list) or len(self.get('cookies')) == 0:
           raise ValueError("Cookies must be a non-empty list")
       
       if self.get('min_delay_between_requests', 0) < 1:
           raise ValueError("Minimum delay must be at least 1 second")
       
       if self.get('max_delay_between_requests', 60) < self.get('min_delay_between_requests', 10):
           raise ValueError("Maximum delay must be greater than minimum delay")
       
       if self.get('max_retries', 3) < 1:
           raise ValueError("Max retries must be at least 1")
       
       if self.get('request_timeout', 30) < 5:
           raise ValueError("Request timeout must be at least 5 seconds")
   
   def get(self, key, default=None):
       return self.config.get(key, default)

class ApplicationManager:
    def __init__(self):
        self.threads: List[threading.Thread] = []
        self.shutdown_event = threading.Event()
        self.running = True
        
    def add_thread(self, target, args=(), daemon=False):
        thread = threading.Thread(target=target, args=args, daemon=daemon)
        self.threads.append(thread)
        return thread
        
    def start_threads(self):
        for thread in self.threads:
            thread.start()
            
    def shutdown(self, timeout=10):
        logger.info("Initiating graceful shutdown...")
        self.running = False
        self.shutdown_event.set()
        
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    logger.warning(f"Thread {thread.name} did not shutdown gracefully")
                    
class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"
        self.lock = threading.Lock()
        
    def can_execute(self):
        with self.lock:
            if self.state == "CLOSED":
                return True
            elif self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    return True
                return False
            else:
                return True
                
    def record_success(self):
        with self.lock:
            self.failure_count = 0
            self.state = "CLOSED"
            
    def record_failure(self):
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
                logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

def deduplicate_queue(queue_file, already_added_file):
    try:
        with open(queue_file, 'r') as f:
            queue = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        queue = []

    try:
        with open(already_added_file, 'r') as f:
            already_added = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        already_added = set()

    seen = set()
    new_queue = []
    for item in queue:
        if item not in seen and item not in already_added:
            seen.add(item)
            new_queue.append(item)

    if len(new_queue) != len(queue):
        with open(queue_file, 'w') as f:
            for item in new_queue:
                f.write(f"{item}\n")
        logger.info(f"Deduplicated queue. Removed {len(queue) - len(new_queue)} duplicates or already added items.")

class Statistics:
   def __init__(self):
       self.requests_sent = 0
       self.successful_requests = 0
       self.failed_requests = 0
       self.groups_processed = 0
       self.start_time = time.time()
   
   def log_summary(self):
       runtime = time.time() - self.start_time
       logger.info(f"Runtime: {runtime:.2f}s | Requests: {self.requests_sent} | "
                  f"Success: {self.successful_requests} | Failed: {self.failed_requests}")

class QueueManager:
   def __init__(self, file_path, already_added_manager=None, max_size=10000):
       self.file_path = file_path
       self.already_added = already_added_manager
       self.queue = []
       self.max_size = max_size
       self.lock = threading.Lock()
       self.last_save_time = 0
       self.save_interval = 30
       self.load_from_file()
   
   def load_from_file(self):
       try:
           with open(self.file_path, 'r') as f:
               self.queue = [str(line.strip()) for line in f if line.strip()]
           logger.info(f"Loaded {len(self.queue)} groups from queue file")
       except FileNotFoundError:
           logger.info("No existing queue file found, starting with empty queue")
           self.queue = []
       except Exception as e:
           logger.error(f"Error loading queue file: {e}")
           self.queue = []
   
   def save_to_file(self, force=False):
       current_time = time.time()
       if not force and current_time - self.last_save_time < self.save_interval:
           return
       
       try:
           with self.lock:
               self._atomic_write(self.queue)
               self.last_save_time = current_time
       except Exception as e:
           logger.error(f"Error saving queue file: {e}")
           
   def _atomic_write(self, data):
       if os.path.exists(self.file_path):
           shutil.copy2(self.file_path, f"{self.file_path}.backup")
       
       temp_file = f"{self.file_path}.tmp"
       try:
           with open(temp_file, 'w') as f:
               for group_id in data:
                   f.write(f"{group_id}\n")
           shutil.move(temp_file, self.file_path)
       except Exception as e:
           if os.path.exists(temp_file):
               os.remove(temp_file)
           raise e
   
   def add_groups(self, group_ids):
    with self.lock:
        current_queue_set = set(str(item) for item in self.queue)
        
        new_groups = []
        for group_id in group_ids:
            group_str = str(group_id)
            if (not self.already_added.is_group_processed(group_str) and 
                group_str not in current_queue_set):
                new_groups.append(group_str)
                current_queue_set.add(group_str)
        
        if new_groups:
            available_space = self.max_size - len(self.queue)
            if available_space <= 0:
                logger.warning(f"Queue at maximum size ({self.max_size}), dropping {len(new_groups)} new groups")
                return
            
            groups_to_add = new_groups[:available_space]
            if len(groups_to_add) < len(new_groups):
                logger.warning(f"Queue size limit reached, adding only {len(groups_to_add)} of {len(new_groups)} groups")
            
            self.queue = groups_to_add + self.queue
            logger.info(f"Added {len(groups_to_add)} new groups to queue (filtered {len(group_ids) - len(groups_to_add)} duplicates/processed). Queue size: {len(self.queue)}")
    self.save_to_file()
   
   def get_next_group(self):
       with self.lock:
           if self.queue:
               return self.queue.pop(0)
       return None
   
   def get_size(self):
       with self.lock:
           return len(self.queue)
   
   def clear(self):
       with self.lock:
           self.queue = []
       self.save_to_file(force=True)

   def remove_all_instances(self, group_id):
       group_str = str(group_id)
       with self.lock:
           original_size = len(self.queue)
           self.queue = [item for item in self.queue if str(item) != group_str]
           removed_count = original_size - len(self.queue)
           if removed_count > 0:
               logger.info(f"Removed {removed_count} duplicate instances of group {group_id} from queue")
       if removed_count > 0:
           self.save_to_file(force=True)
       return removed_count

class AlreadyAddedManager:
   def __init__(self, file_path):
       self.file_path = file_path
       self.added_groups: Set[str] = set()
       self.pending_groups: Set[str] = set()
       self.failed_attempts = defaultdict(int)
       self.lock = threading.Lock()
       self.last_save_time = 0
       self.save_interval = 30
       self.load_from_file()
   
   def load_from_file(self):
       try:
           with open(self.file_path, 'r') as f:
               self.added_groups = {str(line.strip()) for line in f if line.strip()}
           logger.info(f"Loaded {len(self.added_groups)} already processed groups")
       except FileNotFoundError:
           logger.info("No existing already_added file found, starting fresh")
           self.added_groups = set()
       except Exception as e:
           logger.error(f"Error loading already_added file: {e}")
           self.added_groups = set()
   
   def save_to_file(self, force=False):
       current_time = time.time()
       if not force and current_time - self.last_save_time < self.save_interval:
           return
       
       try:
           with self.lock:
               self._atomic_write(sorted(self.added_groups))
               self.last_save_time = current_time
       except Exception as e:
           logger.error(f"Error saving already_added file: {e}")
           
   def _atomic_write(self, data):
       if os.path.exists(self.file_path):
           shutil.copy2(self.file_path, f"{self.file_path}.backup")
       
       temp_file = f"{self.file_path}.tmp"
       try:
           with open(temp_file, 'w') as f:
               for group_id in data:
                   f.write(f"{group_id}\n")
           shutil.move(temp_file, self.file_path)
       except Exception as e:
           if os.path.exists(temp_file):
               os.remove(temp_file)
           raise e
   
   def is_group_processed(self, group_id):
       group_str = str(group_id)
       with self.lock:
           return group_str in self.added_groups or group_str in self.pending_groups
   
   def mark_group_pending(self, group_id):
       group_str = str(group_id)
       with self.lock:
           if group_str not in self.added_groups:
               self.pending_groups.add(group_str)
               return True
       return False
   
   def mark_group_completed(self, group_id, success=True):
       group_str = str(group_id)
       with self.lock:
           self.pending_groups.discard(group_str)
           if success:
               self.added_groups.add(group_str)
               if group_str in self.failed_attempts:
                   del self.failed_attempts[group_str]
           else:
               self.failed_attempts[group_str] += 1
               if self.failed_attempts[group_str] >= 3:
                   self.added_groups.add(group_str)
                   logger.info(f"Marking group {group_id} as permanently failed after 3 attempts")
       
       self.save_to_file()
   
   def get_stats(self):
       with self.lock:
           return {
               'total_processed': len(self.added_groups),
               'currently_pending': len(self.pending_groups),
               'failed_attempts': len(self.failed_attempts)
           }

class Bypass:
   def __init__(self, robloxCookie, request_timeout=30):
       self.cookie = robloxCookie
       self.request_timeout = request_timeout
   
   def start_process(self):
       self.xcsrf_token = self.get_csrf_token()
       self.rbx_authentication_ticket = self.get_rbx_authentication_ticket()
       return self.get_set_cookie()
       
   def get_set_cookie(self):
       response = requests.post("https://auth.roblox.com/v1/authentication-ticket/redeem", 
                              headers={"rbxauthenticationnegotiation":"1"}, 
                              json={"authenticationTicket": self.rbx_authentication_ticket},
                              timeout=self.request_timeout)
       set_cookie_header = response.headers.get("set-cookie")
       assert set_cookie_header, "An error occurred while getting the set_cookie"
       return set_cookie_header.split(".ROBLOSECURITY=")[1].split(";")[0]

   def get_rbx_authentication_ticket(self):
       response = requests.post("https://auth.roblox.com/v1/authentication-ticket", 
                              headers={"rbxauthenticationnegotiation":"1", 
                                      "referer": "https://www.roblox.com/camel", 
                                      'Content-Type': 'application/json', 
                                      "x-csrf-token": self.xcsrf_token}, 
                              cookies={".ROBLOSECURITY": self.cookie},
                              timeout=self.request_timeout)
       assert response.headers.get("rbx-authentication-ticket"), "An error occurred while getting the rbx-authentication-ticket"
       return response.headers.get("rbx-authentication-ticket")
       
   def get_csrf_token(self) -> str:
       response = requests.post("https://auth.roblox.com/v2/logout", 
                              cookies={".ROBLOSECURITY": self.cookie},
                              timeout=self.request_timeout)
       xcsrf_token = response.headers.get("x-csrf-token")
       assert xcsrf_token, "An error occurred while getting the X-CSRF-TOKEN. Could be due to an invalid Roblox Cookie"
       return xcsrf_token

class RobloxCookie:
   def __init__(self, cookie, request_timeout=30):
       self.cookie = cookie
       self.request_timeout = request_timeout
       self._x_token = None
       self.last_generated_time = 0
       self.request_count = 0
       self.last_request_time = 0
       self.consecutive_failures = 0
       self.generate_token()

   def generate_token(self):
       try:
           response = requests.post("https://auth.roblox.com/v2/logout", 
                                  cookies={".ROBLOSECURITY": self.cookie},
                                  timeout=self.request_timeout)
           self._x_token = response.headers.get("x-csrf-token")
           self.last_generated_time = time.time()
       except requests.RequestException as e:
           logger.error(f"Failed to generate token: {e}")
           raise

   def x_token(self):
       current_time = time.time()
       if current_time - self.last_generated_time >= 120:
           self.generate_token()
       return self._x_token
   
   def can_make_request(self):
       current_time = time.time()
       if current_time - self.last_request_time < 1:
           return False
       return True

   def record_request(self, success=True):
       self.request_count += 1
       self.last_request_time = time.time()
       if success:
           self.consecutive_failures = 0
       else:
           self.consecutive_failures += 1
           
   def calculate_backoff_delay(self, base_delay, min_delay, max_delay):
       backoff_multiplier = min(2 ** self.consecutive_failures, 8)
       delay = base_delay * backoff_multiplier
       return max(min_delay, min(delay, max_delay))

class RobloxAllySender:
   def __init__(self, roblox_cookie, config, app_manager):
       self.cookie = roblox_cookie
       self.config = config
       self.app_manager = app_manager
       self.current_cursor = ''
       self.already_added = AlreadyAddedManager(config.get('already_added_file'))
       self.queue_manager = QueueManager(
           config.get('queue_file', 'queue.txt'), 
           self.already_added,
           config.get('max_queue_size', 10000)
       )
       self.stats = Statistics()
       self.circuit_breaker = CircuitBreaker(
           failure_threshold=config.get('circuit_breaker_threshold', 5),
           recovery_timeout=config.get('circuit_breaker_timeout', 300)
       )
       signal.signal(signal.SIGINT, self.signal_handler)

   def signal_handler(self, sig, frame):
       logger.info("Graceful shutdown initiated...")
       self.app_manager.running = False
       self.already_added.save_to_file(force=True)
       self.queue_manager.save_to_file(force=True)
       self.stats.log_summary()
       self.app_manager.shutdown()
       sys.exit(0)

   def send_webhook(self, message, level="INFO"):
       try:
           payload = {
               "content": f"[{level}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}"
           }
           response = requests.post(self.config.get('webhook'), json=payload, timeout=10)
           response.raise_for_status()
       except requests.RequestException as e:
           logger.error(f"Failed to send webhook: {e}")

   def make_request_with_retry(self, func, *args, max_retries=None, **kwargs):
       if max_retries is None:
           max_retries = self.config.get('max_retries', 3)
           
       if not self.circuit_breaker.can_execute():
           raise requests.RequestException("Circuit breaker is open")
           
       for attempt in range(max_retries):
           try:
               result = func(*args, **kwargs)
               self.circuit_breaker.record_success()
               return result
           except requests.RequestException as e:
               self.circuit_breaker.record_failure()
               if attempt == max_retries - 1:
                   raise e
               wait_time = (2 ** attempt) + random.uniform(0, 1)
               logger.warning(f"Request failed, retrying in {wait_time:.2f}s: {e}")
               time.sleep(wait_time)

   def scrape_assets(self):
       try:
           url = f"https://catalog.roblox.com/v1/search/items?category=Clothing&limit=120&salesTypeFilter=1&sortType=3&subcategory=ClassicShirts"
           if self.current_cursor:
               url += f"&cursor={self.current_cursor}"
           
           response = self.make_request_with_retry(
               requests.get,
               url,
               cookies={".ROBLOSECURITY": self.cookie.get_current().cookie}, 
               headers={"x-csrf-token": self.cookie.get_current().x_token()},
               timeout=self.config.get('request_timeout', 30)
           )
           response.raise_for_status()
           
           data = response.json()
           self.current_cursor = data.get("nextPageCursor", "")
           
           logger.info(f"Scraped {len(data['data'])} assets")
           return [item['id'] for item in data["data"]]
           
       except requests.RequestException as e:
           logger.error(f"Error scraping assets: {e}")
           self.send_webhook(f"Error scraping assets: {e}", "ERROR")
           return []

   def sort_assets(self, ids):
       try:
           response = self.make_request_with_retry(
               requests.post,
               "https://catalog.roblox.com/v1/catalog/items/details", 
               json={"items": [{"itemType": "Asset", "id": id} for id in ids]},
               cookies={".ROBLOSECURITY": self.cookie.get_current().cookie},
               headers={"x-csrf-token": self.cookie.get_current().x_token()},
               timeout=self.config.get('request_timeout', 30)
           )
           response.raise_for_status()
           
           groups = []
           for item in response.json()["data"]:
               if item["creatorType"] == "Group":
                   group_id = str(item["creatorTargetId"])
                   if not self.already_added.is_group_processed(group_id):
                       groups.append(group_id)
                   else:
                       logger.debug(f"Skipping already processed group: {group_id}")
           
           logger.info(f"Found {len(groups)} new groups to process from {len(ids)} assets")
           return groups
           
       except requests.RequestException as e:
           logger.error(f"Error sorting assets: {e}")
           self.send_webhook(f"Error sorting assets: {e}", "ERROR")
           return []

   def get_allies_group(self, group_id):
    try:
        all_groups = []
        start_row_index = 0
        max_allies = 50
        page_size = 50
        
        while start_row_index < max_allies:
            url = f"https://groups.roblox.com/v1/groups/{group_id}/relationships/allies?maxRows={page_size}&sortOrder=Asc&startRowIndex={start_row_index}"
            logger.debug(f"Checking allies API for group {group_id} (page {start_row_index//page_size + 1}): {url}")
            
            response = self.make_request_with_retry(
                requests.get,
                url, 
                cookies={".ROBLOSECURITY": self.cookie.get_current().cookie}, 
                headers={"x-csrf-token": self.cookie.get_current().x_token()},
                timeout=self.config.get('request_timeout', 30)
            )
            response.raise_for_status()
            
            data = response.json()
            related_groups = data.get("relatedGroups", [])
            
            if not related_groups or data.get("nextRowIndex") is None:
                logger.debug(f"Reached end of allies for group {group_id} at index {start_row_index}")
                break
            
            page_groups = []
            for group in related_groups:
                ally_group_id = str(group["id"])
                if not self.already_added.is_group_processed(ally_group_id):
                    page_groups.append(ally_group_id)
                else:
                    logger.debug(f"Skipping already processed ally: {ally_group_id}")
            
            all_groups.extend(page_groups)
            logger.debug(f"Found {len(page_groups)} new allies on page {start_row_index//page_size + 1} for group {group_id}")
            
            start_row_index += page_size
            
            if start_row_index < max_allies:
                time.sleep(1)
        
        logger.info(f"Found {len(all_groups)} total new allies from group {group_id} (limited to {max_allies} max)")
        return all_groups
        
    except requests.RequestException as e:
        logger.error(f"Error getting allies for group {group_id}: {e}")
        self.send_webhook(f"Error getting allies for group ||{group_id}||: {e}", "ERROR")
        return []

   def send_ally_request(self, group_id):
    group_str = str(group_id)
    
    self.queue_manager.remove_all_instances(group_id)
    
    if not self.already_added.mark_group_pending(group_id):
        logger.debug(f"Group {group_id} already processed, skipping")
        return False
    
    try:
        logger.info(f"Sending ally request to group: {group_id}")
        response = self.make_request_with_retry(
            requests.post,
            f"https://groups.roproxy.com/v1/groups/{self.config.get('group_id')}/relationships/allies/{group_id}",
            cookies={".ROBLOSECURITY": self.cookie.get_current().cookie}, 
            headers={"x-csrf-token": self.cookie.get_current().x_token()},
            timeout=self.config.get('request_timeout', 30)
        )
        
        self.stats.requests_sent += 1
        
        if response.status_code == 200:
            self.send_webhook(f"✅ Sent ally request to group: ||{group_id}||")
            self.already_added.mark_group_completed(group_id, success=True)
            self.stats.successful_requests += 1
            return True
        else:
            error_msg = f"❌ Group ||{group_id}|| Response ({response.status_code}): {response.text}"
            logger.info(error_msg)
            self.send_webhook(error_msg)
            
            retryable_errors = [429, 500, 502, 503, 504]
            if response.status_code in retryable_errors:
                self.already_added.mark_group_completed(group_id, success=False)
            else:
                self.already_added.mark_group_completed(group_id, success=True)
            
            self.stats.failed_requests += 1
            return False
            
    except requests.RequestException as e:
        error_msg = f"❌ Error sending ally request to group ||{group_id}||: {e}"
        logger.info(error_msg)
        self.send_webhook(error_msg, "ERROR")
        self.already_added.mark_group_completed(group_id, success=False)
        self.stats.failed_requests += 1
        return False

   def start_process(self):
       logger.info("Starting ally request process...")
       
       try:
           initial_queue_size = self.queue_manager.get_size()
           if initial_queue_size > 0:
               logger.info(f"Resuming with {initial_queue_size} groups in queue from previous session")
           
           while self.app_manager.running:
               asset_ids = self.scrape_assets()
               if asset_ids:
                   initial_groups = self.sort_assets(asset_ids)
                   if initial_groups:
                       self.queue_manager.add_groups(initial_groups)
                       logger.info(f"Added {len(initial_groups)} new groups from assets to queue")
               
               processed_count = 0
               while self.queue_manager.get_size() > 0 and self.app_manager.running:
                   current_group = self.queue_manager.get_next_group()
                   if not current_group:
                       break
                   
                   if self.already_added.is_group_processed(current_group):
                       logger.debug(f"Skipping already processed group from queue: {current_group}")
                       continue
                   
                   try:
                       request_success = self.send_ally_request(current_group)
                       if request_success:
                           processed_count += 1
                           self.cookie.get_current().record_request(success=True)
                       else:
                           self.cookie.get_current().record_request(success=False)
                   except Exception as e:
                       logger.error(f"Error processing group {current_group}: {e}")
                       self.cookie.get_current().record_request(success=False)
                       continue
                   
                   allies = self.get_allies_group(current_group)
                   if allies:
                       new_allies = [ally for ally in allies if not self.already_added.is_group_processed(ally)]
                       if new_allies:
                           self.queue_manager.add_groups(new_allies)
                           logger.info(f"Added {len(new_allies)} allies to queue. Queue size: {self.queue_manager.get_size()}")
                   
                   self.cookie.next()
                   
                   if self.queue_manager.get_size() > 0:
                       current_cookie = self.cookie.get_current()
                       base_delay = max(self.config.get('max_delay_between_requests', 60) / len(self.config.get('cookies')), self.config.get('min_delay_between_requests', 10))
                       delay = current_cookie.calculate_backoff_delay(
                           base_delay,
                           self.config.get('min_delay_between_requests', 10),
                           self.config.get('max_delay_between_requests', 60)
                       )
                       logger.debug(f"Sleeping for {delay:.2f} seconds (backoff: {current_cookie.consecutive_failures} failures)")
                       time.sleep(delay)
                   
                   if processed_count % 10 == 0:
                       stats = self.already_added.get_stats()
                       self.stats.log_summary()
                       logger.info(f"Progress: {processed_count} processed this session. Total: {stats['total_processed']}. Queue size: {self.queue_manager.get_size()}")
               
               if self.queue_manager.get_size() == 0:
                   logger.info("Queue empty, waiting before next asset scrape...")
                   time.sleep(60)
               
               self.already_added.save_to_file(force=True)
               self.queue_manager.save_to_file(force=True)
               
       except KeyboardInterrupt:
           logger.info("Process interrupted by user")
       except Exception as e:
           logger.error(f"Unexpected error: {traceback.format_exc()}")
           self.send_webhook(f"💥 CRITICAL ERROR: {traceback.format_exc()}", "CRITICAL")
       finally:
           self.already_added.save_to_file(force=True)
           self.queue_manager.save_to_file(force=True)
           stats = self.already_added.get_stats()
           logger.info(f"Final stats: {stats}")
           logger.info(f"Final queue size: {self.queue_manager.get_size()}")

class CyclicIterator:
   def __init__(self, iterable):
       if not iterable:
           raise ValueError("Cannot create iterator from empty iterable")
       self.iterable = iterable
       self.length = len(iterable)
       self.index = 0

   def __iter__(self):
       return self

   def __next__(self):
       if self.length == 0:
           raise StopIteration
       current = self.iterable[self.index]
       self.index = (self.index + 1) % self.length
       return current

   def get_current(self):
       if self.length == 0:
           return None
       return self.iterable[self.index]
       
   def next(self):
       if self.length == 0:
           return None
       self.index = (self.index + 1) % self.length
       return self.iterable[self.index]

def periodic_cleanup(queue_file, already_added_file, interval, shutdown_event):
    while not shutdown_event.is_set():
        try:
            deduplicate_queue(queue_file, already_added_file)
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")
        shutdown_event.wait(interval)

def main():
    app_manager = ApplicationManager()
    
    try:
        config = Config()
        logger.info("Configuration loaded and validated successfully")
        
        successful_logins = 0
        valid_cookies = []
        cookies = config.get('cookies')
        
        logger.info(f"Attempting to validate {len(cookies)} cookies...")
        
        for i, cookie in enumerate(cookies):
            try:
                logger.info(f"Validating cookie {i+1}/{len(cookies)}")
                bypassed_cookie = Bypass(cookie, config.get('request_timeout', 30)).start_process()
                valid_cookie = RobloxCookie(bypassed_cookie, config.get('request_timeout', 30))
                valid_cookies.append(valid_cookie)
                successful_logins += 1
                logger.info(f"Cookie {i+1} validated successfully")
            except AssertionError as e:
                logger.warning(f"Skipping invalid cookie {i+1}: {e}")
            except Exception as e:
                logger.error(f"Error validating cookie {i+1}: {e}")

        logger.info(f"Successfully logged into {successful_logins} accounts")
        
        if successful_logins == 0:
            logger.error("No valid cookies found. Exiting.")
            sys.exit(1)

        cleanup_thread = app_manager.add_thread(
            target=periodic_cleanup,
            args=(config.get('queue_file', 'queue.txt'), 
                 config.get('already_added_file'), 
                 60, 
                 app_manager.shutdown_event),
            daemon=True
        )
        app_manager.start_threads()
        
        ally_sender = RobloxAllySender(CyclicIterator(valid_cookies), config, app_manager)
        ally_sender.start_process()
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.critical(f"Critical error during startup: {traceback.format_exc()}")
        return 1
    finally:
        app_manager.shutdown()
    
    return 0

if __name__ == "__main__":
    sys.exit(main())