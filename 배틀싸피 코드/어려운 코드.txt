import socket
import heapq
from itertools import count

HOST = '127.0.0.1'
PORT = 8747
NICKNAME = '도전자'

# 게임 매뉴얼의 수치가 다르면 이 부분을 변경하세요.
# 사거리는 첨부 코드의 3칸을 유지했습니다.
FIRE_RANGE = 3
NORMAL_DAMAGE = 10
MEGA_DAMAGE = 30
CIPHER_SHIFT = 9  # 첨부 코드의 +9 유지. 공식 규칙 확인 필요.
SUPPLY_MEGA_COUNT = 1  # 첨부 코드의 보급량 유지. 공식 규칙 확인 필요.
MAX_SEARCH_STATES = 100000

sock = socket.socket()
map_data = []
allies = {}
enemies = {}
codes = []


def init(nickname):
    try:
        print(f'[STATUS] Trying to connect to {HOST}:{PORT}')
        sock.connect((HOST, PORT))
        print('[STATUS] Connected')
        return submit(f'INIT {nickname}')
    except OSError as e:
        print('[ERROR] 배틀싸피를 연결 대기 상태로 실행하세요.', e)
        return None


def submit(string_to_send):
    try:
        sock.sendall((string_to_send + ' ').encode('utf-8'))
        return receive()
    except OSError as e:
        print('[ERROR] 명령 전송 실패:', e)
        return None


def receive():
    # 기존 배포 예시와 같은 요청/응답 구조입니다.
    # TCP 응답이 나뉘면 헤더의 행 수를 기준으로 추가로 받습니다.
    raw = b''
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            raw += chunk
            try:
                data = raw.decode('utf-8')
            except UnicodeDecodeError as e:
                if e.reason == 'unexpected end of data':
                    continue
                raise

            rows = data.splitlines()
            if not rows or not rows[0].strip():
                continue
            header = rows[0].split()
            if not header[0].isdigit() or header[0] == '0':
                print('[RESULT]', data.strip())
                return None
            if len(header) < 5:
                continue
            height, width, ally_count, enemy_count, code_count = map(int, header[:5])
            required = 1 + height + ally_count + enemy_count + code_count
            if len(rows) < required:
                continue
            if any(len(row.split()) < width for row in rows[1:height + 1]):
                continue
            # 원래 프로토콜에는 바이트 길이 구분자가 없습니다.
            # 마지막 줄의 문자열 중간 분할은 행 수만으로 판별할 수 없습니다.
            return data
    except (OSError, ValueError, UnicodeError) as e:
        print('[ERROR] 데이터 수신 실패:', e)
        return None


def close():
    sock.close()
    print('[STATUS] Connection closed')


def parse_data(game_data):
    rows = game_data.strip().splitlines()
    height, width, ally_count, enemy_count, code_count = map(int, rows[0].split()[:5])
    if height <= 0 or width <= 0:
        raise ValueError('올바른 지도 크기가 아닙니다.')
    required = 1 + height + ally_count + enemy_count + code_count
    if len(rows) != required:
        raise ValueError(f'수신 행 수 불일치: 필요 {required}, 실제 {len(rows)}')

    map_data[:] = [row.split() for row in rows[1:height + 1]]
    if any(len(row) != width for row in map_data):
        raise ValueError('지도 열 수가 일치하지 않습니다.')

    index = height + 1
    allies.clear()
    enemies.clear()
    for destination, amount in ((allies, ally_count), (enemies, enemy_count)):
        for row in rows[index:index + amount]:
            name, *info = row.split()
            destination[name] = info
        index += amount
    codes[:] = [row.strip() for row in rows[index:index + code_count]]


def decode_cipher(text):
    # 알파벳만 이동시키고 공백과 기호는 보존합니다.
    result = []
    for char in text:
        if 'A' <= char <= 'Z':
            result.append(chr((ord(char) - ord('A') + CIPHER_SHIFT) % 26 + ord('A')))
        elif 'a' <= char <= 'z':
            result.append(chr((ord(char) - ord('a') + CIPHER_SHIFT) % 26 + ord('a')))
        else:
            result.append(char)
    return ''.join(result)


