"""배틀싸피 3vs3 봇. Python 3.9+, 표준 라이브러리만 사용.

실행: 게임의 연결 대기 화면에서 python main.py
세 팀원이 같은 파일을 사용해도 각자 받은 M 위치를 기준으로 판단한다.
닉네임은 아래 NICKNAME 또는 BATTLE_NICKNAME 환경변수로 바꿀 수 있다.
"""

import heapq
import math
import os
import select
import socket
import sys
from collections import Counter, deque
from dataclasses import dataclass


HOST = '192.168.32.98'
PORT = 8747
NICKNAME = os.environ.get('BATTLE_NICKNAME', '영준삼')[:12]
FIRE_RANGE = 3
NORMAL_DAMAGE = 30
MEGA_DAMAGE = 70
SAND_DAMAGE = 10
# 팀전은 탱크 두 대를 잃으면 패배한다. 불가피한 모래길에서도 메가탄
# 한 발을 버틸 체력을 남기고, 일반 지형 우회가 있으면 모래를 밟지 않는다.
SAND_HP_RESERVE = MEGA_DAMAGE
CIPHER_SHIFT = 9
# 나무 내구도와 보급량은 매뉴얼에 수치가 없다. 나무는 우선 한 발씩
# 발사하고 매번 새 지도를 확인한다. 보급 성공량도 다음 응답으로 확인한다.
DIRS = ((-1, 0, 'U'), (0, 1, 'R'), (1, 0, 'D'), (0, -1, 'L'))
GROUND = frozenset(('G', 'S', 'G1', 'G2'))
TRANSPARENT = frozenset(('G', 'S', 'W', 'G1', 'G2'))
MAX_ROUTE_LABELS = 16000
INF = float('inf')


def decode_cipher(text, shift=CIPHER_SHIFT):
    result = []
    for char in text:
        if 'A' <= char <= 'Z':
            result.append(chr((ord(char) - 65 + shift) % 26 + 65))
        elif 'a' <= char <= 'z':
            result.append(chr((ord(char) - 97 + shift) % 26 + 97))
        else:
            result.append(char)
    return ''.join(result)


@dataclass
class GameState:
    grid: tuple
    allies: dict
    enemies: dict
    codes: tuple

    @property
    def height(self):
        return len(self.grid)

    @property
    def width(self):
        return len(self.grid[0]) if self.grid else 0

    def positions(self):
        return {cell: (r, c) for r, row in enumerate(self.grid)
                for c, cell in enumerate(row)
                if cell == 'M' or cell in self.allies or cell in self.enemies}

    def hp(self, name):
        info = self.allies.get(name, self.enemies.get(name, ()))
        return max(0, int(info[0])) if info else 0


def parse_state(raw):
    rows = raw.strip().splitlines()
    if not rows:
        raise ValueError('빈 게임 데이터')
    header = rows[0].split()
    if len(header) != 5:
        raise ValueError('헤더는 다섯 개의 정수여야 합니다.')
    height, width, na, ne, nc = map(int, header)
    if height <= 0 or width <= 0 or min(na, ne, nc) < 0:
        raise ValueError('잘못된 지도 크기 또는 유닛 수')
    if len(rows) != 1 + height + na + ne + nc:
        raise ValueError('수신 데이터의 행 수 불일치')
    grid = tuple(tuple(row.split()) for row in rows[1:height + 1])
    if any(len(row) != width for row in grid):
        raise ValueError('수신 지도의 열 수 불일치')
    offset = height + 1
    groups = []
    for amount in (na, ne):
        units = {}
        for row in rows[offset:offset + amount]:
            name, *info = row.split()
            expected = 4 if name == 'M' else 1
            if len(info) != expected:
                raise ValueError('유닛 정보 형식 불일치: ' + name)
            int(info[0])
            if name == 'M':
                int(info[2])
                int(info[3])
            units[name] = tuple(info)
        groups.append(units)
        offset += amount
    return GameState(grid, groups[0], groups[1], tuple(rows[offset:]))


