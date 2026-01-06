urllibimport BigWorld
import json
import os
import threading
import urllib
import urllib2
import time
import datetime
from helpers import dependency
from skeletons.gui.app_loader import IAppLoader
from PlayerEvents import g_playerEvents
from items import vehicles as vehiclesWG
import ArenaType
from constants import ARENA_BONUS_TYPE
import Account
from api_client import BattleAPIClient
from helpers import i18n


# Constants
MOD_NAME = "WinChanceMod"
MOD_VERSION = "1.1.0"
APPLICATION_ID = "d8e40cfdafb13e426126fd330b61e104"  # User must replace this!

# API Configuration
API_CONFIG = {
    'api_url': 'https://wot.stasev.dev',  # URL вашего API
    'region': 'EU',
    'enabled': True,  # Включить/выключить отправку на API
    'token': None,  # Токен авторизации (опционально)
    'nickname': None,
    'account_id': None
}

# Logging configuration
LOG_FILE_PATH = os.path.abspath('./mods/logs/WinChanceMod.log')

def _write_to_logfile(msg):
    try:
        log_dir = os.path.dirname(LOG_FILE_PATH)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        import datetime
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        
        with open(LOG_FILE_PATH, 'a') as f:
            f.write("[{}] {}\n".format(timestamp, msg))
    except Exception as e:
        print("[WinChanceMod] Logging error: {}".format(e))

def log(msg):
    formatted = "[{}] {}".format(MOD_NAME, msg)
    print(formatted)
    _write_to_logfile(formatted)

def err(msg):
    formatted = "[{}] ERROR: {}".format(MOD_NAME, msg)
    print(formatted)
    _write_to_logfile(formatted)

def debug(msg):
    formatted = "[{}] DEBUG: {}".format(MOD_NAME, msg)
    print(formatted)
    _write_to_logfile(formatted)


class StatsFetcher(object):
    def __init__(self, app_id):
        self.app_id = app_id
        self.base_url = "https://api.worldoftanks.eu/wot/account/info/"

    def fetch_stats(self, account_ids, callback):
        def _worker():
            try:
                log("fetch_stats worker started")
                if not account_ids:
                    log("No account IDs provided")
                    callback({})
                    return
                
                ids_str = ",".join(map(str, account_ids))
                url = "{}?application_id={}&account_id={}&fields=global_rating".format(
                    self.base_url, self.app_id, ids_str)
                
                log("Fetching stats for {} players...".format(len(account_ids)))
                log("API URL: {}".format(url[:100] + "..."))
                
                response = urllib2.urlopen(url, timeout=10)
                data = json.load(response)
                
                log("API response status: {}".format(data.get('status', 'unknown')))
                
                if data.get('status') == 'ok':
                    result_data = data.get('data', {})
                    log("API returned data for {} players".format(len(result_data)))
                    callback(result_data)
                else:
                    err("API Error: {}".format(data.get('error', 'unknown')))
                    callback({})
                    
            except urllib2.HTTPError as e:
                err("HTTP Error {}: {}".format(e.code, e.reason))
                callback({})
            except urllib2.URLError as e:
                err("URL Error: {}".format(e.reason))
                callback({})
            except Exception as e:
                err("Fetch Error: {}".format(e))
                import traceback
                err(traceback.format_exc())
                callback({})

        log("Starting stats fetch thread...")
        t = threading.Thread(target=_worker)
        t.daemon = True  # Делаем поток демоном чтобы не блокировать выход
        t.start()
        log("Stats fetch thread started")


class WinChanceCalculator(object):
    @staticmethod
    def calculate_win_chance(team_ratings, enemy_ratings):
        # Simple logic: Compare average ratings
        def get_avg(ratings):
            valid_ratings = [r for r in ratings if r > 0]
            if not valid_ratings:
                return 0
            return sum(valid_ratings) / float(len(valid_ratings))

        avg_team = get_avg(team_ratings)
        avg_enemy = get_avg(enemy_ratings)

        if avg_team == 0 and avg_enemy == 0:
            return 50.0

        total = avg_team + avg_enemy
        if total == 0:
            return 50.0
            
        chance = (avg_team / total) * 100.0
        return max(0.0, min(100.0, chance))


