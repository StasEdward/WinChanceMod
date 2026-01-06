# -*- coding: utf-8 -*-
"""
API Client для отправки результатов боев на внешний сервер
"""

import json
import urllib
import urllib2
import threading
import os
import codecs
import BigWorld
import time


# Logging configuration
LOG_FILE_PATH = os.path.abspath('./mods/logs/WinChanceMod.log')

class BattleAPIClient(object):
    """Клиент для отправки данных боев на API"""
    
    def __init__(self, api_url, api_token=None,api_account_id=None,api_nickname=None,api_config=None):
        """
        Args:
            api_url (str): URL API сервера
            api_token (str): Токен авторизации (опционально)
            api_account_id (int): ID аккаунта
            api_nickname (str): Никнейм
            api_config (dict): Ссылка на конфиг API
        """
        self.api_url = api_url
        self.api_token = api_token
        self.api_account_id = api_account_id
        self.api_nickname = api_nickname
        self.timeout = 10
        self.api_config = api_config  # Ссылка на глобальный конфиг
        
        # Create log directory if it doesn't exist
        try:
            log_dir = os.path.dirname(LOG_FILE_PATH)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
        except:
            pass
    
    def _write_log(self, msg):
        try:
            import datetime
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            with open(LOG_FILE_PATH, 'a') as f:
                f.write("[{}] {}\n".format(timestamp, msg))
        except:
            pass

    def log(self, msg):
        """Логирование"""
        formatted = "[WinChanceMod] [API] {}".format(msg)
        print(formatted)
        self._write_log(formatted)
    
    def err(self, msg):
        """Логирование ошибок"""
        formatted = "[WinChanceMod] [API] ERROR: {}".format(msg)
        print(formatted)
        self._write_log(formatted)
    
    def send_battle_result(self, api_data):
        """
        Отправка результата боя на API
        
        Args:
            api_data (dict): Данные в формате BattleResultDto
        
        Returns:
            bool: True если отправка запущена успешно
        """
        try:
            self._send_async('POST', '/api/battles', api_data)
            return True
        except Exception as e:
            self.err("Failed to send battle result: {}".format(e))
            return False
    
    def send_raw_battle_result(self, battle_id, account_id, battle_time, raw_json):
        """
        Отправка сырых результатов боя на API
        
        Args:
            battle_id (int): ID боя (arenaUniqueID)
            account_id (int): ID аккаунта игрока
            battle_time (str): Время боя в формате ISO 8601
            raw_json (str): Сырой JSON с результатами боя
        
        Returns:
            bool: True если отправка запущена успешно
        """
        try:
            api_data = {
                'battleId': battle_id,
                'accountId': account_id,
                'battleTime': battle_time,
                'rawJson': raw_json
            }
            self._send_async('POST', '/api/BattlesRaw', api_data)
            return True
        except Exception as e:
            self.err("Failed to send raw battle result: {}".format(e))
            return False
    
    def _send_async(self, method, endpoint, data, retries=3, delay=5):
        """
        Асинхронная отправка данных на сервер
        
        Args:
            method (str): HTTP метод (GET, POST, PUT)
            endpoint (str): API endpoint
            data (dict): Данные для отправки
            retries (int): Количество повторных попыток
            delay (int): Задержка между попытками (сек)
        """
        def _worker():
            try:
                # Подготовка данных (делаем один раз)
                url = self.api_url + endpoint 
                self.log("Preparing JSON data for {}...".format(endpoint))
                # ensure_ascii=True - безопасная сериализация, экранирует не-ASCII символы
                json_data = json.dumps(data, ensure_ascii=True)
                self.log("JSON prepared, size: {} bytes".format(len(json_data)))
                
                # Логируем первые 500 символов для проверки содержимого
                preview = json_data[:500] + "..." if len(json_data) > 500 else json_data
                self.log("Data preview: {}".format(preview))
                
                current_try = 0
                while current_try <= retries:
                    try:
                      
                        # Создание запроса (каждый раз новый, на случай если хедеры/токен обновятся)
                        req = urllib2.Request(url, json_data)
                        req.add_header('Content-Type', 'application/json')
                        
                        if self.api_token:
                            req.add_header('Authorization', 'Bearer {}'.format(self.api_token))
                        
                        # Отправка
                        self.log("Sending {} to {} (Attempt {}/{})".format(method, url, current_try + 1, retries + 1))
                        self.log("Data size: {} bytes".format(len(json_data)))
                        
                        response = urllib2.urlopen(req, timeout=self.timeout)
                        
                        # Обработка ответа
                        response_data = json.load(response)
                        self.log("Response: {}".format(response_data.get('message', 'unknown')))
                        
                        # Если успешно - выходим
                        return
                        
                    except urllib2.HTTPError as e:
                        self.err("HTTP Error {}: {}".format(e.code, e.reason))
                        try:
                            error_data = json.load(e)
                            self.err("Error details: {}".format(error_data))
                        except:
                            pass
                        
                        # Если ошибка клиента (4xx), не ретраим (кроме таймаута мб, но пока так)
                        if 400 <= e.code < 500:
                            self.err("Client error (4xx), aborting retries.")
                            break

                    except urllib2.URLError as e:
                        self.err("URL Error: {}".format(e.reason))
                        
                    except Exception as e:
                        self.err("Send error: {}".format(e))
                        import traceback
                        self.err(traceback.format_exc())
                    
                    # Логика ретрая
                    current_try += 1
                    if current_try <= retries:
                        self.log("Retrying check in {}s...".format(delay))
                        time.sleep(delay)
                
                self.err("Failed to send data after {} attempts".format(retries + 1))
                
            except Exception as e:
                self.err("Critical error in _worker: {}".format(e))
                import traceback
                self.err(traceback.format_exc())
        
        # Запускаем в отдельном потоке
        t = threading.Thread(target=_worker)
        t.daemon = True
        t.start()
    
    def test_connection(self):
        """
        Проверка подключения к API
        
        Returns:
            bool: True если API доступен (отвечает), False иначе
        """
        try:
            self.log("Testing API connection to {}".format(self.api_url))
            url = self.api_url + '/api/health'
            
            try:
                response = urllib2.urlopen(url, timeout=5)
                # 200 OK - всё хорошо
                self.log("API connection successful (status: 200)")
                return True
            except urllib2.HTTPError as e:
                # Если 401 - сервер отвечает, просто требует авторизацию
                if e.code == 401:
                    self.log("API connection successful (status: 401, auth required)")
                    return True
                else:
                    self.err("API returned unexpected status: {}".format(e.code))
                    return False
                
        except Exception as e:
            self.err("Connection test failed: {}".format(e))
            return False
    
    def get_player_info(self):
        """Получает информацию об игроке"""
        try:
            player = BigWorld.player()
            if player and hasattr(player, 'databaseID'):
                account_id = player.databaseID
                nickname = getattr(player, 'name', 'Unknown')
                
                # Определяем регион по серверу
                try:
                    from constants import AUTH_REALM
                    region_map = {
                        'RU': 'RU',
                        'EU': 'EU',
                        'NA': 'NA',
                        'ASIA': 'ASIA'
                    }
                    region = region_map.get(AUTH_REALM, 'RU')
                except:
                    region = 'RU'
                
                return {
                    'account_id': account_id,
                    'nickname': nickname,
                    'region': region
                }
        except Exception as e:
            self.err("Error getting player info: {}".format(e))
        
        return None
    
    def register_in_api(self):
        """
        Автоматически регистрирует мод в API и получает токен
        
        Returns:
            str: Токен или None при ошибке
        """
        self.log("Registering in API...")
        try:
            # Получаем информацию об игроке
            player_info = self.get_player_info()
            if not player_info:
                self.log("Player info not yet available (normal in hangar)")
                return None
            
            url = "{}/api/auth/register".format(self.api_url)
            
            # Данные для регистрации
            register_data = {
                'AccountId': player_info['account_id'],
                'Nickname': player_info['nickname'],
                'Region': player_info['region']
            }
            
            self.log("Registering in API: {} {} REG: {}".format(
                player_info['nickname'], player_info['account_id'], player_info['region']))
            
            # Отправляем запрос
            request = urllib2.Request(url)
            request.add_header('Content-Type', 'application/json; charset=utf-8')
            data = json.dumps(register_data, ensure_ascii=False).encode('utf-8')
            
            response = urllib2.urlopen(request, data, timeout=10)
            response_data = json.loads(response.read())
            
            token = response_data.get('Token')
            
            if token:
                self.log("Registration successful! Token received.")
                
                # Обновляем локальные переменные
                self.api_token = token
                self.api_account_id = player_info['account_id']
                self.api_nickname = player_info['nickname']
                
                # Обновляем конфиг через ссылку
                if self.api_config:
                    self.api_config['token'] = token
                    self.api_config['account_id'] = player_info['account_id']
                    self.api_config['nickname'] = player_info['nickname']
                    self.api_config['region'] = player_info['region']
                    
                    # Сохраняем конфиг
                    self.save_api_config()
                
                return token
            else:
                self.err("Registration failed: no token in response")
                return None
                
        except urllib2.HTTPError as e:
            self.err("HTTP Error during registration: {} - {}".format(e.code, e.read()))
            return None
        except urllib2.URLError as e:
            self.err("URL Error during registration: {}".format(e.reason))
            return None
        except Exception as e:
            self.err("Error during registration: {}".format(e))
            import traceback
            self.err(traceback.format_exc())
            return None
    
    def save_api_config(self):
        """Сохраняет конфигурацию API"""
        self.log("Saving API config")
        try:
            config_path = './mods/configs/mod_winchance/mod_winchance_api.json'
            
            # Создаем директорию если не существует
            config_dir = os.path.dirname(config_path)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            with codecs.open(config_path, 'w', 'utf-8-sig') as f:
                json.dump(self.api_config if self.api_config else {}, f, indent=2, ensure_ascii=False)
            
            self.log("API config saved")
            return True
            
        except Exception as e:
            self.err("Error saving API config: {}".format(e))
            return False
    
    def check_and_register_if_needed(self):
        """
        Проверяет наличие токена и регистрируется если нужно
        
        Returns:
            bool: True если токен есть или получен успешно
        """
        self.log("Checking and registering if needed")
        try:
            # Если интеграция отключена - не регистрируемся
            if not self.api_config or not self.api_config.get('enabled'):
                self.log("API integration is disabled in config")
                return False
            
            # Проверяем наличие токена
            if self.api_config.get('token'):
                self.log("Token found in config")
                # Обновляем локальные переменные из конфига
                self.api_token = self.api_config.get('token')
                self.api_account_id = self.api_config.get('account_id')
                self.api_nickname = self.api_config.get('nickname')
                self.log("Loaded account_id from config: {}".format(self.api_account_id))
                return True
            
            self.log("No token found, attempting automatic registration...")
            
            # Пытаемся зарегистрироваться
            token = self.register_in_api()
            
            if token:
                self.log("Automatic registration successful!")
                return True
            else:
                self.log("Registration postponed (will retry when entering battle)")
                return False
                
        except Exception as e:
            self.err("Error in check_and_register_if_needed: {}".format(e))
            return False