def shots_needed(hp, normal, mega):
    """현재 탄약으로 목표를 파괴할 최소 사격 수. 탄약 부족이면 무한대."""
    if hp <= 0:
        return 0
    best = INF
    for used_mega in range(min(mega, math.ceil(hp / MEGA_DAMAGE)) + 1):
        used_normal = math.ceil(max(0, hp - used_mega * MEGA_DAMAGE) / NORMAL_DAMAGE)
        if used_normal <= normal:
            best = min(best, used_mega + used_normal)
    return best


class Board:
    def __init__(self, state, cleared=frozenset(), blocked=frozenset(), team_mode=None):
        self.state = state
        self.positions = state.positions()
        self.me = self.positions.get('M')
        self.cleared = cleared
        self.tanks = {name: pos for name, pos in self.positions.items()
                      if name.startswith('E') and state.hp(name) > 0}
        allied_tanks = sum(name.startswith('M') and state.hp(name) > 0
                           for name in state.allies)
        self.team_mode = (allied_tanks > 1 or len(self.tanks) > 1
                          if team_mode is None else team_mode)
        self.blocked = frozenset(blocked)
        self.forbidden = self.own_base_zone() | self.blocked

    def own_base_zone(self):
        """v1.3.0 팀전: 자기 포탑 모서리의 3x3은 진입 금지.

        서버는 G1/G2를 모두 G로 바꾸어 전송한다. 따라서 땅 문자만 보고
        이동하면 경고 3회로 사망한다. H 위치에서 자기 진영을 판별한다.
        포탄 통과/적 진영 진입은 허용하므로 이동 가능 여부에서만 제외한다.
        """
        home = self.positions.get('H')
        if not self.team_mode or (self.state.height, self.state.width) != (16, 16):
            return frozenset()
        if home not in ((0, 0), (0, 15), (15, 0), (15, 15)):
            return frozenset()
        rows = range(3) if home[0] == 0 else range(13, 16)
        cols = range(3) if home[1] == 0 else range(13, 16)
        return frozenset((r, c) for r in rows for c in cols)

    def inside(self, pos):
        return 0 <= pos[0] < self.state.height and 0 <= pos[1] < self.state.width

    def cell(self, pos):
        if pos == self.me or pos in self.cleared:
            return 'G'
        return self.state.grid[pos[0]][pos[1]]

    def first_hit(self, origin, direction):
        dr, dc, _ = direction
        for step in range(1, FIRE_RANGE + 1):
            pos = (origin[0] + dr * step, origin[1] + dc * step)
            if not self.inside(pos):
                break
            cell = self.cell(pos)
            if cell not in TRANSPARENT:
                return cell, pos
        return None

    def aim(self, origin, target):
        for direction in DIRS:
            hit = self.first_hit(origin, direction)
            if hit and hit[1] == target:
                return direction[2]
        return None

    def danger(self, pos, removed=frozenset()):
        """적 탄약은 비공개이므로 메가탄 한 발을 맞을 가능성을 보수적으로 평가."""
        direct = 0
        near = 0
        for name, enemy_pos in self.tanks.items():
            if name in removed or enemy_pos in self.cleared:
                continue
            if self.aim(enemy_pos, pos) is not None:
                # aim은 빈 칸을 hit로 취급하지 않으므로 별도 광선 판정을 쓴다.
                direct += 1
            elif self.clear_ray(enemy_pos, pos):
                direct += 1
            else:
                # 다음에 한 칸 이동하면 사격선에 들어올 수 있는 위치.
                for dr, dc, _ in DIRS:
                    moved = (enemy_pos[0] + dr, enemy_pos[1] + dc)
                    if self.inside(moved) and self.cell(moved) in GROUND:
                        if self.clear_ray(moved, pos):
                            near += 1
                            break
        return direct, near

    def clear_ray(self, origin, target):
        dr, dc = target[0] - origin[0], target[1] - origin[1]
        distance = abs(dr) + abs(dc)
        if (dr and dc) or not 1 <= distance <= FIRE_RANGE:
            return False
        sr = (dr > 0) - (dr < 0)
        sc = (dc > 0) - (dc < 0)
        for step in range(1, distance):
            cell = self.cell((origin[0] + sr * step, origin[1] + sc * step))
            if cell not in TRANSPARENT:
                return False
        return True

    def passable(self, pos, shells):
        if pos in self.forbidden:
            return False
        cell = self.cell(pos)
        return cell in GROUND or (cell == 'T' and shells > 0)