# Main Mod Controller
class WinChanceMod(object):
    appLoader = dependency.descriptor(IAppLoader)

    def __init__(self):
        self.stats_fetcher = StatsFetcher(APPLICATION_ID)
        self.started = False
        self.overlay = None  # Будет создан при старте боя
        
        # Сохраняем статистику WGR для использования после боя
        self.win_chance = 0.0
        self.ally_wgr = 0.0
        self.enemy_wgr = 0.0
        self.player_vehicle_info = None
        self.api_initialized = False  # Флаг инициализации API
        self.current_space_id = 0  # Текущий Space ID (3=ангар, 4=загрузка, 5=бой)
        self.pending_loop_active = False  # Флаг активности цикла проверки pending боёв
        
    def start(self):
        if self.started: 
            return
        self.started = True
        log("Started v{}".format(MOD_VERSION))
        
        self.appLoader.onGUISpaceEntered += self.on_gui_space_entered
        self.appLoader.onGUISpaceLeft += self.on_gui_space_left
        g_playerEvents.onBattleResultsReceived += self.on_battle_results_received
        

    # Persistence Logic
    PENDING_BATTLES_FILE = os.path.abspath('./mods/configs/mod_winchance/pending_battles.json')
    BATTLE_CONTEXT_DIR = os.path.abspath('./mods/configs/mod_winchance/battle_context/')

    def load_battle_context(self, arena_id):
        """Loads context for specific arena from its file"""
        if not arena_id: return {}
        try:
            file_path = os.path.join(self.BATTLE_CONTEXT_DIR, '{}.json'.format(arena_id))
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            err("Error loading battle context for {}: {}".format(arena_id, e))
        return {}

    def save_battle_context(self, arena_id, context_data):
        """Saves context data for a specific arena to a separate file"""
        try:
            if not os.path.exists(self.BATTLE_CONTEXT_DIR):
                os.makedirs(self.BATTLE_CONTEXT_DIR)
            
            # Load existing if any to merge updates
            current_data = self.load_battle_context(arena_id)
            current_data.update(context_data)
            
            file_path = os.path.join(self.BATTLE_CONTEXT_DIR, '{}.json'.format(arena_id))
            with open(file_path, 'w') as f:
                json.dump(current_data, f)
            log("Saved context to {}".format(file_path))
        except Exception as e:
            err("Error saving battle context: {}".format(e))
            
    def delete_battle_context(self, arena_id):
        """Removes context file for an arena"""
        try:
            file_path = os.path.join(self.BATTLE_CONTEXT_DIR, '{}.json'.format(arena_id))
            if os.path.exists(file_path):
                os.remove(file_path)
                log("Deleted context file {}".format(file_path))
        except:
            pass

    def load_pending_battles(self):
        try:
            if os.path.exists(self.PENDING_BATTLES_FILE):
                with open(self.PENDING_BATTLES_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            err("Error loading pending battles: {}".format(e))
        return []

    def save_pending_battles(self, battles):
        try:
            config_dir = os.path.dirname(self.PENDING_BATTLES_FILE)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            with open(self.PENDING_BATTLES_FILE, 'w') as f:
                json.dump(battles, f)
        except Exception as e:
            err("Error saving pending battles: {}".format(e))

    def add_pending_battle(self, arena_id):
        battles = self.load_pending_battles()
        if arena_id not in battles:
            battles.append(arena_id)
            self.save_pending_battles(battles)
            log("Added pending battle ID: {}".format(arena_id))

    def remove_pending_battle(self, arena_id):
        battles = self.load_pending_battles()
        if arena_id in battles:
            battles.remove(arena_id)
            self.save_pending_battles(battles)
            log("Removed pending battle ID: {}".format(arena_id))

    def check_pending_battles_loop(self):
        # Не запрашиваем результаты если игрок не в ангаре (Space 3)
        # Space 1 = загрузка, 4 = загрузка боя, 5 = бой
        if self.current_space_id != 3:
            log("Skipping pending check - not in hangar (space={})".format(self.current_space_id))
            self.pending_loop_active = False
            return
        
        battles = self.load_pending_battles()
        if not battles:
            self.pending_loop_active = False
            return
        
        self.pending_loop_active = True
        log("Polling {} pending battles...".format(len(battles)))
        for arena_id in battles:
            self.request_battle_results(arena_id)
            
        # Schedule next check только если всё ещё в ангаре
        if self.current_space_id == 3:
            BigWorld.callback(5.0, self.check_pending_battles_loop)
        else:
            self.pending_loop_active = False

    def on_gui_space_entered(self, spaceID):
        log("Space entered: {}".format(spaceID))
        self.current_space_id = spaceID  # Сохраняем текущий Space ID
        
        # Space ID 15/3 = Hangar 
        # Note: 3 is Lobby (Hangar). 
        if spaceID == 3:
            # Check pending battles (только если цикл ещё не активен)
            if not self.pending_loop_active:
                self.check_pending_battles_loop()

            # Initialize API
            if not self.api_initialized and self.api_client:
                self.api_initialized = True
                log("Entering hangar, initializing API connection...")
                if self.api_client.test_connection():
                    self.api_client.check_and_register_if_needed()
                else:
                    log("API is not available, will retry on next hangar entry")
                    self.api_initialized = False 
        
        # Space ID 5 = Battle
        if spaceID == 5:
            # Record current arena ID for persistence
            try:
                player = BigWorld.player()
                if player and hasattr(player, 'arenaUniqueID'):
                     self.add_pending_battle(player.arenaUniqueID)
                else:
                    # Retry if player not ready
                    BigWorld.callback(5.0, self.retry_add_pending_battle)
            except Exception as e:
                err("Error adding pending battle on entry: {}".format(e))

            # Создаем overlay для боя
            if self.overlay is None:
                self.overlay = DraggableWinChanceWindow()
                self.overlay.create()
            # Delay slightly to ensure player is ready
            BigWorld.callback(15.0, self.calculate_battle_stats)

    def retry_add_pending_battle(self):
        try:
             player = BigWorld.player()
             if player and hasattr(player, 'arenaUniqueID'):
                 self.add_pending_battle(player.arenaUniqueID)
        except:
            pass

    def on_gui_space_left(self, spaceID):
        # Space ID 5 = Battle
        if spaceID == 5:
            # Уничтожаем overlay при выходе из боя
            if self.overlay:
                self.overlay.destroy()
                self.overlay = None

    def calculate_battle_stats(self):
        log("Calculating stats...")
        player = BigWorld.player()
        if not player or not hasattr(player, 'arena'):
            log("Player/Arena not ready, retrying...")
            BigWorld.callback(1.0, self.calculate_battle_stats)
            return
        
        # Ensure we captured the ID (redundant check)
        try:
             if hasattr(player, 'arenaUniqueID'):
                 self.add_pending_battle(player.arenaUniqueID)
        except: pass

        arena = player.arena
        if not arena:
            log("No arena found")
            return

        vehicles = arena.vehicles
        log("Vehicles count: {}".format(len(vehicles)))
        
        player_team = player.team
        
        team_ids = []
        enemy_ids = []
        
        for v_id, v_info in vehicles.items():
            acc_id = v_info.get('accountDBID')
            if not acc_id: 
                continue
         
            if v_info['team'] == player_team:
                team_ids.append(acc_id)
            else:
                enemy_ids.append(acc_id)
        
        log("Team IDs: {}, Enemy IDs: {}".format(len(team_ids), len(enemy_ids)))
        all_ids = team_ids + enemy_ids
        
        # =======================================================================
        # ВАЖНО: Захватываем ВСЕ данные ДО асинхронного вызова!
        # К моменту callback'а игрок может уже быть в другом бою!
        # =======================================================================
        current_arena_id = None
        captured_vehicle_info = None
        captured_map_name = 'Unknown'
        
        try:
            if player and hasattr(player, 'arenaUniqueID'):
                current_arena_id = player.arenaUniqueID
                log("Captured arena_id: {}".format(current_arena_id))
        except Exception as e:
            err("Failed to capture arena_id: {}".format(e))
        
        # Захватываем информацию о танке СЕЙЧАС, пока player валиден
        try:
            if player and hasattr(player, 'vehicleTypeDescriptor'):
                veh_descr = player.vehicleTypeDescriptor
                vehicle_type = getattr(veh_descr, 'type', None)
                vehicle_id = getattr(vehicle_type, 'compactDescr', 0) if vehicle_type else 0
                # Используем техническое имя (name) вместо локализованного (userString)
                # name имеет формат "nation:tank_code", например "france:F97_ELC_EVEN_90"
                vehicle_name = getattr(vehicle_type, 'name', 'Unknown') if vehicle_type else 'Unknown'
                vehicle_tags = getattr(vehicle_type, 'tags', frozenset()) if vehicle_type else frozenset()
                
                vehicle_type_class = 'unknown'
                if vehicle_tags:
                    tags_list = list(vehicle_tags)
                    for tag in tags_list:
                        if 'Tank' in tag or 'SPG' in tag:
                            vehicle_type_class = tag
                            break
                    if vehicle_type_class == 'unknown' and tags_list:
                        vehicle_type_class = tags_list[0]
                
                vehicle_name_parts = getattr(vehicle_type, 'name', '') if vehicle_type else ''
                vehicle_nation = vehicle_name_parts.split(':')[0] if vehicle_name_parts and ':' in vehicle_name_parts else 'unknown'
                
                captured_vehicle_info = {
                    'id': vehicle_id,
                    'name': vehicle_name,
                    'tier': veh_descr.level if hasattr(veh_descr, 'level') else 0,
                    'type': vehicle_type_class,
                    'nation': vehicle_nation
                }
                log("Captured vehicle info: {} (Tier {})".format(vehicle_name, captured_vehicle_info['tier']))
        except Exception as e:
            err("Failed to capture vehicle info: {}".format(e))
            captured_vehicle_info = {'id': 0, 'name': 'Unknown', 'tier': 0, 'type': 'unknown', 'nation': 'unknown'}
        
        # Захватываем название карты СЕЙЧАС
        try:
            if arena and hasattr(arena, 'arenaType') and hasattr(arena.arenaType, 'geometryName'):
                geometry_name = arena.arenaType.geometryName
                captured_map_name = i18n.makeString('#arenas:%s/name' % geometry_name)
                if not captured_map_name or captured_map_name.startswith('#arenas:'):
                    captured_map_name = geometry_name
                log("Captured map name: {}".format(captured_map_name))
        except Exception as e:
            err("Failed to capture map name: {}".format(e))
        
        # Сохраняем захваченные данные в контекст СРАЗУ (до async операции)
        if current_arena_id:
            initial_context = {
                'TankId': captured_vehicle_info['id'] if captured_vehicle_info else 0,
                'TankName': captured_vehicle_info['name'] if captured_vehicle_info else 'Unknown',
                'TankTier': captured_vehicle_info['tier'] if captured_vehicle_info else 0,
                'TankType': captured_vehicle_info['type'] if captured_vehicle_info else 'unknown',
                'TankNation': captured_vehicle_info['nation'] if captured_vehicle_info else 'unknown',
                'MapName': captured_map_name
            }
            self.save_battle_context(current_arena_id, initial_context)
            log("Saved initial context (tank+map) for arena {}".format(current_arena_id))
        
        def on_stats_received(data):
            log("Stats received, data len: {}".format(len(data)))
            team_ratings = []
            enemy_ratings = []
            team_wgr_xvm = []
            enemy_wgr_xvm = []
            
            for acc_id in team_ids:
            #    debug("Team ID: {}".format(acc_id))
                p_data = data.get(str(acc_id))
                if p_data:
                    wgr = p_data.get('global_rating', 0)
            #    debug("Player WGR: {}".format(wgr))
                    team_ratings.append(wgr)
                    if wgr > 0:
                        team_wgr_xvm.append(wgr)
 
            for acc_id in enemy_ids:
            #    debug("Enemy ID: {}".format(acc_id))
                p_data = data.get(str(acc_id))
                if p_data:
                    wgr = p_data.get('global_rating', 0)
            #    debug("Enemy WGR: {}".format(wgr))
                    enemy_ratings.append(wgr)
                    if wgr > 0:
                        enemy_wgr_xvm.append(wgr)
            
            chance = WinChanceCalculator.calculate_win_chance(team_ratings, enemy_ratings)
            
            # Calculate average WGR on XVM scale
            avg_team_wgr = sum(team_wgr_xvm) / float(len(team_wgr_xvm)) if team_wgr_xvm else 0
            avg_enemy_wgr = sum(enemy_wgr_xvm) / float(len(enemy_wgr_xvm)) if enemy_wgr_xvm else 0
            
            # Сохраняем статистику для использования после боя
            self.win_chance = chance
            self.ally_wgr = avg_team_wgr
            self.enemy_wgr = avg_enemy_wgr
            
            # --- PERSISTENCE: Save WinChance and WGR to context ---
            context_update = {
                'WinChance': chance,
                'AllyWgr': avg_team_wgr,
                'EnemyWgr': avg_enemy_wgr
            }
            # Используем захваченный arena_id из closure (не от player, т.к. он может быть уже недоступен)
            if current_arena_id:
                self.save_battle_context(current_arena_id, context_update)
                log("Saved WinChance/WGR context for arena {}".format(current_arena_id))
            else:
                err("CRITICAL: Cannot save WinChance - no arena_id available!")
            # ------------------------------------------------------
            
            log("Calculated Chance: {:.1f}%".format(chance))
            log("Avg Team WGR: {:.0f}".format(avg_team_wgr))
            log("Avg Enemy WGR: {:.0f}".format(avg_enemy_wgr))
            
            display_text = "Win Chance: {:.1f}%\nTeam WGR: {:.0f} | Enemy WGR: {:.0f}".format(
                    chance, avg_team_wgr, avg_enemy_wgr)
 
            # Используем overlay вместо show_text
            log("Attempting to display data...")
            if self.overlay:
                log("Calling overlay.update_text()")
                self.overlay.update_text(display_text)
            else:
                err("Overlay is None! Cannot display data")
                log(display_text)
        
        # ВАЖНО: Вызываем fetch_stats здесь, а не в stop()!
        log("Calling fetch_stats for {} account IDs...".format(len(all_ids)))
        self.stats_fetcher.fetch_stats(all_ids, on_stats_received)

    def request_battle_results(self, arena_id):
        """Request battle results from cache/server"""
        log("Requesting battle results for arena {}".format(arena_id))
        
        # Валидация arena_id - должен быть разумным числом
        try:
            arena_id_int = int(arena_id)
            # Типичные arena_id имеют длину 16-19 цифр
            if arena_id_int <= 0 or arena_id_int > 10**20:
                err("Invalid arena_id: {} - removing from pending".format(arena_id))
                self.remove_pending_battle(arena_id)
                self.delete_battle_context(arena_id)
                return
        except (ValueError, TypeError):
            err("Invalid arena_id format: {} - removing from pending".format(arena_id))
            self.remove_pending_battle(arena_id)
            return
        
        # Не запрашиваем если не в ангаре
        if self.current_space_id != 3:
            log("Skipping request - not in hangar (space={})".format(self.current_space_id))
            return
        
        try:
            # We need to access the account repository
            import Account 
            if Account.g_accountRepository:
                 if Account.g_accountRepository.battleResultsCache:
                     # Оборачиваем в try-except на случай ошибок XVM и др. модов
                     try:
                         Account.g_accountRepository.battleResultsCache.get(
                             arena_id, 
                             lambda code, res: self.on_battle_results_callback(code, res, arena_id)
                         )
                     except Exception as e:
                         # НЕ удаляем данные при временных ошибках!
                         # Это может быть из-за загрузки боя или других временных проблем
                         err("BattleResultsCache.get failed: {} - will retry later".format(e))
                         # НЕ вызываем remove_pending_battle и delete_battle_context!
                 else:
                     log("BattleResultsCache is None - will retry later")
            else:
                log("AccountRepository is None - will retry later")
        except Exception as e:
            err("Error requesting battle results: {} - will retry later".format(e))
            import traceback
            err(traceback.format_exc())
            
    def read_battle_result_from_dat(self, arena_id):
        """Читает результаты боя из .dat файла и сохраняет в JSON"""
        try:
            import os
            import glob
            
            # Путь к папке с результатами боев
            appdata = os.environ.get('APPDATA', '')
            if not appdata:
                err("APPDATA environment variable not found")
                return None
            
            battle_results_dir = os.path.join(appdata, 'Wargaming.net', 'WorldOfTanks', 'battle_results')
            
            if not os.path.exists(battle_results_dir):
                log("Battle results directory not found: {}".format(battle_results_dir))
                return None
            
            log("Scanning battle results directory: {}".format(battle_results_dir))
            
            # Ищем все .dat файлы во всех подпапках
            dat_files = []
            subdirs_scanned = []
            
            for root, dirs, files in os.walk(battle_results_dir):
                # Логируем найденные подпапки
                if root != battle_results_dir:
                    subdir_name = os.path.basename(root)
                    subdirs_scanned.append(subdir_name)
                    log("Scanning subfolder: {}".format(subdir_name))
                
                # Ищем .dat файлы в текущей папке
                for file in files:
                    if file.endswith('.dat'):
                        full_path = os.path.join(root, file)
                        dat_files.append(full_path)
                        log("Found .dat file: {} in {}".format(file, os.path.basename(root)))
            
            log("Total subfolders scanned: {}".format(len(subdirs_scanned)))
            log("Total .dat files found: {}".format(len(dat_files)))
            
            if not dat_files:
                log("No .dat files found in {}".format(battle_results_dir))
                return None
            
            # Пытаемся загрузить результаты используя BattleResultsCache
            try:
                from BattleReplay import BattleResultsCache
                
                for dat_file in dat_files:
                    try:
                        # Читаем файл
                        with open(dat_file, 'rb') as f:
                            data = f.read()
                        
                        if not data:
                            continue
                        
                        # Используем BattleResultsCache для конвертации
                        cache = BattleResultsCache()
                        full_results = cache.convertToFullForm(data)
                        
                        if not full_results:
                            continue
                        
                        # Проверяем, соответствует ли arena_id
                        file_arena_id = full_results.get('arenaUniqueID')
                        if file_arena_id == arena_id:
                            log("Found matching battle result in {}".format(os.path.basename(dat_file)))
                            
                            # Сохраняем в JSON
                            self.save_battle_result_json(arena_id, full_results)
                            return full_results
                    
                    except Exception as e:
                        debug("Error reading {}: {}".format(os.path.basename(dat_file), e))
                        continue
                
                log("No matching .dat file found for arena {}".format(arena_id))
                return None
                
            except ImportError:
                err("BattleResultsCache import failed")
                return None
            
        except Exception as e:
            err("Error in read_battle_result_from_dat: {}".format(e))
            import traceback
            err(traceback.format_exc())
            return None
    
    def save_battle_result_json(self, arena_id, results):
        """Сохраняет результаты боя в JSON файл"""
        try:
            save_dir = os.path.abspath('./mods/configs/mod_winchance/battle_results_backup/')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            file_path = os.path.join(save_dir, '{}.json'.format(arena_id))
            
            with open(file_path, 'w') as f:
                json.dump(results, f, indent=2)
            
            log("Saved battle results to {}".format(file_path))
        except Exception as e:
            err("Error saving battle results JSON: {}".format(e))

    def save_raw_battle_results(self, arena_id, results):
        """Сохраняет сырые результаты боя из callback в JSON для анализа"""
        try:
            save_dir = os.path.abspath('./mods/configs/mod_winchance/raw_battle_results/')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            
            file_path = os.path.join(save_dir, '{}.json'.format(arena_id))
            
            # Конвертируем данные в JSON-совместимый формат
            # (некоторые значения могут быть не сериализуемы)
            def make_serializable(obj, is_key=False):
                if isinstance(obj, dict):
                    return {make_serializable(k, is_key=True): make_serializable(v) for k, v in obj.items()}
                elif is_key:
                    # Ключи словаря должны быть строками
                    if isinstance(obj, (list, tuple)):
                        return str(obj)
                    return str(obj)
                elif isinstance(obj, (list, tuple)):
                    return [make_serializable(item) for item in obj]
                elif isinstance(obj, (int, float, bool, type(None))):
                    return obj
                elif isinstance(obj, bytes):
                    # Python 2: str это bytes
                    try:
                        return obj.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            return obj.decode('cp1251')
                        except UnicodeDecodeError:
                            return obj.decode('latin-1')
                elif isinstance(obj, (frozenset, set)):
                    return [make_serializable(item) for item in obj]
                else:
                    try:
                        return str(obj).decode('utf-8')
                    except:
                        return repr(obj)
            
            serializable_results = make_serializable(results)
            
            with open(file_path, 'w') as f:
                json.dump(serializable_results, f, indent=2, ensure_ascii=False)
            
            log("Saved raw battle results to {}".format(file_path))
            
            # Отправляем сырые данные на API
            if self.api_client:
                try:
                    # Сериализуем в JSON строку для отправки
                    raw_json = json.dumps(serializable_results, ensure_ascii=True)
                    
                    # Получаем account_id из результатов боя (несколько источников)
                    account_id = 0
                    
                    # 1. Пробуем из personal -> avatar -> accountDBID
                    personal = results.get('personal', {})
                    if personal:
                        avatar_data = personal.get('avatar', {})
                        if avatar_data and 'accountDBID' in avatar_data:
                            account_id = avatar_data.get('accountDBID', 0)
                            log("Got account_id from personal.avatar: {}".format(account_id))
                        
                        # 2. Пробуем из первого элемента personal (vehicle_cd -> data -> avatar)
                        if not account_id:
                            for key, data in personal.items():
                                if isinstance(data, dict) and 'avatar' in data:
                                    account_id = data['avatar'].get('accountDBID', 0)
                                    if account_id:
                                        log("Got account_id from personal[{}].avatar: {}".format(key, account_id))
                                        break
                    
                    # 3. Fallback - из BigWorld.player()
                    if not account_id:
                        player_info = self.api_client.get_player_info()
                        if player_info:
                            account_id = player_info.get('account_id', 0)
                            log("Got account_id from BigWorld.player: {}".format(account_id))
                    
                    # 4. Fallback - из сохранённого api_account_id в клиенте
                    if not account_id and self.api_client.api_account_id:
                        account_id = self.api_client.api_account_id
                        log("Got account_id from api_client.api_account_id: {}".format(account_id))
                    
                    if not account_id:
                        err("WARNING: Could not get account_id from any source!")
                    
                    # Время боя из arenaCreateTime + duration
                    common = results.get('common', {})
                    arena_create_time = common.get('arenaCreateTime', 0)
                    duration = common.get('duration', 0)
                    
                    if arena_create_time > 0:
                        end_ts = arena_create_time + duration
                        battle_time = datetime.datetime.fromtimestamp(end_ts).strftime('%Y-%m-%dT%H:%M:%S')
                    else:
                        battle_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                    
                    self.api_client.send_raw_battle_result(
                        battle_id=int(arena_id) if arena_id else 0,
                        account_id=account_id,
                        battle_time=battle_time,
                        raw_json=raw_json
                    )
                    log("Raw battle results sent to API")
                except Exception as e:
                    err("Error sending raw results to API: {}".format(e))
                    
        except Exception as e:
            err("Error saving raw battle results: {}".format(e))
            import traceback
            err(traceback.format_exc())

    def on_battle_results_callback(self, responseCode, results, arena_id):
        """Callback for battle results request - обрабатывает результаты независимо от текущего Space"""
        try:
            # Check if results are valid
            if results and (responseCode == 0 or responseCode == 1): # Accept success codes
                log("Battle results retrieved via callback for {} (space={})".format(arena_id, self.current_space_id))
                self.remove_pending_battle(arena_id)
                self.on_hangar_battle_results(0, results)
            elif results:
                # Even if code is weird, if we have results, take them.
                log("Battle results retrieved (Code {}) for {} (space={})".format(responseCode, arena_id, self.current_space_id))
                self.remove_pending_battle(arena_id)
                self.on_hangar_battle_results(0, results)
            else:
                # No results - will retry on next poll
                log("No results yet for {} (Code: {}) - will retry".format(arena_id, responseCode))
                 
        except Exception as e:
            err("Error in on_battle_results_callback: {}".format(e))

    def on_battle_results_received(self, isPlayerVehicle, results):
        """Event handler - получает результаты от игры независимо от текущего Space"""
        if not isPlayerVehicle or not results:
            return
        
        try:
             arena_id = results.get('arenaUniqueID')
             log("Received onBattleResultsReceived for arena {} (space={})".format(arena_id, self.current_space_id))
             # Обрабатываем результаты независимо от Space - даже если мы уже в новом бою
             self.remove_pending_battle(arena_id)
             self.on_hangar_battle_results(0, results)
        except Exception as e:
            err("Error in on_battle_results_received: {}".format(e))

    def on_hangar_battle_results(self, responseCode, results):
        """Process battle results (from cache or event)"""
        try:
            log("Processing battle results...")
            
            if not results:
                return

            # Сохраняем сырые результаты боя в JSON для анализа
            arena_id = results.get('arenaUniqueID')
            self.save_raw_battle_results(arena_id, results)

            common = results.get('common', {})
            personal = results.get('personal', {})
            players = results.get('players', {})
            vehicles = results.get('vehicles', {})
            
            arena_id = results.get('arenaUniqueID')
            winner_team = common.get('winnerTeam', 0)
            duration = common.get('duration', 0)
            arena_create_time = common.get('arenaCreateTime', 0)
            arena_type_id = common.get('arenaTypeID', 0)
            bonus_type = common.get('bonusType', 0)
            
            # Load preserved context (WinChance, WGR, Tank Info from battle start)
            context_data = self.load_battle_context(arena_id)
            
            log("Loaded context for arena {}: {}".format(arena_id, context_data.keys()))

            # Map Name extraction
            map_name = context_data.get('MapName', 'Unknown')
            # Fallback if not in context
            if map_name == 'Unknown' or not map_name:
                try:
                    if arena_type_id in ArenaType.g_cache:
                        at = ArenaType.g_cache[arena_type_id]
                        # Try exact same key as in battle
                        map_name = i18n.makeString('#arenas:%s/name' % at.geometryName)
                        if map_name.startswith('#arenas:'):
                             map_name = at.geometryName # Fallback to technical name
                except: pass

            # Battle Time
            try:
                if arena_create_time > 0:
                    end_ts = arena_create_time + duration
                    battle_time = datetime.datetime.fromtimestamp(end_ts).strftime('%Y-%m-%dT%H:%M:%S')
                else:
                     battle_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            except:
                battle_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


            # Player Personal Data
            personal_data = {}
            vehicle_cd = 0
            if personal:
                try:
                    vehicle_cd, personal_data = personal.items()[0]
                except: pass
            
            player_team = 1
            if 'avatar' in personal_data:
                 player_team = personal_data['avatar']['team']
            elif 'avatar' in personal:
                 player_team = personal['avatar']['team']
            
            if winner_team == 0:
                battle_result = "draw"
            elif winner_team == player_team:
                battle_result = "win"
            else:
                battle_result = "lose"

            # Parse Vehicle Info
            # Priority: Context > Derived from Result
            tank_info = {
                'id': context_data.get('TankId', 0),
                'name': context_data.get('TankName', 'Unknown'),
                'tier': context_data.get('TankTier', 0),
                'type': context_data.get('TankType', 'unknown'),
                'nation': context_data.get('TankNation', 'unknown')
            }
            
            # Formatting tank name to just "ELC EVEN 90" instead of "france:F97_ELC_EVEN_90" if possible
            # Context usually saves localized name if available, but let's check.
            # If context is missing/empty, derive from vehicle_cd
            if not tank_info['id'] and vehicle_cd:
                tank_info['id'] = vehicle_cd
                try:
                    vt = vehiclesWG.getVehicleType(vehicle_cd)
                    # Используем техническое имя вместо локализованного
                    tank_info['name'] = vt.name
                    tank_info['tier'] = vt.level
                except: pass


            # Extract Stats
            damage_dealt = personal_data.get('damageDealt', 0)
            damage_assisted = personal_data.get('damageAssistedRadio', 0) + personal_data.get('damageAssistedTrack', 0) + personal_data.get('damageAssistedStun', 0)
            damage_blocked = personal_data.get('damageBlockedByArmor', 0)
            kills = personal_data.get('kills', 0)
            spotted = personal_data.get('spotted', 0)
            xp = personal_data.get('originalXP', 0) 
            credits_ = personal_data.get('originalCredits', 0)
            shots = personal_data.get('shots', 0)
            hits = personal_data.get('directEnemyHits', 0)
            piercings = personal_data.get('piercingEnemyHits', 0)

            # WinChance and WGR from context
            win_chance = context_data.get('WinChance', 0.0)
            ally_wgr = context_data.get('AllyWgr', 0.0)
            enemy_wgr = context_data.get('EnemyWgr', 0.0)
            
            # Предупреждение если данные WinChance отсутствуют
            if win_chance == 0.0 and ally_wgr == 0.0 and enemy_wgr == 0.0:
                err("WARNING: WinChance/WGR data missing for arena {}! Context keys: {}".format(
                    arena_id, list(context_data.keys())))
                err("This likely means the API stats request didn't complete before battle ended.")
                # НЕ используем self.win_chance как fallback - эти значения могут быть от ДРУГОГО боя!
                # Лучше отправить 0 чем неправильные данные

            log("Result: {} Map: {} Tank: {} DMG: {} Chance: {:.1f}".format(
                battle_result, map_name, tank_info['name'], damage_dealt, win_chance))
            
            # Сериализуем результаты боя для DetailedStats (как объект, не строка)
            serializable_results = {}
            try:
                def make_serializable(obj, is_key=False):
                    if isinstance(obj, dict):
                        return {make_serializable(k, is_key=True): make_serializable(v) for k, v in obj.items()}
                    elif is_key:
                        # Ключи словаря должны быть строками
                        if isinstance(obj, (list, tuple)):
                            return str(obj)
                        return str(obj)
                    elif isinstance(obj, (list, tuple)):
                        return [make_serializable(item) for item in obj]
                    elif isinstance(obj, (int, float, bool, type(None))):
                        return obj
                    elif isinstance(obj, bytes):
                        # Python 2: str это bytes, нужно декодировать
                        try:
                            return obj.decode('utf-8')
                        except UnicodeDecodeError:
                            try:
                                return obj.decode('cp1251')
                            except UnicodeDecodeError:
                                return obj.decode('latin-1')  # fallback, всегда работает
                    elif isinstance(obj, (frozenset, set)):
                        return [make_serializable(item) for item in obj]
                    else:
                        # Для unicode и прочих типов
                        try:
                            return obj if isinstance(obj, basestring) else str(obj).decode('utf-8')
                        except:
                            return repr(obj)
                
                serializable_results = make_serializable(results)
                # Сериализуем в строку для DetailedStatsJson
                detailed_stats_json = json.dumps(serializable_results, ensure_ascii=True)
                log("DetailedStats prepared, keys: {}, size: {} bytes".format(
                    list(serializable_results.keys()) if serializable_results else [], len(detailed_stats_json)))
            except Exception as e:
                err("Error serializing detailed stats: {}".format(e))
                import traceback
                err(traceback.format_exc())
                serializable_results = {}
                detailed_stats_json = None
            
            # Определяем победу/поражение/ничью
            if winner_team == 0:
                # winner_team = 0 означает ничью
                result_str = 'draw'
            elif winner_team == player_team:
                # Наша команда победила
                result_str = 'win'
            else:
                # Вражеская команда победила
                result_str = 'lose'
            dto = {
                'ArenaUniqueId': str(arena_id),
                'BattleTime': battle_time,
                'Duration': duration,
                'MapName': map_name,
                'BattleType': bonus_type,
                'Team': player_team,
                'WinnerTeam': winner_team,
                'Result': result_str,
                'DamageDealt': damage_dealt,
                'DamageAssisted': damage_assisted,
                'DamageBlocked': damage_blocked,
                'Kills': kills,
                'Spotted': spotted,
                'Experience': xp,
                'Credits': credits_,
                'Shots': shots,
                'Hits': hits,
                'Penetrations': piercings,
                'WinChance': win_chance,
                'AllyWgr': ally_wgr,
                'EnemyWgr': enemy_wgr,
                'Tank': {
                    'TankId': tank_info['id'],
                    'Name': tank_info['name'],
                    'Tier': tank_info['tier'],
                    'Type': tank_info['type'],
                    'Nation': tank_info['nation']
                },
                'DetailedStatsJson': detailed_stats_json,
                'DetailedStats': serializable_results
            }
            
            # Сохраняем отправляемый DTO для отладки
            try:
                dto_save_dir = os.path.abspath('./mods/configs/mod_winchance/sent_dto/')
                if not os.path.exists(dto_save_dir):
                    os.makedirs(dto_save_dir)
                dto_file = os.path.join(dto_save_dir, '{}.json'.format(arena_id))
                with open(dto_file, 'w') as f:
                    json.dump(dto, f, indent=2, ensure_ascii=True)
                log("Saved DTO to {}".format(dto_file))
            except Exception as e:
                err("Error saving DTO: {}".format(e))
            
            if self.api_client:
                success = self.api_client.send_battle_result(dto)
                if success:
                    # Cleanup context after successful send
                    self.delete_battle_context(arena_id)
                
        except Exception as e:
            err("Error in on_hangar_battle_results: {}".format(e))
            import traceback
            err(traceback.format_exc())

# Remove the old hook code 
# Hook ServiceChannelManager via MessengerEntry instance (Listener Swap)
# try:
#     from messenger import MessengerEntry
#     from messenger.m_constants import PROTO_TYPE
#     from chat_shared import SYS_MESSAGE_TYPE, CHAT_ACTIONS
# ...

            
    def stop(self):
        log("Stopping mod and removing handlers...")
        """Остановка мода и отключение обработчиков"""
        if not self.started:
            return
        
      
        try:
            self.appLoader.onGUISpaceEntered -= self.on_gui_space_entered
        except:
            pass
        
        try:
            self.appLoader.onGUISpaceLeft -= self.on_gui_space_left
        except:
            pass
        
        try:
            g_playerEvents.onBattleResultsReceived -= self.on_battle_results_received
        except:
            pass
        
        # Уничтожаем overlay
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None
        
        self.started = False
    
    def fini(self):
        """Финализация мода"""
        try:
            log("Shutting down mod...")
            self.stop()
            log("Mod shut down successfully")
        except Exception as e:
            err("Error in fini: {}".format(e))
            
#        self.stats_fetcher.fetch_stats(all_ids, on_stats_received)

class DraggableWinChanceWindow(object):
    """Перетаскиваемое окно для отображения Win Chance"""
    
    def __init__(self):
        self.components = []
        self.isDragging = False
        self.lastMousePos = (0, 0)
        self.mouseHandlerActive = False
        self.callbackID = None
        
        # Дефолтная позиция (правый верхний угол)
        self.posX = 0.75
        self.posY = 0.05
        
        self.loadConfig()
    
    def loadConfig(self):
        """Загружает позицию из конфига"""
        try:
            config_path = './mods/configs/mod_winchance/mod_winchance.json'
            if os.path.exists(config_path):
                import json
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    self.posX = config.get('posX', self.posX)
                    self.posY = config.get('posY', self.posY)
                    
                    # Валидация позиции - должна быть в пределах экрана
                    if self.posX < 0 or self.posX > 1.0:
                        log("Invalid posX {}, resetting to default".format(self.posX))
                        self.posX = 0.75
                    if self.posY < 0 or self.posY > 1.0:
                        log("Invalid posY {}, resetting to default".format(self.posY))
                        self.posY = 0.05
                    
                    log("Config loaded: position ({:.3f}, {:.3f})".format(self.posX, self.posY))
        except Exception as e:
            debug("Error loading config: {}".format(e))
    
    def saveConfig(self):
        """Сохраняет позицию в конфиг"""
        try:
            import json
            config_path = './mods/configs/mod_winchance/mod_winchance.json'
            config_dir = os.path.dirname(config_path)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            config = {
                'posX': self.posX,
                'posY': self.posY
            }
            
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            log("Config saved: position ({:.3f}, {:.3f})".format(self.posX, self.posY))
        except Exception as e:
            debug("Error saving config: {}".format(e))
    
    def create(self):
        """Cоздает окно"""
        try:
            log("Window created (GUI-based)")
            return True
        except Exception as e:
            err("Error creating window: {}".format(e))
            return False
    
    def update_text(self, text):
        """Обновляет текст окна"""
        try:
            log("Updating overlay text: {}".format(text))
            self.createWindow(text)
        except Exception as e:
            err("Update text error: {}".format(e))
            import traceback
            err(traceback.format_exc())
    
    def createWindow(self, message):
        """Создает/обновляет окно с текстом"""
        try:
            self.destroyWindow()
            
            import GUI
            
            # Парсим сообщение: "Win Chance: 56.5%\nTeam WGR: 5684 | Enemy WGR: 5160"
            lines = message.split('\n')
            
            if len(lines) < 2:
                log("Invalid message format: {}".format(message))
                return
            
            # === Win Chance (первая строка) ===
            chance_line = lines[0]  # "Win Chance: 56.5%"
            
            # Извлекаем процент - берем только до '%'
            try:
                # Разбиваем по ':' и берем вторую часть, затем по '%' и берем первую
                parts = chance_line.split(':')
                if len(parts) >= 2:
                    percent_part = parts[1].split('%')[0].strip()
                    chance_value = float(percent_part)
                    log("Parsed chance value: {}%".format(chance_value))
                else:
                    log("Cannot parse chance from: {}".format(chance_line))
                    chance_value = 50.0
            except Exception as e:
                err("Error parsing chance value from '{}': {}".format(chance_line, e))
                import traceback
                err(traceback.format_exc())
                chance_value = 50.0
            
            # Цвет на основе шанса
            if chance_value >= 60:
                color = (50, 205, 50, 255)  # Зеленый
            elif chance_value >= 45:
                color = (255, 215, 0, 255)  # Желтый/золотой
            else:
                color = (220, 20, 60, 255)  # Красный
            
            chanceComp = GUI.Text(chance_line)
            chanceComp.font = "default_medium.font"
            chanceComp.colour = color
            chanceComp.position = (self.posX, self.posY, 0.95)
            GUI.addRoot(chanceComp)
            self.components.append(('text', chanceComp, 0))
            
            # === WGR (вторая строка) ===
            if len(lines) >= 2:
                wgr_line = lines[1]  # "Team WGR: 5684 | Enemy WGR: 5160"
                
                wgrText = GUI.Text(wgr_line)
                wgrText.font = "default_small.font"
                wgrText.colour = (255, 255, 255, 255)
                wgrText.position = (self.posX, self.posY + 0.025, 0.95)
                GUI.addRoot(wgrText)
                self.components.append(('text', wgrText, 0.025))
            
            self.startMouseHandler()
            log("Window created successfully")
            
        except Exception as e:
            err("Error creating window: {}".format(e))
    
    def destroyWindow(self):
        """Уничтожает окно"""
        try:
            import GUI
            self.stopMouseHandler()
            for _, component, _ in self.components:
                try:
                    GUI.delRoot(component)
                except:
                    pass
            self.components = []
        except:
            pass
    
    def startMouseHandler(self):
        """Запускает обработчик мыши для перетаскивания"""
        if not self.mouseHandlerActive:
            self.mouseHandlerActive = True
            self.checkMouseInput()
    
    def stopMouseHandler(self):
        """Останавливает обработчик мыши"""
        self.mouseHandlerActive = False
        if self.callbackID is not None:
            try:
                BigWorld.cancelCallback(self.callbackID)
            except:
                pass
            self.callbackID = None
    
    def checkMouseInput(self):
        """Проверяет ввод мыши для перетаскивания (Ctrl + ЛКМ)"""
        if not self.mouseHandlerActive:
            return
        
        try:
            import GUI
            import Keys
            
            cursor = GUI.mcursor()
            if cursor:
                mouseX, mouseY = cursor.position[0], cursor.position[1]
                
                # Проверяем Ctrl + ЛКМ
                ctrlPressed = BigWorld.isKeyDown(Keys.KEY_LCONTROL) or BigWorld.isKeyDown(Keys.KEY_RCONTROL)
                leftMouseDown = BigWorld.isKeyDown(Keys.KEY_LEFTMOUSE)
                
                if ctrlPressed and leftMouseDown:
                    if not self.isDragging:
                        self.isDragging = True
                        self.lastMousePos = (mouseX, mouseY)
                    else:
                        deltaX = mouseX - self.lastMousePos[0]
                        deltaY = mouseY - self.lastMousePos[1]
                        
                        self.posX += deltaX
                        self.posY += deltaY
                        self.updateWindowPosition()
                        self.lastMousePos = (mouseX, mouseY)
                else:
                    if self.isDragging:
                        self.isDragging = False
                        self.saveConfig()
        except Exception as e:
            debug("Mouse error: {}".format(e))
        
        # Следующая проверка
        self.callbackID = BigWorld.callback(0.05, self.checkMouseInput)
    
    def updateWindowPosition(self):
        """Обновляет позицию всех компонентов"""
        try:
            import GUI
            for comp_type, component, offset in self.components:
                if comp_type == 'bg':
                    component.position = (self.posX - 0.01, self.posY + offset * 0.022, 0.9)
                elif comp_type == 'text':
                    component.position = (self.posX, self.posY + offset, 0.95)
        except:
            pass
    
    def destroy(self):
        """Уничтожает окно"""
        try:
            self.destroyWindow()
            log("Window destroyed")
        except Exception as e:
            err("Error destroying window: {}".format(e))
    
# Initialization
g_winChanceMod = WinChanceMod()

def init():
    # Инициализация API клиента на уровне глобального объекта
    if API_CONFIG['enabled']:
        g_winChanceMod.api_client = BattleAPIClient(
            api_url=API_CONFIG['api_url'],
            api_token=API_CONFIG.get('token'),
            api_account_id=API_CONFIG.get('account_id'),
            api_nickname=API_CONFIG.get('nickname'),    
            api_config=API_CONFIG  # Передаем ссылку на конфиг
        )
        log("API client initialized")
    else:
        g_winChanceMod.api_client = None
        log("API client disabled")

    g_winChanceMod.start()


    
# Hook ServiceChannelManager via MessengerEntry instance (Listener Swap)
try:
    from messenger import MessengerEntry
    from messenger.m_constants import PROTO_TYPE
    from chat_shared import SYS_MESSAGE_TYPE, CHAT_ACTIONS
    
    # We need to find the 'bw' plugin. 
    # MessengerEntry.g_instance.protos is a decorator wrapping plugins.
    # Accessing attribute 'bw' should work if ROPropertyMeta is used correctly.
    
    def hook_service_channel():
        try:
            messenger = MessengerEntry.g_instance
            if not messenger:
                err("MessengerEntry.g_instance is None")
                return

            # Try to get BW plugin
            # PROTO_TYPE_NAMES keys are usually uppercase 'BW'.
            bw_plugin = getattr(messenger.protos, 'BW', None)
        except Exception:
            import traceback
            err(traceback.format_exc())

    # Call the hook function
    hook_service_channel()

except Exception as e:
    err("Error initializing ServiceChannel hook: {}".format(e))

# Global fini
def fini():
    """Глобальная функция финализации"""
    try:
        g_winChanceMod.fini()
    except Exception as e:
        err("Error in global fini: {}".format(e)) 