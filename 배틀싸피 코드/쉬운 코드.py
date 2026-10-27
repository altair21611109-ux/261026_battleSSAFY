import socket
from collections import deque

HOST = '127.0.0.1'
PORT = 8747

sock = socket.socket()

# 오른쪽, 아래, 왼쪽, 위
DIRS = [(0, 1, 'R'), (1, 0, 'D'), (0, -1, 'L'), (-1, 0, 'U')]


# ---------- 기본 통신 부분 ----------

def init(nickname):
    sock.connect((HOST, PORT))
    return submit(f'INIT {nickname}')


def submit(command):
    sock.sendall((command + ' ').encode('utf-8'))
    return receive()


def receive():
    data = sock.recv(65536).decode('utf-8')

    if not data.strip():
        return None

    first = data.split()[0]
    if not first.isdigit() or int(first) == 0:
        return None

    return data


def close():
    sock.close()


# ---------- 지도 읽기 ----------

def parse_data(data):
    rows = data.strip().splitlines()
    height, width, ally_count, _, _ = map(int, rows[0].split())

    grid = [row.split() for row in rows[1:height + 1]]

    allies = {}
    for row in rows[height + 1:height + 1 + ally_count]:
        name, *info = row.split()
        allies[name] = info

    return grid, allies


# ---------- BFS ----------

def bfs(grid, start, fire):
    queue = deque([(start, [])])
    visited = {start}

    while queue:
        (r, c), path = queue.popleft()

        for dr, dc, direction in DIRS:
            nr, nc = r + dr, c + dc

            if not (0 <= nr < len(grid) and 0 <= nc < len(grid[0])):
                continue

            # 포탑 바로 옆이면 발사
            if grid[nr][nc] == 'X':
                return path + [f'{direction} {fire}']

            # 일반 땅으로 이동
            if grid[nr][nc] == 'G' and (nr, nc) not in visited:
                visited.add((nr, nc))
                queue.append(((nr, nc), path + [f'{direction} A']))

    return []


# ---------- 메인 실행 ----------

NICKNAME = 'EasyCode'

try:
    game_data = init(NICKNAME)

    while game_data is not None:
        grid, allies = parse_data(game_data)

        start = None
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                if cell == 'M':
                    start = (r, c)

        if start is None:
            break

        normal = int(allies['M'][2])
        mega = int(allies['M'][3])

        if normal <= 0 and mega <= 0:
            print('포탄이 없습니다.')
            break

        fire = 'F' if normal > 0 else 'F M'
        actions = bfs(grid, start, fire)

        if not actions:
            print('포탑까지 갈 수 있는 경로가 없습니다.')
            break

        output = actions[0]
        print('명령:', output)

        game_data = submit(output)

finally:
    close()