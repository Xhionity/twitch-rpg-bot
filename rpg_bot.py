import asyncio
import json
import os
import random
import time
import shutil
import logging
from collections import Counter
from filelock import FileLock
from twitchio.ext import commands

# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from settings import TOKEN, CHANNEL, SAVE_FILE
    from consts import MONSTERS, ITEM_DESCRIPTIONS, ITEMS, BLACK_MARKET_ITEMS
except ImportError as e:
    logging.error(f"Ошибка импорта настроек или констант: {e}")
    raise ImportError(f"Ошибка импорта настроек или констант: {e}")

def calculate_hp(level):
    """Рассчитать максимальное HP персонажа по уровню."""
    return 30 + (level - 1) * 5

def calculate_damage(level):
    """Рассчитать базовый урон персонажа по уровню."""
    return random.randint(5 + level * 2, 10 + level * 3)

class RPGbot(commands.Bot):
    """Twitch RPG бот с системой уровней, боев, экономики и кражи."""

    def __init__(self):
        """Инициализация бота с загрузкой данных игроков и настройкой параметров."""
        super().__init__(token=TOKEN, prefix='!', initial_channels=[CHANNEL])
        self.players = self.load_players()
        self.black_market_items = []
        self.black_market_last_refresh = 0
        self.pending_duels = {}
        self.races = {
            'человек': {'hp_bonus': 5, 'xp_bonus': 0},
            'эльф': {'hp_bonus': 0, 'xp_bonus': 0.1},
            'орк': {'hp_bonus': 10, 'xp_bonus': -0.05}
        }
        self.classes = {
            'воин': {'attack_bonus': (2, 5), 'hp_bonus': 10},
            'маг': {'attack_bonus': (0, 3), 'xp_bonus': 0.1},
            'вор': {'attack_bonus': (1, 4), 'steal_chance_bonus': 0.05}
        }

    def load_players(self):
        """Загрузить данные игроков из JSON-файла с проверкой структуры."""
        if not os.path.exists(SAVE_FILE):
            logging.info(f"Файл {SAVE_FILE} не существует, создаётся пустой словарь игроков.")
            return {}

        lock = FileLock(f"{SAVE_FILE}.lock")
        with lock:
            try:
                with open(SAVE_FILE, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        logging.warning(f"Файл {SAVE_FILE} пуст.")
                        return {}
                    players = json.loads(content)
                    # Дополняем старые данные новыми полями
                    default_player = {
                        'level': 1,
                        'xp': 0,
                        'gold': 15,
                        'inventory': [],
                        'equipment': {'weapon': None, 'armor': None, 'helmet': None, 'pet': None, 'amulet': None},
                        'last_xp_time': 0,
                        'last_fight_time': 0,
                        'last_pvp_time': 0,
                        'pvp_wins': 0,
                        'pvp_losses': 0,
                        'prison': False,
                        'prison_until': 0,
                        'race': None,
                        'class': None,
                        'current_hp': None
                    }
                    for user, data in players.items():
                        for key, value in default_player.items():
                            if key not in data:
                                data[key] = value
                        # Устанавливаем current_hp, если не задано
                        if data['current_hp'] is None:
                            data['current_hp'] = calculate_hp(data['level']) + self.get_equipment_bonuses(data)[2]
                    return players
            except (json.JSONDecodeError, IOError) as e:
                logging.error(f"Ошибка загрузки {SAVE_FILE}: {e}")
                print(f"⚠️ Ошибка загрузки {SAVE_FILE}: {e}")
                return {}

    def save_players(self):
        """Сохранить данные игроков в JSON-файл с резервной копией."""
        lock = FileLock(f"{SAVE_FILE}.lock")
        with lock:
            try:
                # Создаём резервную копию
                if os.path.exists(SAVE_FILE):
                    shutil.copy(SAVE_FILE, f"{SAVE_FILE}.bak")
                    logging.info(f"Создана резервная копия {SAVE_FILE}.bak")
                with open(SAVE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.players, f, ensure_ascii=False, indent=2)
                logging.info(f"Данные игроков сохранены в {SAVE_FILE}")
            except IOError as e:
                logging.error(f"Ошибка сохранения {SAVE_FILE}: {e}")
                print(f"⚠️ Ошибка сохранения {SAVE_FILE}: {e}")

    def try_level_up(self, player):
        """Проверить и повысить уровень игрока, если достаточно XP."""
        leveled_up = False
        while player['xp'] >= player['level'] * 100:
            player['xp'] -= player['level'] * 100
            player['level'] += 1
            leveled_up = True
            # Обновляем максимальное HP при повышении уровня
            player['current_hp'] = calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2]
        return leveled_up

    def get_equipment_bonuses(self, player):
        """Рассчитать бонусы от экипировки и класса."""
        equip = player.get('equipment', {})
        attack_bonus_min, attack_bonus_max, hp_bonus = 0, 0, 0

        for slot, item_name in equip.items():
            if item_name and item_name in ITEMS:
                item = ITEMS[item_name]
                ab_min, ab_max = item['attack_bonus'] if isinstance(item['attack_bonus'], tuple) else (item['attack_bonus'], item['attack_bonus'])
                attack_bonus_min += ab_min
                attack_bonus_max += ab_max
                hp_bonus += item.get('hp_bonus', 0)

        # Бонусы от класса
        player_class = player.get('class')
        if player_class in self.classes:
            class_info = self.classes[player_class]
            ab_min, ab_max = class_info['attack_bonus'] if isinstance(class_info['attack_bonus'], tuple) else (class_info['attack_bonus'], class_info['attack_bonus'])
            attack_bonus_min += ab_min
            attack_bonus_max += ab_max
            hp_bonus += class_info.get('hp_bonus', 0)

        return attack_bonus_min, attack_bonus_max, hp_bonus

    async def check_cooldown(self, player, key, cooldown, ctx):
        """Проверить кулдаун для действия."""
        now = time.time()
        last_time = player.get(key, 0)
        if now - last_time < cooldown:
            remain = int(cooldown - (now - last_time))
            await ctx.send(f'{ctx.author.name}, подожди {remain} секунд.')
            return False
        player[key] = now
        return True

    def refresh_black_market(self):
        """Обновить ассортимент черного рынка."""
        self.black_market_items = random.sample(BLACK_MARKET_ITEMS, k=min(3, len(BLACK_MARKET_ITEMS)))
        self.black_market_last_refresh = time.time()
        logging.info("Чёрный рынок обновлён")

    async def event_ready(self):
        """Обработчик события готовности бота."""
        print(f'✅ Бот подключен как {self.nick}')
        logging.info(f'Бот подключен как {self.nick}')

    @commands.command(name='черныйрынок')
    async def cmd_black_market(self, ctx):
        """Показать доступные предметы на черном рынке."""
        now = time.time()
        if now - self.black_market_last_refresh > 600 or not self.black_market_items:
            self.refresh_black_market()

        msg_lines = ['🕶️ Тёмный торговец шепчет:\nСегодня в продаже:']
        for idx, item in enumerate(self.black_market_items, start=1):
            msg_lines.append(f'{idx}. {item["name"]} — {item["price"]} золота ({item["description"]})')
        msg_lines.append('Купи через команду !купить <номер>')

        for line in msg_lines:
            await ctx.send(line)

    @commands.command(name='купить')
    async def cmd_buy(self, ctx):
        """Купить предмет с черного рынка."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        parts = ctx.message.content.strip().split()
        if len(parts) != 2 or not parts[1].isdigit():
            await ctx.send('Используй формат: !купить <номер>')
            return

        choice = int(parts[1]) - 1
        if choice < 0 or choice >= len(self.black_market_items):
            await ctx.send(f'{ctx.author.name}, нет такого товара.')
            return

        player = self.players[user]
        item = self.black_market_items[choice]

        if player['gold'] < item['price']:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота.')
            return

        player['gold'] -= item['price']
        player['inventory'].append(item['name'])
        self.save_players()
        logging.info(f"{user} купил {item['name']} за {item['price']} золота")

        if item['type'] in ['pet', 'amulet', 'consumable']:
            await ctx.send(f'{ctx.author.name}, ты приобрел {item["type"]}: {item["name"]}! '
                          f'Используй {"!надеть" if item["type"] in ["pet", "amulet"] else "!использовать"} {item["name"]}.')
        else:
            await ctx.send(f'{ctx.author.name}, ты купил: {item["name"]}')

    @commands.command(name='старт')
    async def cmd_start(self, ctx):
        """Создать нового персонажа."""
        user = ctx.author.name.lower()
        if user in self.players:
            await ctx.send(f'{ctx.author.name}, ты уже начал игру!')
            return

        max_hp = calculate_hp(1)
        self.players[user] = {
            'level': 1,
            'xp': 0,
            'gold': 0,
            'inventory': [],
            'equipment': {'weapon': None, 'armor': None, 'helmet': None, 'pet': None, 'amulet': None},
            'last_xp_time': 0,
            'last_fight_time': 0,
            'last_pvp_time': 0,
            'pvp_wins': 0,
            'pvp_losses': 0,
            'prison': False,
            'prison_until': 0,
            'race': None,
            'class': None,
            'current_hp': max_hp
        }
        self.save_players()
        logging.info(f"Создан персонаж для {user}")
        await ctx.send(f'{ctx.author.name}, персонаж создан! Уровень 1, XP 0, золото 0. Выбери расу (!раса) и класс (!класс).')

    @commands.command(name='статус')
    async def cmd_status(self, ctx):
        """Показать статус игрока."""
        parts = ctx.message.content.strip().split()
        target = parts[1].lstrip('@').lower() if len(parts) == 2 else ctx.author.name.lower()

        if target not in self.players:
            await ctx.send(f'{ctx.author.name}, у {target} нет персонажа.')
            return

        p = self.players[target]
        lvl = p["level"]
        min_bonus, max_bonus, hp_bonus = self.get_equipment_bonuses(p)
        base_min = 5 + lvl * 2
        base_max = 10 + lvl * 3
        dmg_range = f'{base_min + min_bonus}-{base_max + max_bonus}'
        hp = calculate_hp(lvl) + hp_bonus
        current_hp = p.get('current_hp', hp)

        msg = f'{target} — Уровень {lvl}, XP {p["xp"]}, Золото {p["gold"]}, Урон {dmg_range}, HP {current_hp}/{hp}'
        if p.get('race'):
            msg += f', Раса: {p["race"]}'
        if p.get('class'):
            msg += f', Класс: {p["class"]}'
        await ctx.send(msg)

        now = time.time()
        status = []
        if p.get('xp_buff_until', 0) > now:
            status.append('📈 +50% XP (бордель)')
        if p.get('xp_penalty', False):
            status.append('⚠️ -50% XP (штраф)')
        if p.get('prison', False) and p.get('prison_until', 0) > now:
            remain = int(p['prison_until'] - now)
            status.append(f'🔒 В тюрьме ({remain} сек.)')
        if p.get('attack_buff_until', 0) > now:
            remain = int(p['attack_buff_until'] - now)
            status.append(f'⚔️ +10% урона ({remain} сек.)')
        if status:
            await ctx.send(f'{target}, активные эффекты: {", ".join(status)}')

    @commands.command(name='инвентарь')
    async def cmd_inventory(self, ctx):
        """Показать инвентарь игрока."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        inventory = self.players[user].get('inventory', [])
        if not inventory:
            await ctx.send(f'@{ctx.author.name}, твой инвентарь пуст.')
            return

        item_counts = Counter(inventory)
        formatted_items = [f'{item} x{count}' if count > 1 else item for item, count in item_counts.items()]
        await ctx.send(f'@{ctx.author.name}, инвентарь: {", ".join(formatted_items)}')

    @commands.command(name='экипировка')
    async def cmd_equipment(self, ctx):
        """Показать текущую экипировку игрока."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        equipment = self.players[user].get('equipment', {})
        eq_text = ', '.join(
            f'{slot.capitalize()}: {equipment[slot] if equipment[slot] else "—"}'
            for slot in ['weapon', 'armor', 'helmet', 'pet', 'amulet']
        )
        await ctx.send(f'🛡️ Экипировка {ctx.author.name}: {eq_text}')

    @commands.command(name='опыт')
    async def cmd_xp(self, ctx):
        """Получить опыт с учетом кулдауна и баффов."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, сначала создай персонажа (!старт).')
            return

        player = self.players[user]
        if not await self.check_cooldown(player, 'last_xp_time', 300, ctx):
            return

        base_xp = 50
        race_bonus = self.races[player.get('race', '')].get('xp_bonus', 0) if player.get('race') else 0
        class_bonus = self.classes[player.get('class', '')].get('xp_bonus', 0) if player.get('class') else 0

        now = time.time()
        if player.get('xp_buff_until', 0) > now:
            base_xp = int(base_xp * 1.5)
        if player.get('xp_penalty', False):
            base_xp = int(base_xp * 0.5)
        base_xp = int(base_xp * (1 + race_bonus + class_bonus))

        player['xp'] += base_xp
        leveled = self.try_level_up(player)
        self.save_players()
        logging.info(f"{user} получил {base_xp} XP")

        msg = f'{ctx.author.name}, получено {base_xp} XP. Текущий XP: {player["xp"]}'
        if leveled:
            msg += f' 📈 Уровень повышен! Теперь уровень {player["level"]}.'
        await ctx.send(msg)

    @commands.command(name='надеть')
    async def cmd_equip(self, ctx):
        """Надеть предмет из инвентаря."""
        user = ctx.author.name.lower()
        item_name = ctx.message.content.strip()[7:].strip()

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        player = self.players[user]
        if item_name.lower() not in [i.lower() for i in player['inventory']]:
            await ctx.send(f'{ctx.author.name}, у тебя нет предмета "{item_name}".')
            return

        if item_name not in ITEMS:
            await ctx.send(f'{ctx.author.name}, предмет "{item_name}" не может быть надет.')
            return

        item_info = ITEMS[item_name]
        slot = item_info['slot']
        if slot == 'consumable':
            await ctx.send(f'{ctx.author.name}, этот предмет нельзя надеть. Используй !использовать {item_name}.')
            return

        current_equipped = player['equipment'].get(slot)
        if current_equipped == item_name:
            await ctx.send(f'{ctx.author.name}, у тебя уже надет "{item_name}".')
            return

        if current_equipped:
            player['inventory'].append(current_equipped)
        player['inventory'].remove(item_name)
        player['equipment'][slot] = item_name
        # Обновляем максимальное HP при смене экипировки
        player['current_hp'] = min(player['current_hp'], calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2])
        self.save_players()
        logging.info(f"{user} надел {item_name} в слот {slot}")

        msg = f'{ctx.author.name}, ты надел {item_name} в слот {slot}.'
        if current_equipped:
            msg = f'{ctx.author.name}, ты заменил {current_equipped} на {item_name} в слоте {slot}.'
        await ctx.send(msg)

    @commands.command(name='снять')
    async def cmd_unequip(self, ctx):
        """Снять предмет из указанного слота."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) < 2:
            await ctx.send(f'{ctx.author.name}, укажи слот: !снять <weapon|armor|helmet|pet|amulet>')
            return

        slot = parts[1].strip().lower()
        player = self.players[user]
        if slot not in player['equipment'] or not player['equipment'][slot]:
            await ctx.send(f'{ctx.author.name}, в слоте "{slot}" ничего не надето.')
            return

        item_name = player['equipment'][slot]
        player['equipment'][slot] = None
        player['inventory'].append(item_name)
        # Обновляем максимальное HP
        player['current_hp'] = min(player['current_hp'], calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2])
        self.save_players()
        logging.info(f"{user} снял {item_name} из слота {slot}")

        await ctx.send(f'{ctx.author.name}, ты снял "{item_name}" из слота "{slot}".')

    @commands.command(name='использовать')
    async def cmd_use(self, ctx):
        """Использовать расходуемый предмет."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) < 2:
            await ctx.send(f'{ctx.author.name}, укажи предмет: !использовать <название>')
            return

        item_name = parts[1].strip()
        player = self.players[user]
        if item_name.lower() not in [i.lower() for i in player['inventory']]:
            await ctx.send(f'{ctx.author.name}, у тебя нет предмета "{item_name}".')
            return

        if item_name not in ITEMS or ITEMS[item_name]['slot'] != 'consumable':
            await ctx.send(f'{ctx.author.name}, предмет "{item_name}" нельзя использовать.')
            return

        effect = ITEMS[item_name].get('effect', {})
        if 'heal' in effect:
            max_hp = calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2]
            old_hp = player['current_hp']
            player['current_hp'] = min(player['current_hp'] + effect['heal'], max_hp)
            player['inventory'].remove(item_name)
            self.save_players()
            logging.info(f"{user} использовал {item_name}, восстановлено {effect['heal']} HP")
            await ctx.send(f'{ctx.author.name}, ты использовал "{item_name}" и восстановил {player['current_hp'] - old_hp} HP. Текущие HP: {player['current_hp']}/{max_hp}.')

    @commands.command(name='бой')
    async def cmd_fight(self, ctx):
        """Сражение с монстром."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, создай персонажа с помощью !старт.')
            return

        player = self.players[user]
        now = time.time()
        if player.get('prison', False) and player.get('prison_until', 0) > now:
            remain = int(player['prison_until'] - now)
            await ctx.send(f'@{ctx.author.name}, ты в тюрьме! Заплати взятку (!взятка) или жди {remain} сек.')
            return

        if not await self.check_cooldown(player, 'last_fight_time', 90, ctx):
            return

        parts = ctx.message.content.strip().split()
        # Учитываем редких монстров
        monster_name = parts[1].capitalize() if len(parts) > 1 and parts[1].capitalize() in MONSTERS else random.choice(
            [k for k, v in MONSTERS.items() if not v.get('rare', False) or random.random() < 0.1]
        )
        base = MONSTERS[monster_name]
        level = player['level']
        scale_factor = 1 + (level - 1) * 0.25
        monster_hp = int(base['base_hp'] * scale_factor)
        monster_attack = int(base['base_attack'] * scale_factor)

        min_bonus, max_bonus, hp_bonus = self.get_equipment_bonuses(player)
        player_hp = calculate_hp(level) + hp_bonus
        current_hp = player.get('current_hp', player_hp)

        # Учёт баффа таверны
        attack_multiplier = 1.1 if player.get('attack_buff_until', 0) > now else 1.0

        log = [f'{ctx.author.name} сражается с {monster_name}! (Монстр: {monster_hp} HP, {monster_attack} ATK)']
        raund = 0

        while monster_hp > 0 and current_hp > 0:
            raund += 1
            total_attack = int((calculate_damage(level) + random.randint(min_bonus, max_bonus)) * attack_multiplier)
            monster_hp -= total_attack
            if monster_hp <= 0:
                break
            current_hp -= monster_attack

        if current_hp > 0:
            xp_reward = random.randint(*base['xp_reward'])
            gold_reward = random.randint(*base['gold_reward'])
            player['xp'] += xp_reward
            player['gold'] += gold_reward
            drop = random.choice(base['loot']) if base['loot'] and random.random() < base['loot_chance'] else None
            if drop:
                player['inventory'].append(drop)
            player['current_hp'] = min(current_hp + player_hp // 2, player_hp)
            leveled = self.try_level_up(player)
            self.save_players()
            logging.info(f"{user} победил {monster_name}, получил {xp_reward} XP, {gold_reward} золота, дроп: {drop}")

            msg = f'🏆 Победа за {raund} ходов! +{xp_reward} XP, +{gold_reward} золота.'
            if drop:
                msg += f' Дроп: {drop}.'
            log.append(msg)
            if leveled:
                log.append(f'📈 Уровень повышен! Текущий уровень: {player["level"]}')
        else:
            xp_loss = int(player['xp'] * 0.1)
            player['xp'] = max(0, player['xp'] - xp_loss)
            player['current_hp'] = player_hp // 2
            log.append(f'💀 Поражение от {monster_name}... Потеряно {xp_loss} XP')
            self.save_players()
            logging.info(f"{user} проиграл {monster_name}, потеряно {xp_loss} XP")

        for l in log:
            await ctx.send(l)

    @commands.command(name='топ')
    async def cmd_top(self, ctx):
        """Показать топ-10 игроков по уровню и XP."""
        if not self.players:
            await ctx.send('Нет данных для рейтинга.')
            return
        top = sorted(self.players.items(), key=lambda i: (i[1]['level'], i[1]['xp']), reverse=True)[:10]
        result = ', '.join([f'{i + 1}. {name} (Lvl {p["level"]}, XP {p["xp"]})' for i, (name, p) in enumerate(top)])
        await ctx.send(f'🏆 ТОП игроков: {result}')

    @commands.command(name='дуэль')
    async def cmd_duel(self, ctx):
        """Вызвать игрока на дуэль."""
        challenger = ctx.author.name.lower()
        parts = ctx.message.content.strip().split()

        if len(parts) < 2:
            await ctx.send('Формат: !дуэль @ник [ставка]')
            return

        target = parts[1].lstrip('@').lower()
        amount = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) >= 0 else 0

        if challenger == target:
            await ctx.send('Нельзя вызвать самого себя.')
            return

        if challenger not in self.players or target not in self.players:
            await ctx.send('Оба игрока должны иметь персонажей.')
            return

        if self.players[challenger]['gold'] < amount:
            await ctx.send('Недостаточно золота для ставки.')
            return

        if target in self.pending_duels:
            await ctx.send(f'{target} уже ожидает другой дуэли.')
            return

        cl = self.players[challenger]
        tl = self.players[target]
        chp = calculate_hp(cl['level']) + self.get_equipment_bonuses(cl)[2]
        thp = calculate_hp(tl['level']) + self.get_equipment_bonuses(tl)[2]
        cdmg = f'{5 + cl["level"] * 2 + self.get_equipment_bonuses(cl)[0]}-{10 + cl["level"] * 3 + self.get_equipment_bonuses(cl)[1]}'
        tdmg = f'{5 + tl["level"] * 2 + self.get_equipment_bonuses(tl)[0]}-{10 + tl["level"] * 3 + self.get_equipment_bonuses(tl)[1]}'

        self.pending_duels[target] = {'challenger': challenger, 'amount': amount}
        await ctx.send(
            f'⚔️ {ctx.author.name} вызывает @{target} на дуэль{" со ставкой " + str(amount) + " золота" if amount else ""}!\n'
            f'{ctx.author.name}: HP {chp}, Урон {cdmg}; @{target}: HP {thp}, Урон {tdmg}\n'
            f'@{target}, напиши !принять чтобы принять вызов.'
        )
        logging.info(f"{challenger} вызвал {target} на дуэль с ставкой {amount}")

    @commands.command(name='принять')
    async def cmd_accept(self, ctx):
        """Принять вызов на дуэль."""
        defender = ctx.author.name.lower()
        if defender not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        now = time.time()
        if self.players[defender].get('prison', False) and self.players[defender].get('prison_until', 0) > now:
            remain = int(self.players[defender]['prison_until'] - now)
            await ctx.send(f'@{ctx.author.name}, ты в тюрьме! Заплати взятку (!взятка) или жди {remain} сек.')
            return

        if defender not in self.pending_duels:
            await ctx.send('Тебя никто не вызывал на дуэль.')
            return

        duel = self.pending_duels.pop(defender)
        challenger = duel['challenger']
        amount = duel['amount']

        if challenger not in self.players:
            await ctx.send('Игрок-вызывающий не найден.')
            return

        a = self.players[challenger]
        d = self.players[defender]
        if not await self.check_cooldown(a, 'last_pvp_time', 60, ctx) or not await self.check_cooldown(d, 'last_pvp_time', 60, ctx):
            return

        if amount > 0 and (a['gold'] < amount or d['gold'] < amount):
            await ctx.send('У кого-то не хватает золота.')
            return

        if amount > 0:
            a['gold'] -= amount
            d['gold'] -= amount

        min_bonus_a, max_bonus_a, hp_bonus_a = self.get_equipment_bonuses(a)
        min_bonus_d, max_bonus_d, hp_bonus_d = self.get_equipment_bonuses(d)
        hp1 = a.get('current_hp', calculate_hp(a['level']) + hp_bonus_a)
        hp2 = d.get('current_hp', calculate_hp(d['level']) + hp_bonus_d)

        attack_multiplier_a = 1.1 if a.get('attack_buff_until', 0) > now else 1.0
        attack_multiplier_d = 1.1 if d.get('attack_buff_until', 0) > now else 1.0

        attacker_name, defender_name = (challenger, defender) if random.random() < 0.5 else (defender, challenger)
        attacker_p, defender_p = (a, d) if attacker_name == challenger else (d, a)
        hp_attacker, hp_defender = (hp1, hp2) if attacker_name == challenger else (hp2, hp1)
        attacker_bonus = (min_bonus_a, max_bonus_a) if attacker_name == challenger else (min_bonus_d, max_bonus_d)
        defender_bonus = (min_bonus_d, max_bonus_d) if attacker_name == challenger else (min_bonus_a, max_bonus_a)
        attacker_multiplier = attack_multiplier_a if attacker_name == challenger else attack_multiplier_d
        defender_multiplier = attack_multiplier_d if attacker_name == challenger else attack_multiplier_a

        def dmg(p, min_b, max_b, multiplier):
            base = calculate_damage(p['level'])
            bonus = random.randint(min_b, max_b)
            return int((base + bonus) * multiplier)

        round_num = 1
        while True:
            damage = dmg(attacker_p, *attacker_bonus, attacker_multiplier)
            hp_defender -= damage
            if hp_defender <= 0:
                winner, loser = attacker_name, defender_name
                winner_p, loser_p = attacker_p, defender_p
                break

            damage = dmg(defender_p, *defender_bonus, defender_multiplier)
            hp_attacker -= damage
            if hp_attacker <= 0:
                winner, loser = defender_name, attacker_name
                winner_p, loser_p = attacker_p, defender_p
                break

            round_num += 1

        winner_p['current_hp'] = max(1, hp_attacker if winner == attacker_name else hp_defender)
        loser_p['current_hp'] = calculate_hp(loser_p['level']) + self.get_equipment_bonuses(loser_p)[2] // 2

        gold_msg = f' и {amount * 2} золота' if amount > 0 else ''
        xp = 10 * loser_p['level']
        winner_p['xp'] += xp
        level_msg = ''
        if self.try_level_up(winner_p):
            level_msg = f'📈 {winner} повышает уровень! Теперь уровень {winner_p["level"]}.'

        winner_p['pvp_wins'] = winner_p.get('pvp_wins', 0) + 1
        loser_p['pvp_losses'] = loser_p.get('pvp_losses', 0) + 1
        if amount > 0:
            winner_p['gold'] += amount * 2
        self.save_players()
        logging.info(f"Дуэль: {winner} победил {loser}, получил {xp} XP{gold_msg}")

        await ctx.send(f'🏁 Побеждает {winner}, получает {xp} XP{gold_msg}!')
        if level_msg:
            await ctx.send(level_msg)

    @commands.command(name='отмена')
    async def cmd_cancel_duel(self, ctx):
        """Отменить вызов на дуэль."""
        user = ctx.author.name.lower()
        if user in self.pending_duels:
            self.pending_duels.pop(user)
            await ctx.send(f'{ctx.author.name}, твой вызов на дуэль отменён.')
            logging.info(f"{user} отменил входящий вызов на дуэль")
            return

        for target, duel in list(self.pending_duels.items()):
            if duel['challenger'] == user:
                self.pending_duels.pop(target)
                await ctx.send(f'{ctx.author.name}, ты отменил вызов дуэли @{target}.')
                logging.info(f"{user} отменил вызов дуэли для {target}")
                return
        await ctx.send(f'{ctx.author.name}, у тебя нет активных вызовов на дуэль.')

    @commands.command(name='пвп')
    async def cmd_pvp_stats(self, ctx):
        """Показать статистику PvP."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return
        p = self.players[user]
        wins = p.get('pvp_wins', 0)
        losses = p.get('pvp_losses', 0)
        total = wins + losses
        winrate = f"{(wins / total * 100):.1f}%" if total > 0 else "–"
        await ctx.send(f'{ctx.author.name}, PvP: Победы: {wins}, Поражения: {losses}, Winrate: {winrate}')

    @commands.command(name='описание')
    async def cmd_description(self, ctx):
        """Показать описание предмета."""
        parts = ctx.message.content.strip().split(maxsplit=1)
        user = ctx.author.name.lower()

        if len(parts) == 2:
            item_name = parts[1].strip().lower()
            description = ITEM_DESCRIPTIONS.get(item_name)
            if description:
                await ctx.send(f'Описание {parts[1].strip()}: {description}')
            else:
                await ctx.send(f'{ctx.author.name}, описание для "{parts[1].strip()}" не найдено.')
            return

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        inventory = self.players[user].get('inventory', [])
        if not inventory:
            await ctx.send(f'{ctx.author.name}, у тебя пустой инвентарь.')
            return

        unique_items = list(set(inventory))
        if len(unique_items) == 1:
            item_name = unique_items[0].lower()
            description = ITEM_DESCRIPTIONS.get(item_name)
            if description:
                await ctx.send(f'Описание {unique_items[0]}: {description}')
            else:
                await ctx.send(f'{ctx.author.name}, описание для "{unique_items[0]}" не найдено.')
        else:
            await ctx.send(f'{ctx.author.name}, укажи название предмета: !описание <название>. '
                           f'Инвентарь: {", ".join(unique_items)}')

    @commands.command(name='бордель')
    async def cmd_brothel(self, ctx):
        """Посетить бордель для получения баффа или штрафа."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, сначала создай персонажа (!старт).')
            return

        player = self.players[user]
        cost = 100
        now = time.time()

        if player['gold'] < cost:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота (нужно {cost}).')
            return

        if player.get('xp_buff_until', 0) > now:
            await ctx.send(f'{ctx.author.name}, эффект уже активен. Подожди, пока он закончится.')
            return

        player['gold'] -= cost
        if random.random() < 0.25:
            player['xp_penalty'] = True
            await ctx.send(
                f'💋 {ctx.author.name}, ты подцепил что-то... XP уменьшается на 50%! Используй !лечиться за 50 золота.')
            logging.info(f"{user} получил штраф XP в борделе")
        else:
            player['xp_buff_until'] = now + 1800
            await ctx.send(
                f'💃 {ctx.author.name}, ты вдохновлён! В течение 30 минут +50% XP.')
            logging.info(f"{user} получил бафф XP в борделе")
        self.save_players()

    @commands.command(name='лечиться')
    async def cmd_heal(self, ctx):
        """Вылечиться от штрафа за посещение борделя."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        player = self.players[user]
        cost = 50

        if not player.get('xp_penalty'):
            await ctx.send(f'{ctx.author.name}, тебе не нужно лечение.')
            return

        if player['gold'] < cost:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота (нужно {cost}).')
            return

        player['gold'] -= cost
        player['xp_penalty'] = False
        self.save_players()
        logging.info(f"{user} вылечился от штрафа XP")
        await ctx.send(f'🧼 {ctx.author.name}, ты вылечился и готов к приключениям!')

    @commands.command(name='продать')
    async def cmd_sell(self, ctx):
        """Продать предмет из инвентаря."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) != 2:
            await ctx.send(f'{ctx.author.name}, укажи предмет: !продать <название>')
            return

        item_name = parts[1].strip()
        player = self.players[user]
        if item_name.lower() not in [i.lower() for i in player['inventory']]:
            await ctx.send(f'{ctx.author.name}, у тебя нет предмета "{item_name}".')
            return

        if item_name not in ITEMS or 'price' not in ITEMS[item_name]:
            await ctx.send(f'{ctx.author.name}, этот предмет нельзя продать.')
            return

        sell_price = ITEMS[item_name]['price'] // 2
        player['inventory'].remove(item_name)
        player['gold'] += sell_price
        self.save_players()
        logging.info(f"{user} продал {item_name} за {sell_price} золота")
        await ctx.send(f'{ctx.author.name}, ты продал "{item_name}" за {sell_price} золота.')

    @commands.command(name='оценить')
    async def cmd_appraise(self, ctx):
        """Оценить стоимость предмета."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) < 2:
            await ctx.send(f'{ctx.author.name}, укажи предмет: !оценить <название>')
            return

        item_name = parts[1].strip()
        if item_name.lower() not in [i.lower() for i in self.players[user]['inventory']]:
            await ctx.send(f'{ctx.author.name}, у тебя нет предмета "{item_name}".')
            return

        if item_name not in ITEMS:
            await ctx.send(f'{ctx.author.name}, предмет "{item_name}" не подлежит продаже.')
            return

        price = ITEMS[item_name].get('price', 0)
        sell_price = max(price // 2, 1)
        await ctx.send(f'{ctx.author.name}, ты можешь продать "{item_name}" за {sell_price} золота.')

    @commands.command(name='кража')
    async def cmd_steal(self, ctx):
        """Попытаться украсть предмет у другого игрока."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=2)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) < 3:
            await ctx.send(f'{ctx.author.name}, формат: !кража @ник <предмет>')
            return

        target = parts[1].lstrip('@').lower()
        item_name = parts[2].strip()

        if target == user:
            await ctx.send(f'{ctx.author.name}, нельзя украсть у себя.')
            return

        if target not in self.players:
            await ctx.send(f'{target} не имеет персонажа.')
            return

        player = self.players[user]
        now = time.time()
        if not await self.check_cooldown(player, 'steal_time_unteal', 300, ctx):
            return

        if item_name.lower() not in [i.lower() for i in self.players[target]['inventory']]:
            await ctx.send(f'{ctx.author.name}, у @{target} нет предмета "{item_name}".')
            return

        steal_chance = 0.1 + (self.classes[player.get('class', '')].get('steal_chance_bonus', 0) if player.get('class') else 0)
        if player['equipment'].get('amulet') == 'Амулет удачи':
            steal_chance += ITEMS['Амулет удачи']['effect']['steal_chance_bonus']

        if random.random() < steal_chance:
            player['inventory'].append(item_name)
            self.players[target]['inventory'].remove(item_name)
            await ctx.send(f'{ctx.author.name}, {item_name} успешно украден у @{target}!')
            logging.info(f"{user} украл {item_name} у {target}")
        else:
            player['prison'] = True
            player['prison_until'] = now + 300
            await ctx.send(f'@{ctx.author.name}, кража не удалась, тебя схватила стража! Ты в тюрьме на 5 минут.')
            logging.info(f"{user} провалил кражу, отправлен в тюрьму")
        self.save_players()

    @commands.command(name='взятка')
    async def cmd_prison(self, ctx):
        """Заплатить взятку для выхода из тюрьмы."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        player = self.players[user]
        now = time.time()
        if not player.get('prison', False) or player.get('prison_until', 0) <= now:
            await ctx.send(f'{ctx.author.name}, ты не в тюрьме.')
            return

        cost = 50
        if player['gold'] < cost:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота (нужно {cost}).')
            return

        player['gold'] -= cost
        player['prison'] = False
        player['prison_until'] = 0
        self.save_players()
        logging.info(f"{user} заплатил взятку и вышел из тюрьмы")
        await ctx.send(f'@{ctx.author.name}, ты свободен!')

    @commands.command(name='таверна')
    async def cmd_tavern(self, ctx):
        """Посетить таверну для получения баффа на урон."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, сначала создай персонажа (!старт).')
            return

        player = self.players[user]
        cost = 50
        now = time.time()

        if player.get('attack_buff_until', 0) > now:
            await ctx.send(f'{ctx.author.name}, бафф уже активен. Подожди, пока он закончится.')
            return

        if player['gold'] < cost:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота (нужно {cost}).')
            return

        player['gold'] -= cost
        player['attack_buff_until'] = now + 1800
        self.save_players()
        logging.info(f"{user} получил бафф урона в таверне")
        await ctx.send(f'🍺 {ctx.author.name}, ты отдохнул в таверне! В течение 30 минут +10% урона.')

    @commands.command(name='раса')
    async def cmd_race(self, ctx):
        """Выбрать расу для персонажа."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, сначала создай персонажа (!старт).')
            return

        player = self.players[user]
        if len(parts) < 2:
            races = ', '.join(self.races.keys())
            await ctx.send(f'{ctx.author.name}, укажи расу: !раса <название>. Доступные расы: {races}')
            return

        race = parts[1].strip().lower()
        if race not in self.races:
            await ctx.send(f'{ctx.author.name}, раса "{race}" не существует.')
            return

        if player.get('race'):
            await ctx.send(f'{ctx.author.name}, ты уже выбрал расу: {player["race"]}.')
            return

        player['race'] = race
        # Обновляем HP при выборе расы
        player['current_hp'] = calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2]
        self.save_players()
        logging.info(f"{user} выбрал расу {race}")
        await ctx.send(f'{ctx.author.name}, ты выбрал расу: {race.capitalize()}.')

    @commands.command(name='класс')
    async def cmd_class(self, ctx):
        """Выбрать класс для персонажа."""
        user = ctx.author.name.lower()
        parts = ctx.message.content.strip().split(maxsplit=1)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, сначала создай персонажа (!старт).')
            return

        player = self.players[user]
        if len(parts) < 2:
            classes = ', '.join(self.classes.keys())
            await ctx.send(f'{ctx.author.name}, укажи класс: !класс <название>. Доступные классы: {classes}')
            return

        class_name = parts[1].strip().lower()
        if class_name not in self.classes:
            await ctx.send(f'{ctx.author.name}, класс "{class_name}" не существует.')
            return

        if player.get('class'):
            await ctx.send(f'{ctx.author.name}, ты уже выбрал класс: {player["class"]}.')
            return

        player['class'] = class_name
        # Обновляем HP при выборе класса
        player['current_hp'] = calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2]
        self.save_players()
        logging.info(f"{user} выбрал класс {class_name}")
        await ctx.send(f'{ctx.author.name}, ты выбрал класс: {class_name.capitalize()}.')

    @commands.command(name='отдых')
    async def cmd_full_heal(self, ctx):
        """Полностью восстановить HP за 5 золота."""
        user = ctx.author.name.lower()
        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        player = self.players[user]
        cost = 5
        max_hp = calculate_hp(player['level']) + self.get_equipment_bonuses(player)[2]

        if player['current_hp'] >= max_hp:
            await ctx.send(f'{ctx.author.name}, твоё здоровье и так полное!')
            return

        if player['gold'] < cost:
            await ctx.send(f'{ctx.author.name}, у тебя недостаточно золота (нужно {cost}).')
            return

        player['gold'] -= cost
        player['current_hp'] = max_hp
        self.save_players()
        logging.info(f"{user} полностью восстановил HP за {cost} золота")
        await ctx.send(f'🩺 {ctx.author.name}, ты полностью восстановил HP за {cost} золота!')

    @commands.command(name='подарить')
    async def cmd_gift(self, ctx):
        """Подарить любой предмет из инвентаря другому игроку"""
        user = ctx.author.name.lower()
        player = self.players[user]
        parts = ctx.message.content.strip().split(maxsplit=2)

        if user not in self.players:
            await ctx.send(f'{ctx.author.name}, у тебя нет персонажа.')
            return

        if len(parts) != 3:
            await ctx.send(f'@{user}, формат отправки подарка: !подарок <имя персонажа> <название предмета из инвентаря>')
            return

        if len(parts) == 3:
            target = parts[1].lstrip('@').lower()
            item = parts[2].capitalize()
            item_slpit = item.split()
            if target not in self.players:
                await ctx.send(f'@{user}, {target} должен иметь персонажа!')
                return
            if item_slpit[0] == 'Золото':
                if item_slpit[1].isalpha():
                    await ctx.send(f'@{user}, ты хоть сам понял что хочешь?)')
                    return
                if int(item_slpit[1]) <= player['gold']:
                    self.players[target]['gold'] += int(item_slpit[1])
                    player['gold'] -= int(item_slpit[1])
                    self.save_players()
                    await ctx.send(f'@{user} подарил @{target} {int(item_slpit[1])} золотых монет!')
                    return
                elif int(item_slpit[1]) > player['gold']:
                    await ctx.send(f'@{user}, у тебя нет столько золота!')
                    return

            if item in player['inventory']:
                self.players[target]['inventory'].append(item)
                player['inventory'].remove(item)
                self.save_players()
                await ctx.send(f'@{user} успешно передал @{target} предмет {item}')
                return

            if item not in player['inventory']:
                await ctx.send(f'@{user}, у тебя нет такого предмета в инвентаре!')
                return

# async def main():
#     """Запуск бота."""
#     bot = RPGbot()
#     await bot.start()
#
# if __name__ == "__main__":
#     asyncio.run(main())
bot = RPGbot()
bot.run()