class Routes:
    """사격 위치까지 역방향 다익스트라. 모래 피해와 나무 소모량도 추적한다.

    같은 칸의 경로라도 비용/모래/포탄 중 하나가 유리하면 함께 보존한다.
    따라서 짧지만 죽게 되는 모래길 대신 체력이 충분한 우회로를 선택한다.
    """
    def __init__(self, board, goals, hp, shells, danger_map):
        self.labels = {}
        queue = []
        self.board = board
        # 과도한 탄약 수는 탐색 크기에 영향을 주지 않도록 실제 지도 크기로 제한.
        tree_budget = min(shells, board.state.height * board.state.width)

        def add(pos, cost, sand, trees):
            if sand >= hp or trees > tree_budget:
                return
            existing = self.labels.setdefault(pos, [])
            if any(a <= cost and b <= sand and c <= trees for a, b, c in existing):
                return
            existing[:] = [(a, b, c) for a, b, c in existing
                           if not (cost <= a and sand <= b and trees <= c)]
            existing.append((cost, sand, trees))
            heapq.heappush(queue, (cost, sand, trees, pos))

        for pos in goals:
            if board.passable(pos, shells):
                add(pos, 0.0, 0, 0)
        expanded = 0
        while queue and expanded < MAX_ROUTE_LABELS:
            cost, sand, trees, pos = heapq.heappop(queue)
            if (cost, sand, trees) not in self.labels.get(pos, ()):
                continue
            expanded += 1
            cell = board.cell(pos)
            sand_next = sand + (SAND_DAMAGE if cell == 'S' else 0)
            trees_next = trees + (cell == 'T')
            direct, near = danger_map.get(pos, (0, 0))
            # 현재 적 배치는 다음 턴에 바뀔 수 있다. 먼 경로의 위험은 완만하게 반영.
            step_cost = (1.0 + (1.2 if cell == 'S' else 0.0)
                         + (1.25 if cell == 'T' else 0.0)
                         + 0.75 * direct + 0.16 * near)
            for dr, dc, _ in DIRS:
                prev = (pos[0] + dr, pos[1] + dc)
                if board.inside(prev) and board.passable(prev, shells):
                    add(prev, cost + step_cost, sand_next, trees_next)

    def cost(self, pos, hp, shells, sand_budget=None):
        return min((cost for cost, sand, trees in self.labels.get(pos, ())
                    if sand < hp and trees <= shells
                    and (sand_budget is None or sand <= sand_budget)), default=INF)


