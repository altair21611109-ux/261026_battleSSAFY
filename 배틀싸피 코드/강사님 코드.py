import socket

HOST = '127.0.0.1'
PORT = 8747

sock = socket.socket()


# ---------- 기본 통신 ----------

def init(nickname):
    sock.connect((HOST, PORT))
    print('배틀싸피 연결 완료')
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
        print('게임 종료:', data.strip())
        return None

    return data


def close():
    sock.close()
    print('연결 종료')


# ---------- 강사님 코드 기반 BFS ----------

def bfs(maze):
    H = len(maze)
    W = len(maze[0])

    # 내 탱크 위치 찾기
    sr = sc = None

    for i in range(H):
        for j in range(W):
            if maze[i][j] == 'M':
                sr, sc = i, j

    if sr is None:
        return []

    queue = [(sr, sc, [])]

    visited = [[0] * W for _ in range(H)]
    visited[sr][sc] = 1

    dr = [-1, 1, 0, 0]
    dc = [0, 0, -1, 1]
    direction = ['U', 'D', 'L', 'R']

    while queue:
        cr, cc, path = queue.pop(0)

        for d in range(4):
            nr = cr + dr[d]
            nc = cc + dc[d]

            if not (0 <= nr < H and 0 <= nc < W):
                continue

            # 포탑 바로 옆이면 발사
            if maze[nr][nc] == 'X':
                return path + [direction[d] + ' F']

            # 일반 땅이면 이동
            if maze[nr][nc] == 'G' and not visited[nr][nc]:
                visited[nr][nc] = 1
                queue.append((
                    nr,
                    nc,
                    path + [direction[d] + ' A']
                ))

    return []


# ---------- 메인 실행 ----------

NICKNAME = 'EasyCode' # 닉네임 변경 가능

try:
    game_data = init(NICKNAME)

    while game_data is not None:
        rows = game_data.strip().splitlines()
        height, width, ally_count, _, _ = map(int, rows[0].split())

        # 지도 읽기
        maze = [row.split() for row in rows[1:height + 1]]

        # 내 일반 포탄 수 읽기
        normal = 0
        for row in rows[height + 1:height + 1 + ally_count]:
            info = row.split()
            if info[0] == 'M':
                normal = int(info[3])

        if normal <= 0:
            print('일반 포탄이 없습니다.')
            break

        # BFS로 이동·공격 경로 찾기
        commands = bfs(maze)

        if not commands:
            print('포탑이 없거나 이동할 경로가 없습니다.')
            break

        # 명령 하나 실행 후 최신 지도로 다시 판단
        output = commands[0]
        print('실행:', output)

        game_data = submit(output)

finally:
    close()