def ammo_options(hp, normal, mega, is_tree=False):
    """파괴에 충분한 포탄 조합. (턴 수, 일반 소모, 메가 소모)."""
    normal_damage = 1 if is_tree else NORMAL_DAMAGE
    mega_damage = 1 if is_tree else MEGA_DAMAGE
    for used_mega in range(min(mega, (hp + mega_damage - 1) // mega_damage) + 1):
        remaining_hp = max(0, hp - used_mega * mega_damage)
        used_normal = (remaining_hp + normal_damage - 1) // normal_damage
        if used_normal <= normal:
            yield used_normal + used_mega, used_normal, used_mega


def my_func(game_data):
    parse_data(game_data)
    height, width = len(map_data), len(map_data[0])
    directions = [(0, 1, 'R'), (-1, 0, 'U'), (0, -1, 'L'), (1, 0, 'D')]
    ground = {'G', 'G1', 'G2'}

    # M을 우선 사용하고, 구버전 표기는 M이 없을 때만 사용합니다.
    start = None
    me = None
    for name in ('M', 'M1', 'A'):
        positions = [(r, c) for r in range(height) for c in range(width)
                     if map_data[r][c] == name]
        if positions and name in allies:
            start, me = positions[0], name
            break
    if start is None:
        raise ValueError('내 탱크 위치 또는 아군 정보가 없습니다.')
    if len(allies[me]) < 4:
        raise ValueError('내 탱크의 일반/메가 포탄 정보를 읽을 수 없습니다.')
    normal, mega = int(allies[me][2]), int(allies[me][3])

    objects = []
    object_index = {}
    targets = []
    for r in range(height):
        for c in range(width):
            cell = map_data[r][c]
            if cell == 'T' or cell == 'X' or cell.startswith('E'):
                if cell == 'T':
                    hp = 1  # 첨부 코드처럼 나무는 포탄 한 발로 파괴한다고 가정
                else:
                    if cell not in enemies or not enemies[cell]:
                        raise ValueError(f'{cell}의 체력 정보가 없습니다.')
                    hp = max(1, int(enemies[cell][0]))
                object_index[(r, c)] = len(objects)
                objects.append((cell, hp))
                if cell == 'X':
                    targets.append((r, c))
    if not targets:
        print('[STATUS] 적 포탑이 지도에 없습니다. 실행을 종료합니다.')
        return None

    # 상태: 행, 열, 일반 포탄, 메가 포탄, 파괴한 물체 비트마스크,
    #       현재 응답에 포함된 암호 중 이미 사용한 암호 비트마스크
    initial = (start[0], start[1], normal, mega, 0, 0)
    serial = count()
    pq = [(0, next(serial), initial, None)]
    distance = {initial: 0}
    best_goal = float('inf')
    best_command = None
    facility_command = None
    searched = 0
    unique_codes = list(dict.fromkeys(codes))

    def push(cost, state, command):
        if cost < distance.get(state, float('inf')):
            distance[state] = cost
            heapq.heappush(pq, (cost, next(serial), state, command))

    while pq:
        turns, _, state, first_command = heapq.heappop(pq)
        if distance.get(state) != turns:
            continue
        if turns >= best_goal:
            break
        searched += 1
        if searched > MAX_SEARCH_STATES:
            # 검토하지 않은 부분을 최단 경로라고 단정하지 않습니다.
            if best_command is not None:
                print('[STATUS] 탐색 한도: 현재까지 찾은 실행 가능 경로 사용')
                return best_command
            raise RuntimeError('탐색 한도 초과. 지도와 수신 데이터를 확인하세요.')

        r, c, normal, mega, removed, used_codes = state

        # 보급: 현재 받은 암호는 계획 안에서 각각 한 번만 사용합니다.
        adjacent_facility = any(
            0 <= r + dr < height and 0 <= c + dc < width
            and map_data[r + dr][c + dc] == 'F'
            for dr, dc, _ in directions
        )
        if adjacent_facility:
            if not unique_codes and first_command and facility_command is None:
                # 보급소 옆에 도착해야 암호를 받는 버전의 게임에 대비
                facility_command = first_command
            for code_index, cipher in enumerate(unique_codes):
                if used_codes & (1 << code_index):
                    continue
                command = first_command or f'G {decode_cipher(cipher)}'
                next_state = (r, c, normal, mega + SUPPLY_MEGA_COUNT,
                              removed, used_codes | (1 << code_index))
                push(turns + 1, next_state, command)

        for dr, dc, direction in directions:
            nr, nc = r + dr, c + dc
            if 0 <= nr < height and 0 <= nc < width:
                cell = map_data[nr][nc]
                index = object_index.get((nr, nc))
                cleared = index is not None and bool(removed & (1 << index))
                if cell in ground or (nr, nc) == start or cleared:
                    command = first_command or f'{direction} A'
                    push(turns + 1, (nr, nc, normal, mega, removed, used_codes), command)

            # 포탄은 사거리 안의 첫 장애물/적에게만 맞는다고 처리합니다.
            for step in range(1, FIRE_RANGE + 1):
                nr, nc = r + dr * step, c + dc * step
                if not (0 <= nr < height and 0 <= nc < width):
                    break
                cell = map_data[nr][nc]
                index = object_index.get((nr, nc))
                if index is not None and removed & (1 << index):
                    continue
                if cell in ground or cell == 'W' or (nr, nc) == start:
                    continue
                if index is None:
                    # 바위, 보급소, 아군, 알 수 없는 지형은 사격을 막습니다.
                    break

                name, hp = objects[index]
                for shots, used_normal, used_mega in ammo_options(
                        hp, normal, mega, is_tree=(name == 'T')):
                    # 실제로는 한 발만 발사한 뒤 새 체력/탄약으로 다시 탐색합니다.
                    fire = f'{direction} F' if used_normal else f'{direction} F M'
                    command = first_command or fire
                    cost = turns + shots
                    if name == 'X':
                        if cost < best_goal:
                            best_goal, best_command = cost, command
                    else:
                        next_state = (r, c, normal - used_normal, mega - used_mega,
                                      removed | (1 << index), used_codes)
                        push(cost, next_state, command)
                break

    if best_command is not None:
        return best_command
    if facility_command is not None:
        return facility_command
    raise RuntimeError('현재 탄약과 이동 경로로 포탑 파괴 계획을 찾지 못했습니다.')


def main():
    # 1~5단계 중 원하는 단계를 게임에서 직접 선택하고 실행하세요.
    # 수신 데이터에 단계 번호가 없으므로 5단계 자동 판별은 하지 않습니다.
    previous = None
    repeats = 0
    try:
        game_data = init(NICKNAME)
        while game_data is not None:
            print(f'\n[수신 데이터]\n{game_data}')
            output = my_func(game_data)
            if output is None:
                break

            signature = (game_data.strip(), output)
            repeats = repeats + 1 if signature == previous else 0
            previous = signature
            if repeats >= 3:
                print('[ERROR] 같은 상태와 명령이 반복됩니다. 명령/암호 규칙을 확인하세요.')
                break

            print(f'[COMMAND] {output}')
            game_data = submit(output)
    except (ValueError, IndexError, RuntimeError) as e:
        print('[ERROR]', e)
    except KeyboardInterrupt:
        print('[STATUS] 사용자가 중단했습니다.')
    finally:
        close()


if __name__ == '__main__':
    main()
