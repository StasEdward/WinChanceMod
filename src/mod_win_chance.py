import BigWorld
import json
import os
import threading
import urllib
import urllib2
import time
from helpers import dependency
from skeletons.gui.app_loader import IAppLoader
from PlayerEvents import g_playerEvents
import BigWorld
import time
import datetime
from items import vehicles as vehiclesWG
import ArenaType
from constants import ARENA_BONUS_TYPE
import Account
from api_client import BattleAPIClient
from helpers import i18n


# Constants
MOD_NAME = "WinChanceMod"
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
                log("[WinChance] fetch_stats worker started")
                if not account_ids:
                    log("[WinChance] No account IDs provided")
                    callback({})
                    return
                
                ids_str = ",".join(map(str, account_ids))
                url = "{}?application_id={}&account_id={}&fields=global_rating".format(
                    self.base_url, self.app_id, ids_str)
                
                log("[WinChance] Fetching stats for {} players...".format(len(account_ids)))
                log("[WinChance] API URL: {}".format(url[:100] + "..."))
                
                response = urllib2.urlopen(url, timeout=10)
                data = json.load(response)
                
                log("[WinChance] API response status: {}".format(data.get('status', 'unknown')))
                
                if data.get('status') == 'ok':
                    result_data = data.get('data', {})
                    log("[WinChance] API returned data for {} players".format(len(result_data)))
                    callback(result_data)
                else:
                    err("[WinChance] API Error: {}".format(data.get('error', 'unknown')))
                    callback({})
                    
            except urllib2.HTTPError as e:
                err("[WinChance] HTTP Error {}: {}".format(e.code, e.reason))
                callback({})
            except urllib2.URLError as e:
                err("[WinChance] URL Error: {}".format(e.reason))
                callback({})
            except Exception as e:
                err("[WinChance] Fetch Error: {}".format(e))
                import traceback
                err(traceback.format_exc())
                callback({})

        log("[WinChance] Starting stats fetch thread...")
        t = threading.Thread(target=_worker)
        t.daemon = True  # Делаем поток демоном чтобы не блокировать выход
        t.start()
        log("[WinChance] Stats fetch thread started")


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
        
    def start(self):
        if self.started: 
            return
        self.started = True
        log("Started")
        
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
            err("[WinChance] Error loading battle context for {}: {}".format(arena_id, e))
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
            log("[WinChance] Saved context to {}".format(file_path))
        except Exception as e:
            err("[WinChance] Error saving battle context: {}".format(e))
            
    def delete_battle_context(self, arena_id):
        """Removes context file for an arena"""
        try:
            file_path = os.path.join(self.BATTLE_CONTEXT_DIR, '{}.json'.format(arena_id))
            if os.path.exists(file_path):
                os.remove(file_path)
                log("[WinChance] Deleted context file {}".format(file_path))
        except:
            pass

    def load_pending_battles(self):
        try:
            if os.path.exists(self.PENDING_BATTLES_FILE):
                with open(self.PENDING_BATTLES_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            err("[WinChance] Error loading pending battles: {}".format(e))
        return []

    def save_pending_battles(self, battles):
        try:
            config_dir = os.path.dirname(self.PENDING_BATTLES_FILE)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            with open(self.PENDING_BATTLES_FILE, 'w') as f:
                json.dump(battles, f)
        except Exception as e:
            err("[WinChance] Error saving pending battles: {}".format(e))

    def add_pending_battle(self, arena_id):
        battles = self.load_pending_battles()
        if arena_id not in battles:
            battles.append(arena_id)
            self.save_pending_battles(battles)
            log("[WinChance] Added pending battle ID: {}".format(arena_id))

    def remove_pending_battle(self, arena_id):
        battles = self.load_pending_battles()
        if arena_id in battles:
            battles.remove(arena_id)
            self.save_pending_battles(battles)
            log("[WinChance] Removed pending battle ID: {}".format(arena_id))

    def check_pending_battles_loop(self):
        battles = self.load_pending_battles()
        if not battles:
            return
        
        log("[WinChance] Polling {} pending battles...".format(len(battles)))
        for arena_id in battles:
            self.request_battle_results(arena_id)
            
        # Schedule next check
        BigWorld.callback(3.0, self.check_pending_battles_loop)

    def on_gui_space_entered(self, spaceID):
        log("Space entered: {}".format(spaceID))
        
        # Space ID 15/3 = Hangar 
        # Note: 3 is Lobby (Hangar). 
        if spaceID == 3:
            # Check pending battles
            # Check pending battles
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
                err("[WinChance] Error adding pending battle on entry: {}".format(e))

            # Создаем overlay для боя
            if self.overlay is None:
                self.overlay = DraggableWinChanceWindow()
                self.overlay.create()
            # Delay slightly to ensure player is ready
            BigWorld.callback(30.0, self.calculate_battle_stats)

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
            # We need arena ID here. We can get it from player.arena if still valid
            try:
                p = BigWorld.player()
                if p and hasattr(p, 'arenaUniqueID'):
                    self.save_battle_context(p.arenaUniqueID, context_update)
            except:
                pass
            # ------------------------------------------------------
            
            # Сохраняем информацию о танке игрока для отправки в результатах
            try:
                if player and hasattr(player, 'vehicleTypeDescriptor'):
                    veh_descr = player.vehicleTypeDescriptor
                    vehicle_type = getattr(veh_descr, 'type', None)
                    # Extract vehicle information safely
                    vehicle_id = getattr(vehicle_type, 'compactDescr', 0) if vehicle_type else 0
                    vehicle_name = getattr(vehicle_type, 'userString', 'Unknown') if vehicle_type else 'Unknown'
                    # tags is a frozenset, need to convert to list to access elements
                    vehicle_tags = getattr(vehicle_type, 'tags', frozenset()) if vehicle_type else frozenset()
                    if vehicle_tags:
                        # Convert frozenset to list and get first tag
                        tags_list = list(vehicle_tags)
                        # Look for vehicle class tag (heavyTank, mediumTank, etc.)
                        vehicle_type_class = 'unknown'
                        for tag in tags_list:
                            if 'Tank' in tag or 'SPG' in tag:
                                vehicle_type_class = tag
                                break
                        if vehicle_type_class == 'unknown' and tags_list:
                            vehicle_type_class = tags_list[0]
                    else:
                        vehicle_type_class = 'unknown'
                    vehicle_name_parts = getattr(vehicle_type, 'name', '') if vehicle_type else ''
                    vehicle_nation = vehicle_name_parts.split(':')[0] if vehicle_name_parts and ':' in vehicle_name_parts else 'unknown'
  
                    if veh_descr:
                        self.player_vehicle_info = {
                            'id': vehicle_id,
                            'name': vehicle_name,
                            'tier': veh_descr.level if hasattr(veh_descr, 'level') else 0,
                            'type': vehicle_type_class,
                            'nation': vehicle_nation
                        }
                        
                        # --- PERSISTENCE: Update context with Tank Info ---
                        # Also save Map Name here as we have arena
                        map_name = 'Unknown'
                        try:
                           if hasattr(player.arena.arenaType, 'geometryName'):
                               # Try to localize
                               geometry_name = player.arena.arenaType.geometryName
                               # Try standard arena name localization
                               map_name = i18n.makeString('#arenas:%s/name' % geometry_name)
                               if not map_name or map_name.startswith('#arenas:'):
                                    # Fallback to userString if available (sometimes different)
                                    pass
                        except: pass

                        tank_context = {
                            'TankId': vehicle_id,
                            'TankName': vehicle_name,
                            'TankTier': self.player_vehicle_info['tier'],
                            'TankType': vehicle_type_class,
                            'TankNation': vehicle_nation,
                            'MapName': map_name
                        }
                        self.save_battle_context(player.arenaUniqueID, tank_context)
                        # --------------------------------------------------

                        log("Vehicle info saved: {} (Tier {})".format(
                            self.player_vehicle_info['name'], 
                            self.player_vehicle_info['tier'],
                            self.player_vehicle_info['nation']
                        ))
            except Exception as e:
                err("Error getting vehicle info: {}".format(e))
                self.player_vehicle_info = {
                    'id': 0,
                    'name': 'Unknown',
                    'tier': 0,
                    'type': 'unknown',
                    'nation': 'unknown'
                }
            
            log("Calculated Chance: {:.1f}%".format(chance))
            log("Avg Team WGR: {:.0f}".format(avg_team_wgr))
            log("Avg Enemy WGR: {:.0f}".format(avg_enemy_wgr))
            
            display_text = "Win Chance: {:.1f}%\nTeam WGR: {:.0f} | Enemy WGR: {:.0f}".format(
                    chance, avg_team_wgr, avg_enemy_wgr)
 
            # Используем overlay вместо show_text
            log("[WinChance] Attempting to display data...")
            if self.overlay:
                log("[WinChance] Calling overlay.update_text()")
                self.overlay.update_text(display_text)
            else:
                err("[WinChance] Overlay is None! Cannot display data")
                log(display_text)
        
        # ВАЖНО: Вызываем fetch_stats здесь, а не в stop()!
        log("[WinChance] Calling fetch_stats for {} account IDs...".format(len(all_ids)))
        self.stats_fetcher.fetch_stats(all_ids, on_stats_received)

    def request_battle_results(self, arena_id):
        """Request battle results from cache/server"""
        log("[WinChance] Requesting battle results for arena {}".format(arena_id))
        try:
            # We need to access the account repository
            # Account is imported at top level (from Account import g_accountRepository usually available or just Account)
            # Check imports at top of file
            import Account 
            if Account.g_accountRepository:
                 # Check if we can get it
                 if Account.g_accountRepository.battleResultsCache:
                     # get(arenaUniqueID, callback)
                     # We wrap the callback to pass the ID so we can remove it on success
                     Account.g_accountRepository.battleResultsCache.get(arena_id, lambda code, res: self.on_battle_results_callback(code, res, arena_id))
                 else:
                     err("[WinChance] BattleResultsCache is None")
            else:
                err("[WinChance] AccountRepository is None")
        except Exception as e:
            err("[WinChance] Error requesting battle results: {}".format(e))
            import traceback
            err(traceback.format_exc())
            
    def on_battle_results_callback(self, responseCode, results, arena_id):
        """Callback for battle results request"""
        try:
            # Check if results are valid
            if results and (responseCode == 0 or responseCode == 1): # Accept success codes
                log("[WinChance] Battle results retrieved via callback for {}".format(arena_id))
                self.remove_pending_battle(arena_id)
                self.on_hangar_battle_results(0, results)
            elif results:
                # Even if code is weird, if we have results, take them.
                log("[WinChance] Battle results retrieved (Code {}) for {}".format(responseCode, arena_id))
                self.remove_pending_battle(arena_id)
                self.on_hangar_battle_results(0, results)
            else:
                # log("[WinChance] No results returned in callback for {} (Code: {})".format(arena_id, responseCode))
                # Do NOT remove from pending, try again later.
                pass
                 
        except Exception as e:
            err("[WinChance] Error in on_battle_results_callback: {}".format(e))

    def on_battle_results_received(self, isPlayerVehicle, results):
        if not isPlayerVehicle or not results:
            return
        
        try:
             arena_id = results.get('arenaUniqueID')
             log("[WinChance] Received onBattleResultsReceived for arena {}".format(arena_id))
             # If we receive it naturally, we can process it and remove from pending (if it was there)
             self.remove_pending_battle(arena_id)
             self.on_hangar_battle_results(0, results)
        except Exception as e:
            err("[WinChance] Error in on_battle_results_received: {}".format(e))

    def on_hangar_battle_results(self, responseCode, results):
        """Process battle results (from cache or event)"""
        try:
            log("[WinChance] Processing battle results...")
            
            if not results:
                return

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
            
            log("[WinChance] Loaded context for arena {}: {}".format(arena_id, context_data.keys()))

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
                    tank_info['name'] = vt.userString
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

            log("[WinChance] Result: {} Map: {} Tank: {} DMG: {} Chance: {:.1f}".format(
                battle_result, map_name, tank_info['name'], damage_dealt, win_chance))
            
            dto = {
                'ArenaUniqueId': str(arena_id),
                'BattleTime': battle_time,
                'Duration': duration,
                'MapName': map_name,
                'BattleType': bonus_type,
                'Team': player_team,
                'WinnerTeam': winner_team,
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
                }
            }
            
            if self.api_client:
                success = self.api_client.send_battle_result(dto)
                if success:
                    # Cleanup context after successful send
                    self.delete_battle_context(arena_id)
                
        except Exception as e:
            err("[WinChance] Error in on_hangar_battle_results: {}".format(e))
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
        log("[WinChance] Stopping mod and removing handlers...")
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
            log("[WinChance] Shutting down mod...")
            self.stop()
            log("[WinChance] Mod shut down successfully")
        except Exception as e:
            err("[WinChance] Error in fini: {}".format(e))
            
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
                        log("[WinChance] Invalid posX {}, resetting to default".format(self.posX))
                        self.posX = 0.75
                    if self.posY < 0 or self.posY > 1.0:
                        log("[WinChance] Invalid posY {}, resetting to default".format(self.posY))
                        self.posY = 0.05
                    
                    log("[WinChance] Config loaded: position ({:.3f}, {:.3f})".format(self.posX, self.posY))
        except Exception as e:
            debug("[WinChance] Error loading config: {}".format(e))
    
    def saveConfig(self):
        """Сохраняет позицию в конфиг"""
        try:
            import json
            config_path = './mods/configs/mod_winchance.json'
            config_dir = os.path.dirname(config_path)
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            config = {
                'posX': self.posX,
                'posY': self.posY
            }
            
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            log("[WinChance] Config saved: position ({:.3f}, {:.3f})".format(self.posX, self.posY))
        except Exception as e:
            debug("[WinChance] Error saving config: {}".format(e))
    
    def create(self):
        """Cоздает окно"""
        try:
            log("[WinChance] Window created (GUI-based)")
            return True
        except Exception as e:
            err("[WinChance] Error creating window: {}".format(e))
            return False
    
    def update_text(self, text):
        """Обновляет текст окна"""
        try:
            log("[WinChance] Updating overlay text: {}".format(text))
            self.createWindow(text)
        except Exception as e:
            err("[WinChance] Update text error: {}".format(e))
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
                log("[WinChance] Invalid message format: {}".format(message))
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
                    log("[WinChance] Parsed chance value: {}%".format(chance_value))
                else:
                    log("[WinChance] Cannot parse chance from: {}".format(chance_line))
                    chance_value = 50.0
            except Exception as e:
                err("[WinChance] Error parsing chance value from '{}': {}".format(chance_line, e))
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
            log("[WinChance] Window created successfully")
            
        except Exception as e:
            err("[WinChance] Error creating window: {}".format(e))
    
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
            debug("[WinChance] Mouse error: {}".format(e))
        
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
            log("[WinChance] Window destroyed")
        except Exception as e:
            err("[WinChance] Error destroying window: {}".format(e))
    
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
                err("[WinChance] MessengerEntry.g_instance is None")
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
    err("[WinChance] Error initializing ServiceChannel hook: {}".format(e))

# Global fini
def fini():
    """Глобальная функция финализации"""
    try:
        g_winChanceMod.fini()
    except Exception as e:
        err("[WinChance] Error in global fini: {}".format(e)) 