class Bot:
    def __init__(self):
        self.history = deque(maxlen=10)
        self.initial_allies = set()
        self.initial_enemies = set()
        self.my_slot = None
        self.team_mode = False
        self.last_signature = None
        self.last_command = None
        self.failed = Counter()
        self.last_supply = None
        self.supply_failures = 0
        self.blocked_cells = set()
        self.last_move = None
        self.navigation_key = None

    def choose(self, state):
        board = Board(state, blocked=self.blocked_cells,
                      team_mode=True if self.team_mode else None)
        if board.me is None or 'M' not in state.allies or state.hp('M') <= 0:
            return 'S'
        navigation_key = (state.height, state.width,
                          board.positions.get('H'), board.positions.get('X'))
        if self.navigation_key != navigation_key:
            self.blocked_cells.clear()
            self.last_move = None
            self.navigation_key = navigation_key
        if self.last_move is not None:
            origin, destination = self.last_move
            # 회전은 성공하고 전진만 거절돼도 위치로 실패를 알아낸다.
            # 방향까지 포함한 전체 상태 비교만으로는 이 실패를 놓친다.
            if board.me == origin and board.inside(destination):
                if state.grid[destination[0]][destination[1]] in GROUND:
                    self.blocked_cells.add(destination)
            self.last_move = None
        board = Board(state, blocked=self.blocked_cells,
                      team_mode=board.team_mode)
        hp = state.hp('M')
        normal, mega = map(int, state.allies['M'][2:4])
        normal, mega = max(0, normal), max(0, mega)
        alive_allies = {name for name in state.allies
                        if name != 'H' and state.hp(name) > 0}
        alive_enemies = set(board.tanks)
        self.initial_allies.update(alive_allies)
        self.initial_enemies.update(alive_enemies)
        self.team_mode = self.team_mode or len(alive_allies) >= 2 or len(alive_enemies) >= 2
        if self.my_slot is None and len(alive_allies) == 3:
            missing = {1, 2, 3} - {int(name[1:]) for name in alive_allies if name != 'M'}
            if len(missing) == 1:
                self.my_slot = missing.pop()
        lost_allies = max(0, len(self.initial_allies) - len(alive_allies))
        expected_enemy_count = max(len(self.initial_enemies),
                                   3 if len(self.initial_allies) >= 3 else 0)
        lost_enemies = max(0, expected_enemy_count - len(alive_enemies))
        signature = (state.grid, tuple(state.allies.get('M', ())))
        if signature == self.last_signature and self.last_command not in (None, 'S'):
            self.failed[(board.me, self.last_command)] += 1
        else:
            self.failed.clear()
        if self.last_supply is not None:
            old_normal, old_mega = self.last_supply
            if mega > old_mega or normal > old_normal:
                self.supply_failures = 0
            else:
                self.supply_failures += 1
            self.last_supply = None

        danger_cache = {}
        route_cache = {}

        def danger_for(current_board):
            key = current_board.cleared
            if key not in danger_cache:
                danger_cache[key] = {
                    (r, c): current_board.danger((r, c))
                    for r in range(state.height) for c in range(state.width)
                    if current_board.passable((r, c), normal + mega)
                }
            return danger_cache[key]

        def goals_for(current_board, target):
            target_pos = board.positions[target]
            goals = []
            for dr, dc, _ in DIRS:
                for step in range(1, FIRE_RANGE + 1):
                    pos = (target_pos[0] + dr * step, target_pos[1] + dc * step)
                    if current_board.inside(pos) and current_board.passable(pos, normal + mega):
                        if current_board.aim(pos, target_pos) is not None:
                            goals.append(pos)
            return goals

        def routes_for(current_board, target):
            key = (current_board.cleared, target)
            if key not in route_cache:
                goals = goals_for(current_board, target)
                route_cache[key] = Routes(current_board, goals, hp, normal + mega,
                                          danger_for(current_board))
            return route_cache[key]

        home = board.positions.get('H')

        def target_value(name, target_hp):
            if name == 'X':
                return 215.0 + max(0, 100 - target_hp) * 0.35
            pos = board.positions[name]
            value = 165.0 + max(0, 100 - target_hp) * 0.55
            if self.team_mode and (lost_enemies >= 1 or len(alive_enemies) == 1):
                value += 65.0
            # 본진에 붙은 적은 방어 대상으로 취급한다.
            if home and abs(home[0] - pos[0]) + abs(home[1] - pos[1]) <= 5:
                value += 32.0
                if state.hp('H') <= MEGA_DAMAGE:
                    value += 28.0
            # 팀원이 사격 가능한 상대에게 집중해 적의 행동 수를 줄인다.
            friends = sum(1 for ally, ally_pos in board.positions.items()
                          if ally.startswith('M') and ally != 'M'
                          and board.aim(ally_pos, pos) is not None)
            return value + 16.0 * friends

        dry_mode = False

        def strategic_value(pos, new_hp, n, m, current_board, damaged=None,
                            dry_only=None, unreachable_value=-350.0):
            values = []
            dry_only = dry_mode if dry_only is None else dry_only
            sand_budget = 0 if dry_only else max(0, new_hp - SAND_HP_RESERVE - 1)
            for name in state.enemies:
                if name not in board.positions or state.hp(name) <= 0:
                    continue
                target_hp = damaged.get(name, state.hp(name)) if damaged else state.hp(name)
                if target_hp <= 0:
                    continue
                shots = shots_needed(target_hp, n, m)
                # 부족한 탄약으로도 접근은 가능하되, 보급 계획보다 낮게 평가한다.
                shortage = 0.0
                if shots == INF:
                    if dry_only:
                        continue
                    shots = math.ceil(target_hp / MEGA_DAMAGE)
                    shortage = 70.0
                # 나무에 쓴 포탄을 목표 공격에 다시 쓰는 가상의 경로를 배제.
                spare_damage = max(0, n * NORMAL_DAMAGE + m * MEGA_DAMAGE - target_hp)
                tree_normal = min(n, spare_damage // NORMAL_DAMAGE)
                tree_mega = min(m, (spare_damage - tree_normal * NORMAL_DAMAGE) // MEGA_DAMAGE)
                tree_budget = tree_normal + tree_mega if shortage == 0 else n + m
                route = routes_for(current_board, name).cost(pos, new_hp, tree_budget,
                                                             sand_budget)
                if route != INF:
                    values.append(target_value(name, target_hp) - 10.0 * route
                                  - 10.0 * shots - shortage)
            # 보급 목표: 접근 + 필요한 횟수만 충전 후 다시 출발한다.
            if self.supply_failures < 2 and m < 2:
                key = (current_board.cleared, 'supply')
                if key not in route_cache:
                    goals = []
                    for r, row in enumerate(state.grid):
                        for c, cell in enumerate(row):
                            if cell == 'F':
                                for dr, dc, _ in DIRS:
                                    pos2 = (r + dr, c + dc)
                                    if current_board.inside(pos2):
                                        goals.append(pos2)
                    route_cache[key] = Routes(current_board, goals, hp, normal + mega,
                                              danger_for(current_board))
                cost = route_cache[key].cost(pos, new_hp, n + m, sand_budget)
                if cost != INF:
                    need = 190.0 if n + m == 0 else (153.0 if m == 0 else 118.0)
                    values.append(need - 10.0 * cost)
            return max(values, default=unreachable_value)

        # 목적지까지 이어지는 경로 전체를 확인한다. 단순히 옆에 G가 있다고
        # S를 금지하면 반드시 모래를 지나야 하는 지도에서 제자리만 돌게 된다.
        dry_mode = strategic_value(board.me, hp, normal, mega, board,
                                   dry_only=True, unreachable_value=None) is not None

        def risk_penalty(pos, new_hp, current_board, removed=frozenset()):
            direct, near = current_board.danger(pos, removed)
            conservative_damage = MEGA_DAMAGE * direct
            penalty = 20.0 * direct + 2.5 * near
            if direct and new_hp <= conservative_damage:
                penalty += 180.0 + (70.0 if lost_allies else 0.0)
            # 체력이 적을수록 같은 노출도 위험하다.
            penalty += direct * max(0, 80 - new_hp) * 0.4
            return penalty

        candidates = []

        def add(command, pos, new_hp, n, m, current_board=board,
                reward=0.0, damaged=None, removed=frozenset(), winning=False,
                completed_value=None):
            if new_hp <= 0:
                return
            sand_entry = command.endswith(' A') and board.cell(pos) == 'S'
            if sand_entry and (dry_mode or new_hp <= SAND_HP_RESERVE):
                return
            score = strategic_value(pos, new_hp, n, m, current_board, damaged,
                                    unreachable_value=None)
            if score is None:
                if sand_entry:
                    return  # 체력을 남기고 도달할 목표가 없는 모래길로 진입 금지.
                score = -350.0
            # 처치/보급으로 현재 목표가 사라져도 달성한 목표의 가치는 보존.
            if completed_value is not None:
                score = max(score, completed_value)
            score += reward
            score -= risk_penalty(pos, new_hp, current_board, removed)
            score -= (hp - new_hp) * (0.6 if hp > 40 else 1.4)
            if command.endswith(' A'):
                score -= 3.0 * self.history.count(pos)
                # 셋 모두 같은 길로 몰리는 동률을 부드럽게 분산한다.
                enemy_home = board.positions.get('X')
                if home and enemy_home and self.my_slot:
                    fr, fc = enemy_home[0] - home[0], enemy_home[1] - home[1]
                    side = (fr * (pos[1] - home[1]) - fc * (pos[0] - home[0])) / max(1, abs(fr) + abs(fc))
                    score += 0.05 * (self.my_slot - 2) * side
            score -= 65.0 * self.failed.get((board.me, command), 0)
            if winning:
                score = 100000.0 + reward
            direct, _ = current_board.danger(pos, removed)
            candidates.append((score, command, winning, MEGA_DAMAGE * direct, new_hp))

        # 합법적인 이동만 후보로 추가한다. 빈 칸 없이 막히면 S가 항상 남는다.
        for dr, dc, direction in DIRS:
            pos = (board.me[0] + dr, board.me[1] + dc)
            if board.inside(pos) and board.passable(pos, 0):
                new_hp = hp - (SAND_DAMAGE if board.cell(pos) == 'S' else 0)
                add(direction + ' A', pos, new_hp, normal, mega, reward=0.2)

        for direction in DIRS:
            hit = board.first_hit(board.me, direction)
            if not hit:
                continue
            name, pos = hit
            if name == 'T':
                # 나무를 없앤 지도에서 실제로 길/사격선이 좋아지는지 평가한다.
                if normal or mega:
                    use_mega = normal == 0
                    cleared_board = Board(state, frozenset((pos,)), self.blocked_cells,
                                          board.team_mode)
                    command = direction[2] + (' F M' if use_mega else ' F')
                    add(command, board.me, hp, normal - (not use_mega), mega - use_mega,
                        cleared_board, reward=-3.0 if use_mega else -1.0)
                continue
            if name not in state.enemies or state.hp(name) <= 0:
                continue  # 아군/바위/보급소에는 발사하지 않는다.
            target_hp = state.hp(name)
            for use_mega, available, damage in ((False, normal, NORMAL_DAMAGE),
                                                (True, mega, MEGA_DAMAGE)):
                if not available:
                    continue
                if use_mega and normal and target_hp <= NORMAL_DAMAGE:
                    continue
                remaining = max(0, target_hp - damage)
                killed = remaining == 0
                is_base = name == 'X'
                winning = killed and (is_base or (self.team_mode and
                           (lost_enemies >= 1 or len(alive_enemies) == 1)))
                removed = frozenset((name,)) if killed else frozenset()
                cleared_board = (Board(state, frozenset((pos,)), self.blocked_cells,
                                       board.team_mode) if killed else board)
                n, m = normal - (not use_mega), mega - use_mega
                # 실제 피해/처치, 메가탄의 빠른 제압 효과를 평가한다.
                reward = min(target_hp, damage) * (0.72 if is_base else 0.62)
                if killed:
                    reward += 135.0 if is_base else 105.0
                elif not is_base and remaining <= NORMAL_DAMAGE:
                    reward += 18.0
                if use_mega:
                    reward -= 7.0
                # 처치로 현재 목표가 사라져도 목표 달성 가치를 잃지 않도록 보정.
                if killed:
                    reward += target_value(name, target_hp) * 0.45
                command = direction[2] + (' F M' if use_mega else ' F')
                completed = target_value(name, target_hp) - 10.0 if killed else None
                add(command, board.me, hp, n, m, cleared_board, reward,
                    {name: remaining}, removed, winning, completed)

        adjacent_supply = any(board.inside((board.me[0] + dr, board.me[1] + dc))
                              and board.cell((board.me[0] + dr, board.me[1] + dc)) == 'F'
                              for dr, dc, _ in DIRS)
        if adjacent_supply and state.codes and mega < 2 and self.supply_failures < 2:
            plaintext = decode_cipher(state.codes[0].strip())
            if plaintext and not any(char.isspace() for char in plaintext):
                add('G ' + plaintext, board.me, hp, normal, mega + 1,
                    reward=18.0 if mega == 0 else 4.0,
                    completed_value=strategic_value(board.me, hp, normal, mega, board))

        add('S', board.me, hp, normal, mega, reward=-6.0)
        winning_actions = [item for item in candidates if item[2]]
        survivable_actions = [item for item in candidates if item[3] < item[4]]
        if winning_actions:
            choices = winning_actions
        elif survivable_actions:
            # 공격 보상이 아무리 높아도 안전한 행동이 있으면 확실한 자멸은 제외.
            choices = survivable_actions
        else:
            least_damage = min(item[3] for item in candidates)
            choices = [item for item in candidates if item[3] == least_damage]
        _, command, _, _, _ = max(choices, key=lambda item: (item[0], item[1] != 'S'))
        self.last_signature = signature
        self.last_command = command
        if command.endswith(' A'):
            dr, dc, _ = next(direction for direction in DIRS if direction[2] == command[0])
            self.last_move = (board.me, (board.me[0] + dr, board.me[1] + dc))
        if command.startswith('G '):
            self.last_supply = (normal, mega)
        self.history.append(board.me)
        return command


class GameClient:
    """기본 코드의 접속 주소/INIT/런처 인자를 유지한다."""
    def __init__(self, prefix=''):
        self.sock = None
        self.prefix = prefix

    def connect(self, nickname):
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        # 로비 또는 다른 플레이어의 차례에는 오래 기다릴 수 있다.
        self.sock.settimeout(None)
        return self.submit('INIT ' + nickname)

    def submit(self, command):
        self.sock.sendall((self.prefix + command + ' ').encode('utf-8'))
        return self.receive()

    def receive(self):
        data = bytearray()
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                if data:
                    try:
                        final_text = data.decode('utf-8')
                        parse_state(final_text)
                        return final_text
                    except (ValueError, UnicodeError):
                        pass
                return None
            data.extend(chunk)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError('게임 데이터가 최대 허용 크기를 초과했습니다.')
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError as exc:
                if exc.reason == 'unexpected end of data':
                    continue
                raise
            stripped = text.lstrip()
            if not stripped:
                continue
            if not stripped[0].isdigit() or stripped.split()[0] == '0':
                print('[RESULT]', stripped[:300].strip(), flush=True)
                return None
            try:
                parse_state(text)
            except (ValueError, IndexError):
                continue
            # 원 프로토콜에 길이 필드가 없어 마지막 숫자/암호가 중간에
            # 끊긴 경우까지 완벽히 구별할 수 없다. 짧은 추가 수신 대기로 보완.
            ready, _, _ = select.select([self.sock], [], [], 0.025)
            if ready:
                continue
            return text

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def main():
    # 게임 실행기가 전달한 첫 인자를 원본 기본 코드와 똑같이 접두어로 보존.
    prefix = sys.argv[1] if len(sys.argv) > 1 else ''
    client = GameClient(prefix)
    bot = Bot()
    try:
        print('[STATUS] 배틀싸피 연결 시도 - 자기 포탑 구역 진입 금지 적용', flush=True)
        raw = client.connect(NICKNAME)
        while raw is not None:
            try:
                state = parse_state(raw)
                command = bot.choose(state)
            except Exception as exc:
                # 알고리즘 예외 하나로 팀이 즉시 패배하지 않도록 턴을 유지.
                print('[WARNING] 판단 오류, 이번 턴 대기:', exc, flush=True)
                command = 'S'
            print('[COMMAND]', command, flush=True)
            raw = client.submit(command)
    except (OSError, ValueError, UnicodeError) as exc:
        print('[ERROR] 게임 연결 상태를 확인하세요:', exc, flush=True)
    except KeyboardInterrupt:
        print('[STATUS] 사용자 종료', flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
